#!/usr/bin/env python3
"""Headless GUI self-test: load a scan, detect, edit boxes, save, screenshot.

usage: QT_QPA_PLATFORM=offscreen XDG_CONFIG_HOME=/tmp/x XDG_CACHE_HOME=/tmp/y \
       python tools/gui_selftest.py dev/books/books_1.jpg OUT_DIR
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image                                   # noqa: E402
from PySide6 import QtCore, QtWidgets                   # noqa: E402

from sv600 import gui, settings                         # noqa: E402

src, out = sys.argv[1], os.path.abspath(sys.argv[2])
os.makedirs(out, exist_ok=True)
cfg = settings.load()
cfg['output']['folder'] = os.path.join(out, 'saved')
settings.save(cfg)

app = QtWidgets.QApplication(sys.argv)
w = gui.MainWindow()
w.resize(1500, 900)
w.show()


def wait(timeout=60):
    t = time.time()
    while (w.worker is not None or w.thumb_timer.isActive()) and time.time() - t < timeout:
        app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 50)
    for _ in range(20):
        app.processEvents()


def shot(name):
    wait()
    w.grab().save(os.path.join(out, name))
    print('screenshot', name)


shot('0_start.png')
w.run_worker(gui.load_job, (src, w.cfg), w.loaded, 'Opening')
wait()      # load
wait()      # detection started by loaded()
print('boxes after detection:', [(b.number, [round(v) for v in b.box()]) for b in w.boxes])
shot('1_detected.png')

# edit: turn box 1 right, move box 2 a bit, draw a new box, delete it again, draw another
w.boxes[0].setSelected(True)
w.turn(1)
b2 = w.boxes[1]
b2.setPos(b2.pos() + QtCore.QPointF(30, 0))
b2.changed.emit()
w.scene.clearSelection()
w.add_box_from_rect(QtCore.QRectF(200, 3600, 600, 500))
print('after adding:', len(w.boxes), 'boxes; selected:', [b.number for b in w.selected_boxes()])
w.delete_selected()
w.add_box_from_rect(QtCore.QRectF(150, 150, 700, 400))
w.scene.clearSelection()
w.boxes[0].setSelected(True)
shot('2_edited.png')

w.save()
wait()
files = sorted(os.listdir(os.path.join(out, 'saved')))
print('saved files:', files)
for f in files:
    im = Image.open(os.path.join(out, 'saved', f))
    print(f'   {f}: {im.size}')
shot('3_saved.png')

d = gui.SettingsDialog(w.cfg, w)
d.show()
for _ in range(10):
    app.processEvents()
d.grab().save(os.path.join(out, '4_settings.png'))
print('screenshot 4_settings.png')
d.close()
w.dirty = False
w.close()
