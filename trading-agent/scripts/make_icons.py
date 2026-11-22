"""Draws the web app's home-screen icons (run once; the PNGs are committed).

    uv run --with pillow python scripts/make_icons.py

Three rising bars in the A, B and C colors on a dark rounded square.
"""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "src/papertrader/web/static/icons"
BACKGROUND = (0, 0, 0)  # the app's page colour
BARS = [(19, 156, 140), (217, 107, 58), (139, 124, 240)]  # A teal, B orange, C violet


def icon(size: int, *, padding: float, rounded: bool) -> Image.Image:
    scale = 4  # draw big, then shrink: smooth edges
    big = size * scale
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0) if rounded else (*BACKGROUND, 255))
    draw = ImageDraw.Draw(image)
    if rounded:
        draw.rounded_rectangle(
            (0, 0, big - 1, big - 1), radius=int(big * 0.22), fill=(*BACKGROUND, 255)
        )
    inner = big * (1 - 2 * padding)
    left0 = big * padding
    bottom = big * (1 - padding) - inner * 0.08
    width = inner * 0.2
    gap = (inner - 3 * width) / 4
    for i, (color, height) in enumerate(zip(BARS, (0.38, 0.6, 0.82), strict=True)):
        x0 = left0 + gap + i * (width + gap)
        top = bottom - inner * height
        draw.rounded_rectangle(
            (x0, top, x0 + width, bottom), radius=int(width * 0.28), fill=(*color, 255)
        )
    draw.rectangle(
        (left0 + gap * 0.5, bottom + inner * 0.03, big - left0 - gap * 0.5, bottom + inner * 0.055),
        fill=(195, 194, 183, 255),
    )
    return image.resize((size, size), Image.LANCZOS)


SPLASH = [(1170, 2532), (1179, 2556), (1284, 2778), (1290, 2796), (1125, 2436), (750, 1334)]


def splash(width: int, height: int) -> Image.Image:
    """The dark launch screen iOS shows while the app opens (no white flash)."""
    image = Image.new("RGB", (width, height), BACKGROUND)
    mark = icon(int(width * 0.28), padding=0.14, rounded=True)
    image.paste(mark, ((width - mark.width) // 2, (height - mark.height) // 2), mark)
    return image


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    icon(180, padding=0.16, rounded=False).convert("RGB").save(
        OUT / "apple-touch-icon.png"
    )  # iOS rounds it
    icon(192, padding=0.14, rounded=True).save(OUT / "icon-192.png")
    icon(512, padding=0.14, rounded=True).save(OUT / "icon-512.png")
    icon(512, padding=0.24, rounded=False).convert("RGB").save(OUT / "icon-maskable-512.png")
    icon(32, padding=0.08, rounded=True).save(OUT / "favicon-32.png")
    for width, height in SPLASH:
        splash(width, height).save(OUT / f"splash-{width}x{height}.png", optimize=True)


if __name__ == "__main__":
    main()
