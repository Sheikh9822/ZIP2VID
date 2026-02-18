import os
import re
import shutil
import zipfile
import requests
import numpy as np
from PIL import Image, ImageFilter, ImageEnhance

# --- FIX FOR PILLOW 10+ AND MOVIEPY 1.0.3 ---
if not hasattr(Image, 'ANTIALIAS'):
    Image.ANTIALIAS = Image.LANCZOS
# --------------------------------------------

from moviepy.editor import (
    VideoFileClip, ImageClip, CompositeVideoClip, 
    concatenate_videoclips
)

# --- CONFIGURATION ---
URL = os.getenv('FILE_URL')
FILENAME = os.getenv('FILENAME', '').strip()
if not FILENAME:
    # Fallback: Extract name from URL parameter 'f' or path
    try:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(URL)
        qs = parse_qs(parsed.query)
        if 'f' in qs:
            FILENAME = os.path.splitext(qs['f'][0])[0]
        else:
            FILENAME = os.path.basename(parsed.path).split(".")[0]
    except:
        FILENAME = "output"

# Timing logic
try:
    raw_val = float(os.getenv('IMG_PER_SEC', '2.0'))
    DURATION = raw_val if raw_val >= 0.1 else 2.0
except:
    DURATION = 2.0

AR_TYPE = os.getenv('ASPECT_RATIO', 'Landscape (1920x1080)')
CRF = os.getenv('CRF', '32')
PRESET = os.getenv('PRESET', '10')

# Standardize FPS and Transitions
FPS = 30
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
    img = pil_img.convert('RGB')
    img_w, img_h = img.size
    img_aspect = img_w / img_h
    canvas_aspect = CANVAS_W / CANVAS_H

    if img_aspect > canvas_aspect:
        new_h = CANVAS_H
        new_w = int(CANVAS_H * img_aspect)
    else:
        new_w = CANVAS_W
        new_h = int(CANVAS_W / img_aspect)

    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = (new_w - CANVAS_W) // 2
    top = (new_h - CANVAS_H) // 2
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
            fg_clip = fg_clip.resize(lambda t: 1 + 0.04 * (t / DURATION))

        return CompositeVideoClip([bg_clip, fg_clip], size=(CANVAS_W, CANVAS_H)).set_fps(FPS)
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
        headers = {'User-Agent': 'Mozilla/5.0'}
        with requests.get(URL, stream=True, timeout=60, headers=headers) as r:
            r.raise_for_status()
            with open(zip_p, 'wb') as f: shutil.copyfileobj(r.raw, f)
    except Exception as e:
        print(f"Download failed: {e}")
        return

    print("Extracting files...")
    try:
        with zipfile.ZipFile(zip_p, 'r') as z: z.extractall(extract_path)
    except Exception as e:
        print(f"Extraction failed: {e}")
        return

    valid_exts = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.mp4'}
    all_files = sorted([os.path.join(r, f) for r, _, fs in os.walk(extract_path) for f in fs if os.path.splitext(f)[1].lower() in valid_exts], key=natural_sort_key)
    
    if not all_files:
        print("No valid media files found.")
        return

    print(f"Processing {len(all_files)} files...")
    clips = []
    for f in all_files:
        processed = process_media(f)
        if processed:
            if clips and TRANSITION > 0: processed = processed.crossfadein(TRANSITION)
            clips.append(processed)

    if clips:
        print("Merging clips...")
        final = concatenate_videoclips(clips, method="compose", padding=-TRANSITION if TRANSITION > 0 else 0)
        out_path = f"output/{FILENAME}.mp4"
        
        # Optimized FFmpeg parameters for SVT-AV1
        # Removed '-svtav1-params' to fix "Unrecognized option" error
        ffmpeg_params = [
            '-crf', str(CRF),
            '-preset', str(PRESET),
            '-pix_fmt', 'yuv420p10le'
        ]
        
        print(f"Rendering Video: {out_path}")
        try:
            final.write_videofile(
                out_path, 
                codec='libsvtav1', 
                audio_codec='aac',
                threads=os.cpu_count(),
                ffmpeg_params=ffmpeg_params
            )
        except Exception as e:
            print(f"AV1 Encoding failed: {e}")
            print("FALLING BACK TO H.264 (Standard MP4)...")
            final.write_videofile(
                out_path, 
                codec='libx264', 
                audio_codec='aac',
                threads=os.cpu_count(),
                preset='medium',
                bitrate='5000k'
            )
        print(f"Done! Saved to {out_path}")

if __name__ == "__main__":
    main()