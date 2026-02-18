import os, re, shutil, subprocess, requests, random
from PIL import Image, ImageOps
from tqdm import tqdm

# --- CONFIG ---
URL = os.getenv('FILE_URL')
MUSIC_URL = os.getenv('MUSIC_URL')
DURATION = float(os.getenv('IMG_DURATION', '2.5')) # Higher duration recommended for Ken Burns
FPS = 30
CRF = os.getenv('CRF', '32')
PRESET = os.getenv('PRESET', '10') # 10-12 is significantly faster for AV1
AR_TYPE = os.getenv('ASPECT_RATIO', 'Landscape (1920x1080)')
FILENAME = os.getenv('FILENAME', 'av1_slideshow')

RES_MAP = {
    'Landscape (1920x1080)': (1920, 1080),
    'Portrait (1080x1920)': (1080, 1920),
    'Square (1080x1080)': (1080, 1080)
}
W, H = RES_MAP.get(AR_TYPE, (1920, 1080))

def run(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)

def prepare_image(input_path, output_path):
    """Resizes image to fit canvas with blurred background (letterboxing)."""
    with Image.open(input_path) as img:
        img = img.convert('RGB')
        # Create Blurred Background
        bg = img.resize((W, H), Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(20))
        # Resize foreground
        img.thumbnail((W, H), Image.Resampling.LANCZOS)
        # Center foreground on background
        offset = ((W - img.width) // 2, (H - img.height) // 2)
        bg.paste(img, offset)
        bg.save(output_path, 'JPEG', quality=95)

def main():
    os.makedirs("workspace/extracted", exist_ok=True)
    os.makedirs("workspace/processed", exist_ok=True)
    os.makedirs("output", exist_ok=True)

    # 1. SCRAPE / EXTRACT
    is_archive = any(URL.lower().split('?')[0].endswith(e) for e in ('.zip', '.rar', '.7z', '.cbz'))
    if is_archive:
        print("Extracting Archive...")
        with requests.get(URL, stream=True) as r:
            with open("workspace/input", 'wb') as f: shutil.copyfileobj(r.raw, f)
        subprocess.run(['7z', 'x', 'workspace/input', '-oworkspace/extracted', '-y'])
    else:
        print(f"Scraping with gallery-dl: {URL}")
        run(f'gallery-dl --dest workspace/extracted "{URL}"')

    # 2. AUDIO DOWNLOAD
    has_audio = False
    if MUSIC_URL:
        print("Downloading Audio...")
        run(f'yt-dlp -x --audio-format mp3 -o "workspace/audio.mp3" "{MUSIC_URL}"')
        if os.path.exists("workspace/audio.mp3"): has_audio = True

    # 3. PROCESS IMAGES
    files = sorted([os.path.join(dp, f) for dp, dn, fs in os.walk("workspace/extracted") 
                    for f in fs if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))],
                    key=lambda s: [int(t) if t.isdigit() else t.lower() for t in re.split('([0-9]+)', s)])
    
    if not files:
        print("No images found!"); return

    print(f"Processing {len(files)} images...")
    processed_files = []
    for i, f in enumerate(tqdm(files)):
        out_f = f"workspace/processed/img_{i:04d}.jpg"
        try:
            # We use FFmpeg to pad/blur because it's faster for bulk
            cmd = f'ffmpeg -y -i "{f}" -vf "scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2" "{out_f}"'
            run(cmd)
            processed_files.append(out_f)
        except: pass

    # 4. GENERATE VIDEO VIA FFMPEG FILTER (FASTEST METHOD)
    # Creating a concat file for FFmpeg
    concat_path = "workspace/input.txt"
    with open(concat_path, "w") as f:
        for img in processed_files:
            f.write(f"file '{os.path.abspath(img)}'\nduration {DURATION}\n")
        f.write(f"file '{os.path.abspath(processed_files[-1])}'\n") # End frame

    # Final Command Construction
    out_video = f"output/{FILENAME}.mkv"
    
    # SVT-AV1 with Ken Burns (Zoompan) and Audio Loop
    audio_str = f'-i workspace/audio.mp3 -map 0:v:0 -map 1:a:0 -shortest -c:a libopus -b:a 128k' if has_audio else '-c:a copy'
    
    # This filter adds a subtle Ken Burns zoom and maintains 30fps
    kb_filter = f"zoompan=z='min(zoom+0.0015,1.5)':d={int(DURATION*FPS)}:s={W}x{H},framerate=30"

    cmd = (
        f'ffmpeg -y -f concat -safe 0 -i {concat_path} {audio_str if has_audio else ""} '
        f'-vf "{kb_filter},format=yuv420p10le" '
        f'-c:v libsvtav1 -crf {CRF} -preset {PRESET} '
        f'-svtav1-params "tune=0:enable-overlays=1:color-primaries=1:transfer-characteristics=1:matrix-coefficients=1" '
        f'"{out_video}"'
    )

    print("Encoding AV1 Video (FFmpeg Native)...")
    subprocess.run(cmd, shell=True)
    
    # Generate Thumbnail
    subprocess.run(f'ffmpeg -y -i "{out_video}" -ss 00:00:01 -vframes 1 output/poster.jpg', shell=True)

if __name__ == "__main__":
    main()