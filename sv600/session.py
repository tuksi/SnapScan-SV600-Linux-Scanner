"""The GUI session: every scan with its boxes and undo history, kept in one folder so that it
survives closing the program (paths.SESSION_DIR, ~/.local/share/sv600-linux/session):

  session.json        format version, pages (labels, prefixes, saved state, undo history, ...)
  pageNNNN.png        the corrected scan, lossless (written once, in the background)
  pageNNNN_small.jpg  1/4-size copy for thumbnails, so a session opens without decoding scans
  pageNNNN_det.png    optional 1/4-size copy in the scanner's own colours (object detection)
  lock                held while a window uses the folder

session.json is small and rewritten after every scan and edit; the images are never copied.

Versioning: session.json holds the format VERSION and the program version that wrote it.
Raise VERSION whenever what is stored changes, and only ever add: new fields at the end of a box,
new keys in a page or at the top. An older program opens a newer session and keeps working
with it: it carries what it does not know through unchanged (extra box fields, unknown keys)
and writes the higher version back, so switching between versions loses nothing.
"""
import fcntl
import json
import os
import time

import cv2
import numpy as np

from . import __version__

FORMAT = 'sv600-session'
VERSION = 2              # of the session format; see the module docstring
BOX_FIELDS = 8           # cx, cy, w, h, angle, quarter, name, height (+ newer fields, carried along)
META = 'session.json'
LOCK = 'lock'


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


def snapshot_from_json(s):
    area, boxes, sel = s
    return (tuple(area), tuple(tuple(b) for b in boxes), tuple(sel))


def lock(folder):
    """Claim the session folder for this window. Returns an open file to keep (closing it
    releases the lock), or None if another window is using the folder."""
    os.makedirs(folder, exist_ok=True)
    f = open(os.path.join(folder, LOCK), 'w')
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    f.write(str(os.getpid()))
    f.flush()
    return f


def store_page(folder, number, rgb, small, det=None):
    """Write one scan into the session folder; each file appears complete or not at all.
    Returns the paths: {'png', 'small', 'det' (or None)}."""
    def put(name, write):
        path = os.path.join(folder, name)
        tmp = os.path.join(folder, 'tmp_' + name)
        write(tmp)
        os.replace(tmp, path)
        return path

    def jpg(img):
        def write(path):
            if not cv2.imwrite(path, np.ascontiguousarray(img[:, :, ::-1]), [cv2.IMWRITE_JPEG_QUALITY, 90]):
                raise OSError(f'could not write {path}')
        return write

    stem = f'page{number:04d}'
    out = {'small': put(stem + '_small.jpg', jpg(small)),
           'det': put(stem + '_det.png', lambda p: encode_png(det, p)) if det is not None else None}
    out['png'] = put(stem + '.png', lambda p: encode_png(rgb, p))
    return out


def write(folder, meta, version=VERSION):
    """Replace session.json (atomically). version: the format the content has, which is higher
    than VERSION if it carries fields of a newer program."""
    meta = dict(meta, format=FORMAT, version=max(version, VERSION), app=__version__,
                saved=time.strftime('%Y-%m-%d %H:%M:%S'))
    path = os.path.join(folder, META)
    with open(path + '.part', 'w') as f:
        json.dump(meta, f, indent=1)
    os.replace(path + '.part', path)


def read(folder):
    """session.json as a dict, or None if there is no session. Pages whose scan is missing
    are left out. A newer session is read too (see the module docstring): newer(meta) tells."""
    path = os.path.join(folder, META)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        meta = json.load(f)
    if meta.get('format') != FORMAT:
        raise ValueError(f'{path} is not an SV600 session')
    meta['pages'] = [m for m in meta.get('pages', []) if os.path.exists(os.path.join(folder, m['image']))]
    return meta


def newer(meta):
    """True if the session holds data of a newer program (a higher format, or boxes with more
    fields than this version knows, as written before versioning)."""
    return meta.get('version', 0) > VERSION or any(
        len(b) > BOX_FIELDS for m in meta['pages'] for snap in m.get('history', []) for b in snap[1])


def unsaved(meta):
    """Number of pages whose boxes changed since they were last saved as images."""
    n = 0
    for m in meta['pages']:
        boxes = snapshot_from_json(m['history'][m['pos']])[1] if m['pos'] >= 0 else ()
        sb = m.get('saved_boxes')
        n += sb is None or boxes != tuple(tuple(b) for b in sb)
    return n


def clean(folder, keep=()):
    """Delete everything in the folder except session.json, the lock and the files in keep
    (names), e.g. scans of an earlier session or left over after a crash."""
    keep = set(keep) | {META, LOCK}
    for name in os.listdir(folder):
        if name not in keep:
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass
