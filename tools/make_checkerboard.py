#!/usr/bin/env python3
"""Make the printable calibration checkerboards (A4 and US Letter).

15 mm squares, a white dot in the top-left black square, and a 100 mm line to check the print
scale with a ruler. A4: 12 x 18 squares (11 x 17 inner corners); Letter is shorter, so
12 x 16 squares (11 x 15 inner corners), leaving room for printer margins. tools/find_corners.py
accepts both.

usage: make_checkerboard.py [OUT_DIR]   (default calibration)
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

DPI = 600
SQ = 15                                    # square size, mm
PAGES = {'A4': (210, 297, 12, 18), 'Letter': (215.9, 279.4, 12, 16)}   # paper mm, squares


def mm(v):
    return round(v / 25.4 * DPI)


def page(width, height, nx, ny):
    img = Image.new('L', (mm(width), mm(height)), 255)
    d = ImageDraw.Draw(img)
    x0, y0 = (width - SQ * nx) / 2, (height - SQ * ny) / 2 + 3
    for j in range(ny):
        for i in range(nx):
            if (i + j) % 2 == 0:
                d.rectangle([mm(x0 + i * SQ), mm(y0 + j * SQ), mm(x0 + (i + 1) * SQ) - 1,
                             mm(y0 + (j + 1) * SQ) - 1], fill=0)
    # orientation marker: white dot in the top-left black square
    cx, cy = mm(x0 + SQ / 2), mm(y0 + SQ / 2)
    d.ellipse([cx - mm(3), cy - mm(3), cx + mm(3), cy + mm(3)], fill=255)
    # 100 mm scale check line
    l0 = width / 2 - 50
    ly = mm(y0 - 6)
    d.line([mm(l0), ly, mm(l0 + 100), ly], fill=0, width=mm(0.4))
    for x in (l0, l0 + 100):
        d.line([mm(x), ly - mm(2), mm(x), ly + mm(2)], fill=0, width=mm(0.4))
    try:
        f = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', mm(3.2))
    except OSError:
        f = ImageFont.load_default()
    d.text((mm(l0), ly - mm(7.5)), 'SV600 calibration - print at 100% / Actual size - '
           'this line must be exactly 100 mm', font=f, fill=0)
    d.text((mm(x0), mm(y0 + SQ * ny + 2)), f'Checkerboard {nx}x{ny} squares of {SQ} mm '
           f'({nx - 1}x{ny - 1} inner corners). Dot = top-left.', font=f, fill=0)
    return img


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else 'calibration'
    for name, (w, h, nx, ny) in PAGES.items():
        path = os.path.join(out, f'sv600_checkerboard_{name}.pdf')
        page(w, h, nx, ny).save(path, resolution=DPI)
        print(f'wrote {path}')


if __name__ == '__main__':
    main()
