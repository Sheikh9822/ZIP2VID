import os, re, shutil, subprocess, requests, asyncio, imageio
import numpy as np
from PIL import Image, ImageFilter, ImageEnhance
from tqdm import tqdm
from playwright.async_api import async_playwright
from urllib.parse import urljoin, urlparse

if not hasattr(Image, 'ANTIALIAS'): Image.ANTIALIAS = Image.LANCZOS

# --- CONFIG ---
URL = os.getenv('FILE_URL')
DURATION = float(os.getenv('IMG_DURATION', '0.33'))
FPS = 30
CRF = os.getenv('CRF', '32')
PRESET = os.getenv('PRESET', '12')
AR_TYPE = os.getenv('ASPECT_RATIO', 'Landscape (1920x1080)')
FILENAME = os.getenv('FILENAME', 'av1_slideshow')

RES_MAP = {
    'Landscape (1920x1080)': (1920, 1080),
    'Portrait (1080x1920)': (1080, 1920),
    'Square (1080x1080)': (1080, 1080)
}
W, H = RES_MAP.get(AR_TYPE, (1920, 1080))

async def scrape_media(target_url, folder):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        await page.goto(target_url, wait_until="networkidle")
        urls = await page.evaluate("""() => {
            const list = [];
            document.querySelectorAll('a.image-link').forEach(a => list.push(a.href));
            document.querySelectorAll('.post__thumbnail img, .post__image img').forEach(img => list.push(img.src || img.dataset.src));
            document.querySelectorAll('a.post__attachment-link').forEach(a => list.push(a.href));
            return list.filter(u => u);
        }""")
        await browser.close()
        urls = list(dict.fromkeys(urls))
        for i, u in enumerate(urls):
            try:
                full_u = urljoin(target_url, u)
                ext = os.path.splitext(urlparse(full_u).path)[1] or ".jpg"
                with requests.get(full_u, stream=True, timeout=15) as r:
                    if r.status_code == 200:
                        with open(os.path.join(folder, f"{i:04d}{ext}"), 'wb') as f:
                            shutil.copyfileobj(r.raw, f)
            except: pass

def prepare_background(pil_img):
    bg = pil_img.convert('RGB')
    s = max(W/bg.width, H/bg.height)
    bg = bg.resize((int(bg.width*s), int(bg.height*s)), Image.Resampling.LANCZOS)
    bg = bg.crop(((bg.width-W)//2, (bg.height-H)//2, (bg.width+W)//2, (bg.height+H)//2))
    small = bg.resize((80, 45), Image.Resampling.NEAREST)
    blurred = small.filter(ImageFilter.GaussianBlur(radius=1))
    bg = blurred.resize((W, H), Image.Resampling.LANCZOS)
    return ImageEnhance.Brightness(bg).enhance(0.4)

def composite_frame(bg_cached, pil_img, zoom=1.0):
    canvas = bg_cached.copy()
    fg = pil_img.convert('RGB')
    fs = min(W/fg.width, H/fg.height)
    fg = fg.resize((int(fg.width*fs*zoom), int(fg.height*fs*zoom)), Image.Resampling.LANCZOS)
    canvas.paste(fg, ((W-fg.width)//2, (H-fg.height)//2))
    return canvas

async def main():
    os.makedirs("workspace/extracted", exist_ok=True)
    os.makedirs("output", exist_ok=True)
    
    is_archive = any(URL.lower().split('?')[0].endswith(e) for e in ('.zip', '.rar', '.7z', '.cbz'))
    if is_archive:
        with requests.get(URL, stream=True) as r:
            with open("workspace/input", 'wb') as f: shutil.copyfileobj(r.raw, f)
        subprocess.run(['7z', 'x', 'workspace/input', '-oworkspace/extracted', '-y'], check=True)
    else:
        await scrape_media(URL, "workspace/extracted")

    files = sorted([os.path.join(dp, f) for dp, dn, fs in os.walk("workspace/extracted") for f in fs if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif', '.mp4'))],
                    key=lambda s: [int(t) if t.isdigit() else t.lower() for t in re.split('([0-9]+)', s)])

    if not files: return
    out_path = f"output/{FILENAME}.mkv"

    # --- UPDATED FFMPEG PIPE FOR COLOR ACCURACY ---
    cmd = [
        'ffmpeg', '-y',
        '-f', 'rawvideo', '-vcodec', 'rawvideo', 
        '-s', f'{W}x{H}', '-pix_fmt', 'rgb24', '-r', str(FPS), 
        '-i', '-', 
        # Explicitly convert RGB to YUV using BT.709 Matrix (HD Standard)
        '-vf', 'scale=out_color_matrix=bt709:out_range=pc,format=yuv420p10le',
        '-c:v', 'libsvtav1', 
        '-crf', CRF, 
        '-preset', PRESET,
        # Signal BT.709 metadata to the player
        '-svtav1-params', 'tune=0:enable-overlays=1:tile-columns=1:color-primaries=1:transfer-characteristics=1:matrix-coefficients=1:color-range=1',
        '-c:a', 'libopus', '-b:a', '128k', 
        out_path
    ]
    
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    for f in tqdm(files, desc="Encoding"):
        try:
            if f.lower().endswith(('.mp4', '.gif', '.webp')):
                reader = imageio.get_reader(f)
                limit = int(DURATION * FPS) if not f.lower().endswith('.mp4') else 9999
                bg_cached = None
                for i, frame in enumerate(reader):
                    pil_frame = Image.fromarray(frame)
                    if bg_cached is None: bg_cached = prepare_background(pil_frame)
                    final_frame = composite_frame(bg_cached, pil_frame)
                    process.stdin.write(final_frame.tobytes())
                    if i >= limit: break
                reader.close()
            else:
                with Image.open(f) as img:
                    bg_cached = prepare_background(img)
                    num_frames = int(DURATION * FPS)
                    for i in range(num_frames):
                        z = 1.0 + (0.02 * (i/num_frames)) if DURATION > 0.5 else 1.0
                        final_frame = composite_frame(bg_cached, img, zoom=z)
                        process.stdin.write(final_frame.tobytes())
        except Exception as e:
            pass

    process.stdin.close()
    process.wait()

if __name__ == "__main__":
    asyncio.run(main())