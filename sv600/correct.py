"""Geometric, colour-registration and brightness correction for SV600 raw scans.

Uses the scanner's calibration (built by tools/build_calibration.py; see paths.calibration_file):
  1. flat-field: undo the brightness fall-off on the far side of the sweep
  2. colour registration: R and B are offset by +/- dv(v) lines from G
  3. geometry: resample onto a flat desk grid at a uniform 300 dpi
  4. orientation: rotate so a page placed normally comes out upright
"""
import hashlib
import json
import os

import cv2
import numpy as np

from . import paths

OUT_DPI = 300
MM_PER_PX = 25.4 / OUT_DPI
COARSE = 8          # the inverse map is solved every COARSE output pixels, then interpolated
COLOUR_STEPS = 16384  # linear-light quantisation for the sRGB encoding table


class Calibration:
    def __init__(self, path=None):
        path = path or paths.calibration_file()
        raw = open(path, 'rb').read()
        c = json.loads(raw)
        self.digest = hashlib.sha1(raw).hexdigest()[:16]
        if c.get('model') != 'separable-v1':
            raise ValueError(f'unsupported calibration model {c.get("model")!r}')
        self.c = c
        self.path = os.path.abspath(path)
        self.W, self.H = c['width_px'], c['height_px']
        self.g = c['geometry']
        self.shift = np.array(c['channel_shift_poly'])
        f = c['flatfield']
        self.flat_v, self.flat_g = np.array(f['v']), np.array(f['gain'])
        self.flat_u = np.array(f['u_quadratic'])
        self.rotation = c.get('raw_rotation_deg', 0)
        self.head_height = c.get('head_height_mm')      # mm, for raised objects
        col = c.get('colour')
        self.colour = None
        if col:
            lut = np.asarray(col['lut_linear'], np.float32)                 # 256 x 3
            enc_x = np.linspace(0, 1, COLOUR_STEPS)
            enc = np.where(enc_x <= 0.0031308, 12.92 * enc_x, 1.055 * enc_x ** (1 / 2.4) - 0.055)
            self.colour = (lut.reshape(1, 256, 3), np.asarray(col['matrix'], np.float32),
                           np.clip(np.round(enc * 255), 0, 255).astype(np.uint8))

    # --- forward model: raw (u, v) -> desk (x, y) mm
    def S(self, vp):
        g = self.g
        return g['s0'] + g['s1'] * vp + g['s2'] * vp ** 2 + g['s3'] * vp ** 3

    def Y(self, vp):
        g = self.g
        return sum(g[f'y{i}'] * vp ** i for i in range(1, 6))

    def gu(self, up):
        g = self.g
        return up + g['b0'] + g['b2'] * up ** 2 + g['b3'] * up ** 3

    def raw_to_desk(self, u, v):
        up, vp = (u - self.W / 2) / self.W, v / self.H
        S = self.S(vp)
        g = self.g
        return S * self.gu(up), self.Y(vp) + S * (g['e1'] * up + g['e2'] * up ** 2)

    # --- inverse model: desk (x, y) -> raw (u, v), by table lookup + fixed-point iteration
    def desk_to_raw(self, x, y):
        g = self.g
        vt = np.linspace(-0.05, 1.05, 4096)
        yt = self.Y(vt)
        ut = np.linspace(-0.6, 0.6, 4096)
        gt = self.gu(ut)
        assert np.all(np.diff(yt) > 0) and np.all(np.diff(gt) > 0), 'model not monotonic'
        vp = np.interp(y, yt, vt)
        up = np.zeros_like(x)
        for _ in range(6):
            S = self.S(vp)
            up = np.interp(x / S, gt, ut)
            vp = np.interp(y - S * (g['e1'] * up + g['e2'] * up ** 2), yt, vt)
        return up * self.W + self.W / 2, vp * self.H

    def output_area(self, area='inscribed'):
        """Desk rectangle (x0, x1, y0, y1) in mm covered by the raw frame."""
        v = np.linspace(0, self.H - 1, 200)
        u = np.linspace(0, self.W - 1, 200)
        xl, yl = self.raw_to_desk(np.zeros_like(v), v)
        xr, yr = self.raw_to_desk(np.full_like(v, self.W - 1), v)
        xt, yt = self.raw_to_desk(u, np.zeros_like(u))
        xb, yb = self.raw_to_desk(u, np.full_like(u, self.H - 1))
        if area == 'full':
            return min(xl.min(), xb.min()), max(xr.max(), xb.max()), yt.min(), yb.max()
        return xl.max(), xr.min(), yt.max(), yb.min()

    def dv(self, v):
        return np.polyval(self.shift, v / self.H)

    def flat_gain(self, u, v):
        """Brightness gain at raw position (u, v)."""
        up = (u - self.W / 2) / self.W
        return np.interp(v, self.flat_v, self.flat_g) / np.polyval(self.flat_u, up)

    def coarse_maps(self, area='inscribed'):
        """Solve the mapping on a coarse grid; cached in ~/.cache/sv600-linux/.

        Coarse sample k sits at output pixel (k + 0.5) C - 0.5, which is exactly where
        cv2.resize(INTER_LINEAR) puts it when upscaling by C, so block-wise upscaling
        reproduces a dense bilinear map. One extra sample on each side keeps borders exact.
        Returns (dict of float32 arrays U, VR, VG, VB, G; (ow, oh); area_mm).
        """
        # keyed by the calibration's content, not its path or date: the bundled file inside
        # an AppImage has no meaningful timestamp
        cache = os.path.join(paths.CACHE_DIR, f'maps_{self.digest}_{area}_c{COARSE}.npz')
        key = np.frombuffer(f'{self.digest}/{area}/{COARSE}'.encode(), np.uint8)
        if os.path.exists(cache):
            try:
                z = np.load(cache)
                if np.array_equal(z['key'], key):
                    return ({k: z[k] for k in ('U', 'VR', 'VG', 'VB', 'G')},
                            tuple(int(x) for x in z['size']), tuple(z['area']))
            except (OSError, KeyError, ValueError):
                pass
        x0, x1, y0, y1 = self.output_area(area)
        ow, oh = int((x1 - x0) / MM_PER_PX), int((y1 - y0) / MM_PER_PX)
        C = COARSE
        cw, ch = -(-ow // C) + 2, -(-oh // C) + 2
        X, Y = np.meshgrid(x0 + ((np.arange(-1, cw - 1) + 0.5) * C - 0.5) * MM_PER_PX,
                           y0 + ((np.arange(-1, ch - 1) + 0.5) * C - 0.5) * MM_PER_PX)
        U, V = self.desk_to_raw(X, Y)
        D = self.dv(V)
        maps = {k: a.astype(np.float32) for k, a in
                dict(U=U, VR=V + D, VG=V, VB=V - D, G=self.flat_gain(U, V)).items()}
        try:
            os.makedirs(paths.CACHE_DIR, exist_ok=True)
            np.savez(cache, key=key, size=np.array([ow, oh]), area=np.array([x0, x1, y0, y1]), **maps)
        except OSError:
            pass                                    # no writable cache: just don't cache
        return maps, (ow, oh), (x0, x1, y0, y1)


STRIP = 256          # output rows per block (multiple of COARSE)


def apply_colour(rgb, cal):
    """Colour calibration on an 8-bit RGB image (any size): per-channel tone curve to linear
    light, 3x3 matrix, sRGB encoding. Returns a new uint8 image; unchanged if not calibrated."""
    if cal.colour is None:
        return rgb
    lut, M, enc = cal.colour
    lin = cv2.LUT(np.ascontiguousarray(rgb), lut)                          # float32, linear
    lin = cv2.transform(lin, M)
    idx = np.clip(lin * (COLOUR_STEPS - 1), 0, COLOUR_STEPS - 1).astype(np.uint16)
    return enc[idx]


def unapply_colour(rgb, cal):
    """Inverse of apply_colour (up to clipping): back to the scanner's own colours. Object
    detection was tuned on those, so it runs on an uncorrected copy (see crop.find_objects)."""
    if cal.colour is None:
        return rgb
    lut, M, enc = cal.colour
    v = np.asarray(rgb, np.float32) / 255
    lin = np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4).astype(np.float32)
    lin = cv2.transform(lin, np.linalg.inv(M).astype(np.float32))
    out = np.empty(rgb.shape, np.uint8)
    xs = np.arange(256, dtype=np.float32)
    for c in range(3):
        curve = lut[0, :, c]                                      # increasing: value -> linear
        out[..., c] = np.clip(np.round(np.interp(lin[..., c], curve, xs)), 0, 255)
    return out


