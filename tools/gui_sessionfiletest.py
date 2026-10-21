#!/usr/bin/env python3
"""Headless test of session files: save, reopen in a new window, everything restored."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np                                     # noqa: E402
from PIL import Image                                  # noqa: E402
from PySide6 import QtCore, QtWidgets                  # noqa: E402

from sv600 import gui, settings, session              # noqa: E402

out = os.path.abspath(sys.argv[1])
cfg = settings.load()
cfg['output']['folder'] = os.path.join(out, 'saved')
cfg['continuous']['scanner_button'] = False
settings.save(cfg)
frames = {'books': np.asarray(Image.open('dev/books/books_1.jpg').convert('RGB')),
          'desk': np.asarray(Image.open('dev/desk_c.png').convert('RGB'))}
seq = ['books', 'desk', 'books']
calls = []


def fake_scan(progress, cfg):
    calls.append(1)
    img = frames[seq[(len(calls) - 1) % len(seq)]].copy()
    return img, img[::4, ::4].copy()                   # stands in for the detection copy


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
        if time.time() > end and not w.worker and not w.thumb_timer.isActive():
            return


w = gui.MainWindow()
w.show()
w.prefix_edit.setText('Knjiga')
w.scan_prefixed(); pump(w, 0.5)                        # page 1: prefix, will be edited + saved
w.boxes[0].setSelected(True); w.turn(1); pump(w)
w.boxes[1].name = 'IKEA katalog'; w.commit('name'); pump(w)
w.save(); pump(w, 0.5)
w.scan_plain(); pump(w, 0.5)                           # page 2: unsaved, with undo history
w.height_box.setValue(4.0); pump(w)
w.scan_plain(); pump(w, 0.5)                           # page 3: then removed again
w.session.setCurrentRow(2); pump(w)
w.pages[2].saved_boxes = w.pages[2].boxes_now(); w.remove_page(); pump(w)
w.session.setCurrentRow(1); pump(w)
check('title shows unsaved session', '*' in w.windowTitle(), w.windowTitle())
before = [(p.label, p.prefix, p.base, p.pos, len(p.history), p.boxes_now(), p.saved_boxes, p.dirty) for p in w.pages]
path = os.path.join(out, 'test' + session.EXT)
w.session_path = path
t = time.time(); w.save_session(); pump(w, 0.5)
t1 = time.time() - t
check('session file written', os.path.exists(path), f'{os.path.getsize(path) / 1e6:.0f} MB in {t1:.1f} s')
check('title clean after saving', '*' not in w.windowTitle(), w.windowTitle())
t = time.time(); w.save_session(); pump(w, 0.5)
check('saving again is quick (images already encoded)', time.time() - t < t1 / 2, f'{time.time() - t:.1f} s')
img_before = w.pages[0].image().copy()
w.dirty = False
w.close()

w2 = gui.MainWindow()
w2.show()
w2.open_session(path); pump(w2, 0.5)
after = [(p.label, p.prefix, p.base, p.pos, len(p.history), p.boxes_now(), p.saved_boxes, p.dirty) for p in w2.pages]
check('same pages, labels, prefixes, saved state, boxes and undo positions', after == before,
      '' if after == before else f'\n {before}\n {after}')
check('shown page restored (scan 2)', w2.page is w2.pages[1])
check('image restored losslessly', np.array_equal(w2.pages[0].image(), img_before))
check('detection copy restored', isinstance(w2.pages[0].hint, np.ndarray))
w2.session.setCurrentRow(0); pump(w2)
n = w2.page.pos
w2.undo(); pump(w2)
check('undo history works after reopening', w2.page.pos == n - 1)
w2.redo(); pump(w2)
w2.save(); pump(w2, 0.5)
saved = sorted(os.listdir(os.path.join(out, 'saved')))
check('re-saving an already saved scan overwrites its own files', saved.count('Knjiga_001.jpg') == 1
      and not any(f.startswith('Knjiga_002') for f in saved), f'{saved}')
check('session marked modified after a change', '*' in w2.windowTitle())
w2.dirty = False
w2.close()
print('ALL PASS' if ok else 'SOME FAILED')
