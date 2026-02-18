import os
import re
import shutil
import subprocess
import requests
import numpy as np
from PIL import Image, ImageFilter, ImageEnhance

# --- MONKEYPATCH PILLOW 10 FOR MOVIEPY ---
if not hasattr(Image, 'ANTIALIAS'):
    Image.ANTIALIAS = Image.LANCZOS
# -----------------------------------------

from moviepy.editor import (
    VideoFileClip, ImageClip, CompositeVideoClip, 
    concatenate_videoclips
)

# --- LOAD CONFIG FROM GITHUB ENVIRONMENT ---
URL = os.getenv('FILE_URL')
FILENAME = os.getenv('FILENAME', 'output').strip()
DURATION = float(os.getenv('IMG_DURATION', '0.33'))
AR_TYPE = os.getenv('ASPECT_RATIO', 'Landscape (1920x1080)')
CRF = os.getenv('CRF', '32')
PRESET = os.getenv('PRESET', '10')

FPS = 30
# If duration is 0.33s (3 img/sec), disable transitions to avoid blur
TRANSITION = min(0.3, DURATION * 0.4) if DURATION > 0.5 else 0

# Resolution Mapping
RES_MAP = {
    'Landscape (1920x1080)': (1920, 1080),
    'Portrait (1080x1920)': (1080, 1920),
    'Square (1080x1080)': (1080, 1080)
}
CANVAS_W, CANVAS_H = RES_MAP.get(AR_TYPE, (1920, 1080))

def natural_sort_key(s):
    return [int(text) if text.isdigit() else text.lower() for text in re.split('([0-9]+)', s)]

def create_blurred_bg(pil_img):
    """Universal Strategy: Fit media inside, fill empty areas with blurred background."""
    img = pil_img.convert('RGB')
    scale = max(CANVAS_W / img.width, CANVAS_H / img.height)
    new_w, new_h = int(img.width * scale), int(img.height * scale)
    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    
    # Center Crop
    left = (new_w - CANVAS_W) // 2
    top = (new_h - CANVAS_H) // 2
    img = img.crop((left, top, left + CANVAS_W, top + CANVAS_H))
    
    # Blur and Darken
    img = img.filter(ImageFilter.GaussianBlur(radius=50))
    return np.array(ImageEnhance.Brightness(img).enhance(0.4))

def process_media(filepath):
    try:
        ext = os.path.splitext(filepath)[1].lower()
        is_video = ext in ['.mp4', '.mov', '.gif', '.webm', '.webp']
        
        if is_video:
            clip = VideoFileClip(filepath)
            if ext in ['.gif', '.webp']: clip = clip.without_audio()
            # Loop short animations to match requested duration
            if clip.duration < DURATION:
                clip = clip.loop(duration=DURATION)
            
            # Extract first frame for background
            temp_f = f"{filepath}_t.jpg"
            clip.save_frame(temp_f, t=0)
            with Image.open(temp_f) as thumb: bg_arr = create_blurred_bg(thumb)
            os.remove(temp_f)
        else:
            with Image.open(filepath) as img: bg_arr = create_blurred_bg(img)
            clip = ImageClip(filepath).set_duration(DURATION)

        bg_clip = ImageClip(bg_arr).set_duration(clip.duration)
        
        # FOREGROUND FIT (No Cropping)
        scale = min(CANVAS_W / clip.w, CANVAS_H / clip.h)
        fg_clip = clip.resize(scale).set_position("center")
        
        # Subtle Zoom for static images (only if slow duration)
        if not is_video and DURATION > 1.0:
            fg_clip = fg_clip.resize(lambda t: 1 + 0.03 * (t / DURATION))

        return CompositeVideoClip([bg_clip, fg_clip], size=(CANVAS_W, CANVAS_H)).set_fps(FPS)
    except Exception as e:
        print(f"Skipping {filepath}: {e}")
        return None

def main():
    extract_path = "workspace/extracted"
    os.makedirs(extract_path, exist_ok=True)
    os.makedirs("output", exist_ok=True)
    
    # 1. Download
    archive_p = "workspace/input_archive"
    print(f"Downloading: {URL}")
    headers = {'User-Agent': 'Mozilla/5.0'}
    with requests.get(URL, stream=True, headers=headers) as r:
        r.raise_for_status()
        with open(archive_p, 'wb') as f: shutil.copyfileobj(r.raw, f)

    # 2. Universal Extract using 7-Zip (Handles RAR, ZIP, CBZ)
    print("Extracting with 7-Zip...")
    try:
        subprocess.run(['7z', 'x', archive_p, f'-o{extract_path}', '-y'], check=True)
    except Exception as e:
        print(f"Extraction Error: {e}")
        return

    # 3. Collect files
    valid = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.mp4', '.mov'}
    all_files = []
    for r, d, fs in os.walk(extract_path):
        for f in fs:
            if os.path.splitext(f)[1].lower() in valid:
                all_files.append(os.path.join(r, f))
    all_files.sort(key=natural_sort_key)
    
    if not all_files:
        print("No valid media files found.")
        return

    # 4. Process into Clips
    clips = []
    print(f"Processing {len(all_files)} files...")
    for f in all_files:
        p = process_media(f)
        if p:
            if clips and TRANSITION > 0: p = p.crossfadein(TRANSITION)
            clips.append(p)

    # 5. Concatenate & Render to AV1
    if clips:
        final = concatenate_videoclips(clips, method="compose", padding=-TRANSITION if TRANSITION > 0 else 0)
        out_path = f"output/{FILENAME}.mp4"
        
        print(f"Encoding AV1 (CRF: {CRF}, Preset: {PRESET})...")
        final.write_videofile(
            out_path, 
            codec='libsvtav1', 
            audio_codec='aac',
            threads=os.cpu_count(),
            ffmpeg_params=[
                '-crf', str(CRF), 
                '-preset', str(PRESET), 
                '-pix_fmt', 'yuv420p10le'
            ]
        )
        print(f"Success! Saved: {out_path}")

if __name__ == "__main__":
    main()