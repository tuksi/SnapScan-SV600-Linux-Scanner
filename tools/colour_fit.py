#!/usr/bin/env python3
"""Fit the scanner colour correction from SpyderCHECKR 24 measurements.

Model: per-channel tone curve (from the 6 grey patches: scanner value -> linear luminance of
the reference grey, so it fixes brightness and grey balance), then a 3x3 matrix in linear RGB
fitted on all 24 patches with rows summing to 1 (greys stay grey), then sRGB encoding.
Reports CIEDE2000 per patch: as scanned / curves only / curves + matrix, and leave-one-scan-out.

usage: colour_fit.py [calibration/colour/patches.json]  -> calibration/colour/colour_fit.json
"""
import json
import os
import sys

import numpy as np
from scipy.interpolate import PchipInterpolator

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from colour_patches import REF                           # noqa: E402

GREYS = ['A1', 'A2', 'A3', 'A4', 'A5', 'A6']
NAMES = sorted(REF)
BRAD_50_65 = np.array([[0.9555766, -0.0230393, 0.0631636], [-0.0282895, 1.0099416, 0.0210077],
                       [0.0122982, -0.0204830, 1.3299098]])
XYZ_TO_RGB = np.array([[3.2404542, -1.5371385, -0.4985314], [-0.9692660, 1.8760108, 0.0415560],
                       [0.0556434, -0.2040259, 1.0572252]])
WHITE_D50 = np.array([0.96422, 1.0, 0.82521])


def lab_to_xyz50(lab):
    L, a, b = np.asarray(lab, float).T
    fy = (L + 16) / 116
    fx, fz = fy + a / 500, fy - b / 200
    f = lambda t: np.where(t > 6 / 29, t ** 3, 3 * (6 / 29) ** 2 * (t - 4 / 29))
    return (np.vstack([f(fx), f(fy), f(fz)]).T * WHITE_D50)


def xyz50_to_lab(xyz):
    t = np.asarray(xyz, float) / WHITE_D50
    f = np.where(t > (6 / 29) ** 3, np.cbrt(t), t / (3 * (6 / 29) ** 2) + 4 / 29)
    return np.stack([116 * f[:, 1] - 16, 500 * (f[:, 0] - f[:, 1]), 200 * (f[:, 1] - f[:, 2])], 1)


def lab_to_lin(lab):
    return (XYZ_TO_RGB @ BRAD_50_65 @ lab_to_xyz50(lab).T).T


def lin_to_lab(lin):
    xyz65 = (np.linalg.inv(XYZ_TO_RGB) @ np.asarray(lin, float).T)
    return xyz50_to_lab((np.linalg.inv(BRAD_50_65) @ xyz65).T)


def decode(v8):
    v = np.asarray(v8, float) / 255
    return np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)


def ciede2000(lab1, lab2):
    L1, a1, b1 = np.asarray(lab1, float).T
    L2, a2, b2 = np.asarray(lab2, float).T
    C1, C2 = np.hypot(a1, b1), np.hypot(a2, b2)
    Cm = (C1 + C2) / 2
    G = 0.5 * (1 - np.sqrt(Cm ** 7 / (Cm ** 7 + 25 ** 7)))
    a1p, a2p = (1 + G) * a1, (1 + G) * a2
    C1p, C2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360
    dLp, dCp = L2 - L1, C2p - C1p
    dh = h2p - h1p
    dh = np.where(C1p * C2p == 0, 0, np.where(dh > 180, dh - 360, np.where(dh < -180, dh + 360, dh)))
    dHp = 2 * np.sqrt(C1p * C2p) * np.sin(np.radians(dh / 2))
    Lpm, Cpm = (L1 + L2) / 2, (C1p + C2p) / 2
    hs = h1p + h2p
    hpm = np.where(C1p * C2p == 0, hs, np.where(np.abs(h1p - h2p) <= 180, hs / 2,
                                                 np.where(hs < 360, (hs + 360) / 2, (hs - 360) / 2)))
    T = (1 - 0.17 * np.cos(np.radians(hpm - 30)) + 0.24 * np.cos(np.radians(2 * hpm))
         + 0.32 * np.cos(np.radians(3 * hpm + 6)) - 0.20 * np.cos(np.radians(4 * hpm - 63)))
    dtheta = 30 * np.exp(-(((hpm - 275) / 25) ** 2))
    Rc = 2 * np.sqrt(Cpm ** 7 / (Cpm ** 7 + 25 ** 7))
    Sl = 1 + 0.015 * (Lpm - 50) ** 2 / np.sqrt(20 + (Lpm - 50) ** 2)
    Sc, Sh = 1 + 0.045 * Cpm, 1 + 0.015 * Cpm * T
    Rt = -np.sin(np.radians(2 * dtheta)) * Rc
    return np.sqrt((dLp / Sl) ** 2 + (dCp / Sc) ** 2 + (dHp / Sh) ** 2 + Rt * (dCp / Sc) * (dHp / Sh))


