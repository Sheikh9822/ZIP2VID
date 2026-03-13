import os, re, shutil, subprocess, sys, time, logging
import requests
from PIL import Image, ImageFilter
from concurrent.futures import ThreadPoolExecutor
from tqdm import tqdm

# --- LOGGING ---
logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

# --- CONFIG ---
URL       = os.getenv('FILE_URL', '').strip()
MUSIC_URL = os.getenv('MUSIC_URL', '').strip()
FILENAME  = os.getenv('FILENAME', 'av1_video').strip()
AR_TYPE   = os.getenv('ASPECT_RATIO', 'Landscape (1920x1080)')
PRESET    = os.getenv('PRESET', '12')

# --- INPUT VALIDATION ---
if not URL:
    log.error("FILE_URL environment variable is not set. Aborting.")
    sys.exit(1)

try:
    DURATION = float(os.getenv('IMG_DURATION', '0.33'))
    if DURATION <= 0:
        raise ValueError("IMG_DURATION must be greater than 0")
except ValueError as e:
    log.error(f"Invalid IMG_DURATION: {e}")
    sys.exit(1)

try:
    PRESET_INT = int(PRESET)
    if not (0 <= PRESET_INT <= 13):
        raise ValueError("PRESET must be between 0 and 13")
except ValueError as e:
    log.error(f"Invalid PRESET: {e}")
    sys.exit(1)

FPS            = 15
TARGET_BITRATE = "400k"
AUDIO_BITRATE  = "96k"

RES_MAP = {
    'Landscape (1920x1080)': (1920, 1080),
    'Portrait (1080x1920)':  (1080, 1920),
    'Square (1080x1080)':    (1080, 1080)
}
W, H = RES_MAP.get(AR_TYPE, (1920, 1080))

DOWNLOAD_THREADS = 20
MAX_RETRIES      = 3

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/122.0.0.0 Safari/537.36'
    ),
    'Referer': 'https://kemono.cr/'  # Fix: hotlink protection bypass
}


# ---------------------------------------------------------------------------
# DOWNLOAD
# ---------------------------------------------------------------------------

def fast_download(args):
    """Download a single URL to dest with retries and error logging."""
    url, dest = args
    if os.path.exists(dest):
        return dest
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with requests.get(url, headers=HEADERS, stream=True, timeout=20) as r:
                if r.status_code == 200:
                    with open(dest, 'wb') as f:
                        shutil.copyfileobj(r.raw, f)
                    return dest
                else:
                    log.warning(f"HTTP {r.status_code} for {url} (attempt {attempt}/{MAX_RETRIES})")
        except requests.RequestException as e:
            log.warning(f"Download error ({url}): {e} (attempt {attempt}/{MAX_RETRIES})")
        if attempt < MAX_RETRIES:
            time.sleep(1.5 * attempt)
    log.error(f"Failed to download after {MAX_RETRIES} attempts: {url}")
    return None


# ---------------------------------------------------------------------------
# IMAGE PROCESSING
# ---------------------------------------------------------------------------

