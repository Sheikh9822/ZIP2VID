import os, re, shutil, subprocess, requests, time
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
PRESET = "12" 
AR_TYPE = os.getenv('ASPECT_RATIO', 'Landscape (1920x1080)')
FILENAME = os.getenv('FILENAME', 'av1_video')

RES_MAP = {
    'Landscape (1920x1080)': (1920, 1080),
    'Portrait (1080x1920)': (1080, 1920),
    'Square (1080x1080)': (1080, 1080)
}
W, H = RES_MAP.get(AR_TYPE, (1920, 1080))

# 20 concurrent threads for maximum speed
DOWNLOAD_THREADS = 20

def fast_download(args):
    url, dest = args
    if os.path.exists(dest): return dest
    try:
        # Mimic browser to avoid bot detection
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        with requests.get(url, headers=headers, stream=True, timeout=10) as r:
            if r.status_code == 200:
                with open(dest, 'wb') as f:
                    shutil.copyfileobj(r.raw, f)
                return dest
    except: return None

def process_single_image(args):
    img_path, out_path = args
    try:
        with Image.open(img_path) as img:
            img = img.convert('RGB')
            # High speed blur: Resize down then up
            bg = img.resize((W, H), Image.Resampling.NEAREST)
            bg = bg.resize((100, int(100*(H/W))), Image.Resampling.NEAREST)
            bg = bg.filter(ImageFilter.GaussianBlur(5))
            bg = bg.resize((W, H), Image.Resampling.LANCZOS)
            img.thumbnail((W, H), Image.Resampling.LANCZOS)
            offset = ((W - img.width) // 2, (H - img.height) // 2)
            bg.paste(img, offset)
            bg.save(out_path, "JPEG", quality=95, subsampling=0)
            return out_path
    except: return None

def main():
    os.makedirs("workspace/extracted", exist_ok=True)
    os.makedirs("workspace/processed", exist_ok=True)
    os.makedirs("output", exist_ok=True)

    # 1. SCRAPE & PARALLEL DOWNLOAD
    if any(URL.lower().split('?')[0].endswith(e) for e in ('.zip', '.rar', '.7z')):
        with requests.get(URL, stream=True) as r:
            with open("workspace/input", 'wb') as f: shutil.copyfileobj(r.raw, f)
        subprocess.run(['7z', 'x', 'workspace/input', '-oworkspace/extracted', '-y'])
    else:
        print("Scraping image URLs...")
        # Get only the URLs from gallery-dl (very fast)
        result = subprocess.run(f'gallery-dl -g "{URL}"', shell=True, capture_output=True, text=True)
        urls = [u.strip() for u in result.stdout.split('\n') if u.strip().startswith('http')]
        
        if not urls:
            print("No URLs found with gallery-dl. Attempting direct fallback...")
            return

        print(f"Downloading {len(urls)} images using {DOWNLOAD_THREADS} threads...")
        download_tasks = [(u, os.path.join("workspace/extracted", f"raw_{i:04d}.jpg")) for i, u in enumerate(urls)]
        with ThreadPoolExecutor(max_workers=DOWNLOAD_THREADS) as executor:
            list(tqdm(executor.map(fast_download, download_tasks), total=len(download_tasks), desc="Downloading"))

    # 2. AUDIO
    has_audio = False
    if MUSIC_URL:
        print("Fetching audio...")
        audio_cmd = f'yt-dlp --no-check-certificate --user-agent "Mozilla/5.0" -x --audio-format mp3 -o "workspace/audio.mp3" "{MUSIC_URL}"'
        subprocess.run(audio_cmd, shell=True)
        if os.path.exists("workspace/audio.mp3"): has_audio = True

    # 3. PARALLEL PROCESSING
    files = sorted([os.path.join(dp, f) for dp, dn, fs in os.walk("workspace/extracted") for f in fs if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))],
                    key=lambda s: [int(t) if t.isdigit() else t.lower() for t in re.split('([0-9]+)', s)])
    
    tasks = [(f, f"workspace/processed/img_{i:04d}.jpg") for i, f in enumerate(files)]
    with ThreadPoolExecutor() as executor:
        processed_files = list(tqdm(executor.map(process_single_image, tasks), total=len(tasks), desc="Processing"))
    
    processed_files = [f for f in processed_files if f]
    if not processed_files: return

    # 4. ENCODING
    out_video = f"output/{FILENAME}.mkv"
    audio_input = f'-i workspace/audio.mp3' if has_audio else ''
    audio_map = f'-map 0:v:0 -map 1:a:0 -shortest -c:a libopus -b:a {AUDIO_BITRATE}' if has_audio else '-c:a copy'
    
    # Using spline scaling for better color accuracy + 10bit AV1
    cmd = (
        f'ffmpeg -y -framerate 1/{DURATION} -i workspace/processed/img_%04d.jpg {audio_input} '
        f'-vf "fps={FPS},scale={W}:{H}:flags=spline:out_color_matrix=bt709:out_range=pc,format=yuv420p10le" '
        f'-c:v libsvtav1 -b:v {TARGET_BITRATE} -preset {PRESET} '
        f'-svtav1-params "tune=0:enable-overlays=1:keyint=10s:tile-columns=1:fast-decode=1:color-primaries=1:transfer-characteristics=1:matrix-coefficients=1:color-range=1" '
        f'-color_primaries bt709 -color_trc bt709 -colorspace bt709 -color_range pc '
        f'{audio_map} "{out_video}"'
    )

    print(f"Encoding AV1 (Target: {TARGET_BITRATE})...")
    subprocess.run(cmd, shell=True)
    subprocess.run(f'ffmpeg -y -i "{out_video}" -ss 00:00:01 -vframes 1 output/poster.jpg', shell=True)

if __name__ == "__main__":
    main()