#!/usr/bin/env python3
"""Render the README key map from openCodexMicro's shipped D200 assets."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "standalone" / "assets" / "generated" / "runtime"
OUTPUT = ROOT / "docs" / "images" / "open-codex-micro-layout.png"
sys.path.insert(0, str(ROOT / "standalone"))

from d200 import render_usage_values  # noqa: E402


def font(size: int, bold: bool = False):
    candidates = [
        "/System/Library/Fonts/SFNSRounded.ttf",
        "/System/Library/Fonts/SFCompactRounded.ttf",
        "/System/Library/Fonts/HelveticaNeue.ttc",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            index = 1 if bold and candidate.endswith(".ttc") else 0
            return ImageFont.truetype(candidate, size, index=index)
    return ImageFont.load_default()


def asset(name: str) -> Image.Image:
    return Image.open(RUNTIME / name).convert("RGBA")


canvas = Image.new("RGB", (1600, 980), "#f2f4f5")

shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
ImageDraw.Draw(shadow).rounded_rectangle(
    (112, 100, 1488, 898),
    radius=54,
    fill=(0, 0, 0, 95),
)
shadow = shadow.filter(ImageFilter.GaussianBlur(28))
canvas.paste(shadow, (0, 0), shadow)

plate = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
draw = ImageDraw.Draw(plate)
draw.rounded_rectangle(
    (100, 72, 1500, 872),
    radius=54,
    fill="#202329",
    outline="#343941",
    width=4,
)
draw.text(
    (150, 110),
    "openCodexMicro",
    font=font(43, bold=True),
    fill="#f7f8f9",
    anchor="lm",
)
draw.text(
    (1450, 110),
    "ULANZI D200 · NATIVE",
    font=font(22, bold=True),
    fill="#8f98a5",
    anchor="rm",
)
canvas.paste(plate, (0, 0), plate)

key_size = 176
gap = 52
left = 180
top = 178
row_step = 230
positions = [
    left + column * (key_size + gap)
    for column in range(5)
]

task_assets = [
    "task-thinking-v2.png",
    "task-complete-v3.png",
    "task-complete-v3.png",
    "task-thinking-v2.png",
    "task-input-v2.png",
]
middle_assets = [
    "command-fast-final.png",
    None,
    "command-pin-final.png",
    "command-new-final.png",
    "command-fork-final.png",
]
bottom_assets = [
    "command-steer-final.png",
    "command-mic-final.png",
    "command-submit-final.png",
]

usage = Image.open(BytesIO(render_usage_values(92, 79))).convert("RGBA")
usage.putalpha(asset("command-usage-final.png").getchannel("A"))
label_font = font(18, bold=True)
label_color = "#aeb6c1"


def place_key(image: Image.Image, x: int, y: int, label: str) -> None:
    canvas.paste(image.resize((key_size, key_size)), (x, y), image.resize((key_size, key_size)))
    ImageDraw.Draw(canvas).text(
        (x + key_size / 2, y + key_size + 25),
        label,
        font=label_font,
        fill=label_color,
        anchor="mm",
    )


for index, image_name in enumerate(task_assets):
    place_key(asset(image_name), positions[index], top, f"TASK {index + 1}")

middle_labels = ["FAST", "USAGE", "PIN", "NEW", "FORK"]
for index, image_name in enumerate(middle_assets):
    image = usage if image_name is None else asset(image_name)
    place_key(image, positions[index], top + row_step, middle_labels[index])

bottom_labels = ["STEER", "MIC", "SUBMIT"]
for index, image_name in enumerate(bottom_assets):
    place_key(
        asset(image_name),
        positions[index],
        top + 2 * row_step,
        bottom_labels[index],
    )

clock_x = positions[3]
clock_y = top + 2 * row_step + 10
clock_w = key_size * 2 + gap
clock_h = key_size - 20
clock_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
clock_draw = ImageDraw.Draw(clock_layer)
clock_draw.rounded_rectangle(
    (clock_x, clock_y, clock_x + clock_w, clock_y + clock_h),
    radius=26,
    fill="#0d1014",
    outline="#3c424b",
    width=5,
)
clock_draw.ellipse(
    (clock_x + 35, clock_y + 35, clock_x + 120, clock_y + 120),
    outline="#d8dde3",
    width=3,
)
center = (clock_x + 77, clock_y + 77)
clock_draw.line((center[0], center[1], center[0] - 3, center[1] - 28), fill="#f7f8f9", width=4)
clock_draw.line((center[0], center[1], center[0] + 24, center[1] + 10), fill="#ef9b42", width=4)
clock_draw.text(
    (clock_x + 155, clock_y + clock_h / 2),
    "09:53",
    font=font(54, bold=True),
    fill="#f5f7f9",
    anchor="lm",
)
clock_draw.text(
    (clock_x + clock_w / 2, clock_y + clock_h + 35),
    "CLOCK · FOCUS",
    font=label_font,
    fill=label_color,
    anchor="mm",
)
canvas.paste(clock_layer, (0, 0), clock_layer)

OUTPUT.parent.mkdir(parents=True, exist_ok=True)
canvas.save(OUTPUT, "PNG", optimize=True)
print(OUTPUT)
