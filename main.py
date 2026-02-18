import os
import sys
import shutil
import zipfile
import requests
import numpy as np
from PIL import Image, ImageFilter, ImageEnhance
from moviepy.editor import (
    VideoFileClip, ImageClip, CompositeVideoClip, 
    concatenate_videoclips
)

# --- LOAD CONFIG FROM ENV ---
URL = os.getenv('FILE_URL')
FILENAME = os.getenv('FILENAME', 'output')
IPS = float(os.getenv('IMG_PER_SEC', '0.5'))
AR_TYPE = os.getenv('ASPECT_RATIO', 'Landscape')
CRF = os.getenv('CRF', '32')
PRESET = os.getenv('PRESET', '10')

# Timing logic: 3 img/sec -> 0.33s per img. 0.5 img/sec -> 2.0s per img.
DURATION = 1.0 / IPS
TRANSITION = min(0.4, DURATION * 0.3) if DURATION > 0.5 else 0

# Resolution Mapping
RES_MAP = {
    'Landscape (1920x1080)': (1920, 1080),
    'Portrait (1080x1920)': (1080, 1920),
    'Square (1080x1080)': (1080, 1080)
}
CANVAS_W, CANVAS_H = RES_MAP.get(AR_TYPE, (1920, 1080))

def create_blurred_bg(pil_img):
    img = pil_img.copy().convert('RGB')
    # Aspect Fill
    scale = max(CANVAS_W / img.width, CANVAS_H / img.height)
    new_w, new_h = int(img.width * scale), int(img.height * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    # Center Crop & Blur
    left, top = (new_w - CANVAS_W)//2, (new_h - CANVAS_H)//2
    img = img.crop((left, top, left + CANVAS_W, top + CANVAS_H))
    img = img.filter(ImageFilter.GaussianBlur(radius=45))
    return np.array(ImageEnhance.Brightness(img).enhance(0.4))

def process_media(filepath):
    try:
        ext = os.path.splitext(filepath)[1].lower()
        is_video = ext in ['.mp4', '.mov', '.gif', '.webm']
        
        clip = VideoFileClip(filepath) if is_video else ImageClip(filepath).set_duration(DURATION)
        if ext == '.gif': clip = clip.without_audio()
        if is_video and clip.duration < DURATION: clip = clip.loop(duration=DURATION)

        # Background logic
        if is_video:
            temp_f = f"{filepath}_thumb.jpg"
            clip.save_frame(temp_f, t=0)
            bg_arr = create_blurred_bg(Image.open(temp_f))
            os.remove(temp_f)
        else:
            bg_arr = create_blurred_bg(Image.open(filepath))
            
        bg_clip = ImageClip(bg_arr).set_duration(clip.duration)

        # Foreground "Fit" logic (No Crop)
        scale = min(CANVAS_W / clip.w, CANVAS_H / clip.h)
        fg_clip = clip.resize(scale).set_position("center")
        
        # Subtle animation for static images
        if not is_video and DURATION > 1.0:
            fg_clip = fg_clip.resize(lambda t: 1 + 0.02 * (t/DURATION))

        return CompositeVideoClip([bg_clip, fg_clip], size=(CANVAS_W, CANVAS_H)).set_fps(30)
    except Exception as e:
        print(f"Skipping {filepath}: {e}")
        return None

def main():
    os.makedirs("workspace/extracted", exist_ok=True)
    os.makedirs("output", exist_ok=True)
    
    # Download and Extract
    zip_p = "workspace/input.zip"
    with requests.get(URL, stream=True) as r:
        with open(zip_p, 'wb') as f: shutil.copyfileobj(r.raw, f)
    with zipfile.ZipFile(zip_p, 'r') as z: z.extractall("workspace/extracted")

    # Collect and Sort
    valid = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.mp4'}
    files = sorted([os.path.join(r, f) for r, _, fs in os.walk("workspace/extracted") for f in fs if os.path.splitext(f)[1].lower() in valid])
    
    # Process
    clips = []
    for f in files:
        c = process_media(f)
        if c:
            if clips and TRANSITION > 0: c = c.crossfadein(TRANSITION)
            clips.append(c)

    if clips:
        final = concatenate_videoclips(clips, method="compose", padding=-TRANSITION if TRANSITION > 0 else 0)
        out_path = f"output/{FILENAME}.mp4"
        final.write_videofile(out_path, codec='libsvtav1', audio_codec='aac', 
                             ffmpeg_params=['-crf', CRF, '-preset', PRESET, '-pix_fmt', 'yuv420p10le'])

if __name__ == "__main__":
    main()