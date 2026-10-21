#!/usr/bin/env python3
"""Headless test of the GUI mouse handling with synthetic events (QTest)."""
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6 import QtCore, QtTest, QtWidgets          # noqa: E402
from PySide6.QtCore import QPointF, Qt                 # noqa: E402

from sv600 import gui                                  # noqa: E402

app = QtWidgets.QApplication(sys.argv)
w = gui.MainWindow()
w.resize(1500, 900)
w.show()


def pump(t=0.3):
    end = time.time() + t
    while time.time() < end or w.worker is not None or w.thumb_timer.isActive():
        app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 20)


w.run_worker(gui.load_job, (sys.argv[1], w.cfg), w.loaded, 'Opening')
pump(1)
vp = w.view.viewport()
to_view = lambda p: w.view.mapFromScene(p)


def drag(a, b, button=Qt.MouseButton.LeftButton, steps=8):
    a, b = to_view(a), to_view(b)
    QtTest.QTest.mousePress(vp, button, Qt.KeyboardModifier.NoModifier, a)
    for i in range(1, steps + 1):
        p = a + (b - a) * (i / steps)
        ev = QtGui.QMouseEvent(QtCore.QEvent.Type.MouseMove, QPointF(p), QPointF(vp.mapToGlobal(p)),
                               Qt.MouseButton.NoButton, button, Qt.KeyboardModifier.NoModifier)
        app.sendEvent(vp, ev)
    QtTest.QTest.mouseRelease(vp, button, Qt.KeyboardModifier.NoModifier, b)
    pump(0.2)


from PySide6 import QtGui                              # noqa: E402
ok = True


def check(name, cond, info=''):
    global ok
    ok &= bool(cond)
    print(f'{"PASS" if cond else "FAIL"}  {name}  {info}')


b = w.boxes[1]                                          # IKEA, angle ~2.5
cx, cy, bw, bh, ang = b.box()
# 1. click inside selects, drag inside moves
drag(QPointF(cx, cy), QPointF(cx + 200, cy + 100))
nx, ny, *_ = b.box()
check('move by dragging inside', b.isSelected() and abs(nx - cx - 200) < 3 and abs(ny - cy - 100) < 3,
      f'moved by ({nx - cx:.0f}, {ny - cy:.0f})')
# 2. drag the bottom-right corner handle: size grows, opposite (top-left) corner stays put
cx, cy, bw, bh, ang = b.box()
t = math.radians(ang)
corner = lambda u, v: QPointF(cx + u * math.cos(t) - v * math.sin(t), cy + u * math.sin(t) + v * math.cos(t))
tl_before = corner(-bw / 2, -bh / 2)
br = corner(bw / 2, bh / 2)
drag(br, br + QPointF(150, 80))
cx2, cy2, bw2, bh2, ang2 = b.box()
t2 = math.radians(ang2)
tl_after = QPointF(cx2 - bw2 / 2 * math.cos(t2) + bh2 / 2 * math.sin(t2), cy2 - bw2 / 2 * math.sin(t2) - bh2 / 2 * math.cos(t2))
check('resize from corner', bw2 > bw + 100 and bh2 > bh + 50 and (tl_after - tl_before).manhattanLength() < 4,
      f'size {bw:.0f}x{bh:.0f} -> {bw2:.0f}x{bh2:.0f}, opposite corner moved {(tl_after - tl_before).manhattanLength():.1f} px')
# 3. drag the left edge handle: only the width changes
cx, cy, bw, bh, ang = b.box()
t = math.radians(ang)
left_mid = QPointF(cx - bw / 2 * math.cos(t), cy - bw / 2 * math.sin(t))
drag(left_mid, left_mid + QPointF(-120, 0))
_, _, bw3, bh3, _ = b.box()
check('resize from edge', bw3 > bw + 100 and abs(bh3 - bh) < 1, f'{bw:.0f}x{bh:.0f} -> {bw3:.0f}x{bh3:.0f}')
# 4. rotate handle: drag it to the right of the centre -> angle ~ +90; with Shift it snaps to 15 deg
cx, cy, bw, bh, ang = b.box()
t = math.radians(ang)
s = w.view.transform().m11()
rh_local = -bh / 2 - gui.ROT_HANDLE_PX / s
rh = QPointF(cx - rh_local * math.sin(t), cy + rh_local * math.cos(t))
drag(rh, QPointF(cx + 900, cy + 40))
check('rotate with handle', 85 < b.rotation() < 95, f'angle {ang:.1f} -> {b.rotation():.1f}')
# 5. draw a new box on empty desk (top right corner area)
n = len(w.boxes)
drag(QPointF(4900, 200), QPointF(5500, 700))
check('draw new box on empty space', len(w.boxes) == n + 1,
      f'{n} -> {len(w.boxes)} boxes, new {[round(v) for v in w.selected_boxes()[0].box()] if w.selected_boxes() else None}')
# 6. tiny click on empty space: deselects, no box
n = len(w.boxes)
hit = [b for b in w.boxes if b.contains(b.mapFromScene(QPointF(300, 4250)))]
print("      (boxes under the click point:", len(hit), ")")
drag(QPointF(300, 4250), QPointF(302, 4251))
check('click on empty space only deselects', len(w.boxes) == n and not w.selected_boxes())
# 7. keyboard: R turns the selected box, Delete removes it
w.boxes[0].setSelected(True)
QtTest.QTest.keyClick(w, Qt.Key.Key_R)
QtTest.QTest.keyClick(w, Qt.Key.Key_R, Qt.KeyboardModifier.ShiftModifier)
QtTest.QTest.keyClick(w, Qt.Key.Key_R, Qt.KeyboardModifier.ShiftModifier)
check('R / Shift+R turn', w.boxes[0].quarter == 3, f'quarter {w.boxes[0].quarter}')
n = len(w.boxes)
QtTest.QTest.keyClick(w, Qt.Key.Key_Delete)
check('Delete removes', len(w.boxes) == n - 1)
# 8. work area: drag its left edge inwards, then Detect only finds the IKEA
wa = w.work.r
mid_left = QPointF(wa.left() + 1, wa.center().y())
drag(mid_left, QPointF(2500, wa.center().y()))
check('work area edge drag', abs(w.work.r.left() - 2500) < 10, f'left edge -> {w.work.r.left():.0f}')
w.detect()
pump(0.5)
check('detect inside work area', len(w.boxes) == 1 and w.boxes[0].pos().x() > 2500,
      f'{len(w.boxes)} box(es) at x={[round(b.pos().x()) for b in w.boxes]}')
check('work area remembered', gui.load_state().get('work_area', {}).get('rect', [0])[0] > 2400)
w.grab().save(sys.argv[2])
w.dirty = False
w.close()
print('ALL PASS' if ok else 'SOME FAILED')