def correct(rgb, cal=None, area='inscribed', flat=True, colour=True, detect_copy=False):
    """rgb: HxWx3 uint8 raw scan (R, G, B). Returns the corrected uint8 RGB image.

    detect_copy=True also returns a 1/4-size copy *before* the colour calibration (object
    detection is tuned on the scanner's own colours): (image, small).

    Works in horizontal strips so the dense maps stay small and cache-friendly
    (allocating full-size float maps cost more than the remapping itself).
    """
    cal = cal or Calibration()
    if rgb.shape[:2] != (cal.H, cal.W):
        raise ValueError(f'raw image must be {cal.W}x{cal.H}, got {rgb.shape[1]}x{rgb.shape[0]}')
    cm, (ow, oh), _ = cal.coarse_maps(area)
    C = COARSE
    cw = cm['U'].shape[1]
    src = cv2.split(rgb)                            # R, G, B planes
    out = np.empty((oh, ow, 3), np.uint8)
    flip = cal.rotation == 180
    small = np.empty((oh // 4, ow // 4, 3), np.uint8) if detect_copy else None
    # 'inscribed' lies inside the frame, except that R/B are read up to ~15 lines beyond
    # the last line at the far edge: repeat edge pixels there instead of showing black.
    border = cv2.BORDER_REPLICATE if area == 'inscribed' else cv2.BORDER_CONSTANT
    for o0 in range(0, oh, STRIP):
        o1 = min(o0 + STRIP, oh)
        # coarse rows needed for output rows [o0, o1) (big-map rows o0+C .. o1+C)
        a = (o0 + C) // C - 1
        b = min(-(-(o1 + C) // C) + 1, cm['U'].shape[0] - 1)
        r0 = o0 + C - a * C
        dense = {}
        for k, arr in cm.items():
            if k == 'G' and not flat:
                continue
            blk = cv2.resize(arr[a:b + 1], (cw * C, (b - a + 1) * C), interpolation=cv2.INTER_LINEAR)
            dense[k] = blk[r0:r0 + (o1 - o0), C:C + ow]
        planes = []
        for plane, vk in zip(src, ('VR', 'VG', 'VB')):  # R at v+dv, G at v, B at v-dv
            p = cv2.remap(plane, dense['U'], dense[vk], cv2.INTER_LINEAR, borderMode=border, borderValue=0)
            if flat:
                p = cv2.multiply(p, dense['G'], dtype=cv2.CV_8U)
            planes.append(p)
        strip = cv2.merge(planes)
        if small is not None:               # STRIP is a multiple of 4: same as resizing it all
            s4 = cv2.resize(strip[:(o1 - o0) // 4 * 4, :ow // 4 * 4], (ow // 4, (o1 - o0) // 4),
                            interpolation=cv2.INTER_AREA)
            if flip:
                small[oh // 4 - o0 // 4 - s4.shape[0]:oh // 4 - o0 // 4] = cv2.flip(s4, -1)
            else:
                small[o0 // 4:o0 // 4 + s4.shape[0]] = s4
        if colour:
            strip = apply_colour(strip, cal)
        if flip:
            out[oh - o1:oh - o0] = cv2.flip(strip, -1)
        else:
            out[o0:o1] = strip
    return (out, small) if detect_copy else out
