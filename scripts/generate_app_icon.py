import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SIZE = 1024
CENTER = SIZE // 2
output_dir = Path(__file__).resolve().parents[1] / 'build'
output_dir.mkdir(exist_ok=True)

base = Image.new('RGBA', (SIZE, SIZE), (0, 0, 0, 0))
draw = ImageDraw.Draw(base)

# Dark rounded square background.
draw.rounded_rectangle((64, 64, SIZE - 64, SIZE - 64), radius=220, fill=(11, 19, 32, 255))

# Outer ring and chart face.
draw.ellipse((116, 116, SIZE - 116, SIZE - 116), outline=(72, 201, 170, 230), width=18)
draw.ellipse((170, 170, SIZE - 170, SIZE - 170), outline=(30, 42, 70, 255), width=2)

# Subtle glow behind the chart line.
for radius in range(300, 32, -20):
    alpha = max(6, 40 - (300 - radius) // 8)
    glow = Image.new('RGBA', (SIZE, SIZE), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse((CENTER - radius, CENTER - radius, CENTER + radius, CENTER + radius), outline=(90, 208, 154, alpha), width=12)
    base = Image.alpha_composite(base, glow)

# Candlestick bars representing an uptrend and execution activity.
bar_positions = [
    (270, 610, 332, 730),
    (360, 555, 422, 730),
    (450, 490, 512, 730),
    (540, 430, 602, 730),
    (630, 365, 692, 730),
]
for left, top, right, bottom in bar_positions:
    draw.rounded_rectangle((left, top, right, bottom), radius=18, fill=(16, 185, 129, 255))
    draw.rounded_rectangle((left, top, right, bottom), radius=18, outline=(140, 253, 208, 200), width=4)

# Trend line with upward momentum.
line_points = [(238, 700), (332, 625), (426, 575), (520, 510), (614, 455), (708, 390), (802, 330)]
for i in range(len(line_points) - 1):
    x1, y1 = line_points[i]
    x2, y2 = line_points[i + 1]
    draw.line((x1, y1, x2, y2), fill=(125, 211, 252, 255), width=26)

# Arrow head showing bullish break-out momentum.
arrow_points = [(770, 288), (802, 330), (760, 355)]
draw.polygon(arrow_points, fill=(167, 243, 208, 255))

# Data markers along the trend line.
for x, y in line_points:
    draw.ellipse((x - 18, y - 18, x + 18, y + 18), fill=(125, 211, 252, 255))

# Small accent star for premium AI execution.
star_points = [(512, 260), (548, 344), (476, 344)]
draw.polygon(star_points, fill=(251, 191, 36, 255))

# Slight blur for polish.
final = base.filter(ImageFilter.GaussianBlur(1.2))
final = final.crop((0, 0, SIZE, SIZE))

# Save the generated assets.
final.save(output_dir / 'icon.png')
final.save(output_dir / 'icon.ico')

# Create a macOS iconset and .icns bundle.
iconset_dir = output_dir / 'icon.iconset'
if iconset_dir.exists():
    shutil.rmtree(iconset_dir)
iconset_dir.mkdir(parents=True, exist_ok=True)

for size in [16, 32, 64, 128, 256, 512, 1024]:
    icon = final.resize((size, size), Image.LANCZOS)
    icon.save(iconset_dir / f'icon_{size}x{size}.png')

for size in [16, 32, 64, 128, 256, 512, 1024]:
    if size in (16, 32, 128, 256, 512):
        doubled = size * 2
        icon = final.resize((doubled, doubled), Image.LANCZOS)
        icon.save(iconset_dir / f'icon_{size}x{size}@2x.png')

subprocess.run(['iconutil', '-c', 'icns', str(iconset_dir)], check=True, cwd=str(output_dir))

# Also keep a transparent version for any future use.
transparent = Image.new('RGBA', (SIZE, SIZE), (0, 0, 0, 0))
transparent.save(output_dir / 'icon-transparent.png')
