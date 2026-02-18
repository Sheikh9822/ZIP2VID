import os, re, shutil, subprocess, requests, imageio
import numpy as np
from PIL import Image, ImageFilter, ImageEnhance
from tqdm import tqdm

# Configuration
URL = os.getenv('FILE_URL')
DURATION = float(os.getenv('IMG_DURATION', '0.33'))
W, H = 1920, 1080
FPS = 30
CRF = os.getenv('CRF', '32')
PRESET = os.getenv('PRESET', '8')

def get_processed_frame(pil_img, zoom_factor=1.0):
    """Blurred BG + Fit FG with subtle zoom."""
    # 1. Background (Fast Blur)
    bg = pil_img.convert('RGB')
    small = bg.resize((160, 90), Image.Resampling.NEAREST)
    blurred = small.filter(ImageFilter.GaussianBlur(radius=2))
    bg = blurred.resize((W, H), Image.Resampling.LANCZOS)
    bg = ImageEnhance.Brightness(bg).enhance(0.4)

    # 2. Foreground (Fit with Micro-Zoom)
    fg = pil_img.convert('RGB')
    # Apply zoom factor to the fit scale
    base_scale = min(W / fg.width, H / fg.height)
    new_size = (int(fg.width * base_scale * zoom_factor), int(fg.height * base_scale * zoom_factor))
    fg = fg.resize(new_size, Image.Resampling.LANCZOS)
    
    # 3. Composite
    bg.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2))
    return bg

def main():
    os.makedirs("workspace/extracted", exist_ok=True)
    os.makedirs("output", exist_ok=True)
    
    match = re.search(r'f=([^&]+)', URL)
    fname = os.getenv('FILENAME', '').strip() or (match.group(1).rsplit('.', 1)[0] if match else "output")
    fname = re.sub(r'[^a-zA-Z0-9_-]', '_', fname)
    out_path = f"output/{fname}.mkv"

    # Download & Extract
    print(f"--- Downloading Archive ---")
    headers = {'User-Agent': 'Mozilla/5.0'}
    r = requests.get(URL, headers=headers, stream=True)
    with open("workspace/input", 'wb') as f: shutil.copyfileobj(r.raw, f)
    subprocess.run(['7z', 'x', 'workspace/input', '-oworkspace/extracted', '-y'], check=True, stdout=subprocess.DEVNULL)

    # Collect Files
    valid_exts = ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.mp4', '.mov')
    files = []
    for dp, dn, filenames in os.walk("workspace/extracted"):
        for f in filenames:
            if f.lower().endswith(valid_exts):
                files.append(os.path.join(dp, f))
    files.sort(key=lambda s: [int(t) if t.isdigit() else t.lower() for t in re.split('([0-9]+)', s)])

    # Metadata & Chapters Preparation
    chapters_file = "workspace/chapters.txt"
    with open(chapters_file, "w") as ch:
        ch.write(";FFMETADATA1\n")
        current_time_ns = 0
        
    # FFmpeg Pipe Command with Color Signaling
    cmd = [
        'ffmpeg', '-y',
        '-f', 'rawvideo', '-vcodec', 'rawvideo',
        '-s', f'{W}x{H}', '-pix_fmt', 'rgb24', '-r', str(FPS),
        '-i', '-', 
        '-c:v', 'libsvtav1',
        '-crf', CRF,
        '-preset', PRESET,
        '-svtav1-params', 'tune=0:enable-overlays=1:scd=1',
        # Color Signaling (BT.709)
        '-color_range', '1', '-colorspace', '1', '-color_primaries', '1', '-color_trc', '1',
        '-pix_fmt', 'yuv420p10le',
        '-c:a', 'libopus', '-b:a', '128k',
        out_path
    ]
    
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    print(f"--- Processing {len(files)} items to AV1 MKV ---")
    
    # Progress Bar
    pbar = tqdm(total=len(files), unit="item")
    
    total_frames_clock = 0
    
    for i, f in enumerate(files):
        ext = f.lower()
        
        # Add Chapter Marker
        start_time_ms = int((total_frames_clock / FPS) * 1000)
        with open(chapters_file, "a") as ch:
            ch.write(f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={start_time_ms}\n")
            ch.write(f"END={start_time_ms + int(DURATION * 1000)}\n")
            ch.write(f"title={os.path.basename(f)}\n")

        try:
            if ext.endswith(('.mp4', '.mov', '.gif', '.webp')):
                reader = imageio.get_reader(f)
                n_frames = int(max(DURATION * FPS, 1)) if ext.endswith(('.gif', '.webp')) else None
                count = 0
                for frame in reader:
                    processed = get_processed_frame(Image.fromarray(frame))
                    process.stdin.write(processed.tobytes())
                    count += 1
                    total_frames_clock += 1
                    if n_frames and count >= n_frames: break
                reader.close()
            else:
                with Image.open(f) as img:
                    num_frames = int(max(DURATION * FPS, 1))
                    for frame_idx in range(num_frames):
                        # Apply subtle zoom (1.0 to 1.02)
                        zoom = 1.0 + (0.02 * (frame_idx / num_frames))
                        processed = get_processed_frame(img, zoom_factor=zoom)
                        process.stdin.write(processed.tobytes())
                        total_frames_clock += 1
        except Exception as e:
            pass
        
        pbar.update(1)

    pbar.close()
    process.stdin.close()
    process.wait()

    # Step 3: Inject Chapters (Using FFmpeg to remux metadata)
    print("--- Finalizing Chapters & Metadata ---")
    final_out = out_path.replace(".mkv", "_final.mkv")
    subprocess.run([
        'ffmpeg', '-i', out_path, '-i', chapters_file, 
        '-map_metadata', '1', '-codec', 'copy', final_out
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    os.replace(final_out, out_path)

    with open(os.getenv('GITHUB_OUTPUT'), 'a') as go:
        go.write(f"final_name={fname}\n")

if __name__ == "__main__":
    main()