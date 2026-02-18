import os, re, shutil, subprocess, requests
from PIL import Image, ImageFilter
from concurrent.futures import ThreadPoolExecutor
from tqdm import tqdm

# --- CONFIG ---
URL = os.getenv('FILE_URL')
MUSIC_URL = os.getenv('MUSIC_URL')
DURATION = float(os.getenv('IMG_DURATION', '2.5'))
FPS = 15 
TARGET_BITRATE = "400k"
AUDIO_BITRATE = "96k"
PRESET = os.getenv('PRESET', '12') 
AR_TYPE = os.getenv('ASPECT_RATIO', 'Landscape (1920x1080)')
FILENAME = os.getenv('FILENAME', 'av1_video')

RES_MAP = {
    'Landscape (1920x1080)': (1920, 1080),
    'Portrait (1080x1920)': (1080, 1920),
    'Square (1080x1080)': (1080, 1080)
}
W, H = RES_MAP.get(AR_TYPE, (1920, 1080))

def process_single_image(args):
    img_path, out_path = args
    try:
        with Image.open(img_path) as img:
            img = img.convert('RGB')
            # High speed blur
            bg = img.resize((W, H), Image.Resampling.NEAREST)
            bg = bg.resize((100, int(100*(H/W))), Image.Resampling.NEAREST)
            bg = bg.filter(ImageFilter.GaussianBlur(5))
            bg = bg.resize((W, H), Image.Resampling.LANCZOS)
            
            img.thumbnail((W, H), Image.Resampling.LANCZOS)
            offset = ((W - img.width) // 2, (H - img.height) // 2)
            bg.paste(img, offset)
            bg.save(out_path, "JPEG", quality=95, subsampling=0)
            return out_path
    except:
        return None

def main():
    os.makedirs("workspace/extracted", exist_ok=True)
    os.makedirs("workspace/processed", exist_ok=True)
    os.makedirs("output", exist_ok=True)

    # 1. Scrape
    if any(URL.lower().split('?')[0].endswith(e) for e in ('.zip', '.rar', '.7z')):
        with requests.get(URL, stream=True) as r:
            with open("workspace/input", 'wb') as f: shutil.copyfileobj(r.raw, f)
        subprocess.run(['7z', 'x', 'workspace/input', '-oworkspace/extracted', '-y'])
    else:
        subprocess.run(f'gallery-dl --dest workspace/extracted "{URL}"', shell=True)

    # 2. Audio (Improved Resilience)
    has_audio = False
    if MUSIC_URL:
        # Added --no-check-certificate and user-agent to help bypass bot detection
        audio_cmd = f'yt-dlp --no-check-certificate --user-agent "Mozilla/5.0" -x --audio-format mp3 -o "workspace/audio.mp3" "{MUSIC_URL}"'
        subprocess.run(audio_cmd, shell=True)
        if os.path.exists("workspace/audio.mp3"): 
            has_audio = True
        else:
            print("Audio download failed (Bot Blocked). Proceeding without audio.")

    # 3. Parallel Image Processing
    files = []
    for dp, dn, fs in os.walk("workspace/extracted"):
        for f in fs:
            if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                files.append(os.path.join(dp, f))
    files.sort(key=lambda s: [int(t) if t.isdigit() else t.lower() for t in re.split('([0-9]+)', s)])
    
    tasks = [(f, f"workspace/processed/img_{i:04d}.jpg") for i, f in enumerate(files)]
    with ThreadPoolExecutor() as executor:
        processed_files = list(tqdm(executor.map(process_single_image, tasks), total=len(tasks), desc="Processing Images"))
    
    processed_files = [f for f in processed_files if f]
    if not processed_files: return

    # 4. Encoding Fixes
    out_video = f"output/{FILENAME}.mkv"
    audio_input = f'-i workspace/audio.mp3' if has_audio else ''
    audio_map = f'-map 0:v:0 -map 1:a:0 -shortest -c:a libopus -b:a {AUDIO_BITRATE}' if has_audio else '-c:a copy'
    
    # 1. Use numeric IDs for color (1 = BT.709, 1 = PC/Full Range)
    # 2. Remove -maxrate to satisfy SVT-AV1 target bitrate mode
    # 3. Explicitly set color space flags for the MKV container
    cmd = (
        f'ffmpeg -y -framerate 1/{DURATION} -i workspace/processed/img_%04d.jpg {audio_input} '
        f'-vf "fps={FPS},scale={W}:{H}:out_color_matrix=bt709:out_range=pc,format=yuv420p10le" '
        f'-c:v libsvtav1 -b:v {TARGET_BITRATE} -preset {PRESET} '
        f'-svtav1-params "tune=0:enable-overlays=1:keyint=10s:tile-columns=1:fast-decode=1:color-primaries=1:transfer-characteristics=1:matrix-coefficients=1:color-range=1" '
        f'-color_primaries bt709 -color_trc bt709 -colorspace bt709 -color_range pc '
        f'{audio_map} "{out_video}"'
    )

    print(f"Encoding Video (Target: {TARGET_BITRATE} Video + {AUDIO_BITRATE} Audio)...")
    subprocess.run(cmd, shell=True)
    subprocess.run(f'ffmpeg -y -i "{out_video}" -ss 00:00:01 -vframes 1 output/poster.jpg', shell=True)

if __name__ == "__main__":
    main()