class Model:
    """Tone curves (PCHIP, value 0-255 -> linear) + 3x3 matrix (rows sum to 1)."""

    def fit(self, scans):
        ref_lin = lab_to_lin([REF[k] for k in NAMES])
        target_grey = lab_to_xyz50([REF[k] for k in GREYS])[:, 1]          # luminance Y
        v_grey = np.array([[s[k]['rgb'] for k in GREYS] for s in scans]).mean(0)   # 6 x 3
        self.curves = []
        for c in range(3):
            x, y = v_grey[:, c], target_grey
            o = np.argsort(x)
            x, y = x[o], y[o]
            # anchor: 0 -> 0 below the darkest patch; straight extension above the whitest
            x = np.concatenate([[0.0], x])
            y = np.concatenate([[0.0], y])
            self.curves.append((x, y))
        self.lut = np.stack([self.curve(c, np.arange(256.0)) for c in range(3)], 1)  # 256 x 3
        X = np.concatenate([self.linearise(np.array([s[k]['rgb'] for k in NAMES])) for s in scans])
        Y = np.concatenate([ref_lin for _ in scans])
        # each output row r: minimise |X m - Y_r| with sum(m) = 1  ->  substitute m3 = 1 - m1 - m2
        self.M = np.zeros((3, 3))
        for r in range(3):
            A = X[:, :2] - X[:, 2:3]
            b = Y[:, r] - X[:, 2]
            m12, *_ = np.linalg.lstsq(A, b, rcond=None)
            self.M[r] = [m12[0], m12[1], 1 - m12.sum()]
        return self

    def curve(self, c, v):
        x, y = self.curves[c]
        f = PchipInterpolator(x, y, extrapolate=False)
        out = f(np.clip(v, x[0], x[-1]))
        slope = (y[-1] - y[-2]) / (x[-1] - x[-2])
        return np.where(v > x[-1], y[-1] + slope * (v - x[-1]), out)

    def linearise(self, v8):
        v8 = np.asarray(v8, float)
        return np.stack([self.curve(c, v8[:, c]) for c in range(3)], 1)

    def apply(self, v8, matrix=True):
        lin = self.linearise(v8)
        return lin @ self.M.T if matrix else lin


def report(scans, model, label):
    ref = np.array([REF[k] for k in NAMES])
    out = {}
    for mode in ('as scanned', 'curves only', 'curves + matrix'):
        des = []
        for s in scans:
            v = np.array([s[k]['rgb'] for k in NAMES])
            lin = decode(v) if mode == 'as scanned' else model.apply(v, mode == 'curves + matrix')
            des.append(ciede2000(ref, lin_to_lab(lin)))
        des = np.array(des)
        out[mode] = des
        print(f'  {label:22} {mode:16} mean dE00 {des.mean():5.2f}  median {np.median(des):5.2f}  '
              f'max {des.max():5.2f}  (greys {des[:, [NAMES.index(g) for g in GREYS]].mean():4.2f})')
    return out


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else 'calibration/colour/patches.json'
    data = json.load(open(path))
    names = sorted(data)
    scans = [data[n] for n in names]
    print(f'{len(scans)} scans: {", ".join(names)}')
    model = Model().fit(scans)
    print('fit on all scans:')
    res = report(scans, model, 'all scans')
    print('leave-one-scan-out (fit on the others, test on this one):')
    for i, n in enumerate(names):
        m = Model().fit(scans[:i] + scans[i + 1:])
        report([scans[i]], m, n)
    print('per patch (all scans, mean dE00):  as scanned -> corrected')
    for j, k in enumerate(NAMES):
        print(f'  {k}  {res["as scanned"][:, j].mean():5.1f} -> {res["curves + matrix"][:, j].mean():4.1f}')
    print('matrix:\n', np.round(model.M, 4))
    json.dump({'reference': 'Datacolor SpyderCheckr 24, darktable spyder_24 (2016), L*a*b* D50',
               'scans': names, 'lut_linear': model.lut.tolist(), 'matrix': model.M.tolist(),
               'curve_points': [[list(map(float, x)), list(map(float, y))] for x, y in model.curves],
               'de00_mean_before': float(res['as scanned'].mean()),
               'de00_mean_after': float(res['curves + matrix'].mean()),
               'de00_max_after': float(res['curves + matrix'].max())},
              open('calibration/colour/colour_fit.json', 'w'), indent=1)
    print('wrote calibration/colour/colour_fit.json')


if __name__ == '__main__':
    main()
