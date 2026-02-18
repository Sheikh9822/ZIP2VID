import os
import re
import shutil
import zipfile
import requests
import numpy as np
from PIL import Image, ImageFilter, ImageEnhance

# --- FIX FOR PILLOW 10+ AND MOVIEPY 1.0.3 ---
# This must happen BEFORE moviepy imports
if not hasattr(Image, 'ANTIALIAS'):
    Image.ANTIALIAS = Image.LANCZOS
# --------------------------------------------

from moviepy.editor import (
    VideoFileClip, ImageClip, CompositeVideoClip, 
    concatenate_videoclips
)

# --- CONFIGURATION ---
URL = os.getenv('FILE_URL')
# Use ENV filename, if empty try to guess from URL
FILENAME = os.getenv('FILENAME', '').strip()
if not FILENAME:
    # Fallback if the GitHub Action meta step failed to pass a name
    FILENAME = os.path.basename(URL.split("?")[0]).split(".")[0] or "output"

# Timing: Use the value from ENV. Logs show ".5" which means 1 image every 2 seconds.
try:
    # If the user provides 0.5, we treat it as seconds per image. 
    # If they provide 3, we treat it as 3 images per second.
    raw_val = float(os.getenv('IMG_PER_SEC', '2.0'))
    if raw_val >= 1.0:
        DURATION = raw_val
    else:
        # If value is less than 1 (like .5), assume they mean 1/val (e.g. 1/0.5 = 2 seconds)
        DURATION = 1.0 / raw_val
except:
    DURATION = 2.0

AR_TYPE = os.getenv('ASPECT_RATIO', 'Landscape (1920x1080)')
CRF = os.getenv('CRF', '32')
PRESET = os.getenv('PRESET', '10')

TRANSITION = min(0.4, DURATION * 0.3) if DURATION > 0.5 else 0

RES_MAP = {
    'Landscape (1920x1080)': (1920, 1080),
    'Portrait (1080x1920)': (1080, 1920),
    'Square (1080x1080)': (1080, 1080)
}
CANVAS_W, CANVAS_H = RES_MAP.get(AR_TYPE, (1920, 1080))

def natural_sort_key(s):
    return [int(text) if text.isdigit() else text.lower() for text in re.split('([0-9]+)', s)]

def create_blurred_bg(pil_img):
    """Creates a darkened, blurred background that fills the canvas."""
    img = pil_img.convert('RGB')
    img_w, img_h = img.size
    
    # Aspect Fill logic
    img_aspect = img_w / img_h
    canvas_aspect = CANVAS_W / CANVAS_H

    if img_aspect > canvas_aspect:
        new_h = CANVAS_H
        new_w = int(CANVAS_H * img_aspect)
    else:
        new_w = CANVAS_W
        new_h = int(CANVAS_W / img_aspect)

    # Use Resampling.LANCZOS for Pillow 10 compatibility
    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    
    # Center Crop
    left = (new_w - CANVAS_W) // 2
    top = (new_h - CANVAS_H) // 2
    img = img.crop((left, top, left + CANVAS_W, top + CANVAS_H))
    
    # Blur and Darken
    img = img.filter(ImageFilter.GaussianBlur(radius=50))
    enhancer = ImageEnhance.Brightness(img)
    return np.array(enhancer.enhance(0.4))

def process_media(filepath):
    try:
        ext = os.path.splitext(filepath)[1].lower()
        is_video = ext in ['.mp4', '.mov', '.gif', '.webm']
        
        if is_video:
            clip = VideoFileClip(filepath)
            if ext == '.gif': 
                clip = clip.without_audio()
            if clip.duration < DURATION:
                clip = clip.loop(duration=DURATION)
            
            # Extract first frame for background
            temp_f = f"{filepath}_thumb.jpg"
            clip.save_frame(temp_f, t=0)
            with Image.open(temp_f) as thumb:
                bg_arr = create_blurred_bg(thumb)
            os.remove(temp_f)
        else:
            with Image.open(filepath) as img:
                bg_arr = create_blurred_bg(img)
            clip = ImageClip(filepath).set_duration(DURATION)

        bg_clip = ImageClip(bg_arr).set_duration(clip.duration)

        # Foreground "Fit" logic (Universal Strategy)
        scale = min(CANVAS_W / clip.w, CANVAS_H / clip.h)
        # Using MoviePy's resize which we fixed with the monkeypatch above
        fg_clip = clip.resize(scale).set_position("center")
        
        # Ken Burns effect for static images
        if not is_video and DURATION > 1.0:
            fg_clip = fg_clip.resize(lambda t: 1 + 0.04 * (t / DURATION))

        return CompositeVideoClip([bg_clip, fg_clip], size=(CANVAS_W, CANVAS_H)).set_fps(30)
    except Exception as e:
        print(f"Error processing {filepath}: {e}")
        return None

def main():
    extract_path = "workspace/extracted"
    os.makedirs(extract_path, exist_ok=True)
    os.makedirs("output", exist_ok=True)
    
    print(f"Downloading: {URL}")
    zip_p = "workspace/input.zip"
    try:
        # Set a user-agent to avoid being blocked by some servers (like Kemono)
        headers = {'User-Agent': 'Mozilla/5.0'}
        with requests.get(URL, stream=True, timeout=60, headers=headers) as r:
            r.raise_for_status()
            with open(zip_p, 'wb') as f:
                shutil.copyfileobj(r.raw, f)
    except Exception as e:
        print(f"Download failed: {e}")
        return

    print("Extracting files...")
    try:
        with zipfile.ZipFile(zip_p, 'r') as z:
            z.extractall(extract_path)
    except Exception as e:
        print(f"Extraction failed (might not be a valid ZIP): {e}")
        return

    valid_exts = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.mp4'}
    all_files = []
    for root, _, fs in os.walk(extract_path):
        for f in fs:
            if os.path.splitext(f)[1].lower() in valid_exts:
                all_files.append(os.path.join(root, f))
    
    all_files.sort(key=natural_sort_key)
    
    if not all_files:
        print("No valid media files found.")
        return

    print(f"Processing {len(all_files)} files...")
    clips = []
    for f in all_files:
        processed = process_media(f)
        if processed:
            if clips and TRANSITION > 0:
                processed = processed.crossfadein(TRANSITION)
            clips.append(processed)

    if clips:
        print("Concatenating and rendering (AV1)...")
        final = concatenate_videoclips(clips, method="compose", padding=-TRANSITION if TRANSITION > 0 else 0)
        out_path = f"output/{FILENAME}.mp4"
        
        ffmpeg_params = [
            '-crf', str(CRF),
            '-preset', str(PRESET),
            '-pix_fmt', 'yuv420p10le',
            '-svtav1-params', 'tune=0:enable-overlays=1'
        ]
        
        final.write_videofile(
            out_path, 
            codec='libsvtav1', 
            audio_codec='aac',
            threads=os.cpu_count(),
            ffmpeg_params=ffmpeg_params
        )
        print(f"Done! Saved to {out_path}")

if __name__ == "__main__":
    main()