#!/usr/bin/env python3
"""Find a SpyderCHECKR 24 in corrected scans and measure its 24 patches.

For each scan: find the card (sv600.crop), cut it out upright, locate the patch squares,
fit the regular 4 x 6 grid through them (dark patches that are hard to see are filled in from
the grid), and take the median colour of the central 50 % of each patch. The grid is then
matched to the reference layout (any of the 8 rotations/mirrorings) by comparing colours.

usage: colour_patches.py scan1.png scan2.png ... -> calibration/colour/patches.json
"""
import json
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sv600 import crop                                   # noqa: E402

PX_MM = crop.PX_PER_MM

# Datacolor SpyderCheckr 24, L*a*b* (darktable src/common/colorchecker.h, "spyder_24", 2016)
REF = {
    'A1': (96.04, 2.16, 2.60), 'A2': (80.44, 1.17, 2.05), 'A3': (65.52, 0.69, 1.86),
    'A4': (49.62, 0.58, 1.56), 'A5': (33.55, 0.35, 1.40), 'A6': (16.91, 1.43, -0.81),
    'B1': (47.12, -32.52, -28.75), 'B2': (50.49, 53.45, -13.55), 'B3': (83.61, 3.36, 87.02),
    'B4': (41.05, 60.75, 31.17), 'B5': (54.14, -40.76, 34.75), 'B6': (24.75, 13.78, -49.48),
    'C1': (60.94, 38.21, 61.31), 'C2': (37.80, 7.30, -43.04), 'C3': (49.81, 48.50, 15.76),
    'C4': (28.88, 19.36, -24.48), 'C5': (72.45, -23.57, 60.47), 'C6': (71.65, 23.74, 72.28),
    'D1': (70.19, -31.85, 1.98), 'D2': (54.38, 8.84, -25.71), 'D3': (42.03, -15.78, 22.93),
    'D4': (48.82, -5.11, -23.08), 'D5': (65.10, 18.14, 18.68), 'D6': (36.13, 14.15, 15.78),
}
REF_GRID = [[f'{r}{c}' for c in range(1, 7)] for r in 'ABCD']     # 4 rows x 6


def find_card(rgb):
    """The largest detected object whose size fits the card (~ 150-260 mm)."""
    rects, _ = crop.find_objects(rgb)
    best = None
    for r in rects:
        w, h = sorted(r[1])
        if 120 * PX_MM < w < 260 * PX_MM and 150 * PX_MM < h < 330 * PX_MM:
            if best is None or w * h > best[1][0] * best[1][1]:
                best = r
    if best is None:
        raise RuntimeError('card not found')
    return best


