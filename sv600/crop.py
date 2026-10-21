"""Find flat objects (pages, books) on a corrected SV600 scan, straighten each one and
save it as its own image with a margin around it.

Detection runs on a reduced copy: edges (Canny on every colour channel) are closed into
outlines, filled, cleaned of thin things such as cables, and every remaining blob that is
large and roughly rectangular is taken as an object. Its minimum-area rectangle gives the
skew angle; the crop is cut from the full-resolution image with one affine warp.

Thresholds come from the [detection] and [split] settings (see data/settings.toml).
"""
import cv2
import numpy as np

from . import settings as sv600settings

DPI = 300
PX_PER_MM = DPI / 25.4
WORK_SCALE = 0.25            # detection at 75 dpi: 1 px = 0.34 mm


def desk_distance(lab):
    """Chroma distance (Lab a/b only) from the dominant desk colour.

    Lightness is ignored on purpose: lamp glare on the shiny desk near the scanner base
    is much brighter than the wood but nearly the same hue (glare ~7, paper ~30).

    The reference is the most common *clearly coloured* colour: neutral pixels (black, grey,
    white: a, b ~ 128) and very dark or bright ones are left out, otherwise a dark book plus
    the black frame edges can outnumber any single wood shade (seen on the books test scan).
    Returns (distance, reference Lab), or (None, None) if no coloured desk is found, e.g.
    on a black or grey mat; the caller then relies on outlines only.
    """
    L = lab[:, :, 0].ravel().astype(np.int16)
    a = lab[:, :, 1].ravel().astype(np.int16)
    b = lab[:, :, 2].ravel().astype(np.int16)
    use = (np.hypot(a - 128, b - 128) > 8) & (L > 25) & (L < 235)
    if use.mean() < 0.15:                      # less than 15 % coloured pixels: no wood desk
        return None, None
    hist, ae, be = np.histogram2d(a[use], b[use], bins=64, range=[[0, 256], [0, 256]])
    i, j = np.unravel_index(np.argmax(hist), hist.shape)
    sel = use & (np.abs(a - (ae[i] + 2)) < 6) & (np.abs(b - (be[j] + 2)) < 6)
    ref = np.median(lab.reshape(-1, 3)[sel], axis=0)
    d = lab.astype(np.float32) - ref
    return np.hypot(d[:, :, 1], d[:, :, 2]), ref


