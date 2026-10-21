#!/usr/bin/env python3
"""Headless test of the object preview: open by double-click, rotate, name, step, save names."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image                                  # noqa: E402
from PySide6 import QtCore, QtTest, QtWidgets          # noqa: E402
from PySide6.QtCore import Qt                          # noqa: E402

from sv600 import gui, settings                        # noqa: E402

out = os.path.abspath(sys.argv[2])
cfg = settings.load()
cfg['output']['folder'] = os.path.join(out, 'saved')
cfg['continuous']['scanner_button'] = False
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


def pump(t=0.3):
    end = time.time() + t
    while time.time() < end or w.worker is not None or w.thumb_timer.isActive():
        app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 20)


w.run_worker(gui.load_job, (sys.argv[1], w.cfg), w.loaded, 'Opening')
pump(1)
dlg = {}


def drive():
    d = next(x for x in app.topLevelWidgets() if isinstance(x, gui.ObjectPreview) and x.isVisible())
    dlg['d'] = d
    b0 = d.box
    check('double-click on a box opens its preview', b0 is w.boxes[0], f'object {b0.number}')
    shape0 = (d.pix.width(), d.pix.height())
    check('the image, not the name field, has the keyboard focus on open', d.view.hasFocus())
    QtTest.QTest.keyClick(d.focusWidget() or d, Qt.Key.Key_R)
    app.processEvents()
    check('R in the preview turns the saved image', b0.quarter == 1 and (d.pix.width(), d.pix.height()) == shape0[::-1],
          f'{shape0} -> {(d.pix.width(), d.pix.height())}')
    d.grab().save(os.path.join(out, 'preview.png'))
    d.name.setFocus()
    QtTest.QTest.keyClicks(d.name, 'Prva oseba, mno')      # QTest can only type ASCII keys
    d.name.insert('ž')
    QtTest.QTest.keyClicks(d.name, 'ine! rRr')
    for _ in range(4):
        QtTest.QTest.keyClick(d.name, Qt.Key.Key_Backspace)
    QtTest.QTest.keyClick(d.name, Qt.Key.Key_Return)
    app.processEvents()
    check('typing "r" in the name field does not rotate', b0.quarter == 1, f'name {b0.name!r}')
    d.next_b.click()
    app.processEvents()
    check('Next goes to object 2', d.box is w.boxes[1])
    QtTest.QTest.keyClick(d.focusWidget() or d, Qt.Key.Key_R, Qt.KeyboardModifier.ShiftModifier)
    app.processEvents()
    check('Shift+R turns object 2 the other way', w.boxes[1].quarter == 3)
    before = (d.pix.width(), d.pix.height())
    d.height.setValue(4.0)
    d.height.editingFinished.emit()
    app.processEvents()
    k = (329.547486577669 - 40) / 329.547486577669
    after = (d.pix.width(), d.pix.height())
    check('height 40 mm shrinks object 2 to true size', abs(after[0] / before[0] - k) < 0.002
          and w.boxes[1].height_mm == 40, f'{before} -> {after} (expected x{k:.4f})')
    check('preview says it is raised', 'raised 4' in d.info.text(), d.info.text())
    dlg['k'], dlg['size2'] = k, after
    d.accept()


b = w.boxes[0]
QtCore.QTimer.singleShot(300, drive)
QtTest.QTest.mouseDClick(w.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         w.view.mapFromScene(b.pos()))
pump(0.3)
check('thumbnail shows the name', 'Prva oseba, množine!' in w.thumbs.item(0).text())
w.undo()
pump()
check('undo reverts the height', w.boxes[1].height_mm == 0 and w.boxes[1].quarter == 3)
w.undo()
pump()
check('undo reverts the turn of object 2', w.boxes[1].quarter == 0 and w.boxes[0].quarter == 1)
w.redo()
w.redo()
pump()
w.save()
pump(0.5)
files = sorted(os.listdir(os.path.join(out, 'saved')))
check('saved with the typed name (made file-name safe)', 'Prva_oseba_množine.jpg' in files, f'{files}')
im = Image.open(os.path.join(out, 'saved', 'Prva_oseba_množine.jpg'))
check('saved image is turned (landscape)', im.width > im.height, f'{im.size}')
two = [f for f in files if f.startswith('scan_') and f.endswith('_2.jpg')]
im2 = Image.open(os.path.join(out, 'saved', two[0]))
check('saved raised object has the true-size dimensions', im2.size == dlg['size2'], f'{im2.size} vs {dlg["size2"]}')
# a name that already exists in the folder (from another scan) is not overwritten
w.run_worker(gui.load_job, (sys.argv[1], w.cfg), w.loaded, 'Opening')
pump(1)
w.boxes[0].name = 'Prva oseba množine'
w.commit('name')
w.save()
pump(0.5)
files2 = sorted(os.listdir(os.path.join(out, 'saved')))
check('same name in a later scan gets _2', 'Prva_oseba_množine_2.jpg' in files2, f'{sorted(set(files2) - set(files))}')
w.dirty = False
w.close()
print('ALL PASS' if ok else 'SOME FAILED')
