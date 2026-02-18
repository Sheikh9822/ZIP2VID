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
MAX_BITRATE = "600k"
AUDIO_BITRATE = "96k"  # Set to user requested 96k
PRESET = os.getenv('PRESET', '8') 
AR_TYPE = os.getenv('ASPECT_RATIO', 'Landscape (1920x1080)')
FILENAME = os.getenv('FILENAME', 'av1_slideshow')

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
            # Fast Background Blur (Low CPU usage method)
            bg = img.resize((W, H), Image.Resampling.NEAREST)
            bg = bg.resize((100, int(100*(H/W))), Image.Resampling.NEAREST)
            bg = bg.filter(ImageFilter.GaussianBlur(5))
            bg = bg.resize((W, H), Image.Resampling.LANCZOS)
            
            img.thumbnail((W, H), Image.Resampling.LANCZOS)
            offset = ((W - img.width) // 2, (H - img.height) // 2)
            bg.paste(img, offset)
            # High quality JPEG intermediate
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
        # gallery-dl is much faster and lighter for web links
        subprocess.run(f'gallery-dl --dest workspace/extracted "{URL}"', shell=True)

    # 2. Audio
    has_audio = False
    if MUSIC_URL:
        subprocess.run(f'yt-dlp -x --audio-format mp3 -o "workspace/audio.mp3" "{MUSIC_URL}"', shell=True)
        if os.path.exists("workspace/audio.mp3"): has_audio = True

    # 3. Parallel Image Processing (Multi-core)
    files = sorted([os.path.join(dp, f) for dp, dn, fs in os.walk("workspace/extracted") 
                    for f in fs if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))],
                    key=lambda s: [int(t) if t.isdigit() else t.lower() for t in re.split('([0-9]+)', s)])
    
    tasks = [(f, f"workspace/processed/img_{i:04d}.jpg") for i, f in enumerate(files)]
    with ThreadPoolExecutor() as executor:
        processed_files = list(tqdm(executor.map(process_single_image, tasks), total=len(tasks), desc="Processing Images"))
    
    processed_files = [f for f in processed_files if f]

    # 4. Encoding with Bitrate Targeting and Color Fixes
    out_video = f"output/{FILENAME}.mkv"
    # Audio bitrate set to 96k using libopus
    audio_cmd = f'-i workspace/audio.mp3 -map 0:v:0 -map 1:a:0 -shortest -c:a libopus -b:a {AUDIO_BITRATE}' if has_audio else '-c:a copy'
    
    # Scale and Color Matrix to match original source and remove deprecated warnings
    vf_chain = f"fps={FPS},scale={W}:{H}:out_color_matrix=bt709:out_range=pc,format=yuv420p10le"

    cmd = (
        f'ffmpeg -y -framerate 1/{DURATION} -i workspace/processed/img_%04d.jpg {audio_cmd} '
        f'-vf "{vf_chain}" '
        f'-c:v libsvtav1 -b:v {TARGET_BITRATE} -maxrate {MAX_BITRATE} -bufsize 2M -preset {PRESET} '
        f'-svtav1-params "tune=0:enable-overlays=1:keyint=10s:tile-columns=1:fast-decode=1:color-matrix=bt709:color-range=pc" '
        f'"{out_video}"'
    )

    print(f"Encoding Video (V:{TARGET_BITRATE} + A:{AUDIO_BITRATE})...")
    subprocess.run(cmd, shell=True)
    
    # Clean up and generate thumbnail
    subprocess.run(f'ffmpeg -y -i "{out_video}" -ss 00:00:01 -vframes 1 output/poster.jpg', shell=True)

if __name__ == "__main__":
    main()