def _candidates(edges, colour, gap_mm, p, mm, debug=None):
    """One detection pass: outlines (gaps up to gap_mm bridged) + colour mask -> rect candidates."""
    k = lambda r: cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, k(max(1, int(gap_mm * mm))))
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    filled = np.zeros_like(closed)
    cv2.drawContours(filled, contours, -1, 255, cv2.FILLED)
    mask = cv2.morphologyEx(filled | colour, cv2.MORPH_CLOSE, k(int(p['merge_gap_mm'] * mm)))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    mask = np.zeros_like(mask)
    cv2.drawContours(mask, contours, -1, 255, cv2.FILLED)          # fill holes
    solid = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k(int(p['thin_object_mm'] * mm)))  # cables, grain
    contours, _ = cv2.findContours(solid, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    H, W = solid.shape
    edge_band = int(1.5 * mm)
    out = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        rect = cv2.minAreaRect(cnt)
        (cx, cy), (w, h), ang = rect
        if w * h == 0:
            continue
        fill = area / (w * h)
        x, y, bw, bh = cv2.boundingRect(cnt)
        cut = x <= edge_band or y <= edge_band or x + bw >= W - edge_band or y + bh >= H - edge_band
        ok = (area >= p['min_area_cm2'] * 100 * mm * mm and min(w, h) >= p['min_side_mm'] * mm
              and fill >= p['min_rectangularity'])
        if debug is not None:
            debug.append(((cx, cy), (w, h), ang, fill, area / (mm * mm), ok, cut))
        if ok:
            out.append((rect, area))
    return out, solid


def _inside(inner, outer):
    """Fraction of rect `inner` (minAreaRect) that lies inside rect `outer`."""
    a = cv2.boxPoints(inner).astype(np.float32)
    b = cv2.boxPoints(outer).astype(np.float32)
    area, _ = cv2.intersectConvexConvex(a, b)
    return area / max(inner[1][0] * inner[1][1], 1e-6)


def _find_objects(rgb, det=None, debug=None, prepare=None, small=None):
    """Return a list of cv2.minAreaRect tuples (centre, (w, h), angle) in full-res pixels.

    det: the [detection] settings (defaults from sv600settings.DEFAULTS).
    Two passes: the normal one, and one that bridges bigger gaps in outlines. A box lying inside
    a bigger box is replaced by the bigger one (e.g. the picture on a book cover -> the book),
    unless the boxes inside fill most of it (then the bigger one is two objects merged).
    """
    p = dict(sv600settings.DEFAULTS['detection'], **(det or {}))
    if small is None:
        small = cv2.resize(rgb, None, fx=WORK_SCALE, fy=WORK_SCALE, interpolation=cv2.INTER_AREA)
    if prepare is not None:            # e.g. undo the colour calibration: thresholds are tuned
        small = prepare(small)         # on the scanner's own colours
    mm = PX_PER_MM * WORK_SCALE                                   # small px per mm
    blur = cv2.GaussianBlur(small, (5, 5), 0)
    # cue 1: edges in every colour channel (closed and filled per pass)
    edges = np.zeros(small.shape[:2], np.uint8)
    for c in cv2.split(blur):
        edges |= cv2.Canny(c, p['edge_low'], p['edge_high'])
    # cue 2: anything clearly not desk-coloured (also works where an outline is open)
    lab = cv2.cvtColor(blur, cv2.COLOR_RGB2LAB)
    dist, _ = desk_distance(lab)
    if dist is None:                           # no coloured desk (black/grey mat): outlines only
        colour = np.zeros(lab.shape[:2], np.uint8)
    else:
        # dark AND colourless (scanner base, shadows, floor at the sides) is left to the edge cue,
        # so a page pushed against the scanner is not merged with the base; dark but coloured
        # (a deep purple / blue cover) still counts
        neutral = np.hypot(lab[:, :, 1].astype(np.float32) - 128, lab[:, :, 2].astype(np.float32) - 128)
        keep = (lab[:, :, 0] > p['dark_limit']) | (neutral > p['dark_colour_chroma'])
        colour = ((dist > p['desk_colour_threshold']) & keep).astype(np.uint8) * 255

    found, solid = _candidates(edges, colour, p['outline_gap_mm'], p, mm, debug)
    if p['outer_gap_mm'] > p['outline_gap_mm']:
        more, _ = _candidates(edges, colour, p['outer_gap_mm'], p, mm)
        found += [m for m in more if all(_inside(m[0], f[0]) < 0.95 or _inside(f[0], m[0]) < 0.95
                                         for f in found)]          # skip duplicates
    # prefer outer boxes
    keep = []
    for i, (r, a) in enumerate(found):
        inners = [f for j, f in enumerate(found) if j != i and _inside(f[0], r) > 0.9
                  and f[1] < a]
        outer = [f for j, f in enumerate(found) if j != i and _inside(r, f[0]) > 0.9 and f[1] > a]
        merged = sum(f[1] for f in inners) > p['outer_max_inner_fraction'] * a
        if merged:
            continue                                   # two+ objects merged: keep the inner ones
        if any(sum(g[1] for g in found if g is not o and _inside(g[0], o[0]) > 0.9 and g[1] < o[1])
               <= p['outer_max_inner_fraction'] * o[1] for o in outer):
            continue                                   # an outer box takes over
        keep.append(r)
    s = 1 / WORK_SCALE
    found = [((cx * s, cy * s), (w * s, h * s), ang) for (cx, cy), (w, h), ang in keep]
    # reading order: top to bottom, then left to right
    found.sort(key=lambda r: (round(r[0][1] / (p['row_height_mm'] * PX_PER_MM)), r[0][0]))
    return found, dict(edges=edges, colour=colour, solid=solid, small=small)


def find_objects(rgb, det=None, debug=None, roi=None, prepare=None, small=None):
    """Find objects; optionally only inside roi = (x0, y0, x1, y1) in full-res pixels.

    small: an already reduced (WORK_SCALE) copy to detect on instead of rgb, e.g. the copy
    sv600.correct made before the colour calibration. prepare: applied to the reduced copy.

    With a work area the detection simply runs on that part of the scan, so clutter outside
    it can neither be found nor merge with objects inside it.
    """
    if roi is None:
        return _find_objects(rgb, det, debug, prepare, small)
    H, W = rgb.shape[:2]
    x0, y0 = max(0, int(roi[0])), max(0, int(roi[1]))
    x1, y1 = min(W, int(roi[2])), min(H, int(roi[3]))
    if x1 - x0 < 50 or y1 - y0 < 50:
        return [], {}
    sub = None
    if small is not None:
        s0, s1 = int(round(x0 * WORK_SCALE)), int(round(y0 * WORK_SCALE))
        sub = np.ascontiguousarray(small[s1:int(round(y1 * WORK_SCALE)), s0:int(round(x1 * WORK_SCALE))])
        x0, y0 = s0 / WORK_SCALE, s1 / WORK_SCALE        # keep offsets on the reduced grid
    found, extra = _find_objects(np.ascontiguousarray(rgb[int(y0):y1, int(x0):x1]), det, debug, prepare, sub)
    return [((cx + x0, cy + y0), wh, ang) for (cx, cy), wh, ang in found], extra


def box_from_rect(rect, margin_mm=0.0):
    """cv2.minAreaRect -> crop box (cx, cy, w, h, angle) with the margin added.

    The angle is brought into (-45, 45] (no quarter turns, just straight); w/h follow it.
    Rotating the image by +angle (counter-clockwise, cv2 convention) makes the box upright,
    i.e. on screen the box is rotated by angle degrees clockwise.
    """
    (cx, cy), (w, h), ang = rect
    while ang > 45:
        ang -= 90
        w, h = h, w
    while ang <= -45:
        ang += 90
        w, h = h, w
    m = margin_mm * PX_PER_MM
    return cx, cy, w + 2 * m, h + 2 * m, ang


def height_scale(height_mm, head_height_mm):
    """Size factor for a flat object whose top is height_mm above the desk.

    The desk calibration maps everything onto the desk plane; a surface raised by z is closer
    to the head and appears enlarged by H / (H - z) about the point below the head. Scaling the
    cut-out by (H - z) / H restores its true size (exact for a single projection centre).
    """
    if not height_mm:
        return 1.0
    if not head_height_mm or height_mm >= 0.8 * head_height_mm:
        raise ValueError(f'object height {height_mm} mm is not possible with head height {head_height_mm} mm')
    return (head_height_mm - height_mm) / head_height_mm


def extract(rgb, box, quarter=0, border=(0, 0, 0), scale=1.0):
    """Cut out box = (cx, cy, w, h, angle) upright, resized by scale (height correction),
    then turn it by quarter * 90 deg clockwise. One resampling step."""
    cx, cy, w, h, ang = box
    ow, oh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    M = cv2.getRotationMatrix2D((cx, cy), ang, scale)
    M[0, 2] += ow / 2 - cx
    M[1, 2] += oh / 2 - cy
    out = cv2.warpAffine(rgb, M, (ow, oh), flags=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=border)
    q = quarter % 4
    if q:
        out = cv2.rotate(out, (cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_180, cv2.ROTATE_90_COUNTERCLOCKWISE)[q - 1])
    return out


def straighten(rgb, rect, margin_mm=10.0, border=(0, 0, 0), scale=1.0):
    """Cut out one detected object plus margin, rotated by the smallest angle that makes it
    axis-aligned, resized by scale (height correction). Returns (image, angle)."""
    box = box_from_rect(rect, margin_mm / scale)        # margin is true mm on the object plane
    return extract(rgb, box, 0, border, scale), box[4]


def draw_debug(rgb_small, cands, path):
    img = cv2.cvtColor(rgb_small, cv2.COLOR_RGB2BGR).copy()
    for (c, wh, ang, fill, area_mm2, ok, cut) in cands:
        if area_mm2 < 500:
            continue
        box = cv2.boxPoints((c, wh, ang)).astype(np.int32)
        col = (0, 255, 0) if ok else (0, 160, 255) if cut else (0, 0, 255)
        cv2.drawContours(img, [box], 0, col, 2)
        cv2.putText(img, f'{area_mm2 / 100:.0f}cm2 f{fill:.2f}{" cut" if cut else ""}',
                    (int(c[0]) - 40, int(c[1])),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, col, 1, cv2.LINE_AA)
    cv2.imwrite(path, img, [cv2.IMWRITE_JPEG_QUALITY, 85])
