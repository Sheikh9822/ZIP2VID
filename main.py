import os, re, shutil, subprocess, requests, imageio, json
import numpy as np
from PIL import Image, ImageFilter, ImageEnhance
from tqdm import tqdm
from urllib.parse import urljoin, urlparse, unquote

# --- CONFIGURATION ---
URL = os.getenv('FILE_URL')
DURATION = float(os.getenv('IMG_DURATION', '0.33'))
FPS = 30
CRF = os.getenv('CRF', '32')
PRESET = os.getenv('PRESET', '8')
AR_TYPE = os.getenv('ASPECT_RATIO', 'Landscape (1920x1080)')

RES_MAP = {
    'Landscape (1920x1080)': (1920, 1080),
    'Portrait (1080x1920)': (1080, 1920),
    'Square (1080x1080)': (1080, 1080)
}
W, H = RES_MAP.get(AR_TYPE, (1920, 1080))
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}

# --- API & DOWNLOAD LOGIC ---

def get_api_urls(url):
    """Extracts API parameters from a Kemono/Coomer URL."""
    parsed = urlparse(url)
    domain = parsed.netloc # kemono.cr, coomer.su, etc.
    parts = parsed.path.strip('/').split('/')
    
    # Pattern: /service/user/ID/post/ID
    if len(parts) >= 5 and 'post' in parts:
        service = parts[0]
        user_id = parts[2]
        post_id = parts[4]
        return f"https://{domain}/api/v1/{service}/user/{user_id}/post/{post_id}", f"https://{domain}"
    return None, f"https://{domain}"

def download_file(url, folder, name=None):
    try:
        if not name:
            name = os.path.basename(urlparse(url).path)
        target = os.path.join(folder, name)
        with requests.get(url, headers=HEADERS, stream=True, timeout=30) as r:
            if r.status_code == 200:
                with open(target, 'wb') as f:
                    shutil.copyfileobj(r.raw, f)
                return target
    except: pass
    return None

# --- IMAGE PROCESSING ---

def get_processed_frame(pil_img, zoom_factor=1.0):
    bg = pil_img.convert('RGB')
    # Fast Blur BG
    small = bg.resize((160, int(160*(H/W))), Image.Resampling.NEAREST)
    blurred = small.filter(ImageFilter.GaussianBlur(radius=2))
    bg = blurred.resize((W, H), Image.Resampling.LANCZOS)
    bg = ImageEnhance.Brightness(bg).enhance(0.4)

    # Fit FG
    fg = pil_img.convert('RGB')
    scale = min(W / fg.width, H / fg.height)
    new_size = (int(fg.width * scale * zoom_factor), int(fg.height * scale * zoom_factor))
    fg = fg.resize(new_size, Image.Resampling.LANCZOS)
    
    bg.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2))
    return bg

# --- MAIN ---

def main():
    workspace = "workspace"
    extract_path = os.path.join(workspace, "extracted")
    os.makedirs(extract_path, exist_ok=True)
    os.makedirs("output", exist_ok=True)

    # 1. API EXTRACTION OR DIRECT DOWNLOAD
    api_url, base_server = get_api_urls(URL)
    
    # Check if URL is an archive or direct file first
    is_direct_file = any(URL.lower().split('?')[0].endswith(e) for e in ('.zip', '.rar', '.7z', '.cbz', '.bin'))

    if is_direct_file:
        print("Detected Direct File/Archive Link.")
        archive_p = os.path.join(workspace, "input_archive")
        download_file(URL, workspace, "input_archive")
        subprocess.run(['7z', 'x', archive_p, f'-o{extract_path}', '-y'], check=True, stdout=subprocess.DEVNULL)
    elif api_url:
        print(f"Detected Post Link. Calling API: {api_url}")
        resp = requests.get(api_url, headers=HEADERS).json()
        
        # Get Title for filename
        post_title = resp.get('title', 'output')
        os.environ['FINAL_FNAME'] = re.sub(r'[^a-zA-Z0-9_-]', '_', post_title)

        # Collect all files from API response
        # Kemono API provides 'attachments' and 'previews'
        files_to_get = []
        for att in resp.get('attachments', []):
            files_to_get.append(f"{att['server']}/data{att['path']}?f={att['name']}")
        
        # If no attachments, try embedded images (previews)
        if not files_to_get:
            for img in resp.get('previews', []):
                files_to_get.append(f"{img['server']}/data{img['path']}")

        print(f"Found {len(files_to_get)} files. Downloading...")
        for i, f_url in enumerate(tqdm(files_to_get)):
            download_file(f_url, extract_path, f"{i:04d}_{os.path.basename(urlparse(f_url).path)}")

    # 2. FILE COLLECTION
    valid_exts = ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.mp4', '.mov')
    all_files = sorted([os.path.join(dp, f) for dp, dn, fs in os.walk(extract_path) for f in fs if f.lower().endswith(valid_exts)], 
                       key=lambda s: [int(t) if t.isdigit() else t.lower() for t in re.split('([0-9]+)', s)])
    
    if not all_files:
        print("No media found to process."); return

    # Filename Handling
    user_fname = os.getenv('FILENAME', '').strip()
    fname = user_fname or os.getenv('FINAL_FNAME', 'output')
    out_path = f"output/{fname}.mkv"

    # 3. DIRECT ENCODE PIPE
    cmd = [
        'ffmpeg', '-y', '-f', 'rawvideo', '-vcodec', 'rawvideo', '-s', f'{W}x{H}', 
        '-pix_fmt', 'rgb24', '-r', str(FPS), '-i', '-', 
        '-c:v', 'libsvtav1', '-crf', CRF, '-preset', PRESET,
        '-svtav1-params', 'tune=0:enable-overlays=1:scd=1',
        '-pix_fmt', 'yuv420p10le', '-c:a', 'libopus', '-b:a', '128k', out_path
    ]
    
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    
    for f in tqdm(all_files, desc="Encoding Video"):
        try:
            if f.lower().endswith(('.mp4', '.mov', '.gif', '.webp')):
                reader = imageio.get_reader(f)
                max_f = int(DURATION * FPS) if f.lower().endswith(('.gif', '.webp')) else 9999
                for i, frame in enumerate(reader):
                    process.stdin.write(get_processed_frame(Image.fromarray(frame)).tobytes())
                    if i >= max_f: break
                reader.close()
            else:
                with Image.open(f) as img:
                    num_frames = int(max(DURATION * FPS, 1))
                    for idx in range(num_frames):
                        zoom = 1.0 + (0.02 * (idx / num_frames))
                        process.stdin.write(get_processed_frame(img, zoom_factor=zoom).tobytes())
        except: pass

    process.stdin.close()
    process.wait()

    with open(os.getenv('GITHUB_OUTPUT'), 'a') as go:
        go.write(f"final_name={fname}\n")

if __name__ == "__main__":
    main()