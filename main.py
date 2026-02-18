import os, re, shutil, subprocess, requests, zipfile
import numpy as np
from PIL import Image, ImageFilter, ImageEnhance
from moviepy.editor import VideoFileClip, ImageClip, CompositeVideoClip, concatenate_videoclips

# Monkeypatch for Pillow 10
if not hasattr(Image, 'ANTIALIAS'): Image.ANTIALIAS = Image.LANCZOS

# Configuration
URL = os.getenv('FILE_URL')
DURATION = float(os.getenv('IMG_DURATION', '0.33'))
AR_TYPE = os.getenv('ASPECT_RATIO', 'Landscape (1920x1080)')
RES_MAP = {'Landscape (1920x1080)': (1920, 1080), 'Portrait (1080x1920)': (1080, 1920), 'Square (1080x1080)': (1080, 1080)}
W, H = RES_MAP.get(AR_TYPE, (1920, 1080))

def create_bg(pil_img):
    img = pil_img.convert('RGB')
    scale = max(W / img.width, H / img.height)
    img = img.resize((int(img.width * scale), int(img.height * scale)), Image.Resampling.LANCZOS)
    img = img.crop(((img.width-W)//2, (img.height-H)//2, (img.width+W)//2, (img.height+H)//2))
    img = img.filter(ImageFilter.GaussianBlur(radius=40))
    return np.array(ImageEnhance.Brightness(img).enhance(0.4))

def process_media(path):
    try:
        ext = os.path.splitext(path)[1].lower()
        is_vid = ext in ['.mp4', '.mov', '.gif', '.webp']
        clip = VideoFileClip(path) if is_vid else ImageClip(path).set_duration(DURATION)
        if ext in ['.gif', '.webp']: clip = clip.without_audio()
        if is_vid and clip.duration < DURATION: clip = clip.loop(duration=DURATION)
        
        # BG Logic
        if is_vid:
            clip.save_frame("t.jpg", t=0)
            bg_arr = create_bg(Image.open("t.jpg"))
            os.remove("t.jpg")
        else:
            bg_arr = create_bg(Image.open(path))
        
        # FG Logic (FIT - NO CROP)
        scale = min(W / clip.w, H / clip.h)
        fg = clip.resize(scale).set_position("center")
        return CompositeVideoClip([ImageClip(bg_arr).set_duration(clip.duration), fg], size=(W, H)).set_fps(30)
    except: return None

def main():
    os.makedirs("workspace/extracted", exist_ok=True)
    os.makedirs("output", exist_ok=True)
    
    # Name Logic
    user_fname = os.getenv('FILENAME', '').strip()
    if user_fname: fname = user_fname
    else: fname = re.search(r'f=([^&]+)', URL).group(1).rsplit('.', 1)[0] if 'f=' in URL else "slideshow"
    fname = re.sub(r'[^a-zA-Z0-9_-]', '_', fname)
    
    # Download & Extract
    r = requests.get(URL, headers={'User-Agent': 'Mozilla/5.0'})
    with open("workspace/input", 'wb') as f: f.write(r.content)
    subprocess.run(['7z', 'x', 'workspace/input', '-oworkspace/extracted', '-y'])
    
    # Process
    files = sorted([os.path.join(dp, f) for dp, dn, filenames in os.walk("workspace/extracted") for f in filenames if os.path.splitext(f)[1].lower() in ['.jpg', '.jpeg', '.png', '.webp', '.gif', '.mp4']], key=lambda s: [int(t) if t.isdigit() else t.lower() for t in re.split('([0-9]+)', s)])
    clips = [process_media(f) for f in files]
    clips = [c for c in clips if c is not None]
    
    if clips:
        # Write high-quality H264 master
        final = concatenate_videoclips(clips, method="compose")
        final.write_videofile("workspace/master.mp4", codec="libx264", bitrate="20000k", fps=30)
        
        # Pass filename to GitHub Actions
        with open(os.getenv('GITHUB_OUTPUT'), 'a') as go:
            go.write(f"final_name={fname}\n")

if __name__ == "__main__": main()