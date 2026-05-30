#!/usr/bin/env python3
"""Generates EditOps.app/Contents/Resources/AppIcon.icns during setup."""
import math, os, shutil, subprocess, sys, tempfile

try:
    from PIL import Image, ImageDraw
except ImportError:
    print("⚠️  Pillow not available — skipping icon generation")
    sys.exit(0)

DEST = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "EditOps.app", "Contents", "Resources", "AppIcon.icns")

def make_frame(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Black rounded-rect background
    r = size // 5
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=r,
                            fill=(17, 17, 17, 255))

    # White four-pointed star (✦)
    cx, cy = size / 2, size / 2
    outer = size * 0.33
    inner = size * 0.10
    pts = []
    for i in range(8):
        angle = math.pi / 4 * i - math.pi / 2
        rad = outer if i % 2 == 0 else inner
        pts.append((cx + rad * math.cos(angle), cy + rad * math.sin(angle)))
    draw.polygon(pts, fill=(255, 255, 255, 255))

    return img

iconset = tempfile.mkdtemp(suffix=".iconset")
try:
    specs = [
        ("icon_16x16.png",       16),
        ("icon_16x16@2x.png",    32),
        ("icon_32x32.png",       32),
        ("icon_32x32@2x.png",    64),
        ("icon_128x128.png",    128),
        ("icon_128x128@2x.png", 256),
        ("icon_256x256.png",    256),
        ("icon_256x256@2x.png", 512),
        ("icon_512x512.png",    512),
        ("icon_512x512@2x.png",1024),
    ]
    for name, px in specs:
        make_frame(px).save(os.path.join(iconset, name))

    os.makedirs(os.path.dirname(DEST), exist_ok=True)
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", DEST], check=True)
    print(f"✅  App icon created")
finally:
    shutil.rmtree(iconset, ignore_errors=True)
