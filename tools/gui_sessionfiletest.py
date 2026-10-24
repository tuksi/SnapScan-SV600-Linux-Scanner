#!/usr/bin/env python3
"""Headless test of the kept session: scans are stored as they come, a new window offers to load
them with boxes, undo history and saved state; declining (after the warning) deletes them.

usage: tools/gui_sessionfiletest.py OUT_DIR    (config, session and saved files all go there)
"""
import os
import sys
import time

out = os.path.abspath(sys.argv[1])
os.environ['XDG_CONFIG_HOME'] = os.path.join(out, 'config')     # keep the user's settings and session
os.environ['XDG_DATA_HOME'] = os.path.join(out, 'data')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np                                     # noqa: E402
from PySide6 import QtCore, QtWidgets                  # noqa: E402

from sv600 import gui, paths, settings                 # noqa: E402

cfg = settings.load()
cfg['output']['folder'] = os.path.join(out, 'saved')
cfg['continuous']['scanner_button'] = False
settings.save(cfg)


def frame(seed):
    """A grey desk with two light 'pages' on it."""
    rng = np.random.default_rng(seed)
    img = np.full((1800, 2400, 3), 90, np.uint8) + rng.integers(0, 8, (1800, 2400, 3), dtype=np.uint8)
    img[300:1300, 200:1000] = (235, 230, 220)
    img[400:1500, 1300:2100] = (210, 225, 240)
    return img


def fake_scan(progress, cfg):
    img = frame(len(calls))
    calls.append(1)
    return img, img[::4, ::4].copy()                   # stands in for the detection copy


calls = []
gui.scan_job = fake_scan
app = QtWidgets.QApplication(sys.argv)
ok = True


def check(name, cond, info=''):
    global ok
    ok &= bool(cond)
    print(f'{"PASS" if cond else "FAIL"}  {name}  {info}')


def pump(w, t=0.3, timeout=120):
    end, t0 = time.time() + t, time.time()
    while time.time() - t0 < timeout:
        app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 20)
        if time.time() > end and not w.worker and not w.thumb_timer.isActive() and not w.meta_timer.isActive():
            return


def state(w):
    return [(p.label, p.prefix, p.base, p.pos, len(p.history), p.boxes_now(), p.saved_boxes, p.dirty)
            for p in w.pages]


w = gui.MainWindow()
w.show()
w.attach_session()
check('fresh start: kept session, nothing asked', w.persistent and not w.pages, w.windowTitle())
w.prefix_edit.setText('Knjiga')
w.scan_prefixed(); pump(w, 1)                          # page 1: prefix, edited + saved
check('boxes found', len(w.boxes) == 2, f'{len(w.boxes)}')
w.boxes[0].setSelected(True); w.turn(1); pump(w)
w.boxes[1].name = 'IKEA katalog'; w.commit('name'); pump(w)
w.save(); pump(w, 0.5)
w.scan_plain(); pump(w, 1)                             # page 2: unsaved, with undo history
w.height_box.setValue(4.0); pump(w)
w.scan_plain(); pump(w, 1)                             # page 3: then removed again
w.session.setCurrentRow(2); pump(w)
w.pages[2].saved_boxes = w.pages[2].boxes_now(); w.remove_page(); pump(w)
w.session.setCurrentRow(1); pump(w)
w.storer.submit(lambda: None).result(); pump(w)       # background storing finished
files = sorted(os.listdir(paths.SESSION_DIR))
check('scans stored in the session folder', files == ['lock', 'page0001.png', 'page0001_det.png',
      'page0001_small.jpg', 'page0002.png', 'page0002_det.png', 'page0002_small.jpg', 'session.json'], files)
before = state(w)
img_before = w.pages[0].image().copy()
w.close()                                              # no question: nothing is lost

asked = []
w2 = gui.MainWindow()
w2.show()
w2.ask_continue = lambda meta: asked.append((len(meta['pages']), gui.session.unsaved(meta))) or True
w2.attach_session(); pump(w2, 0.5)
check('asked to load the previous session', asked == [(2, 1)], f'{asked}')
after = state(w2)
check('same pages, labels, prefixes, saved state, boxes and undo positions', after == before,
      '' if after == before else f'\n {before}\n {after}')
check('shown page restored (scan 2)', w2.page is w2.pages[1])
check('image restored losslessly', np.array_equal(w2.pages[0].image(), img_before))
check('detection copy restored', isinstance(w2.pages[0].hint, np.ndarray))
w2.session.setCurrentRow(0); pump(w2)
n = w2.page.pos
w2.undo(); pump(w2)
check('undo history works after reloading', w2.page.pos == n - 1)
w2.redo(); pump(w2)
w2.save(); pump(w2, 0.5)
saved = sorted(os.listdir(os.path.join(out, 'saved')))
check('re-saving an already saved scan overwrites its own files', saved.count('Knjiga_001.jpg') == 1
      and not any(f.startswith('Knjiga_002') for f in saved), f'{saved}')
w2.scan_plain(); pump(w2, 1)
w2.close()

w3 = gui.MainWindow()
w3.show()
w3.ask_continue = lambda meta: asked.append(len(meta['pages'])) and False
w3.attach_session(); pump(w3, 0.5)
check('a scan made after reloading is kept too', asked[-1] == 3, f'{asked}')
check('declining starts an empty session and deletes the scans',
      not w3.pages and sorted(os.listdir(paths.SESSION_DIR)) == ['lock', 'session.json'],
      f'{sorted(os.listdir(paths.SESSION_DIR))}')
check('saved images are not affected', sorted(os.listdir(os.path.join(out, 'saved'))) == saved)
w3.scan_plain(); pump(w3, 1)
w3.confirm_delete = lambda *a: True
w3.new_session(); pump(w3, 0.5)
w3.storer.submit(lambda: None).result(); pump(w3)
check('New session empties the session', not w3.pages and
      sorted(os.listdir(paths.SESSION_DIR)) == ['lock', 'session.json'], f'{sorted(os.listdir(paths.SESSION_DIR))}')
QtWidgets.QMessageBox.warning = lambda *a, **k: None
w4 = gui.MainWindow()
w4.attach_session()
check('a second window gets a temporary session', not w4.persistent, w4.windowTitle())
w4.close()
w3.close()
print('ALL PASS' if ok else 'SOME FAILED')
