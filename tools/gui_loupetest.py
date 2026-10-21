#!/usr/bin/env python3
"""Headless test of the magnifier: shows while dragging corners/edges/rubber band, hides after."""
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6 import QtCore, QtGui, QtTest, QtWidgets   # noqa: E402
from PySide6.QtCore import QPointF, Qt                 # noqa: E402

from sv600 import gui                                  # noqa: E402

app = QtWidgets.QApplication(sys.argv)
w = gui.MainWindow()
w.resize(1500, 900)
w.show()
out = sys.argv[2]
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
vp = w.view.viewport()
L = w.view.loupe


def move(p, buttons=Qt.MouseButton.LeftButton):
    app.sendEvent(vp, QtGui.QMouseEvent(QtCore.QEvent.Type.MouseMove, QPointF(p), QPointF(vp.mapToGlobal(p)),
                                        Qt.MouseButton.NoButton, buttons, Qt.KeyboardModifier.NoModifier))
    app.processEvents()


# 1. drag the top-left corner of box 2 (IKEA) and screenshot mid-drag
b = w.boxes[1]
b.setSelected(True)
pump(0.1)
cx, cy, bw, bh, ang = b.box()
corner = b.mapToScene(QtCore.QPointF(-bw / 2, -bh / 2))
start = w.view.mapFromScene(corner)
QtTest.QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
for i in range(1, 6):
    move(start + QtCore.QPoint(4 * i, 3 * i))
pump(0.05)
new_corner = b.mapToScene(b.handle_points(b.local_rect())['tl'])
check('loupe visible while dragging a corner', L.isVisible())
check('loupe centred on the corner being placed', (L.center - new_corner).manhattanLength() < 0.01,
      f'centre {L.center.x():.1f},{L.center.y():.1f}')
cur = w.view.mapFromScene(new_corner)
lg = L.geometry()
check('loupe does not cover the point', not lg.contains(cur), f'loupe {lg.getRect()}, point {cur.toTuple()}')
t = time.perf_counter()
for _ in range(20):
    L.repaint()
dt = (time.perf_counter() - t) / 20 * 1000
check('loupe paint fast enough for smooth dragging', dt < 30, f'{dt:.1f} ms per paint')
w.grab().save(os.path.join(out, 'loupe_corner.png'))
L.grab().save(os.path.join(out, 'loupe_only.png'))
QtTest.QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start + QtCore.QPoint(20, 15))
pump(0.1)
check('loupe hidden after release', not L.isVisible())

# 2. moving a whole box: no loupe
c = w.view.mapFromScene(b.pos())
QtTest.QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c)
move(c + QtCore.QPoint(10, 0))
check('no loupe when moving a whole box', not L.isVisible())
QtTest.QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, c + QtCore.QPoint(10, 0))
pump(0.1)

# 3. rubber band near the top-left of the window: loupe flips to the other side
p0 = w.view.mapFromScene(QPointF(120, 120))
QtTest.QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
move(p0 + QtCore.QPoint(30, 25))
cur = p0 + QtCore.QPoint(30, 25)
check('loupe while drawing a new box, placed beside the cursor', L.isVisible() and not L.geometry().contains(cur),
      f'loupe at {L.geometry().topLeft().toTuple()}, cursor {cur.toTuple()}')
QtTest.QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, cur)
pump(0.1)
check('hidden after drawing', not L.isVisible())

# 4. work area corner
wa = w.work.r
p = w.view.mapFromScene(wa.bottomRight())
QtTest.QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p)
move(p - QtCore.QPoint(15, 10))
check('loupe on work-area corner', L.isVisible() and (L.center - w.work.r.bottomRight()).manhattanLength() < 0.01)
QtTest.QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p - QtCore.QPoint(15, 10))
pump(0.1)

# 5. turned off in settings
w.cfg['gui']['loupe'] = False
w.apply_display_settings()
QtTest.QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0)
move(p0 + QtCore.QPoint(30, 25))
check('setting off -> no loupe', not L.isVisible())
QtTest.QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p0 + QtCore.QPoint(30, 25))
pump(0.1)
w.dirty = False
w.close()
print('ALL PASS' if ok else 'SOME FAILED')
