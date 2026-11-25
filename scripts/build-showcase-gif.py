"""Encode real, visibly labeled browser frames; never generates or retouches UI data.

Optional tool dependency: Pillow in a separate local virtual environment.
"""
from pathlib import Path
import argparse
from PIL import Image

parser = argparse.ArgumentParser()
parser.add_argument("frames", type=Path)
parser.add_argument("output", type=Path)
args = parser.parse_args()
paths = sorted(args.frames.glob("*.png"))
if not paths:
    raise SystemExit("No browser PNG frames found")
frames = []
for path in paths:
    with Image.open(path) as source:
        # The first 36px contain the mandatory synthetic/PAPER ONLY ribbon.
        # Fit the whole screenshot; no content or provenance label is cropped.
        frame = source.convert("RGB")
        frame.thumbnail((1008, 630), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (1008, 630), "#0a0e13")
        canvas.paste(frame, ((1008 - frame.width) // 2, 0))
        frames.append(canvas.quantize(colors=160, method=Image.Quantize.MEDIANCUT))
args.output.parent.mkdir(parents=True, exist_ok=True)
frames[0].save(args.output, save_all=True, append_images=frames[1:], duration=1250,
               loop=0, disposal=2, optimize=False)
with Image.open(args.output) as check:
    if check.width != 1008 or check.height != 630:
        raise SystemExit("Unexpected encoded dimensions")
print(f"{args.output.name}: {len(paths)} captured browser frames, {args.output.stat().st_size} bytes")
