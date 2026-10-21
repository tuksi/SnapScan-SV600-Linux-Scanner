"""Session files (.sv600session): every scan of a GUI session with its boxes and undo history.

A session file is a ZIP (stored, not re-compressed: the images are PNG already) containing
  session.json        format version, pages (labels, prefixes, saved state, undo history, ...)
  pageNNNN.png        the corrected scan, lossless
  pageNNNN_det.png    optional 1/4-size copy in the scanner's own colours (object detection)

Images are encoded once into the session cache folder (png_path) and only copied into the ZIP
on every save, so saving a session again is fast. Writing goes to a temporary file that
replaces the old one only when complete.
"""
import json
import os
import time
import zipfile

import cv2
import numpy as np

FORMAT = 'sv600-session'
VERSION = 1
EXT = '.sv600session'


def encode_png(rgb, path):
    """Lossless PNG (fast zlib level + RLE, as for scans)."""
    if not cv2.imwrite(path, np.ascontiguousarray(rgb[:, :, ::-1]),
                       [cv2.IMWRITE_PNG_COMPRESSION, 1, cv2.IMWRITE_PNG_STRATEGY, cv2.IMWRITE_PNG_STRATEGY_RLE]):
        raise OSError(f'could not write {path}')


def read_png(path):
    bgr = cv2.imread(path, cv2.IMREAD_COLOR)
    if bgr is None:
        raise OSError(f'could not read {path}')
    return np.ascontiguousarray(bgr[:, :, ::-1])


def _tuples(x):
    """JSON lists back to the tuples the undo snapshots are compared as."""
    return tuple(_tuples(v) for v in x) if isinstance(x, list) else x


def snapshot_from_json(s):
    area, boxes, sel = s
    return (tuple(area), tuple(tuple(b) for b in boxes), tuple(sel))


def save(path, pages, current, progress=None):
    """pages: list of dicts with 'meta' (JSON-able), 'png' (path of the encoded scan) and
    optional 'det_png'. Writes path atomically."""
    tmp = path + '.part'
    meta = {'format': FORMAT, 'version': VERSION, 'saved': time.strftime('%Y-%m-%d %H:%M:%S'),
            'current': current, 'pages': []}
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_STORED, allowZip64=True) as z:
        for i, p in enumerate(pages):
            if progress:
                progress(i, len(pages))
            name = f'page{i + 1:04d}'
            m = dict(p['meta'], image=name + '.png')
            z.write(p['png'], name + '.png')
            if p.get('det_png'):
                z.write(p['det_png'], name + '_det.png')
                m['det_image'] = name + '_det.png'
            meta['pages'].append(m)
        z.writestr('session.json', json.dumps(meta, indent=1))
    os.replace(tmp, path)


def load(path, cache_dir, progress=None):
    """Returns (pages, current): pages = list of dicts with 'meta', 'png' (extracted into
    cache_dir) and optional 'det' (numpy array)."""
    with zipfile.ZipFile(path) as z:
        meta = json.loads(z.read('session.json'))
        if meta.get('format') != FORMAT:
            raise ValueError(f'{path} is not an SV600 session file')
        if meta.get('version', 0) > VERSION:
            raise ValueError(f'{path} was written by a newer version of this program')
        out = []
        stamp = int(time.time() * 1000)
        for i, m in enumerate(meta['pages']):
            if progress:
                progress(i, len(meta['pages']))
            png = os.path.join(cache_dir, f'loaded{stamp}_{i + 1:04d}.png')
            with z.open(m['image']) as src, open(png, 'wb') as dst:
                while chunk := src.read(1 << 22):
                    dst.write(chunk)
            p = {'meta': m, 'png': png, 'det': None}
            if m.get('det_image'):
                buf = np.frombuffer(z.read(m['det_image']), np.uint8)
                p['det'] = np.ascontiguousarray(cv2.imdecode(buf, cv2.IMREAD_COLOR)[:, :, ::-1])
            out.append(p)
    return out, meta.get('current', 0)
