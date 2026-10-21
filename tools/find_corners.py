#!/usr/bin/env python3
"""Detect checkerboard inner corners in SV600 calibration scans.

Writes calibration/corners.npz with, per image, the corner pixel positions
(u = column, v = scan line) and their integer board indices (i, j).
Partially visible boards are handled by trying smaller pattern sizes.

usage: find_corners.py calibration/cal*.png
"""
import sys
import numpy as np
import cv2

FULL = (11, 17)          # inner corners of the printed board (cols, rows)


def detect(gray):
    """Return (corners Nx2, (cols, rows)) for the largest pattern found."""
    flags = cv2.CALIB_CB_NORMALIZE_IMAGE | cv2.CALIB_CB_EXHAUSTIVE | cv2.CALIB_CB_ACCURACY
    sizes = [FULL, (FULL[1], FULL[0])]
    # partial boards (cut by the scan edge): shrink the longer side, then the shorter
    for r in range(FULL[1] - 1, 5, -1):
        sizes += [(FULL[0], r), (r, FULL[0])]
    for c in range(FULL[0] - 1, 5, -1):
        sizes += [(c, 8), (8, c)]
    for size in sizes:
        ok, corners = cv2.findChessboardCornersSB(gray, size, flags=flags)
        if ok:
            return corners.reshape(-1, 2), size
    return None, None


def main():
    out = {}
    for path in sys.argv[1:]:
        img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        # work at half resolution for speed, then refine at full resolution
        small = cv2.resize(img, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
        corners, size = detect(small)
        if corners is None:
            print(f'{path}: no board found')
            continue
        corners = corners * 2.0
        crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 50, 0.01)
        corners = cv2.cornerSubPix(img, corners.reshape(-1, 1, 2).astype(np.float32),
                                   (9, 9), (-1, -1), crit).reshape(-1, 2)
        cols, rows = size
        ij = np.array([(i, j) for j in range(rows) for i in range(cols)], float)
        key = path.rsplit('/', 1)[-1].rsplit('.', 1)[0]
        out[key + '_uv'] = corners
        out[key + '_ij'] = ij
        print(f'{path}: {cols}x{rows} corners, u {corners[:, 0].min():.0f}-{corners[:, 0].max():.0f}, '
              f'v {corners[:, 1].min():.0f}-{corners[:, 1].max():.0f}')
    np.savez('calibration/corners.npz', **out)


if __name__ == '__main__':
    main()
