#!/usr/bin/env python3
"""Fit the SV600 sweep geometry from detected checkerboard corners.

Model (raw pixel u = column, v = scan line -> desk mm):
    u' = (u - W/2) / W,  v' = v / H_PX
    theta = a0 + a1 v' + a2 v'^2 + a3 v'^3          (head angle per line)
    y     = H tan(theta) + H (e1 u' + e2 u'^2) / cos^2(theta)   (line tilt/bow)
    x     = H c (u' + b0 + b2 u'^2 + b3 u'^3) / cos(theta)      (footprint grows with distance)
Each calibration image has its own board pose (rotation, translation).

usage: fit_geometry.py -> calibration/geometry.json (square size from calibration/board.json)
"""
import json
import sys
import numpy as np
from scipy.optimize import least_squares

W, H_PX = 5572, 4429
BOARD = json.load(open('calibration/board.json'))
SQUARE = BOARD['square_mm'] * BOARD['print_scale']     # printed square size, mm
GLOBAL = ['H', 'a0', 'a1', 'a2', 'a3', 'c', 'b0', 'b2', 'b3', 'e1', 'e2']
INIT = dict(H=400.0, a0=0.25, a1=0.62, a2=0.0, a3=0.0, c=1.0, b0=0.0, b2=0.0, b3=0.0,
            e1=0.0, e2=0.0)


def raw_to_desk(g, u, v):
    up = (u - W / 2) / W
    vp = v / H_PX
    th = g['a0'] + g['a1'] * vp + g['a2'] * vp ** 2 + g['a3'] * vp ** 3
    ct = np.cos(th)
    y = g['H'] * np.tan(th) + g['H'] * (g['e1'] * up + g['e2'] * up ** 2) / ct ** 2
    x = g['H'] * g['c'] * (up + g['b0'] + g['b2'] * up ** 2 + g['b3'] * up ** 3) / ct
    return x, y


def unpack(p, n_img):
    g = dict(zip(GLOBAL, p[:len(GLOBAL)]))
    poses = p[len(GLOBAL):].reshape(n_img, 3)
    return g, poses


def residuals(p, data, free):
    full = np.array([INIT[k] for k in GLOBAL] + [0.0] * (len(p) - len(free)), float)
    full[free] = p[:len(free)] if False else full[free]
    return None


def fit_physical(write=True):
    """Fit the physical sweep model; returns (params incl. head height H in mm, rms mm)."""
    d = np.load('calibration/corners.npz')
    names = sorted({k[:-3] for k in d.files})
    data = [(n, d[n + '_uv'], d[n + '_ij'] * SQUARE) for n in names]
    n_img = len(data)

    def resid(p):
        g, poses = unpack(p, n_img)
        out = []
        for (name, uv, bp), (phi, tx, ty) in zip(data, poses):
            x, y = raw_to_desk(g, uv[:, 0], uv[:, 1])
            c, s = np.cos(phi), np.sin(phi)
            bx = c * bp[:, 0] - s * bp[:, 1] + tx
            by = s * bp[:, 0] + c * bp[:, 1] + ty
            out += [x - bx, y - by]
        return np.concatenate(out)

    def fit(free_keys, p0):
        """Optimise only free_keys (others fixed at p0 values) plus all poses."""
        idx = [GLOBAL.index(k) for k in free_keys] + list(range(len(GLOBAL), len(p0)))

        def r(q):
            p = p0.copy()
            p[idx] = q
            return resid(p)
        sol = least_squares(r, p0[idx], method='lm', max_nfev=20000)
        p = p0.copy()
        p[idx] = sol.x
        return p

    # initial poses: align board centroid with desk centroid of its corners
    g0 = dict(INIT)
    poses0 = []
    for name, uv, bp in data:
        x, y = raw_to_desk(g0, uv[:, 0], uv[:, 1])
        # board axes may be flipped/rotated relative to the image; pick the best of 4 rotations
        best = None
        for phi in (0, np.pi / 2, np.pi, -np.pi / 2):
            c, s = np.cos(phi), np.sin(phi)
            bx = c * bp[:, 0] - s * bp[:, 1]
            by = s * bp[:, 0] + c * bp[:, 1]
            tx, ty = (x - bx).mean(), (y - by).mean()
            err = np.hypot(x - bx - tx, y - by - ty).mean()
            if best is None or err < best[0]:
                best = (err, phi, tx, ty)
        poses0.append(best[1:])
    p = np.array([INIT[k] for k in GLOBAL] + list(np.ravel(poses0)), float)

    # staged fit: coarse physical model first, then the small correction terms
    p = fit(['H', 'a0', 'a1', 'c'], p)
    p = fit(['H', 'a0', 'a1', 'a2', 'a3', 'c', 'b0'], p)
    p = fit(GLOBAL, p)

    r = resid(p).reshape(-1)
    g, poses = unpack(p, n_img)
    print('global parameters:')
    for k in GLOBAL:
        print(f'  {k:3} = {g[k]: .6f}')
    off = 0
    print('per-image residuals (mm):')
    for (name, uv, bp), pose in zip(data, poses):
        n = len(uv)
        ex = r[off:off + n]
        ey = r[off + n:off + 2 * n]
        off += 2 * n
        e = np.hypot(ex, ey)
        print(f'  {name}: n={n:3}  rms {np.sqrt((e ** 2).mean()):.3f}  max {e.max():.3f}  '
              f'rot {np.degrees(pose[0]):7.2f} deg')
    e = np.hypot(r[0::2], r[1::2]) if False else None
    rms = np.sqrt((r ** 2).mean() * 2)
    print(f'overall rms {rms:.3f} mm  ({rms / 25.4 * 300:.2f} px at 300 dpi)')

    # desk footprint of the raw frame
    corners = {k: raw_to_desk(g, u, v) for k, (u, v) in
               dict(top_left=(0, 0), top_right=(W - 1, 0), bottom_left=(0, H_PX - 1),
                    bottom_right=(W - 1, H_PX - 1)).items()}
    for k, (x, y) in corners.items():
        print(f'  {k:12} x {float(x):8.1f} mm  y {float(y):7.1f} mm')
    json.dump(dict(model='sv600-sweep-v1', width_px=W, height_px=H_PX, square_mm=SQUARE,
                   params=g, rms_mm=rms,
                   images={n: list(map(float, pz)) for (n, _, _), pz in zip(data, poses)}),
              open('calibration/geometry.json', 'w'), indent=1) if write else None
    if write:
        print('wrote calibration/geometry.json')
    return {k: float(v) for k, v in g.items()}, float(rms)


def main():
    fit_physical()


if __name__ == '__main__':
    main()
