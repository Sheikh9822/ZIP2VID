import os
import re
import shutil
import zipfile
import requests
import subprocess
import numpy as np
from PIL import Image, ImageFilter, ImageEnhance

# --- PILLOW 10 + MOVIEPY 1.0.3 COMPATIBILITY ---
if not hasattr(Image, 'ANTIALIAS'):
    Image.ANTIALIAS = Image.LANCZOS
# -----------------------------------------------

from moviepy.editor import (
    VideoFileClip, ImageClip, CompositeVideoClip, 
    concatenate_videoclips
)

# --- CONFIG ---
URL = os.getenv('FILE_URL')
FILENAME = os.getenv('FILENAME', 'video_output').strip()
DURATION = float(os.getenv('IMG_DURATION', '0.33'))
AR_TYPE = os.getenv('ASPECT_RATIO', 'Landscape (1920x1080)')
CRF = os.getenv('CRF', '32')
PRESET = os.getenv('PRESET', '10')

FPS = 30
TRANSITION = min(0.3, DURATION * 0.4) if DURATION > 0.4 else 0

RES_MAP = {
    'Landscape (1920x1080)': (1920, 1080),
    'Portrait (1080x1920)': (1080, 1920),
    'Square (1080x1080)': (1080, 1080)
}
CANVAS_W, CANVAS_H = RES_MAP.get(AR_TYPE, (1920, 1080))

def check_encoder(name):
    """Check if ffmpeg supports the given encoder."""
    try:
        cmd = ['ffmpeg', '-encoders']
        res = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode()
        return name in res
    except:
        return False

def create_blurred_bg(pil_img):
    img = pil_img.convert('RGB')
    scale = max(CANVAS_W / img.width, CANVAS_H / img.height)
    new_w, new_h = int(img.width * scale), int(img.height * scale)
    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left, top = (new_w - CANVAS_W) // 2, (new_h - CANVAS_H) // 2
    img = img.crop((left, top, left + CANVAS_W, top + CANVAS_H))
    img = img.filter(ImageFilter.GaussianBlur(radius=50))
    return np.array(ImageEnhance.Brightness(img).enhance(0.4))

def process_media(filepath):
    try:
        ext = os.path.splitext(filepath)[1].lower()
        is_video = ext in ['.mp4', '.mov', '.gif', '.webm']
        if is_video:
            clip = VideoFileClip(filepath)
            if ext == '.gif': clip = clip.without_audio()
            if clip.duration < DURATION: clip = clip.loop(duration=DURATION)
            temp_f = f"{filepath}_thumb.jpg"
            clip.save_frame(temp_f, t=0)
            with Image.open(temp_f) as thumb: bg_arr = create_blurred_bg(thumb)
            os.remove(temp_f)
        else:
            with Image.open(filepath) as img: bg_arr = create_blurred_bg(img)
            clip = ImageClip(filepath).set_duration(DURATION)
        bg_clip = ImageClip(bg_arr).set_duration(clip.duration)
        scale = min(CANVAS_W / clip.w, CANVAS_H / clip.h)
        fg_clip = clip.resize(scale).set_position("center")
        return CompositeVideoClip([bg_clip, fg_clip], size=(CANVAS_W, CANVAS_H)).set_fps(FPS)
    except Exception as e:
        print(f"Error: {e}")
        return None

def main():
    os.makedirs("workspace/extracted", exist_ok=True)
    os.makedirs("output", exist_ok=True)
    
    zip_p = "workspace/input.zip"
    print(f"Downloading: {URL}")
    headers = {'User-Agent': 'Mozilla/5.0'}
    with requests.get(URL, stream=True, headers=headers) as r:
        r.raise_for_status()
        with open(zip_p, 'wb') as f: shutil.copyfileobj(r.raw, f)

    with zipfile.ZipFile(zip_p, 'r') as z: z.extractall("workspace/extracted")

    valid = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.mp4', '.mov'}
    all_files = sorted([os.path.join(r, f) for r, _, fs in os.walk("workspace/extracted") for f in fs if os.path.splitext(f)[1].lower() in valid], key=lambda s: [int(t) if t.isdigit() else t.lower() for t in re.split('([0-9]+)', s)])
    
    clips = [process_media(f) for f in all_files if process_media(f)]
    if clips:
        final = concatenate_videoclips(clips, method="compose", padding=-TRANSITION if TRANSITION > 0 else 0)
        out_path = f"output/{FILENAME}.mp4"
        
        has_svt = check_encoder('libsvtav1')
        print(f"Encoder libsvtav1 available: {has_svt}")

        if has_svt:
            print("Encoding with AV1 (libsvtav1)...")
            final.write_videofile(out_path, codec='libsvtav1', audio_codec='aac', 
                                 ffmpeg_params=['-crf', str(CRF), '-preset', str(PRESET), '-pix_fmt', 'yuv420p10le'])
        else:
            print("AV1 encoder NOT FOUND. Using libx264 fallback...")
            final.write_videofile(out_path, codec='libx264', audio_codec='aac', preset='medium')
        
    print(f"Saved: {out_path}")

if __name__ == "__main__":
    main()