#!/usr/bin/env python3
"""Build calibration/sv600_calibration.json from the checkerboard measurements.

Inputs (made by find_corners.py and the channel/white sampling step):
  calibration/corners.npz         checkerboard corners per scan
  calibration/channel_shift.npy   R/B vs G phase-correlation offsets
  calibration/white_samples.npy   white-square brightness (u, v, R, G, B)

Geometry model (raw u = column, v = scan line -> desk mm), "separable-v1":
  u' = (u - W/2)/W, v' = v/H
  S(v) = s0 + s1 v' + s2 v'^2 + s3 v'^3               width scale of line v
  x    = S(v) (u' + b0 + b2 u'^2 + b3 u'^3)
  y    = y1 v' + ... + y5 v'^5 + S(v) (e1 u' + e2 u'^2)
"""
import json
import os
import time

import numpy as np
from scipy.optimize import least_squares
from scipy.interpolate import PchipInterpolator

W, HP = 5572, 4429
# Printed square size from calibration/board.json: the sheet was printed at 96.5 % (its 100 mm
# reference line measures 96.5 mm with a ruler), so the nominal 15 mm squares are 14.475 mm.
BOARD = json.load(open('calibration/board.json'))
PRINT_SCALE = BOARD['print_scale']
SQ = BOARD['square_mm'] * PRINT_SCALE
GEOM = ['y1', 'y2', 'y3', 'y4', 'y5', 's0', 's1', 's2', 's3', 'b0', 'b2', 'b3', 'e1', 'e2']


def raw_to_desk(g, u, v):
    up = (u - W / 2) / W
    vp = v / HP
    S = g['s0'] + g['s1'] * vp + g['s2'] * vp ** 2 + g['s3'] * vp ** 3
    x = S * (up + g['b0'] + g['b2'] * up ** 2 + g['b3'] * up ** 3)
    y = sum(g[f'y{i}'] * vp ** i for i in range(1, 6)) + S * (g['e1'] * up + g['e2'] * up ** 2)
    return x, y


def fit_geometry():
    d = np.load('calibration/corners.npz')
    names = sorted({k[:-3] for k in d.files})
    data = [(n, d[n + '_uv'], d[n + '_ij'] * SQ) for n in names]
    N, nG = len(data), len(GEOM)

    def resid(p):
        g = dict(zip(GEOM, p[:nG]))
        out = []
        for (n, uv, bp), (phi, tx, ty) in zip(data, p[nG:].reshape(N, 3)):
            x, y = raw_to_desk(g, uv[:, 0], uv[:, 1])
            c, s = np.cos(phi), np.sin(phi)
            out += [x - (c * bp[:, 0] - s * bp[:, 1] + tx), y - (s * bp[:, 0] + c * bp[:, 1] + ty)]
        return np.concatenate(out)

    g0 = {k: 0.0 for k in GEOM}
    g0.update(y1=400.0, s0=500.0)
    p = np.array([g0[k] for k in GEOM] + [0.0] * (3 * N))
    for i, (n, uv, bp) in enumerate(data):
        x, y = raw_to_desk(g0, uv[:, 0], uv[:, 1])
        p[nG + 3 * i:nG + 3 * i + 3] = (np.pi, (x + bp[:, 0]).mean(), (y + bp[:, 1]).mean())
    sol = least_squares(resid, p, method='lm', max_nfev=50000)
    rms = float(np.sqrt((sol.fun ** 2).mean() * 2))
    return dict(zip(GEOM, map(float, sol.x[:nG]))), rms, len(sol.fun) // 2


def fit_channel_shift():
    a = np.load('calibration/channel_shift.npy')
    v, rdv, bdv = a[:, 1], a[:, 3], a[:, 6]
    dv = (rdv - bdv) / 2
    c = np.polyfit(v / HP, dv, 3)
    return [float(x) for x in c], float(np.sqrt(np.mean((np.polyval(c, v / HP) - dv) ** 2)))


