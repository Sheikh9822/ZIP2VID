import os
import re
import shutil
import zipfile
import requests
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

# --- CONFIGURATION ---
URL = os.getenv('FILE_URL')
# Use ENV filename, if empty use a hard default to prevent crashes
FILENAME = os.getenv('FILENAME', 'video_output').strip()
if not FILENAME: FILENAME = "video_output"

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

def natural_sort_key(s):
    return [int(text) if text.isdigit() else text.lower() for text in re.split('([0-9]+)', s)]

def create_blurred_bg(pil_img):
    img = pil_img.convert('RGB')
    img_aspect = img.width / img.height
    canvas_aspect = CANVAS_W / CANVAS_H
    if img_aspect > canvas_aspect:
        new_h = CANVAS_H
        new_w = int(CANVAS_H * img_aspect)
    else:
        new_w = CANVAS_W
        new_h = int(CANVAS_W / img_aspect)
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
        if not is_video and DURATION > 1.0:
            fg_clip = fg_clip.resize(lambda t: 1 + 0.03 * (t / DURATION))
        return CompositeVideoClip([bg_clip, fg_clip], size=(CANVAS_W, CANVAS_H)).set_fps(FPS)
    except Exception as e:
        print(f"Skipping {filepath}: {e}")
        return None

def main():
    workspace = "workspace"
    extract_path = os.path.join(workspace, "extracted")
    os.makedirs(extract_path, exist_ok=True)
    os.makedirs("output", exist_ok=True)
    
    zip_p = os.path.join(workspace, "input.zip")
    print(f"Downloading: {URL}")
    headers = {'User-Agent': 'Mozilla/5.0'}
    with requests.get(URL, stream=True, headers=headers) as r:
        r.raise_for_status()
        with open(zip_p, 'wb') as f: shutil.copyfileobj(r.raw, f)

    print("Extracting...")
    with zipfile.ZipFile(zip_p, 'r') as z: z.extractall(extract_path)

    valid = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.mp4', '.mov'}
    all_files = sorted([os.path.join(r, f) for r, _, fs in os.walk(extract_path) for f in fs if os.path.splitext(f)[1].lower() in valid], key=natural_sort_key)
    
    if not all_files:
        print("No media found.")
        return

    clips = []
    print(f"Processing {len(all_files)} files...")
    for f in all_files:
        processed = process_media(f)
        if processed:
            if clips and TRANSITION > 0: processed = processed.crossfadein(TRANSITION)
            clips.append(processed)

    if clips:
        final = concatenate_videoclips(clips, method="compose", padding=-TRANSITION if TRANSITION > 0 else 0)
        out_path = f"output/{FILENAME}.mp4"
        
        try:
            print("Attempting libsvtav1...")
            final.write_videofile(out_path, codec='libsvtav1', audio_codec='aac', threads=os.cpu_count(),
                                 ffmpeg_params=['-crf', str(CRF), '-preset', str(PRESET), '-pix_fmt', 'yuv420p10le'])
        except:
            print("Falling back to H.264...")
            final.write_videofile(out_path, codec='libx264', audio_codec='aac', preset='medium')
        
    print(f"Finished: {out_path}")

if __name__ == "__main__":
    main()