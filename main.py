import os, re, shutil, subprocess, requests, asyncio, imageio
import numpy as np
from PIL import Image, ImageFilter, ImageEnhance
from tqdm import tqdm
from playwright.async_api import async_playwright
from urllib.parse import urljoin, urlparse

# --- MONKEYPATCH PILLOW ---
if not hasattr(Image, 'ANTIALIAS'): Image.ANTIALIAS = Image.LANCZOS

# --- CONFIG ---
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

# --- BROWSER SCRAPER ---

async def scrape_media_with_browser(target_url, folder):
    print(f"[INFO] Starting browser to scrape: {target_url}")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        page = await context.new_page()
        
        await page.goto(target_url, wait_until="networkidle")
        
        # Extract logic for Kemono thumbnails and attachments
        media_urls = await page.evaluate("""() => {
            const urls = [];
            // Get high-res images from links
            document.querySelectorAll('a.image-link').forEach(a => urls.push(a.href));
            // Get embedded images if links missing
            document.querySelectorAll('.post__thumbnail img, .post__image img').forEach(img => urls.push(img.src || img.dataset.src));
            // Get video/file attachments
            document.querySelectorAll('a.post__attachment-link').forEach(a => urls.push(a.href));
            return urls.filter(u => u);
        }""")
        
        await browser.close()
        
        # Deduplicate and download
        media_urls = list(dict.fromkeys(media_urls))
        print(f"[INFO] Found {len(media_urls)} items. Downloading...")
        
        for i, m_url in enumerate(tqdm(media_urls)):
            try:
                full_url = urljoin(target_url, m_url)
                ext = os.path.splitext(urlparse(full_url).path)[1] or ".jpg"
                target_file = os.path.join(folder, f"{i:04d}{ext}")
                
                resp = requests.get(full_url, timeout=20, stream=True)
                if resp.status_code == 200:
                    with open(target_file, 'wb') as f:
                        shutil.copyfileobj(resp.raw, f)
            except: pass

# --- IMAGE PROCESSING ---

def get_processed_frame(pil_img, zoom_factor=1.0):
    """Blurred BG + Fit FG with micro-zoom."""
    bg = pil_img.convert('RGB')
    scale_fill = max(W / bg.width, H / bg.height)
    bg = bg.resize((int(bg.width * scale_fill), int(bg.height * scale_fill)), Image.Resampling.LANCZOS)
    bg = bg.crop(((bg.width - W)//2, (bg.height - H)//2, (bg.width + W)//2, (bg.height + H)//2))
    
    # Fast Blur
    small = bg.resize((160, 90), Image.Resampling.NEAREST)
    blurred = small.filter(ImageFilter.GaussianBlur(radius=2))
    bg = blurred.resize((W, H), Image.Resampling.LANCZOS)
    bg = ImageEnhance.Brightness(bg).enhance(0.4)

    # Foreground Fit
    fg = pil_img.convert('RGB')
    base_scale = min(W / fg.width, H / fg.height)
    new_size = (int(fg.width * base_scale * zoom_factor), int(fg.height * base_scale * zoom_factor))
    fg = fg.resize(new_size, Image.Resampling.LANCZOS)
    
    bg.paste(fg, ((W - fg.width) // 2, (H - fg.height) // 2))
    return bg

# --- MAIN ---

async def run_pipeline():
    extract_path = "workspace/extracted"
    os.makedirs(extract_path, exist_ok=True)
    os.makedirs("output", exist_ok=True)
    
    # 1. Input Detection
    is_archive = any(URL.lower().split('?')[0].endswith(e) for e in ('.zip', '.rar', '.7z', '.cbz', '.cbr'))
    
    if is_archive:
        print("[INFO] Archive input detected.")
        archive_p = "workspace/input_file"
        with requests.get(URL, stream=True) as r:
            with open(archive_p, 'wb') as f: shutil.copyfileobj(r.raw, f)
        subprocess.run(['7z', 'x', archive_p, f'-o{extract_path}', '-y'], check=True, stdout=subprocess.DEVNULL)
    else:
        await scrape_media_with_browser(URL, extract_path)

    # 2. File Selection
    valid_exts = ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.mp4', '.mov')
    files = sorted([os.path.join(dp, f) for dp, dn, fs in os.walk(extract_path) for f in fs if f.lower().endswith(valid_exts)],
                    key=lambda s: [int(t) if t.isdigit() else t.lower() for t in re.split('([0-9]+)', s)])

    if not files:
        print("[ERROR] No media files found."); return

    # Filename Logic
    match = re.search(r'f=([^&]+)', URL)
    raw_name = os.getenv('FILENAME', '').strip() or (match.group(1).rsplit('.', 1)[0] if match else "output")
    fname = re.sub(r'[^a-zA-Z0-9_-]', '_', raw_name)
    out_path = f"output/{fname}.mkv"

    # 3. Direct FFmpeg Pipe
    cmd = [
        'ffmpeg', '-y', '-f', 'rawvideo', '-vcodec', 'rawvideo', '-s', f'{W}x{H}', 
        '-pix_fmt', 'rgb24', '-r', str(FPS), '-i', '-', 
        '-c:v', 'libsvtav1', '-crf', CRF, '-preset', PRESET,
        '-svtav1-params', 'tune=0:enable-overlays=1:scd=1',
        '-pix_fmt', 'yuv420p10le', '-c:a', 'libopus', '-b:a', '128k', out_path
    ]
    
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    total_frames = 0
    
    print(f"[INFO] Encoding {len(files)} items into {AR_TYPE} AV1...")
    
    for f in tqdm(files):
        try:
            if f.lower().endswith(('.mp4', '.mov', '.gif', '.webp')):
                reader = imageio.get_reader(f)
                # Apply specified duration to animations
                n_limit = int(DURATION * FPS) if f.lower().endswith(('.gif', '.webp')) else 99999
                for i, frame in enumerate(reader):
                    process.stdin.write(get_processed_frame(Image.fromarray(frame)).tobytes())
                    if i >= n_limit: break
                reader.close()
            else:
                with Image.open(f) as img:
                    num_frames = int(max(DURATION * FPS, 1))
                    for idx in range(num_frames):
                        zoom = 1.0 + (0.02 * (idx / num_frames)) # Subtle Zoom
                        process.stdin.write(get_processed_frame(img, zoom_factor=zoom).tobytes())
        except: pass

    process.stdin.close()
    process.wait()
    print(f"[SUCCESS] Video saved: {out_path}")

if __name__ == "__main__":
    asyncio.run(run_pipeline())