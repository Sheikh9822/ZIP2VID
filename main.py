import os, re, shutil, subprocess, requests, imageio
import numpy as np
from PIL import Image, ImageFilter, ImageEnhance
from tqdm import tqdm
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

# Configuration
URL = os.getenv('FILE_URL')
DURATION = float(os.getenv('IMG_DURATION', '0.33'))
W, H = 1920, 1080
FPS = 30
CRF = os.getenv('CRF', '32')
PRESET = os.getenv('PRESET', '8')
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}

def get_processed_frame(pil_img, zoom_factor=1.0):
    """Blurred BG + Fit FG + Micro-Zoom."""
    bg = pil_img.convert('RGB')
    small = bg.resize((160, 90), Image.Resampling.NEAREST)
    blurred = small.filter(ImageFilter.GaussianBlur(radius=2))
    bg = blurred.resize((W, H), Image.Resampling.LANCZOS)
    bg = ImageEnhance.Brightness(bg).enhance(0.4)

    fg = pil_img.convert('RGB')
    base_scale = min(W / fg.width, H / fg.height)
    new_size = (int(fg.width * base_scale * zoom_factor), int(fg.height * base_scale * zoom_factor))
    fg = fg.resize(new_size, Image.Resampling.LANCZOS)
    
    bg.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2))
    return bg

def download_media(url, folder):
    try:
        fname = os.path.basename(urlparse(url).path)
        if not fname: fname = "downloaded_file"
        target = os.path.join(folder, fname)
        with requests.get(url, headers=HEADERS, stream=True, timeout=30) as r:
            r.raise_for_status()
            with open(target, 'wb') as f:
                shutil.copyfileobj(r.raw, f)
        return target
    except: return None

def scrape_page(url, folder):
    print(f"Scanning webpage for media: {url}")
    response = requests.get(url, headers=HEADERS)
    soup = BeautifulSoup(response.text, 'lxml')
    
    # Common media extensions
    exts = ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.mp4', '.mov')
    found_links = set()

    # Look for <a> tags (high res links) and <img> tags
    for tag in soup.find_all(['a', 'img', 'source']):
        link = tag.get('href') or tag.get('src') or tag.get('data-src')
        if link:
            full_url = urljoin(url, link)
            if any(full_url.lower().endswith(e) for e in exts):
                found_links.add(full_url)
    
    print(f"Found {len(found_links)} potential media files. Downloading...")
    for link in tqdm(found_links):
        download_media(link, folder)

def main():
    workspace = "workspace"
    extract_path = os.path.join(workspace, "extracted")
    os.makedirs(extract_path, exist_ok=True)
    os.makedirs("output", exist_ok=True)
    
    # 1. Determine Source Type & Download
    is_archive = any(URL.lower().split('?')[0].endswith(e) for e in ('.zip', '.rar', '.7z', '.cbz', '.cbr'))
    
    if is_archive:
        print("Input detected as Archive.")
        archive_p = os.path.join(workspace, "input_file")
        with requests.get(URL, headers=HEADERS, stream=True) as r:
            with open(archive_p, 'wb') as f: shutil.copyfileobj(r.raw, f)
        subprocess.run(['7z', 'x', archive_p, f'-o{extract_path}', '-y'], check=True, stdout=subprocess.DEVNULL)
    else:
        scrape_page(URL, extract_path)

    # 2. Collect & Sort Files
    valid_exts = ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.mp4', '.mov')
    files = []
    for dp, dn, filenames in os.walk(extract_path):
        for f in filenames:
            if f.lower().endswith(valid_exts):
                files.append(os.path.join(dp, f))
    files.sort(key=lambda s: [int(t) if t.isdigit() else t.lower() for t in re.split('([0-9]+)', s)])

    if not files:
        print("No media files found to process.")
        return

    # Filename
    match = re.search(r'f=([^&]+)', URL)
    fname = os.getenv('FILENAME', '').strip() or (match.group(1).rsplit('.', 1)[0] if match else "output")
    fname = re.sub(r'[^a-zA-Z0-9_-]', '_', fname)
    out_path = f"output/{fname}.mkv"

    # 3. Process to AV1
    chapters_file = os.path.join(workspace, "chapters.txt")
    with open(chapters_file, "w") as ch: ch.write(";FFMETADATA1\n")

    cmd = [
        'ffmpeg', '-y', '-f', 'rawvideo', '-vcodec', 'rawvideo', '-s', f'{W}x{H}', 
        '-pix_fmt', 'rgb24', '-r', str(FPS), '-i', '-', 
        '-c:v', 'libsvtav1', '-crf', CRF, '-preset', PRESET,
        '-svtav1-params', 'tune=0:enable-overlays=1:scd=1',
        '-color_range', '1', '-colorspace', '1', '-color_primaries', '1', '-color_trc', '1',
        '-pix_fmt', 'yuv420p10le', '-c:a', 'libopus', '-b:a', '128k', out_path
    ]
    
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    pbar = tqdm(total=len(files), unit="item")
    total_frames = 0
    
    for f in files:
        start_ms = int((total_frames / FPS) * 1000)
        with open(chapters_file, "a") as ch:
            ch.write(f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={start_ms}\n")
            ch.write(f"END={start_ms + int(DURATION * 1000)}\ntitle={os.path.basename(f)}\n")

        try:
            if f.lower().endswith(('.mp4', '.mov', '.gif', '.webp')):
                reader = imageio.get_reader(f)
                n_frames = int(DURATION * FPS) if f.lower().endswith(('.gif', '.webp')) else None
                count = 0
                for frame in reader:
                    processed = get_processed_frame(Image.fromarray(frame))
                    process.stdin.write(processed.tobytes())
                    count += 1
                    total_frames += 1
                    if n_frames and count >= n_frames: break
                reader.close()
            else:
                with Image.open(f) as img:
                    num_frames = int(max(DURATION * FPS, 1))
                    frame_bytes_list = []
                    for idx in range(num_frames):
                        zoom = 1.0 + (0.02 * (idx / num_frames))
                        processed = get_processed_frame(img, zoom_factor=zoom)
                        process.stdin.write(processed.tobytes())
                        total_frames += 1
        except: pass
        pbar.update(1)

    pbar.close()
    process.stdin.close()
    process.wait()

    # 4. Inject Chapters
    final_out = out_path.replace(".mkv", "_final.mkv")
    subprocess.run(['ffmpeg', '-i', out_path, '-i', chapters_file, '-map_metadata', '1', '-codec', 'copy', final_out], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.replace(final_out, out_path)

    with open(os.getenv('GITHUB_OUTPUT'), 'a') as go: go.write(f"final_name={fname}\n")

if __name__ == "__main__":
    main()