def patch_grid(card):
    """Centres (6 x 4 or 4 x 6 array of (x, y)) and patch size in px, in the upright card image."""
    g = cv2.cvtColor(card, cv2.COLOR_RGB2GRAY)
    side = 26 * PX_MM
    found = []
    # the patches are separated by the card's black background: anything clearly lighter than
    # it is patch; several thresholds so dark patches have a chance too
    bg = np.percentile(g, 5)
    for t in (bg + 40, bg + 25, bg + 15):
        m = ((g > t) * 255).astype(np.uint8)
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
        cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in cnts:
            (cx, cy), (w, h), a = cv2.minAreaRect(c)
            if 0.75 * side < w < 1.25 * side and 0.75 * side < h < 1.25 * side:
                if all(np.hypot(cx - fx, cy - fy) > side / 2 for fx, fy in found):
                    found.append((cx, cy))
    found = np.array(found)
    if len(found) < 8:
        raise RuntimeError(f'only {len(found)} patches found')
    # cluster into columns / rows by gaps larger than half a patch
    def clusters(v):
        v = np.sort(v)
        groups, cur = [], [v[0]]
        for x in v[1:]:
            (cur.append(x) if x - cur[-1] < side / 2 else (groups.append(cur), cur := [x]))
        groups.append(cur)
        return np.array([np.mean(g) for g in groups])
    xs, ys = clusters(found[:, 0]), clusters(found[:, 1])
    # complete a regular grid: pitch from the clusters, count 4 x 6 (either way round)
    def regular(c, n):
        pitch = np.median(np.diff(c)) if len(c) > 1 else 31 * PX_MM
        k = np.round((c - c[0]) / pitch)
        a, b = np.polyfit(k, c, 1)                # c = b + a * k
        start = 0 if len(c) >= n else -int((n - (k.max() + 1)) // 2)
        return b + a * np.arange(start, start + n), a
    nx, ny = (4, 6) if card.shape[0] > card.shape[1] else (6, 4)
    gx, px = regular(xs, nx)
    gy, py = regular(ys, ny)
    return np.array([[(x, y) for x in gx] for y in gy]), (px + py) / 2


def measure(rgb):
    rect = find_card(rgb)
    card = crop.extract(rgb, crop.box_from_rect(rect, 0))
    centres, pitch = patch_grid(card)
    half = int(13 * PX_MM / 2)                   # central 13 mm of the 26 mm patch
    vals = np.zeros(centres.shape[:2] + (3,))
    spread = np.zeros(centres.shape[:2])
    for i in range(centres.shape[0]):
        for j in range(centres.shape[1]):
            x, y = (int(round(v)) for v in centres[i, j])
            p = card[y - half:y + half, x - half:x + half].reshape(-1, 3).astype(float)
            vals[i, j] = np.median(p, axis=0)
            spread[i, j] = (np.percentile(p, 90, axis=0) - np.percentile(p, 10, axis=0)).max()
    return vals, spread, card, centres, half


def lab_to_srgb8(lab):
    """Reference L*a*b* (D50) -> 8-bit sRGB (D65, Bradford), for orientation matching only."""
    L, a, b = np.asarray(lab, float).T
    fy = (L + 16) / 116
    fx, fz = fy + a / 500, fy - b / 200
    f = lambda t: np.where(t > 6 / 29, t ** 3, 3 * (6 / 29) ** 2 * (t - 4 / 29))
    X, Y, Z = 0.96422 * f(fx), f(fy), 0.82521 * f(fz)
    brad = np.array([[0.9555766, -0.0230393, 0.0631636], [-0.0282895, 1.0099416, 0.0210077],
                     [0.0122982, -0.0204830, 1.3299098]])
    xyz65 = brad @ np.vstack([X, Y, Z])
    m = np.array([[3.2404542, -1.5371385, -0.4985314], [-0.9692660, 1.8760108, 0.0415560],
                  [0.0556434, -0.2040259, 1.0572252]])
    lin = np.clip((m @ xyz65).T, 0, 1)
    enc = np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * lin ** (1 / 2.4) - 0.055)
    return enc * 255


def orient(vals):
    """Map the measured grid onto REF_GRID: try all 8 rotations/mirrorings, best colour match."""
    ref = np.array([[lab_to_srgb8([REF[k]])[0] for k in row] for row in REF_GRID])   # 4 x 6 x 3
    best = None
    for k in range(4):
        for flip in (False, True):
            v = np.rot90(vals, k)
            v = v[:, ::-1] if flip else v
            if v.shape[:2] != ref.shape[:2]:
                continue
            # compare after a per-channel gain (scanner exposure / white balance unknown)
            gain = (ref.reshape(-1, 3) * v.reshape(-1, 3)).sum(0) / (v.reshape(-1, 3) ** 2).sum(0)
            err = np.abs(v * gain - ref).mean()
            if best is None or err < best[0]:
                best = (err, k, flip, v)
    return best


def main():
    out = {}
    for path in sys.argv[1:]:
        rgb = cv2.cvtColor(cv2.imread(path), cv2.COLOR_BGR2RGB)
        vals, spread, card, centres, half = measure(rgb)
        err, k, flip, v = orient(vals)
        # map spread with the same transform
        s = np.rot90(spread, k)
        s = s[:, ::-1] if flip else s
        name = os.path.basename(path)
        out[name] = {REF_GRID[i][j]: {'rgb': [round(float(x), 2) for x in v[i, j]], 'spread': float(s[i, j])}
                     for i in range(4) for j in range(6)}
        print(f'{name}: matched with rotation {90 * k} deg{" + mirror" if flip else ""}, '
              f'mean deviation after gain {err:.1f}/255; max in-patch spread {s.max():.0f}')
        dbg = card.copy()
        for row in centres:
            for (x, y) in row:
                cv2.rectangle(dbg, (int(x - half), int(y - half)), (int(x + half), int(y + half)), (255, 0, 255), 6)
        cv2.imwrite(f'calibration/colour/{name}_patches.jpg',
                    cv2.cvtColor(cv2.resize(dbg, None, fx=0.25, fy=0.25), cv2.COLOR_RGB2BGR))
    json.dump(out, open('calibration/colour/patches.json', 'w'), indent=1)
    print('wrote calibration/colour/patches.json')


if __name__ == '__main__':
    main()
