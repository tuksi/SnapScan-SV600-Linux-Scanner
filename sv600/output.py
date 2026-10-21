"""Saving images and choosing file names."""
import datetime
import os
import re
import string

DPI = 300
FORMATS = {'jpg': 'JPEG', 'jpeg': 'JPEG', 'png': 'PNG', 'tif': 'TIFF', 'tiff': 'TIFF',
           'pdf': 'PDF', 'webp': 'WebP'}


COLOUR_MARK = b'sv600: colour-calibrated'


def save_image(img, path, quality=100, png_compression=1, comment=None):
    """Save a PIL image by file extension. Measured on a full corrected scan: JPEG q90 0.15 s /
    ~4-6 MB, q100 0.3 s / ~17-20 MB, PDF ~0.2 s, PNG (level 1 + RLE) ~2.7 s / ~36-46 MB,
    TIFF 0.06 s / 74 MB."""
    fmt = FORMATS[path.rsplit('.', 1)[-1].lower()]
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if fmt == 'PNG':
        try:
            import cv2
            import numpy as np
        except ImportError:
            img.save(path, dpi=(DPI, DPI), compress_level=png_compression)   # slower fallback
            return
        # Fast zlib level + RLE strategy: faster *and* smaller than Pillow here.
        # (OpenCV does not write the pHYs/dpi chunk.)
        bgr = np.ascontiguousarray(np.asarray(img)[:, :, ::-1])
        if not cv2.imwrite(path, bgr, [cv2.IMWRITE_PNG_COMPRESSION, png_compression,
                                       cv2.IMWRITE_PNG_STRATEGY, cv2.IMWRITE_PNG_STRATEGY_RLE]):
            raise OSError(f'could not write {path}')
    elif fmt == 'JPEG':
        img.save(path, dpi=(DPI, DPI), quality=quality, **({'comment': comment} if comment else {}))
    elif fmt == 'WebP':
        img.save(path, dpi=(DPI, DPI), quality=quality)
    elif fmt == 'PDF':
        img.save(path, resolution=DPI, quality=quality)
    else:
        img.save(path, dpi=(DPI, DPI))


def _fields(when):
    return {'date': when.strftime('%Y-%m-%d'), 'time': when.strftime('%H%M%S')}


def next_base(folder, pattern, when=None):
    """Return folder/name (no extension) for the next scan.

    {date} and {time} are filled in; {n} (any format spec, e.g. {n:03}) becomes one more
    than the highest number already used by files in the folder that match the pattern,
    so earlier scans are never overwritten.
    """
    when = when or datetime.datetime.now()
    folder = os.path.expanduser(folder)
    f = _fields(when)
    parts = list(string.Formatter().parse(pattern))
    if not any(name == 'n' for _, name, _, _ in parts):
        name = pattern.format(**f)
        base = os.path.join(folder, name)
        if not _taken(folder, name):
            return base
        pattern, parts = pattern + '_{n}', list(string.Formatter().parse(pattern + '_{n}'))
    # regex for existing files: literal text and other fields escaped, {n} -> digits
    rx = ''
    for literal, name, spec, conv in parts:
        rx += re.escape(literal)
        if name == 'n':
            rx += r'(\d+)'
        elif name is not None:
            rx += re.escape(format(f[name], spec or ''))
    used = [0]
    if os.path.isdir(folder):
        for entry in os.listdir(folder):
            m = re.match(rx + r'(?:_\d+)?\.[A-Za-z]+$', entry)
            if m:
                used.append(int(m.group(1)))
    n = max(used) + 1
    while _taken(folder, pattern.format(n=n, **f)):
        n += 1
    return os.path.join(folder, pattern.format(n=n, **f))


def _taken(folder, name):
    if not os.path.isdir(folder):
        return False
    rx = re.compile(re.escape(name) + r'(?:_\d+)?\.[A-Za-z]+$')
    return any(rx.match(e) for e in os.listdir(folder))


def safe_name(text, ascii_only=False):
    """Turn free text (typed or OCR) into a file name stem: letters/digits kept, runs of
    anything else become '_'. ascii_only also folds accents (č -> c)."""
    import unicodedata
    text = unicodedata.normalize('NFKC', text or '').strip()
    if ascii_only:
        text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')
    out = re.sub(r'[^\w]+', '_', text, flags=re.UNICODE).strip('_')
    return out[:120]


def unique_path(folder, stem, ext, taken=(), mine=()):
    """folder/stem.ext, or stem_2, stem_3, ... if that file exists (unless it is one of `mine`,
    files this page wrote itself) or was already chosen in this save (`taken`)."""
    n = 1
    while True:
        path = os.path.join(folder, (stem if n == 1 else f'{stem}_{n}') + ext)
        if path not in taken and (path in mine or not os.path.exists(path)):
            return path
        n += 1
