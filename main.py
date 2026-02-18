import os, re, shutil, subprocess, requests, imageio
import numpy as np
from PIL import Image, ImageFilter, ImageEnhance

# Configuration
URL = os.getenv('FILE_URL')
DURATION = float(os.getenv('IMG_DURATION', '0.33'))
W, H = 1920, 1080
FPS = 30
CRF = os.getenv('CRF', '32')
PRESET = os.getenv('PRESET', '8')

def get_processed_frame(pil_img):
    """Universal Strategy: Blurred BG (Fill) + Original (Fit)"""
    # 1. Create Blurred BG (Fast method: resize tiny -> blur -> scale up)
    bg = pil_img.convert('RGB')
    small = bg.resize((160, 90), Image.Resampling.NEAREST)
    blurred = small.filter(ImageFilter.GaussianBlur(radius=2))
    bg = blurred.resize((W, H), Image.Resampling.LANCZOS)
    bg = ImageEnhance.Brightness(bg).enhance(0.4)

    # 2. Create Foreground (Fit - No Crop)
    fg = pil_img.convert('RGB')
    fg.thumbnail((W, H), Image.Resampling.LANCZOS)
    
    # 3. Composite Center
    bg.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2))
    return bg

def main():
    os.makedirs("workspace/extracted", exist_ok=True)
    os.makedirs("output", exist_ok=True)
    
    # Filename Setup (Clean filename from URL or Input)
    match = re.search(r'f=([^&]+)', URL)
    fname = os.getenv('FILENAME', '').strip() or (match.group(1).rsplit('.', 1)[0] if match else "output")
    fname = re.sub(r'[^a-zA-Z0-9_-]', '_', fname)
    out_path = f"output/{fname}.mkv"

    # Download & Extract
    print(f"Downloading archive...")
    headers = {'User-Agent': 'Mozilla/5.0'}
    r = requests.get(URL, headers=headers, stream=True)
    with open("workspace/input", 'wb') as f: shutil.copyfileobj(r.raw, f)
    
    print("Extracting with 7z...")
    subprocess.run(['7z', 'x', 'workspace/input', '-oworkspace/extracted', '-y'], check=True)

    # Collect and Natural Sort Files
    valid_exts = ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.mp4', '.mov')
    files = []
    for dp, dn, filenames in os.walk("workspace/extracted"):
        for f in filenames:
            if f.lower().endswith(valid_exts):
                files.append(os.path.join(dp, f))
    files.sort(key=lambda s: [int(t) if t.isdigit() else t.lower() for t in re.split('([0-9]+)', s)])

    # FFmpeg Pipe Command for MKV + AV1
    # -f rawvideo: receive raw pixels from Python
    # -pix_fmt yuv420p10le: Encode in 10-bit for superior AV1 efficiency
    cmd = [
        'ffmpeg', '-y',
        '-f', 'rawvideo', '-vcodec', 'rawvideo',
        '-s', f'{W}x{H}', '-pix_fmt', 'rgb24', '-r', str(FPS),
        '-i', '-', # Read from stdin pipe
        '-c:v', 'libsvtav1',
        '-crf', CRF,
        '-preset', PRESET,
        '-svtav1-params', 'tune=0:enable-overlays=1:scd=1',
        '-pix_fmt', 'yuv420p10le',
        '-c:a', 'libopus', '-b:a', '128k', # MKV pairs best with Opus audio
        out_path
    ]
    
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    print(f"Direct Encoding to MKV: {len(files)} items...")
    
    for i, f in enumerate(files):
        print(f"[{i+1}/{len(files)}] {os.path.basename(f)}")
        ext = f.lower()
        
        try:
            if ext.endswith(('.mp4', '.mov', '.gif', '.webp')):
                # Handle Videos/Animated (Frame by Frame)
                reader = imageio.get_reader(f)
                n_frames = int(max(DURATION * FPS, 1)) if ext.endswith(('.gif', '.webp')) else None
                
                count = 0
                for frame in reader:
                    pil_frame = Image.fromarray(frame)
                    processed = get_processed_frame(pil_frame)
                    process.stdin.write(processed.tobytes())
                    count += 1
                    if n_frames and count >= n_frames: break
                reader.close()
            else:
                # Handle Static Images (Repeat frame for duration)
                with Image.open(f) as img:
                    processed = get_processed_frame(img)
                    frame_bytes = processed.tobytes()
                    for _ in range(int(max(DURATION * FPS, 1))):
                        process.stdin.write(frame_bytes)
        except Exception as e:
            print(f"Skipping {f}: {e}")

    # Close pipe and wait for FFmpeg to wrap up the MKV container
    process.stdin.close()
    process.wait()

    # Pass the filename back to GitHub for the upload step
    with open(os.getenv('GITHUB_OUTPUT'), 'a') as go:
        go.write(f"final_name={fname}\n")

if __name__ == "__main__":
    main()