def fit_flatfield():
    """Gain table along v (binned medians, PCHIP, linear extrapolation) and a quadratic in u."""
    s = np.load('calibration/white_samples.npy')
    u, v = s[:, 0], s[:, 1]
    G = s[:, 3]
    edges = np.arange(0, HP + 150, 150)
    cv, cg = [], []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (v >= lo) & (v < hi)
        if m.sum() >= 5:
            cv.append(float(np.median(v[m])))
            cg.append(float(np.median(G[m])))
    cv, cg = np.array(cv), np.array(cg)
    ref = float(np.median(G[v < 3000]))                 # level of the evenly lit region
    # u dependence: ratio to the v-profile, quadratic in u'
    prof = PchipInterpolator(cv, cg, extrapolate=True)
    up = (u - W / 2) / W
    qu = np.polyfit(up, G / prof(v), 2)
    # dense v table with linear extrapolation beyond the measured range
    vt = np.linspace(0, HP - 1, 64)
    inside = (vt >= cv[0]) & (vt <= cv[-1])
    level = np.empty_like(vt)
    level[inside] = prof(vt[inside])
    lo_slope = (cg[1] - cg[0]) / (cv[1] - cv[0])
    hi_slope = (cg[-1] - cg[-2]) / (cv[-1] - cv[-2])
    level[vt < cv[0]] = cg[0] + lo_slope * (vt[vt < cv[0]] - cv[0])
    level[vt > cv[-1]] = cg[-1] + hi_slope * (vt[vt > cv[-1]] - cv[-1])
    gain = np.clip(ref / level, 0.5, 2.5)
    return dict(v=[float(x) for x in vt], gain=[float(x) for x in gain],
                u_quadratic=[float(x) for x in qu], reference_level=ref,
                measured_v_range=[float(cv[0]), float(cv[-1])])


def head_height():
    """Height of the projection centre above the desk (mm), from the physical sweep model
    (tools/fit_geometry.py). A flat object raised by z appears magnified by H / (H - z) in the
    desk-corrected image, about the point below the head."""
    import contextlib
    import importlib.util
    import io
    spec = importlib.util.spec_from_file_location('fit_geometry', 'tools/fit_geometry.py')
    fg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fg)
    with contextlib.redirect_stdout(io.StringIO()):
        g, rms = fg.fit_physical(write=False)
    return g['H'], rms


def main():
    geom, rms, npts = fit_geometry()
    shift, shift_rms = fit_channel_shift()
    flat = fit_flatfield()
    H, H_rms = head_height()
    scans = sorted({k[:-3] for k in np.load('calibration/corners.npz').files})
    colour = None
    if os.path.exists('calibration/colour/colour_fit.json'):          # tools/colour_fit.py
        c = json.load(open('calibration/colour/colour_fit.json'))
        colour = {k: c[k] for k in ('reference', 'scans', 'lut_linear', 'matrix', 'de00_mean_before',
                                    'de00_mean_after', 'de00_max_after')}
        colour['note'] = ('scanner value -> linear sRGB per channel (lut_linear, 256 x 3), then 3x3 '
                          'matrix (rows sum to 1), then sRGB encoding; fitted on a SpyderCHECKR 24')
    cal = dict(
        model='separable-v1', width_px=W, height_px=HP,
        geometry=geom, geometry_rms_mm=rms, geometry_points=npts,
        channel_shift_poly=shift, channel_shift_rms_px=shift_rms,
        channel_shift_note='R sampled at v + dv(v), B at v - dv(v); dv = polyval(poly, v/H)',
        flatfield=flat,
        colour=colour,
        raw_rotation_deg=180,
        head_height_mm=H, head_height_note='physical sweep model fit (rms %.2f mm); flat scans '
        'constrain it to about +/-10 mm' % H_rms,
        square_mm=SQ, print_scale=PRINT_SCALE,
        source=f'checkerboard scans {", ".join(scans)}; {BOARD["square_mm"]:g} mm squares printed at '
               f'{100 * PRINT_SCALE:g} %; built {time.strftime("%Y-%m-%d")}')
    json.dump(cal, open('calibration/sv600_calibration.json', 'w'), indent=1)
    print(f'geometry rms {rms:.3f} mm over {npts} corners; channel shift rms {shift_rms:.2f} px')
    print(f'head height {H:.1f} mm (physical model rms {H_rms:.3f} mm)')
    print('flat-field gain at v=0/3000/3600/4000/4428:',
          [round(float(np.interp(x, flat['v'], flat['gain'])), 2) for x in (0, 3000, 3600, 4000, 4428)])
    for name, (u, v) in dict(top_left=(0, 0), top_right=(W - 1, 0), bottom_left=(0, HP - 1),
                             bottom_right=(W - 1, HP - 1)).items():
        x, y = raw_to_desk(geom, u, v)
        print(f'  raw {name:12} -> desk x {x:7.1f} mm, y {y:6.1f} mm')
    print('wrote calibration/sv600_calibration.json')


if __name__ == '__main__':
    main()