def process_single_image(args):
    """
    Resize image onto a blurred background canvas.
    Optimised: blur the small thumbnail first, then upscale once.
    """
    img_path, out_path = args
    try:
        with Image.open(img_path) as img:
            img = img.convert('RGB')

            # Build blurred background efficiently:
            # shrink to thumbnail → blur cheaply → upscale once
            thumb_w = 120
            thumb_h = int(thumb_w * (H / W))
            bg = img.resize((thumb_w, thumb_h), Image.Resampling.NEAREST)
            bg = bg.filter(ImageFilter.GaussianBlur(6))
            bg = bg.resize((W, H), Image.Resampling.LANCZOS)

            # Fit original image inside canvas
            img.thumbnail((W, H), Image.Resampling.LANCZOS)
            offset = ((W - img.width) // 2, (H - img.height) // 2)
            bg.paste(img, offset)
            bg.save(out_path, "JPEG", quality=90)
            return out_path
    except Exception as e:
        log.error(f"Failed to process {img_path}: {e}")
        return None


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    os.makedirs("workspace/extracted", exist_ok=True)
    os.makedirs("workspace/processed", exist_ok=True)
    os.makedirs("output", exist_ok=True)

    # ------------------------------------------------------------------
    # 1. DOWNLOAD / EXTRACT SOURCE IMAGES
    # ------------------------------------------------------------------
    archive_exts = ('.zip', '.rar', '.7z')
    base_url = URL.lower().split('?')[0]

    if any(base_url.endswith(e) for e in archive_exts):
        log.info("Downloading archive...")
        with requests.get(URL, headers=HEADERS, stream=True) as r:
            if r.status_code != 200:
                log.error(f"Archive download failed with HTTP {r.status_code}")
                sys.exit(1)
            with open("workspace/input", 'wb') as f:
                shutil.copyfileobj(r.raw, f)
        result = subprocess.run(
            ['7z', 'x', 'workspace/input', '-oworkspace/extracted', '-y'],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            log.error(f"7z extraction failed:\n{result.stderr}")
            sys.exit(1)
    else:
        log.info("Scraping image URLs via gallery-dl...")
        result = subprocess.run(
            ['gallery-dl', '-g', URL],
            capture_output=True, text=True
        )
        urls = [u.strip() for u in result.stdout.split('\n') if u.strip().startswith('http')]
        if not urls:
            log.error("gallery-dl returned no URLs. Check your FILE_URL.")
            sys.exit(1)
        log.info(f"Found {len(urls)} images. Downloading with {DOWNLOAD_THREADS} threads...")
        download_tasks = [
            (u, os.path.join("workspace/extracted", f"raw_{i:04d}.jpg"))
            for i, u in enumerate(urls)
        ]
        with ThreadPoolExecutor(max_workers=DOWNLOAD_THREADS) as executor:
            results = list(tqdm(
                executor.map(fast_download, download_tasks),
                total=len(download_tasks),
                desc="Downloading Images"
            ))
        failed = results.count(None)
        if failed:
            log.warning(f"{failed}/{len(urls)} images failed to download.")

    # ------------------------------------------------------------------
    # 2. AUDIO DOWNLOAD
    # ------------------------------------------------------------------
    has_audio  = False
    audio_path = "workspace/audio.mp3"

    if MUSIC_URL:
        log.info(f"Downloading audio: {MUSIC_URL[:60]}...")
        audio_cmd = [
            'yt-dlp', '--no-check-certificate',
            '--user-agent', HEADERS['User-Agent'],
            '--referer', 'https://www.google.com/',
            '-f', 'ba/b', '-x', '--audio-format', 'mp3',
            '--retries', '3',
            '-o', audio_path,
            MUSIC_URL
        ]
        proc = subprocess.run(audio_cmd, capture_output=True, text=True)
        if os.path.exists(audio_path) and os.path.getsize(audio_path) > 1000:
            has_audio = True
            log.info("Audio downloaded successfully.")
        else:
            log.warning(f"Audio download failed or file too small.\n{proc.stderr[-500:]}")

    # ------------------------------------------------------------------
    # 3. COLLECT & SORT IMAGE FILES
    # ------------------------------------------------------------------
    IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.webp')
    files = []
    for dp, _, fs in os.walk("workspace/extracted"):
        for f in fs:
            if f.lower().endswith(IMAGE_EXTS):
                files.append(os.path.join(dp, f))

    files.sort(key=lambda s: [
        int(t) if t.isdigit() else t.lower()
        for t in re.split('([0-9]+)', s)
    ])

    if not files:
        log.error("No images found after extraction. Aborting.")
        sys.exit(1)

    log.info(f"Processing {len(files)} images...")

    # ------------------------------------------------------------------
    # 4. PARALLEL IMAGE PROCESSING (capped thread count)
    # ------------------------------------------------------------------
    cpu_count    = os.cpu_count() or 4
    worker_count = min(cpu_count * 2, 32)

    tasks = [
        (f, f"workspace/processed/img_{i:04d}.jpg")
        for i, f in enumerate(files)
    ]
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        processed_files = list(tqdm(
            executor.map(process_single_image, tasks),
            total=len(tasks),
            desc="Processing Frames"
        ))

    processed_files = [f for f in processed_files if f]
    if not processed_files:
        log.error("All image processing failed. Aborting.")
        sys.exit(1)

    failed_proc = len(tasks) - len(processed_files)
    if failed_proc:
        log.warning(f"{failed_proc} image(s) failed processing and will be skipped.")

    # ------------------------------------------------------------------
    # 5. ENCODE VIDEO
    # ------------------------------------------------------------------
    out_video      = f"output/{FILENAME}.mkv"
    frames_per_img = max(1, int(DURATION * FPS))
    zoom_speed     = 0.008 if DURATION < 0.5 else 0.002

    vf = (
        f"zoompan=z='min(zoom+{zoom_speed},1.3)':d={frames_per_img}:s={W}x{H}:fps={FPS},"
        f"scale={W}:{H}:flags=spline:out_color_matrix=bt709:out_range=pc,"
        f"format=yuv420p10le"
    )

    cmd = [
        'ffmpeg', '-y',
        '-framerate', str(1 / DURATION),
        '-i', 'workspace/processed/img_%04d.jpg'
    ]

    if has_audio:
        cmd += ['-i', audio_path]

    cmd += [
        '-vf', vf,
        '-c:v', 'libsvtav1',
        '-b:v', TARGET_BITRATE,
        '-preset', str(PRESET_INT),
        '-svtav1-params',
        'tune=0:enable-overlays=1:keyint=10s:tile-columns=1:'
        'fast-decode=1:color-primaries=1:transfer-characteristics=1:'
        'matrix-coefficients=1:color-range=1',
        '-color_primaries', 'bt709',
        '-color_trc',       'bt709',
        '-colorspace',      'bt709',
        '-color_range',     'pc',
        '-progress', 'pipe:1',
        '-stats_period', '2',
    ]

    if has_audio:
        cmd += [
            '-map', '0:v:0',
            '-map', '1:a:0',
            '-shortest',
            '-c:a', 'libopus',
            '-b:a', AUDIO_BITRATE
        ]
    # Note: intentionally NO '-c:a copy' when there is no audio track

    cmd.append(out_video)

    log.info("Encoding final video (SVT-AV1)...")
    encode_proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    for line in encode_proc.stdout:
        line = line.strip()
        if line.startswith('frame=') or line.startswith('out_time='):
            print(f"\r  {line}", end='', flush=True)
    encode_proc.wait()
    print()

    if encode_proc.returncode != 0:
        log.error("ffmpeg encoding failed.")
        sys.exit(1)

    log.info(f"Video saved: {out_video}")

    # ------------------------------------------------------------------
    # 6. POSTER FRAME (safe: use mid-point instead of hardcoded 1s)
    # ------------------------------------------------------------------
    dur_result = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', out_video],
        capture_output=True, text=True
    )
    try:
        vid_duration = float(dur_result.stdout.strip())
        poster_ts    = min(1.0, vid_duration / 2)
    except ValueError:
        poster_ts = 0.5

    subprocess.run([
        'ffmpeg', '-y', '-i', out_video,
        '-ss', str(poster_ts), '-vframes', '1', 'output/poster.jpg'
    ], capture_output=True)
    log.info("Poster frame saved: output/poster.jpg")

    # ------------------------------------------------------------------
    # 7. CLEANUP WORKSPACE
    # ------------------------------------------------------------------
    shutil.rmtree("workspace", ignore_errors=True)
    log.info("Workspace cleaned up.")
    log.info("Done.")


if __name__ == "__main__":
    main()
