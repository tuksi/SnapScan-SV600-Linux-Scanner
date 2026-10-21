#!/usr/bin/env python3
"""Measure R/B registration offsets and white-paper brightness on calibration scans.

Needs calibration/corners.npz (find_corners.py). Writes:
  calibration/channel_shift.npy   rows: u, v, R-G du, dv, resp, B-G du, dv, resp
  calibration/white_samples.npy   rows: u, v, R, G, B of white-square centres
                                  (R/B sampled at their shifted lines)

usage: measure_channels_flat.py calibration/cal*.png
"""
import sys
import numpy as np
import cv2

H_PX = 4429
P = 96                                   # phase-correlation patch half-size (px)


def main():
    d = np.load('calibration/corners.npz')
    files = sys.argv[1:]
    win = cv2.createHanningWindow((2 * P, 2 * P), cv2.CV_64F)

    # 1. channel offsets: phase correlation of R and B against G around every corner
    rows = []
    for f in files:
        key = f.rsplit('/', 1)[-1].rsplit('.', 1)[0]
        bgr = cv2.imread(f).astype(np.float64)
        H, W = bgr.shape[:2]
        for u, v in d[key + '_uv']:
            u, v = int(round(u)), int(round(v))
            if u < P or v < P or u >= W - P or v >= H - P:
                continue
            G = bgr[v - P:v + P, u - P:u + P, 1]
            out = [u, v]
            for ci in (2, 0):
                (du, dv), resp = cv2.phaseCorrelate(G, bgr[v - P:v + P, u - P:u + P, ci], win)
                out += [du, dv, resp]
            rows.append(out)
    shifts = np.array(rows)
    np.save('calibration/channel_shift.npy', shifts)
    dv = (shifts[:, 3] - shifts[:, 6]) / 2
    poly = np.polyfit(shifts[:, 1] / H_PX, dv, 3)
    print(f'channel offsets: {len(shifts)} patches, dv from {dv.min():+.1f} to {dv.max():+.1f} px')

    # 2. white paper level at the centre of every white square
    samples = []
    for f in files:
        key = f.rsplit('/', 1)[-1].rsplit('.', 1)[0]
        bgr = cv2.imread(f).astype(np.float32)
        uv, ij = d[key + '_uv'], d[key + '_ij'].astype(int)
        idx = {(i, j): k for k, (i, j) in enumerate(ij)}
        for (i, j), k in idx.items():
            q = [idx.get((i + di, j + dj)) for di, dj in ((1, 0), (0, 1), (1, 1))]
            if None in q:
                continue
            c = uv[[k] + q].mean(0)
            h = max(int(0.25 * np.abs(uv[q[2]] - uv[k]).min()), 4)
            u0, v0 = int(c[0]), int(c[1])
            sh = np.polyval(poly, v0 / H_PX)

            def patch(ch, off):
                vv = int(round(v0 + off))
                return bgr[vv - h:vv + h, u0 - h:u0 + h, ch].mean()
            G = patch(1, 0)
            if G < 100:                                   # black square
                continue
            samples.append((c[0], c[1], patch(2, sh), G, patch(0, -sh)))
    white = np.array(samples)
    np.save('calibration/white_samples.npy', white)
    print(f'white samples: {len(white)}, G from {white[:, 3].min():.0f} to {white[:, 3].max():.0f}')


if __name__ == '__main__':
    main()
