#!/usr/bin/env python
"""Regenerate the PWA icon PNGs from the geometry of ``icon.svg``.

The SVG in this directory is the master; these PNGs exist because iOS home
screen and older Android manifest readers want raster. Run from ``apps/web``:

    .venv/bin/python scripts/gen_pwa_icons.py

(PIL only — no font dependency, the mark is drawn from the same coordinates
as the SVG path.)
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw

SKY = (14, 165, 233)
VIOLET = (124, 58, 237)
WHITE = (255, 255, 255)


def _gradient(size: int) -> Image.Image:
    """A top-left → bottom-right sky→violet square, same stops as the SVG."""

    ramp = Image.new("RGB", (64, 64))
    pixels = ramp.load()
    for y in range(64):
        for x in range(64):
            t = (x + y) / 126  # the SVG's unit-square diagonal, normalised
            pixels[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(SKY, VIOLET))
    return ramp.resize((size, size), Image.BICUBIC)


def _rounded_mask(size: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=int(size * 112 / 512), fill=255
    )
    return mask


def _mark(size: int) -> Image.Image:
    """The white 'V' stroke, drawn at the SVG's relative coordinates."""

    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    points = [
        (size * 154 / 512, size * 160 / 512),
        (size * 256 / 512, size * 372 / 512),
        (size * 358 / 512, size * 160 / 512),
    ]
    width = int(size * 42 / 512)
    draw.line(points, fill=WHITE + (255,), width=width, joint="curve")
    # Rounded caps: PIL only joints the polyline, so cap the two ends by hand.
    for x, y in (points[0], points[-1]):
        r = width / 2
        draw.ellipse((x - r, y - r, x + r, y + r), fill=WHITE + (255,))
    return layer


def build(size: int, *, full_bleed: bool) -> Image.Image:
    base = _gradient(size).convert("RGBA")
    base.alpha_composite(_mark(size))
    if full_bleed:
        # iOS apple-touch-icon: no transparency, the OS applies its own mask.
        return base.convert("RGB")
    masked = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    masked.paste(base, (0, 0), _rounded_mask(size))
    return masked


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, "..", "static", "icons")
    for size in (192, 512):
        build(size, full_bleed=False).resize((size, size)).save(
            os.path.join(out, f"icon-{size}.png")
        )
    build(180, full_bleed=True).save(os.path.join(out, "apple-touch-icon.png"))
    # maskable needs its mark safely inside the 80% safe zone
    icon = build(512, full_bleed=True)
    icon.resize((512, 512)).save(os.path.join(out, "icon-maskable-512.png"))
    print("wrote icons to", os.path.normpath(out))


if __name__ == "__main__":
    main()
