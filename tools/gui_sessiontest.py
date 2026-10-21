#!/usr/bin/env python3
"""Headless test: undo/redo, session pages, save all, continuous mode (fake scanner).

usage: QT_QPA_PLATFORM=offscreen XDG_CONFIG_HOME=/tmp/c XDG_CACHE_HOME=/tmp/k \
       python tools/gui_sessiontest.py dev/books/books_1.jpg dev/desk_c.png OUT_DIR
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np                                     # noqa: E402
from PIL import Image                                  # noqa: E402
from PySide6 import QtCore, QtTest, QtWidgets          # noqa: E402
from PySide6.QtCore import QPointF, Qt                 # noqa: E402

from sv600 import gui, settings                        # noqa: E402

img1, img2, out = sys.argv[1], sys.argv[2], os.path.abspath(sys.argv[3])
cfg = settings.load()
cfg['output']['folder'] = os.path.join(out, 'saved')
cfg['continuous'].update(scanner_button=False, timer_s=0.5, auto_save=True)
settings.save(cfg)

app = QtWidgets.QApplication(sys.argv)
w = gui.MainWindow()
w.resize(1500, 900)
w.show()
ok = True


def check(name, cond, info=''):
    global ok
    ok &= bool(cond)
    print(f'{"PASS" if cond else "FAIL"}  {name}  {info}')


def pump(t=0.2, until=None, timeout=60):
    end = time.time() + t
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 20)
        busy = w.worker is not None or w.thumb_timer.isActive()
        if until is None and time.time() > end and not busy:
            return
        if until is not None and until():
            return


def boxes():
    return w.snapshot()[1]


def load(path):
    w.run_worker(gui.load_job, (path, w.cfg), w.loaded, 'Opening')
    pump(0.3)


# ---------------------------------------------------------------- undo / redo
load(img1)
p1 = w.page
detected = boxes()
check('page 1 detected 2 boxes, history = [new, detect]', len(detected) == 2 and p1.pos == 1, f'pos {p1.pos}')
vp = w.view.viewport()
b = w.boxes[1]
a, c = w.view.mapFromScene(b.pos()), w.view.mapFromScene(b.pos() + QPointF(150, 50))
QtTest.QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, a)
from PySide6 import QtGui                              # noqa: E402
for i in range(1, 6):
    p = a + (c - a) * (i / 5)
    app.sendEvent(vp, QtGui.QMouseEvent(QtCore.QEvent.Type.MouseMove, QPointF(p), QPointF(vp.mapToGlobal(p)),
                                        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
QtTest.QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c)
pump()
after_move = boxes()
check('drag = one undo step', p1.pos == 2 and after_move != detected)
QtTest.QTest.keyClick(w, Qt.Key.Key_R)                 # turn selected (box 2)
pump()
after_turn = boxes()
for _ in range(3):                                     # three quick nudges -> one step
    QtTest.QTest.keyClick(w, Qt.Key.Key_Left)
pump()
after_nudge = boxes()
check('3 quick nudges merged into one step', p1.pos == 4, f'pos {p1.pos}')
w.scene.clearSelection()
w.boxes[0].setSelected(True)
QtTest.QTest.keyClick(w, Qt.Key.Key_Delete)
pump()
after_delete = boxes()
check('delete = one step', p1.pos == 5 and len(after_delete) == 1)
states = [None, detected, after_move, after_turn, after_nudge, after_delete]
good = True
for k in range(5, 0, -1):
    QtTest.QTest.keyClick(w, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    pump()
    good &= boxes() == (states[k - 1][1] if False else (states[k - 1] if states[k - 1] is not None else ()))
check('Ctrl+Z steps back through delete, nudge, turn, move, detect', good and boxes() == () and p1.pos == 0,
      f'boxes now {len(boxes())}')
check('undo at the start does nothing', (w.undo() or True) and p1.pos == 0)
for k in range(1, 6):
    QtTest.QTest.keyClick(w, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    pump()
check('Ctrl+Shift+Z redoes everything exactly', boxes() == after_delete and p1.pos == 5)
w.undo()
pump()
w.add_box_from_rect(QtCore.QRectF(100, 100, 400, 300))
pump()
check('a new edit after undo drops the redo branch', p1.pos == len(p1.history) - 1 and len(boxes()) == 3)
r0 = w.work.r
w.work.set_rect(QtCore.QRectF(2450, 0, r0.width() - 2450, r0.height()))
w.work_area_changed()
w.detect()
pump()
check('work area + detect -> 1 box', len(boxes()) == 1)
w.undo()
pump()
w.undo()
pump()
check('undo detect and work area change', len(boxes()) == 3 and abs(w.work.r.left() - r0.left()) < 1)

# ---------------------------------------------------------------- session
load(img2)
p2 = w.page
check('second scan becomes page 2, page 1 parked on disk', len(w.pages) == 2 and p1._image is None
      and os.path.exists(p1.park_path))
w.session.setCurrentRow(0)
pump()
check('switch back to page 1 keeps its boxes', w.page is p1 and len(boxes()) == 3)
w.save_all()
pump(0.5)
saved = sorted(os.listdir(os.path.join(out, 'saved')))
check('save all saved both pages', not any(p.dirty for p in w.pages) and len(saved) >= 5, f'{saved}')
check('saved page that is not shown no longer parked', not os.path.exists(p2.park_path))
w.boxes[0].setSelected(True)
w.delete_selected()
w.save()
pump(0.5)
saved2 = sorted(os.listdir(os.path.join(out, 'saved')))
gone = sorted(set(saved) - set(saved2))
check('re-save overwrites same name and removes the stale cut-out', len(gone) == 1 and gone[0].endswith('_3.jpg'),
      f'removed {gone}')

# ---------------------------------------------------------------- continuous mode (fake scanner)
frames = [np.asarray(Image.open(p).convert('RGB')) for p in (img1, img2, img1)]
calls = []


def fake_scan(progress, cfg):
    progress.step('Scanning (fake)')
    calls.append(time.time())
    return frames[(len(calls) - 1) % len(frames)].copy(), None


gui.scan_job = fake_scan
n_pages = len(w.pages)
w.a_cont.setChecked(True)
w.scan()
pump(until=lambda: len(calls) >= 3 and w.worker is None and w.countdown.isActive(), timeout=120)
QtTest.QTest.keyClick(w, Qt.Key.Key_Escape)
pump(1.0)
gaps = [round(b - a, 2) for a, b in zip(calls, calls[1:])]
check('continuous: 3 scans, each auto-saved, timer between them',
      len(w.pages) == n_pages + 3 and all(not p.dirty for p in w.pages[-3:]) and all(g >= 0.5 for g in gaps),
      f'gaps {gaps} s')
check('Esc stops continuous mode', not w.a_cont.isChecked() and not w.countdown.isActive() and len(calls) == 3)
w.grab().save(os.path.join(out, 'session.png'))
print('files:', sorted(os.listdir(os.path.join(out, 'saved'))))
w.session_modified = False         # no "Save session?" question in a headless test
w.close()
check('closing removes the session cache', not os.path.exists(w.session_dir))
print('ALL PASS' if ok else 'SOME FAILED')
