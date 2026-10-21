"""SV600 scanner GUI: scan, preview, work area, editable crop boxes, quarter turns, save.

Mouse: wheel = zoom, right/middle drag = pan, drag on empty space = new box,
drag inside a box = move, corner/edge handles = resize, round handle = rotate.
Keys: F5/Space scan (or the Scan button on the SV600), Ctrl+K continuous, Esc stop,
Ctrl+Z / Ctrl+Shift+Z undo / redo, D detect, R / Shift+R turn output 90 deg, Delete remove box,
[ ] fine rotate, arrows nudge, F fit, Ctrl+S save, Ctrl+Shift+S save all, Ctrl+O open,
Ctrl+, settings.
"""
import json
import math
import os
import re
import sys
import time
import traceback

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QPointF, QRectF, Qt, Signal

from . import URL, __version__, crop, output, paths, session, settings
from .output import DPI

HANDLE_PX = 9            # handle size on screen
ROT_HANDLE_PX = 34       # distance of the rotate handle above the box, on screen
THUMB = 220              # thumbnail size
BOX_COLOURS = ['#2ecc71', '#3498db', '#e67e22', '#9b59b6', '#e74c3c', '#1abc9c', '#f1c40f']


# ----------------------------------------------------------------------------- helpers
def to_qimage(rgb):
    """numpy HxWx3 uint8 -> QImage (copy, so the array can go away)."""
    h, w = rgb.shape[:2]
    rgb = np.ascontiguousarray(rgb)
    return QtGui.QImage(rgb.data, w, h, 3 * w, QtGui.QImage.Format.Format_RGB888).copy()


def cm(mm):
    """Height for display: mm -> '4,0 cm' (decimal separator from the system locale)."""
    return QtCore.QLocale().toString(float(mm) / 10, 'f', 1) + ' cm'


def make_height_box():
    """Height entry in cm, 0.1 cm steps (values are kept in mm internally)."""
    b = QtWidgets.QDoubleSpinBox()
    b.setRange(0, 20)
    b.setDecimals(1)
    b.setSingleStep(0.1)
    b.setSuffix(' cm')
    return b


_RENDER_SCALE = [None]   # set while the loupe renders the scene at its own magnification


def view_scale(item):
    if _RENDER_SCALE[0]:
        return _RENDER_SCALE[0]
    views = item.scene().views() if item.scene() else []
    return views[0].transform().m11() if views else 1.0


def load_state():
    try:
        with open(paths.STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    try:
        os.makedirs(paths.CONFIG_DIR, exist_ok=True)
        with open(paths.STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=1)
    except OSError:
        pass


class Worker(QtCore.QThread):
    """Run fn(progress, *args) in a thread; progress is a scanner-style progress object."""
    step = Signal(str)
    bar = Signal(float)          # 0..1, or -1 for busy
    done = Signal(object)
    failed = Signal(str, str)    # message, details

    def __init__(self, fn, *args):
        super().__init__()
        self.fn, self.args = fn, args

    def run(self):
        outer = self

        class P:
            def step(self, text): outer.step.emit(text); outer.bar.emit(-1)
            def spin(self, text): pass
            def bar(self, done, total): outer.bar.emit(done / total if total else 0)
            def end(self, note=''): pass
            def detail(self, text): pass
        try:
            self.done.emit(self.fn(P(), *self.args))
        except Exception as e:                       # shown to the user, not swallowed
            self.failed.emit(str(e), traceback.format_exc())


# ----------------------------------------------------------------------------- items
class HandleMixin:
    """Shared resize-handle hit testing in item-local coordinates."""

    def handle_points(self, r):
        c = r.center()
        return {'tl': r.topLeft(), 'tr': r.topRight(), 'bl': r.bottomLeft(), 'br': r.bottomRight(),
                't': QPointF(c.x(), r.top()), 'b': QPointF(c.x(), r.bottom()),
                'l': QPointF(r.left(), c.y()), 'r': QPointF(r.right(), c.y())}

    def hit_handle(self, pos, r, extra=None):
        tol = HANDLE_PX / view_scale(self)
        pts = dict(self.handle_points(r), **(extra or {}))
        for name, p in pts.items():
            if abs(pos.x() - p.x()) <= tol and abs(pos.y() - p.y()) <= tol:
                return name
        return None

    @staticmethod
    def cursor_for(name, angle=0.0):
        if name == 'rot':
            return Qt.CursorShape.CrossCursor
        if name is None:
            return Qt.CursorShape.SizeAllCursor
        base = {'t': 90, 'b': 90, 'l': 0, 'r': 0, 'tl': 135, 'br': 135, 'tr': 45, 'bl': 45}[name]
        a = (base - angle) % 180
        return [Qt.CursorShape.SizeHorCursor, Qt.CursorShape.SizeBDiagCursor,
                Qt.CursorShape.SizeVerCursor, Qt.CursorShape.SizeFDiagCursor][int(((a + 22.5) % 180) // 45)]


class BoxItem(HandleMixin, QtWidgets.QGraphicsObject):
    """A crop box: centre, size, angle (clockwise, degrees) and output quarter turns."""
    changed = Signal()      # while dragging (live thumbnails)
    edited = Signal()       # a drag finished and changed something (one undo step)
    open_preview = Signal() # double-click

    def __init__(self, cx, cy, w, h, angle=0.0, quarter=0, name='', height=0.0):
        super().__init__()
        self.w, self.h, self.quarter, self.number = w, h, quarter, 0
        self.name = name        # file name chosen by the user; '' = automatic
        self.height_mm = height # top surface above the desk (raised objects are scaled to true size)
        self.setPos(cx, cy)
        self.setRotation(angle)
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setAcceptHoverEvents(True)
        self.setZValue(2)
        self.drag = None

    # geometry
    def box(self):
        return self.pos().x(), self.pos().y(), self.w, self.h, self.rotation()

    def local_rect(self):
        return QRectF(-self.w / 2, -self.h / 2, self.w, self.h)

    def rot_handle(self):
        return QPointF(0, -self.h / 2 - ROT_HANDLE_PX / view_scale(self))

    def boundingRect(self):
        pad = (ROT_HANDLE_PX + 2 * HANDLE_PX) / max(view_scale(self), 1e-6)
        return self.local_rect().adjusted(-pad, -pad, pad, pad)

    def shape(self):
        p = QtGui.QPainterPath()
        tol = HANDLE_PX / view_scale(self)
        p.addRect(self.local_rect().adjusted(-tol, -tol, tol, tol))
        rh = self.rot_handle()
        p.addEllipse(rh, 2 * tol, 2 * tol)
        return p

    def colour(self):
        return QtGui.QColor(BOX_COLOURS[(self.number - 1) % len(BOX_COLOURS)] if self.number else '#2ecc71')

    def paint(self, painter, option, widget=None):
        s = view_scale(self)
        r = self.local_rect()
        col = self.colour()
        sel = self.isSelected()
        painter.setBrush(QtGui.QColor(col.red(), col.green(), col.blue(), 40 if sel else 18))
        painter.setPen(QtGui.QPen(col, (3 if sel else 2) / s))
        painter.drawRect(r)
        # the edge that becomes the top of the saved image
        top_edge = [(r.topLeft(), r.topRight()), (r.bottomLeft(), r.topLeft()),
                    (r.bottomRight(), r.bottomLeft()), (r.topRight(), r.bottomRight())][self.quarter % 4]
        painter.setPen(QtGui.QPen(col, 7 / s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        painter.drawLine(*top_edge)
        # number label, upright in the output orientation
        painter.save()
        painter.rotate(90 * self.quarter)
        f = painter.font()
        f.setPixelSize(max(1, int(28 / s)))
        f.setBold(True)
        painter.setFont(f)
        painter.setPen(QtGui.QPen(Qt.GlobalColor.white))
        label = f'{self.number}' if self.number else ''
        fm = QtGui.QFontMetricsF(f)
        tw, th = fm.horizontalAdvance(label) + 12 / s, fm.height()
        painter.setBrush(col)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(QRectF(-tw / 2, -th / 2, tw, th), 4 / s, 4 / s)
        painter.setPen(QtGui.QPen(Qt.GlobalColor.white))
        painter.drawText(QRectF(-tw / 2, -th / 2, tw, th), Qt.AlignmentFlag.AlignCenter, label)
        painter.restore()
        if sel and not _RENDER_SCALE[0]:
            hs = HANDLE_PX / s
            painter.setPen(QtGui.QPen(Qt.GlobalColor.black, 1 / s))
            painter.setBrush(Qt.GlobalColor.white)
            for p in self.handle_points(r).values():
                painter.drawRect(QRectF(p.x() - hs / 2, p.y() - hs / 2, hs, hs))
            rh = self.rot_handle()
            painter.setPen(QtGui.QPen(col, 2 / s))
            painter.drawLine(QPointF(0, r.top()), rh)
            painter.setBrush(col)
            painter.drawEllipse(rh, hs * 0.8, hs * 0.8)

    # interaction
    def hoverMoveEvent(self, e):
        name = self.hit_handle(e.pos(), self.local_rect(), {'rot': self.rot_handle()}) if self.isSelected() else None
        self.setCursor(self.cursor_for(name, self.rotation()))

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            e.ignore()
            return
        if not self.isSelected():
            self.scene().clearSelection()
            self.setSelected(True)
        name = self.hit_handle(e.pos(), self.local_rect(), {'rot': self.rot_handle()})
        self.drag = (name or 'move', e.scenePos(), self.box())
        e.accept()

    def mouseMoveEvent(self, e):
        if not self.drag:
            return
        mode, start, (cx, cy, w, h, ang) = self.drag
        sp = e.scenePos()
        self.prepareGeometryChange()
        if mode == 'move':
            d = sp - start
            self.setPos(cx + d.x(), cy + d.y())
        elif mode == 'rot':
            a = math.degrees(math.atan2(sp.y() - cy, sp.x() - cx)) + 90
            if e.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                a = round(a / 15) * 15
            self.setRotation((a + 180) % 360 - 180)
        else:
            # resize in the box's own (rotated) frame, keeping the opposite side fixed
            t = math.radians(ang)
            ux, uy = math.cos(t), math.sin(t)            # box x axis in scene
            vx, vy = -math.sin(t), math.cos(t)           # box y axis in scene
            dx, dy = sp.x() - start.x(), sp.y() - start.y()
            du, dv = dx * ux + dy * uy, dx * vx + dy * vy
            left, right, top, bottom = -w / 2, w / 2, -h / 2, h / 2
            if 'l' in mode:
                left = min(left + du, right - 5)
            if 'r' in mode:
                right = max(right + du, left + 5)
            if mode in ('t', 'tl', 'tr'):
                top = min(top + dv, bottom - 5)
            if mode in ('b', 'bl', 'br'):
                bottom = max(bottom + dv, top + 5)
            mu, mv = (left + right) / 2, (top + bottom) / 2
            self.w, self.h = right - left, bottom - top
            self.setPos(cx + mu * ux + mv * vx, cy + mu * uy + mv * vy)
            loupe(self, self.mapToScene(self.handle_points(self.local_rect())[mode]))
        self.update()
        self.changed.emit()

    def mouseReleaseEvent(self, e):
        loupe(self, None)
        before = self.drag[2] if self.drag else None
        self.drag = None
        self.changed.emit()
        if before is not None and before != self.box():
            self.edited.emit()

    def mouseDoubleClickEvent(self, e):
        self.drag = None
        self.open_preview.emit()

    def turn(self, steps):
        self.quarter = (self.quarter + steps) % 4
        self.update()
        self.changed.emit()


class WorkAreaItem(HandleMixin, QtWidgets.QGraphicsObject):
    """Axis-aligned work area; everything outside is dimmed and ignored by detection."""
    changed = Signal()

    def __init__(self, image_rect):
        super().__init__()
        self.image_rect = QRectF(image_rect)
        self.r = QRectF(image_rect)
        self.setZValue(1)
        self.setAcceptHoverEvents(True)
        self.drag = None

    def boundingRect(self):
        return self.image_rect.adjusted(-50, -50, 50, 50)

    def band(self):
        return 2 * HANDLE_PX / view_scale(self)

    def shape(self):
        # only the border band is "hot": clicks inside go to boxes / new-box drawing
        p = QtGui.QPainterPath()
        b = self.band()
        p.addRect(self.r.adjusted(-b, -b, b, b))
        inner = QtGui.QPainterPath()
        inner.addRect(self.r.adjusted(b, b, -b, -b))
        return p.subtracted(inner)

    def paint(self, painter, option, widget=None):
        s = view_scale(self)
        dim = QtGui.QPainterPath()
        dim.addRect(self.image_rect)
        hole = QtGui.QPainterPath()
        hole.addRect(self.r)
        painter.fillPath(dim.subtracted(hole), QtGui.QColor(0, 0, 0, 140))
        painter.setPen(QtGui.QPen(QtGui.QColor('#f1c40f'), 2 / s, Qt.PenStyle.DashLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(self.r)
        if _RENDER_SCALE[0]:
            return
        hs = HANDLE_PX / s
        painter.setPen(QtGui.QPen(Qt.GlobalColor.black, 1 / s))
        painter.setBrush(QtGui.QColor('#f1c40f'))
        for p in self.handle_points(self.r).values():
            painter.drawRect(QRectF(p.x() - hs / 2, p.y() - hs / 2, hs, hs))

    def hoverMoveEvent(self, e):
        self.setCursor(self.cursor_for(self.hit_handle(e.pos(), self.r)))

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            e.ignore()
            return
        self.drag = (self.hit_handle(e.pos(), self.r) or 'move', e.scenePos(), QRectF(self.r))
        e.accept()

    def mouseMoveEvent(self, e):
        if not self.drag:
            return
        mode, start, r0 = self.drag
        d = e.scenePos() - start
        r = QRectF(r0)
        if mode == 'move':
            r.translate(d)
            r.moveLeft(min(max(r.left(), self.image_rect.left()), self.image_rect.right() - r.width()))
            r.moveTop(min(max(r.top(), self.image_rect.top()), self.image_rect.bottom() - r.height()))
        else:
            if 'l' in mode:
                r.setLeft(min(r0.left() + d.x(), r0.right() - 50))
            if 'r' in mode:
                r.setRight(max(r0.right() + d.x(), r0.left() + 50))
            if mode in ('t', 'tl', 'tr'):
                r.setTop(min(r0.top() + d.y(), r0.bottom() - 50))
            if mode in ('b', 'bl', 'br'):
                r.setBottom(max(r0.bottom() + d.y(), r0.top() + 50))
            r = r.intersected(self.image_rect)
        self.set_rect(r)
        if mode != 'move':
            loupe(self, self.handle_points(self.r)[mode])

    def mouseReleaseEvent(self, e):
        loupe(self, None)
        self.drag = None
        self.changed.emit()

    def set_rect(self, r):
        self.prepareGeometryChange()
        self.r = QRectF(r).intersected(self.image_rect)
        self.update()


def loupe(item, scene_pt):
    """Show (scene_pt) or hide (None) the magnifier of the view the item is in."""
    views = item.scene().views() if item.scene() else []
    if views and isinstance(views[0], ImageView):
        views[0].show_loupe(scene_pt) if scene_pt is not None else views[0].hide_loupe()


class Loupe(QtWidgets.QWidget):
    """Magnifier shown while placing a corner/edge: the scene rendered at `zoom` screen px per
    scan px around `center`, with a crosshair on the exact point. Ignores the mouse."""

    def __init__(self, view):
        super().__init__(view.viewport())
        self.view = view
        self.center = QPointF()
        self.enabled, self.zoom, self.size_px = True, 2.0, 240
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        frame = QtGui.QPainterPath()
        frame.addRoundedRect(QRectF(1, 1, w - 2, h - 2), 14, 14)
        p.setClipPath(frame)
        p.fillRect(self.rect(), QtGui.QColor('#2b2b2b'))
        z = self.zoom
        src = QRectF(self.center.x() - w / 2 / z, self.center.y() - h / 2 / z, w / z, h / z)
        p.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, z < 2)   # crisp pixels when zoomed in
        _RENDER_SCALE[0] = z
        try:
            self.view.scene().render(p, QRectF(0, 0, w, h), src, Qt.AspectRatioMode.IgnoreAspectRatio)
        finally:
            _RENDER_SCALE[0] = None
        c, gap = QPointF(w / 2, h / 2), 7
        for pen in (QtGui.QPen(QtGui.QColor(0, 0, 0, 170), 3), QtGui.QPen(QtGui.QColor(255, 255, 255, 235), 1)):
            p.setPen(pen)
            p.drawLine(QPointF(0, c.y()), QPointF(c.x() - gap, c.y()))
            p.drawLine(QPointF(c.x() + gap, c.y()), QPointF(w, c.y()))
            p.drawLine(QPointF(c.x(), 0), QPointF(c.x(), c.y() - gap))
            p.drawLine(QPointF(c.x(), c.y() + gap), QPointF(c.x(), h))
        p.setClipping(False)
        p.setPen(QtGui.QPen(QtGui.QColor('#f1c40f'), 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(1, 1, w - 2, h - 2), 14, 14)
        p.setPen(QtGui.QColor('#f1c40f'))
        f = p.font()
        f.setPixelSize(11)
        p.setFont(f)
        span = w / z * 25.4 / DPI
        p.drawText(QRectF(10, h - 20, w - 20, 16), Qt.AlignmentFlag.AlignLeft,
                   f'{z:g}x   {span:.0f} mm across')
        p.end()


class ImageView(QtWidgets.QGraphicsView):
    """Zoom (wheel), pan (right/middle drag), draw new boxes (left drag on empty space)."""
    new_box = Signal(QRectF)

    def __init__(self, scene):
        super().__init__(scene)
        self.setRenderHints(QtGui.QPainter.RenderHint.Antialiasing | QtGui.QPainter.RenderHint.SmoothPixmapTransform)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setViewportUpdateMode(QtWidgets.QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setBackgroundBrush(QtGui.QColor('#2b2b2b'))
        self.pan = None
        self.band = None
        self.band_item = None
        self.loupe = Loupe(self)

    def show_loupe(self, scene_pt):
        """Show the magnifier for scene_pt, beside it (never covering it), inside the viewport."""
        L = self.loupe
        if not L.enabled:
            return
        L.center = QPointF(scene_pt)
        n = L.size_px
        L.resize(n, n)
        vp = self.mapFromScene(scene_pt)
        W, H = self.viewport().width(), self.viewport().height()
        off = 36
        x = vp.x() - n - off if vp.x() - n - off >= 0 else vp.x() + off
        y = vp.y() - n - off if vp.y() - n - off >= 0 else vp.y() + off
        L.move(int(min(max(x, 0), max(W - n, 0))), int(min(max(y, 0), max(H - n, 0))))
        L.show()
        L.raise_()
        L.update()

    def hide_loupe(self):
        self.loupe.hide()

    def wheelEvent(self, e):
        f = 1.25 if e.angleDelta().y() > 0 else 0.8
        s = self.transform().m11() * f
        if 0.02 < s < 8:
            self.scale(f, f)

    def fit(self):
        r = self.sceneRect()
        if not r.isEmpty():
            self.fitInView(r, Qt.AspectRatioMode.KeepAspectRatio)

    def mousePressEvent(self, e):
        if e.button() in (Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton):
            self.pan = e.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        item = self.itemAt(e.position().toPoint())
        if e.button() == Qt.MouseButton.LeftButton and (item is None or isinstance(item, QtWidgets.QGraphicsPixmapItem)):
            self.scene().clearSelection()
            self.band = self.mapToScene(e.position().toPoint())
            self.band_item = self.scene().addRect(QRectF(self.band, self.band),
                                                  QtGui.QPen(QtGui.QColor('#2ecc71'), 0, Qt.PenStyle.DashLine))
            self.band_item.setZValue(5)
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self.pan is not None:
            d = e.position() - self.pan
            self.pan = e.position()
            self.horizontalScrollBar().setValue(int(self.horizontalScrollBar().value() - d.x()))
            self.verticalScrollBar().setValue(int(self.verticalScrollBar().value() - d.y()))
            return
        if self.band is not None:
            cur = self.mapToScene(e.position().toPoint())
            self.band_item.setRect(QRectF(self.band, cur).normalized())
            self.show_loupe(cur)
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self.pan is not None:
            self.pan = None
            self.unsetCursor()
            return
        if self.band is not None:
            self.hide_loupe()
            r = self.band_item.rect()
            self.scene().removeItem(self.band_item)
            self.band = self.band_item = None
            if r.width() * self.transform().m11() > 15 and r.height() * self.transform().m11() > 15:
                self.new_box.emit(r)
            return
        super().mouseReleaseEvent(e)


# ----------------------------------------------------------------------------- settings dialog
def template_help():
    """Comments above each key in the settings template -> {(section, key): text}."""
    out, section, buf = {}, None, []
    for line in open(paths.SETTINGS_TEMPLATE, encoding='utf-8').read().splitlines():
        s = line.strip()
        m = re.match(r'\[([A-Za-z_][A-Za-z0-9_]*)\]$', s)
        if m:
            section, buf = m.group(1), []
        elif s.startswith('#'):
            buf.append(s.lstrip('#').strip())
        elif '=' in s and section:
            out[(section, s.split('=')[0].strip())] = '\n'.join(buf)
            buf = []
        else:
            buf = []
    return out


class SettingsDialog(QtWidgets.QDialog):
    LABELS = {
        'output': 'Output', 'correction': 'Correction', 'brighten': 'Brighten', 'split': 'Split',
        'detection': 'Detection', 'continuous': 'Continuous', 'gui': 'Display', 'scanner': 'Scanner',
    }
    NICE = {'jpeg_quality': 'JPEG quality', 'png_compression': 'PNG compression', 'name': 'File name',
            'flat_field': 'Flat-field (far side)', 'cutoff_percent': 'Cutoff (%)', 'margin_mm': 'Margin (mm)',
            'fill_colour': 'Fill colour', 'min_side_mm': 'Min side (mm)', 'min_area_cm2': 'Min area (cm²)',
            'min_rectangularity': 'Min rectangularity', 'dark_limit': 'Dark limit (L)',
            'outline_gap_mm': 'Outline gap (mm)', 'merge_gap_mm': 'Merge gap (mm)',
            'thin_object_mm': 'Thin object (mm)', 'row_height_mm': 'Row height (mm)',
            'usb_timeout_s': 'USB timeout (s)', 'read_timeout_s': 'Read timeout (s)',
            'image_ready_timeout_s': 'Image-ready timeout (s)', 'enabled': 'Enabled',
            'head_height_mm': 'Head height (mm, 0 = calibration)', 'object_height_mm': 'Object height (mm)',
            'scanner_button': 'Scanner button starts a scan', 'timer_s': 'Timer (s)',
            'auto_save': 'Auto-save each scan', 'colour': 'Colour calibration', 'loupe': 'Magnifier when placing corners',
            'loupe_zoom': 'Magnifier zoom', 'loupe_size_px': 'Magnifier size (px)'}
    CHOICES = {('output', 'format'): ['jpg', 'png', 'tif', 'pdf', 'webp'],
               ('correction', 'area'): ['inscribed', 'full']}
    RANGES = {('output', 'jpeg_quality'): (1, 100), ('output', 'png_compression'): (0, 9),
              ('detection', 'dark_limit'): (0, 255), ('detection', 'edge_low'): (1, 500),
              ('detection', 'edge_high'): (1, 500), ('gui', 'loupe_size_px'): (80, 1000)}

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Settings')
        self.cfg = json.loads(json.dumps(cfg))
        self.widgets = {}
        helps = template_help()
        tabs = QtWidgets.QTabWidget()
        for section, values in settings.DEFAULTS.items():
            page = QtWidgets.QWidget()
            form = QtWidgets.QFormLayout(page)
            for key, default in values.items():
                w = self.make_widget(section, key, default, self.cfg[section][key])
                tip = helps.get((section, key), '')
                label = QtWidgets.QLabel(self.NICE.get(key, key.replace('_', ' ').capitalize()))
                label.setToolTip(tip)
                (w if isinstance(w, QtWidgets.QWidget) else w[0]).setToolTip(tip)
                form.addRow(label, w if isinstance(w, QtWidgets.QWidget) else w[0])
                if tip:
                    hint = QtWidgets.QLabel(tip.replace('\n', ' '))
                    hint.setWordWrap(True)
                    hint.setStyleSheet('color: gray; font-size: 90%;')
                    form.addRow('', hint)
            if section == 'output':
                self.preview = QtWidgets.QLabel()
                form.addRow('Next file', self.preview)
                self.update_preview()
            scroll = QtWidgets.QScrollArea()
            scroll.setWidget(page)
            scroll.setWidgetResizable(True)
            tabs.addTab(scroll, self.LABELS[section])
        info = QtWidgets.QLabel(f'Saved to {paths.SETTINGS_FILE}. Hover over a setting for details.')
        info.setStyleSheet('color: gray;')
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Save
                                             | QtWidgets.QDialogButtonBox.StandardButton.Cancel
                                             | QtWidgets.QDialogButtonBox.StandardButton.RestoreDefaults)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(self.restore)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(info)
        lay.addWidget(buttons)
        self.resize(640, 560)

    def make_widget(self, section, key, default, value):
        k = (section, key)
        if k in self.CHOICES:
            w = QtWidgets.QComboBox()
            w.addItems(self.CHOICES[k])
            w.setCurrentText(value)
            w.currentTextChanged.connect(self.update_preview)
        elif isinstance(default, bool):
            w = QtWidgets.QCheckBox()
            w.setChecked(value)
        elif isinstance(default, int):
            w = QtWidgets.QSpinBox()
            w.setRange(*self.RANGES.get(k, (0, 100000)))
            w.setValue(value)
        elif isinstance(default, float):
            w = QtWidgets.QDoubleSpinBox()
            w.setRange(0, 100000)
            w.setDecimals(2)
            w.setValue(value)
        elif isinstance(default, list):                       # fill colour
            w = QtWidgets.QPushButton()
            w.colour = list(value)
            self.paint_colour(w)
            w.clicked.connect(lambda _=False, b=w: self.pick_colour(b))
        elif k == ('output', 'folder'):
            box = QtWidgets.QWidget()
            h = QtWidgets.QHBoxLayout(box)
            h.setContentsMargins(0, 0, 0, 0)
            edit = QtWidgets.QLineEdit(value)
            btn = QtWidgets.QPushButton('Browse...')
            btn.clicked.connect(lambda: self.browse(edit))
            edit.textChanged.connect(self.update_preview)
            h.addWidget(edit)
            h.addWidget(btn)
            self.widgets[k] = edit
            return box
        else:
            w = QtWidgets.QLineEdit(value)
            w.textChanged.connect(self.update_preview)
        self.widgets[k] = w
        return w

    def paint_colour(self, b):
        r, g, bl = b.colour
        b.setText(f'RGB {r}, {g}, {bl}')
        b.setStyleSheet(f'background: rgb({r},{g},{bl}); color: {"black" if r + g + bl > 380 else "white"};')

    def pick_colour(self, b):
        c = QtWidgets.QColorDialog.getColor(QtGui.QColor(*b.colour), self)
        if c.isValid():
            b.colour = [c.red(), c.green(), c.blue()]
            self.paint_colour(b)

    def browse(self, edit):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, 'Destination folder', os.path.expanduser(edit.text()))
        if d:
            edit.setText(d)

    def value(self, k):
        w = self.widgets[k]
        if isinstance(w, QtWidgets.QComboBox):
            return w.currentText()
        if isinstance(w, QtWidgets.QCheckBox):
            return w.isChecked()
        if isinstance(w, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
            return w.value()
        if isinstance(w, QtWidgets.QPushButton):
            return list(w.colour)
        return w.text()

    def update_preview(self, *_):
        if not hasattr(self, 'preview') or ('output', 'format') not in self.widgets:
            return
        try:
            base = output.next_base(self.value(('output', 'folder')), self.value(('output', 'name')))
            self.preview.setText(base + '.' + self.value(('output', 'format')))
        except (KeyError, ValueError, IndexError) as e:
            self.preview.setText(f'invalid pattern: {e}')

    def restore(self):
        for (section, key), w in self.widgets.items():
            v = settings.DEFAULTS[section][key]
            if isinstance(w, QtWidgets.QComboBox):
                w.setCurrentText(v)
            elif isinstance(w, QtWidgets.QCheckBox):
                w.setChecked(v)
            elif isinstance(w, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
                w.setValue(v)
            elif isinstance(w, QtWidgets.QPushButton):
                w.colour = list(v)
                self.paint_colour(w)
            else:
                w.setText(v)

    def result_settings(self):
        out = json.loads(json.dumps(self.cfg))
        for (section, key) in self.widgets:
            out[section][key] = self.value((section, key))
        return out

    def accept(self):
        new = self.result_settings()
        try:
            output.next_base(new['output']['folder'], new['output']['name'])
            settings.save(new)
        except (ValueError, KeyError, IndexError, OSError) as e:
            QtWidgets.QMessageBox.warning(self, 'Settings', f'Cannot save these settings:\n{e}')
            return
        self.cfg = new
        super().accept()


# ----------------------------------------------------------------------------- object preview
class AboutDialog(QtWidgets.QDialog):
    """The "?" button: what this is, who made it, where it lives, the keys."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle('About SV600 Scanner')
        icon = QtWidgets.QLabel()
        icon.setPixmap(QtGui.QPixmap(os.path.join(paths.DATA_DIR, 'sv600-scanner.png')).scaled(
            96, 96, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        icon.setAlignment(Qt.AlignmentFlag.AlignTop)
        text = QtWidgets.QLabel(
            f'<h2>SV600 Scanner for Linux</h2>'
            f'<p>Version {__version__}</p>'
            '<p>Scanning with the Fujitsu/Ricoh ScanSnap SV600 without Windows: geometry, colour and '
            'brightness correction, object splitting, sessions.</p>'
            '<p>Idea, direction and testing: <b>tuksi</b><br>'
            'Code: Claude (Anthropic), written in Claude Code</p>'
            '<p>Licence: PolyForm Noncommercial 1.0.0. Free to use, change and share for any '
            'non-commercial purpose; selling it or using it in a business is not allowed.</p>')
        text.setWordWrap(True)
        text.setTextFormat(Qt.TextFormat.RichText)
        link = QtWidgets.QLabel(f'<a href="{URL}">{URL}</a>')
        link.setOpenExternalLinks(True)
        keys = QtWidgets.QLabel(__doc__.split('\n', 2)[2].strip())
        keys.setWordWrap(True)
        font = keys.font()
        font.setPointSizeF(font.pointSizeF() * 0.9)
        keys.setFont(font)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(icon)
        top.addSpacing(12)
        col = QtWidgets.QVBoxLayout()
        col.addWidget(text)
        col.addWidget(link)
        top.addLayout(col, 1)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addLayout(top)
        lay.addSpacing(8)
        lay.addWidget(keys)
        lay.addWidget(buttons)
        self.setMinimumWidth(520)


class ObjectPreview(QtWidgets.QDialog):
    """One object as it will be saved: rotate it by quarter turns, name it, step through all."""

    def __init__(self, main, box):
        super().__init__(main)
        self.main, self.box, self.pix = main, box, None
        self.setWindowTitle('Object preview')
        self.view = QtWidgets.QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.view.setMinimumSize(320, 320)
        self.view.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Ignored)
        self.view.setStyleSheet('background: #2b2b2b;')
        self.info = QtWidgets.QLabel()
        SP = QtWidgets.QStyle.StandardPixmap
        st = self.style()

        def button(text, icon, slot, keys, tip):
            b = QtWidgets.QPushButton(st.standardIcon(icon), text)
            b.setToolTip(f'{tip} ({", ".join(keys)})')
            b.clicked.connect(slot)
            for k in keys:
                QtGui.QShortcut(QtGui.QKeySequence(k), self, activated=slot)
            b.setAutoDefault(False)
            return b
        self.prev_b = button('Previous', SP.SP_MediaSkipBackward, lambda: self.step(-1), ['PgUp'], 'Previous object')
        # R / Shift+R are handled in keyPressEvent so typing an "r" in the name field still works
        rl = button('Rotate left', SP.SP_ArrowBack, lambda: self.turn(-1), ['Ctrl+Left'],
                    'Turn the saved image 90 deg counter-clockwise (or Shift+R)')
        rr = button('Rotate right', SP.SP_ArrowForward, lambda: self.turn(1), ['Ctrl+Right'],
                    'Turn the saved image 90 deg clockwise (or R)')
        self.next_b = button('Next', SP.SP_MediaSkipForward, lambda: self.step(1), ['PgDown'], 'Next object')
        close = button('Close', SP.SP_DialogCloseButton, self.accept, ['Esc'], 'Close the preview')
        self.height = make_height_box()
        self.height.setToolTip('Height of the top surface above the desk (e.g. the thickness of a closed '
                               'book). Raised objects look bigger; with the height set they are saved '
                               'at true size.')
        self.height.editingFinished.connect(self.height_changed)
        self.name = QtWidgets.QLineEdit()
        self.name.setClearButtonEnabled(True)
        self.name.editingFinished.connect(self.name_changed)
        self.name.returnPressed.connect(self.view.setFocus)     # Enter: back to R / Shift+R
        self.view.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        row = QtWidgets.QHBoxLayout()
        for w in (self.prev_b, rl, rr, self.next_b):
            row.addWidget(w)
        row.addStretch(1)
        row.addWidget(close)
        form = QtWidgets.QHBoxLayout()
        form.addWidget(QtWidgets.QLabel('File name'))
        form.addWidget(self.name, 1)
        form.addSpacing(16)
        form.addWidget(QtWidgets.QLabel('Height above desk'))
        form.addWidget(self.height)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(self.view, 1)
        lay.addWidget(self.info)
        lay.addLayout(form)
        lay.addLayout(row)
        g = main.geometry()
        self.resize(int(g.width() * 0.6), int(g.height() * 0.85))
        self.load()
        self.view.setFocus()                  # R / Shift+R work right away; click the name to type

    def load(self):
        b = self.box
        self.main.scene.clearSelection()
        b.setSelected(True)
        cx, cy, w, h, a = b.box()
        k = self.main.box_scale(b.height_mm)
        img = crop.extract(self.main.page.image(), b.box(), b.quarter,
                           tuple(self.main.cfg['split']['fill_colour']), k)
        self.pix = QtGui.QPixmap.fromImage(to_qimage(img))
        ow, oh = (w * k, h * k) if b.quarter % 2 == 0 else (h * k, w * k)
        n = len(self.main.boxes)
        hh = self.main.head_height()
        raised = (f',  raised {cm(b.height_mm)}: scaled to {k * 100:.1f} % (head height {cm(hh)})'
                  if b.height_mm else '')
        self.info.setText(f'Object {b.number} of {n}:  {ow / DPI * 25.4:.0f} x {oh / DPI * 25.4:.0f} mm,  '
                          f'straightened by {a:+.1f} deg,  turned {90 * b.quarter} deg{raised}')
        self.height.blockSignals(True)
        self.height.setValue(b.height_mm / 10)
        self.height.blockSignals(False)
        self.name.blockSignals(True)
        self.name.setText(b.name)
        self.name.setPlaceholderText(f'automatic: {self.main.default_name(b)}')
        self.name.blockSignals(False)
        idx = self.main.boxes.index(b)
        self.prev_b.setEnabled(idx > 0)
        self.next_b.setEnabled(idx < n - 1)
        self.show_pix()

    def show_pix(self):
        if self.pix is not None:
            self.view.setPixmap(self.pix.scaled(self.view.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                                Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.show_pix()

    def turn(self, steps):
        self.box.turn(steps)
        self.main.commit('turn')
        self.load()

    def name_changed(self):
        text = self.name.text().strip()
        if text != self.box.name:
            self.box.name = text
            self.main.commit('name')
            self.main.thumb_timer.start()

    def height_changed(self):
        v = round(self.height.value() * 10, 1)            # cm in the box, mm inside
        if v != self.box.height_mm:
            if v and self.main.box_scale(v) == 1.0:
                QtWidgets.QMessageBox.warning(self, 'Height', f'{cm(v)} is too high for this scanner.')
                self.height.setValue(self.box.height_mm / 10)
                return
            self.box.height_mm = v
            self.main.commit('height')
            self.main.thumb_timer.start()
            self.load()

    def step(self, d):
        self.height_changed()
        self.name_changed()
        boxes = self.main.boxes
        i = boxes.index(self.box) + d
        if 0 <= i < len(boxes):
            self.box = boxes[i]
            self.load()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_R and not self.name.hasFocus():
            self.turn(-1 if e.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1)
            return
        super().keyPressEvent(e)

    def accept(self):
        self.height_changed()
        self.name_changed()
        super().accept()


# ----------------------------------------------------------------------------- session pages
class Page:
    """One scan in the session: image (parked on disk when not shown), boxes, undo history."""

    def __init__(self, image, label, session_dir, number):
        self.number = number
        self.label = label
        self.session_dir = session_dir
        self._image = image
        self.size = (image.shape[1], image.shape[0])
        self.park_path = os.path.join(session_dir, f'page{number:04d}.npy')
        self.source = None            # saved full-scan file to reload from, once saved
        self.history, self.pos = [], -1
        self.last_kind, self.last_time = None, 0.0
        self.saved_boxes = None       # boxes part of the snapshot at the last save
        self.base = None              # file base name once saved (re-saves overwrite it)
        self.files = []               # files written for this page
        self.png_path = None          # lossless copy encoded for session files (reload source)
        self.det_png = None
        self.hint, self.prefix = None, ''
        import cv2
        self.set_small(cv2.resize(image, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA))

    def set_small(self, small):
        import cv2
        self.small = small
        h = 150
        self.thumb = QtGui.QPixmap.fromImage(to_qimage(cv2.resize(
            small, (max(1, int(small.shape[1] * h / small.shape[0])), h), interpolation=cv2.INTER_AREA)))

    @classmethod
    def restored(cls, meta, png, det, small, size, session_dir, number):
        """A page from a session file (its image is read from png when shown)."""
        p = cls.__new__(cls)
        p.number, p.label, p.session_dir = number, meta['label'], session_dir
        p._image, p.size = None, tuple(size)
        p.park_path = os.path.join(session_dir, f'page{number:04d}.npy')
        p.source, p.png_path, p.det_png = None, png, None
        p.history = [session.snapshot_from_json(h) for h in meta['history']]
        p.pos = meta['pos']
        p.last_kind, p.last_time = None, 0.0
        sb = meta.get('saved_boxes')
        p.saved_boxes = None if sb is None else tuple(tuple(b) for b in sb)
        p.base, p.files, p.prefix = meta.get('base'), list(meta.get('files', [])), meta.get('prefix', '')
        kind = meta.get('hint')
        p.hint = det if kind == 'small' else ('undo' if kind == 'undo' else None)
        p.set_small(small)
        return p

    def meta(self):
        return {'label': self.label, 'prefix': self.prefix, 'size': list(self.size),
                'base': self.base, 'files': self.files,
                'saved_boxes': None if self.saved_boxes is None else [list(b) for b in self.saved_boxes],
                'history': [[list(a), [list(b) for b in bx], list(sel)] for a, bx, sel in self.history],
                'pos': self.pos,
                'hint': 'small' if isinstance(self.hint, np.ndarray) else self.hint}

    def image(self):
        if self._image is None:
            if os.path.exists(self.park_path):
                self._image = np.load(self.park_path)
            elif self.png_path and os.path.exists(self.png_path):
                self._image = session.read_png(self.png_path)
            else:
                from PIL import Image
                self._image = np.ascontiguousarray(np.asarray(Image.open(self.source).convert('RGB')))
        return self._image

    def park(self):
        """Free the full image from memory (kept on disk until the page has been saved)."""
        if self._image is None:
            return
        if not self.source and not self.png_path and not os.path.exists(self.park_path):
            np.save(self.park_path, self._image)
        self._image = None

    def snapshot(self):
        return self.history[self.pos] if self.pos >= 0 else None

    def boxes_now(self):
        s = self.snapshot()
        return s[1] if s else ()

    @property
    def dirty(self):
        return self.saved_boxes is None or self.boxes_now() != self.saved_boxes

    def drop_files(self):
        try:
            os.remove(self.park_path)
        except OSError:
            pass


class ButtonWatcher(QtCore.QThread):
    """Polls the SV600's Scan button while the app is idle (holds the USB interface)."""
    pressed = Signal()
    status = Signal(str)

    def __init__(self, timeouts):
        super().__init__()
        self.timeouts = timeouts
        self.stop_flag = False

    def run(self):
        from .scanner import SV600, ScannerError, UDEV_HELP
        last_msg = None
        while not self.stop_flag:
            try:
                sv = SV600(None, self.timeouts)
            except ScannerError as e:
                msg = 'no USB permission' if str(e) == UDEV_HELP else \
                      'in use by another program' if 'in use' in str(e) else 'not found'
                if msg != last_msg:
                    self.status.emit(msg)
                    last_msg = msg
                for _ in range(20):                 # retry every 2 s
                    if self.stop_flag:
                        return
                    self.msleep(100)
                continue
            except Exception as e:                 # unexpected USB error: report, retry
                self.status.emit(f'error: {e}')
                self.msleep(2000)
                continue
            self.status.emit('ready')
            last_msg = 'ready'
            try:
                was_down = True                     # ignore a press already held when we start
                while not self.stop_flag:
                    down = sv.scan_button()
                    if down and not was_down:
                        self.pressed.emit()
                        return
                    was_down = down
                    self.msleep(100)
            except Exception:                       # unplugged / switched off while watching
                self.status.emit('not found')
                last_msg = 'not found'
            finally:
                try:
                    sv.close()
                except Exception:
                    pass


# ----------------------------------------------------------------------------- main window
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f'SV600 Scanner {__version__}')
        try:
            self.cfg = settings.load()
        except (OSError, ValueError) as e:
            QtWidgets.QMessageBox.warning(self, 'Settings', f'{e}\n\nUsing built-in defaults.')
            self.cfg = json.loads(json.dumps(settings.DEFAULTS))
        self.state = load_state()
        self.pages = []
        self.page = None
        self.boxes = []
        self.work = None
        self.worker = None
        self.watcher = None
        self.scan_count = 0
        self.session_dir = self.make_session_dir()
        self.session_path = None          # .sv600session file of this session, once saved
        self.session_modified = False

        self.scene = QtWidgets.QGraphicsScene(self)
        self.view = ImageView(self.scene)
        self.view.new_box.connect(self.add_box_from_rect)
        self.scene.selectionChanged.connect(self.sync_selection)
        self.pixmap_item = None
        self.setCentralWidget(self.view)
        self.apply_display_settings()
        self.make_actions()
        self.make_session_panel()
        self.make_side_panel()
        self.make_status()
        self.thumb_timer = QtCore.QTimer(self, singleShot=True, interval=150, timeout=self.update_thumbs)
        self.countdown = QtCore.QTimer(self, interval=100, timeout=self.countdown_tick)
        self.countdown_end = 0.0
        geo = self.state.get('geometry')
        if geo:
            self.restoreGeometry(QtCore.QByteArray.fromBase64(geo.encode()))
        else:
            self.resize(1500, 950)
        self.show_placeholder()
        self.update_next_name()
        self.update_title()
        self.start_watcher()

    # compatibility helpers used by the tests and the jobs
    @property
    def image(self):
        return self.page.image() if self.page else None

    @property
    def dirty(self):
        return any(p.dirty for p in self.pages)

    @dirty.setter
    def dirty(self, value):                  # tests use w.dirty = False to close without asking
        if not value:
            for p in self.pages:
                p.saved_boxes = p.boxes_now()
            self.session_modified = False

    @staticmethod
    def make_session_dir():
        import shutil
        base = paths.CACHE_DIR
        os.makedirs(base, exist_ok=True)
        for d in os.listdir(base):          # sessions of programs that are no longer running
            m = re.match(r'session-(\d+)$', d)
            if m and not os.path.exists(f'/proc/{m.group(1)}'):
                shutil.rmtree(os.path.join(base, d), ignore_errors=True)
        path = os.path.join(base, f'session-{os.getpid()}')
        os.makedirs(path, exist_ok=True)
        return path

    def apply_display_settings(self):
        g = self.cfg['gui']
        L = self.view.loupe
        L.enabled, L.zoom, L.size_px = g['loupe'], g['loupe_zoom'], g['loupe_size_px']

    # ---- UI construction
    def make_actions(self):
        tb = self.addToolBar('Main')
        tb.setObjectName('main toolbar')
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        style = self.style()
        SP = QtWidgets.QStyle.StandardPixmap

        def act(text, slot, key=None, icon=None, tip='', checkable=False):
            a = QtGui.QAction(text, self)
            if icon is not None:
                a.setIcon(style.standardIcon(icon))
            if key:
                a.setShortcuts([QtGui.QKeySequence(k) for k in (key if isinstance(key, list) else [key])])
            keys = ', '.join(s.toString() for s in a.shortcuts())
            a.setToolTip(f'{tip or text} ({keys})' if key else (tip or text))
            a.setCheckable(checkable)
            (a.toggled if checkable else a.triggered).connect(slot)
            self.addAction(a)
            return a

        self.a_scan_as = act('Scan as', self.scan_prefixed, 'Shift+F5', SP.SP_MediaPlay,
                             'Scan and save the files as PREFIX_001, PREFIX_002, ... (prefix on the left; '
                             'Enter in the prefix field also scans)')
        self.a_scan = act('Scan', self.scan_plain, ['F5', 'Space'], SP.SP_MediaPlay,
                          'Scan with the SV600 (or press the Scan button on the scanner)')
        self.a_cont = act('Continuous', self.toggle_continuous, 'Ctrl+K', SP.SP_MediaSeekForward,
                          'Continuous scanning: after each scan get ready for the next one '
                          '(timer / scanner button, auto-save: see Settings > Continuous)', checkable=True)
        self.a_open = act('Open...', self.open_image, 'Ctrl+O', SP.SP_DialogOpenButton, 'Open an existing scan')
        self.a_undo = act('Undo', self.undo, 'Ctrl+Z', SP.SP_ArrowLeft)
        self.a_redo = act('Redo', self.redo, ['Ctrl+Shift+Z', 'Ctrl+Y'], SP.SP_ArrowRight)
        self.a_detect = act('Detect', self.detect, 'D', SP.SP_BrowserReload, 'Find objects inside the work area')
        self.a_area = act('Reset area', self.reset_work_area, None, SP.SP_TitleBarMaxButton,
                          'Make the work area the whole scan')
        self.a_ccw = act('Turn left', lambda: self.turn(-1), 'Shift+R', SP.SP_ArrowBack,
                         'Turn the selected output 90 deg counter-clockwise')
        self.a_cw = act('Turn right', lambda: self.turn(1), 'R', SP.SP_ArrowForward,
                        'Turn the selected output 90 deg clockwise')
        self.a_del = act('Delete box', self.delete_selected, ['Delete', 'Backspace'], SP.SP_TrashIcon)
        self.a_save = act('Save', self.save, 'Ctrl+S', SP.SP_DialogSaveButton, 'Save this scan (boxes and full scan)')
        self.a_save_all = act('Save all', self.save_all, 'Ctrl+Shift+S', SP.SP_DriveHDIcon,
                              'Save every unsaved scan in this session')
        self.a_settings = act('Settings...', self.open_settings, 'Ctrl+,', SP.SP_FileDialogDetailedView)
        act('Fit', self.view.fit, 'F', None, 'Fit the scan in the window')
        act('Select all', self.select_all, 'Ctrl+A')
        act('Stop', self.stop_continuous, 'Esc', None, 'Stop continuous scanning')
        tb.addAction(self.a_scan)
        self.prefix_edit = QtWidgets.QLineEdit(self.state.get('prefix', ''))
        self.prefix_edit.setPlaceholderText('file name prefix')
        self.prefix_edit.setClearButtonEnabled(True)
        self.prefix_edit.setMaximumWidth(180)
        self.prefix_edit.setToolTip('Name for "Scan as": files become PREFIX_001.jpg, PREFIX_001_1.jpg, ... '
                                    'Continuous scanning and the scanner\'s button keep using it after a "Scan as".')
        self.prefix_edit.textChanged.connect(self.prefix_changed)
        self.prefix_edit.returnPressed.connect(self.scan_prefixed)
        tb.addWidget(self.prefix_edit)
        tb.addAction(self.a_scan_as)
        tb.addSeparator()
        for a in (self.a_cont, self.a_open, None, self.a_undo, self.a_redo, None,
                  self.a_detect, self.a_area, None, self.a_ccw, self.a_cw, self.a_del, None):
            tb.addSeparator() if a is None else tb.addAction(a)
        lab = QtWidgets.QLabel(' Height ')
        self.height_box = make_height_box()
        self.height_box.setKeyboardTracking(False)          # apply on Enter / arrows, not per keystroke
        self.height_box.setValue(self.cfg['split']['object_height_mm'] / 10)
        tip = ('Height of the objects\' top above the desk (e.g. 4 cm for a closed 4 cm book): they are saved '
               'at true size. Applies to the selected boxes, or to all boxes of this scan if none are '
               'selected, and is kept as the default for the next scans.')
        lab.setToolTip(tip)
        self.height_box.setToolTip(tip)
        self.height_box.valueChanged.connect(self.toolbar_height_changed)
        tb.addWidget(lab)
        tb.addWidget(self.height_box)
        tb.addSeparator()
        self.session_menu = QtWidgets.QMenu('Session', self)
        self.a_sess_new = self.session_menu.addAction('New session', self.new_session)
        self.a_sess_open = self.session_menu.addAction('Open session...', self.open_session)
        self.a_sess_open.setShortcut(QtGui.QKeySequence('Ctrl+Shift+O'))
        self.a_sess_save = self.session_menu.addAction('Save session', self.save_session)
        self.a_sess_save.setShortcut(QtGui.QKeySequence('Ctrl+E'))
        self.a_sess_save_as = self.session_menu.addAction('Save session as...', self.save_session_as)
        self.recent_menu = self.session_menu.addMenu('Recent sessions')
        self.recent_menu.aboutToShow.connect(self.fill_recent)
        for a in (self.a_sess_open, self.a_sess_save):
            self.addAction(a)                                  # shortcuts work without the menu open
        sess_btn = QtWidgets.QToolButton()
        sess_btn.setText('Session')
        sess_btn.setIcon(self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_DirIcon))
        sess_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        sess_btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        sess_btn.setMenu(self.session_menu)
        sess_btn.setToolTip('Save the whole session (all scans, boxes and undo history) to a file, or open one')
        for a in (self.a_save, self.a_save_all, None):
            tb.addSeparator() if a is None else tb.addAction(a)
        tb.addWidget(sess_btn)
        tb.addSeparator()
        for a in (self.a_settings,):
            tb.addSeparator() if a is None else tb.addAction(a)
        help_btn = QtWidgets.QToolButton()
        help_btn.setText('?')
        help_btn.setToolTip(__doc__)
        help_btn.clicked.connect(lambda: AboutDialog(self).exec())
        tb.addWidget(help_btn)

    def make_session_panel(self):
        dock = QtWidgets.QDockWidget('Session', self)
        dock.setObjectName('session')
        dock.setFeatures(QtWidgets.QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        self.session = QtWidgets.QListWidget()
        self.session.setIconSize(QtCore.QSize(150, 150))
        self.session.setMinimumWidth(240)
        self.session.setSpacing(4)
        self.session.currentRowChanged.connect(self.session_clicked)
        rm = QtWidgets.QPushButton('Remove from session')
        rm.setToolTip('Forget the selected scan (files already saved stay on disk)')
        rm.clicked.connect(self.remove_page)
        lay.addWidget(self.session, 1)
        lay.addWidget(rm)
        dock.setWidget(w)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)

    def make_side_panel(self):
        dock = QtWidgets.QDockWidget('Output', self)
        dock.setObjectName('output')
        dock.setFeatures(QtWidgets.QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
        w = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        self.thumbs = QtWidgets.QListWidget()
        self.thumbs.setViewMode(QtWidgets.QListView.ViewMode.IconMode)
        self.thumbs.setIconSize(QtCore.QSize(THUMB, THUMB))
        self.thumbs.setResizeMode(QtWidgets.QListView.ResizeMode.Adjust)
        self.thumbs.setMovement(QtWidgets.QListView.Movement.Static)
        self.thumbs.setSpacing(6)
        self.thumbs.setMinimumWidth(THUMB + 40)
        self.thumbs.itemSelectionChanged.connect(self.thumb_selected)
        self.thumbs.itemDoubleClicked.connect(
            lambda it: self.open_preview(it.data(Qt.ItemDataRole.UserRole)))
        self.next_label = QtWidgets.QLabel()
        self.next_label.setWordWrap(True)
        self.next_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        save_btn = QtWidgets.QPushButton(self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_DialogSaveButton),
                                         'Save')
        save_btn.clicked.connect(self.save)
        lay.addWidget(self.thumbs, 1)
        lay.addWidget(self.next_label)
        lay.addWidget(save_btn)
        dock.setWidget(w)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

    def make_status(self):
        self.step_label = QtWidgets.QLabel('Ready')
        self.progress = QtWidgets.QProgressBar()
        self.progress.setMaximumWidth(260)
        self.progress.setTextVisible(True)
        self.progress.hide()
        self.scanner_label = QtWidgets.QLabel('Scanner: ...')
        self.scanner_label.setToolTip('Scanner state. With "scanner button" enabled in Settings > Continuous, '
                                      'the Scan button on the SV600 starts a scan.')
        self.statusBar().addWidget(self.step_label, 1)
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().addPermanentWidget(self.scanner_label)

    def show_placeholder(self):
        self.boxes, self.work, self.pixmap_item = [], None, None
        self.scene.clear()
        t = self.scene.addText('Press Scan (F5 / Space), or the Scan button on the SV600,\n'
                               'or Open (Ctrl+O) to load an existing scan.')
        t.setDefaultTextColor(QtGui.QColor('#bbbbbb'))
        f = t.font()
        f.setPointSize(16)
        t.setFont(f)
        self.scene.setSceneRect(t.boundingRect())
        self.thumbs.clear()
        self.update_actions()

    def update_actions(self):
        busy = self.worker is not None
        has = self.page is not None
        for a in (self.a_scan, self.a_open, self.a_settings):
            a.setEnabled(not busy)
        if hasattr(self, 'a_scan_as'):
            self.a_scan_as.setEnabled(not busy and bool(self.prefix()))
        for a in (self.a_detect, self.a_area, self.a_save):
            a.setEnabled(has and not busy)
        self.a_save_all.setEnabled(not busy and any(p.dirty for p in self.pages))
        sel = bool(self.selected_boxes())
        for a in (self.a_cw, self.a_ccw, self.a_del):
            a.setEnabled(sel and not busy)
        self.a_undo.setEnabled(has and not busy and self.page.pos > 0)
        self.a_redo.setEnabled(has and not busy and self.page.pos < len(self.page.history) - 1)

    def update_next_name(self):
        o = self.cfg['output']
        if self.page is not None and self.page.base:
            self.next_label.setText(f'<b>Saved as</b><br>{os.path.dirname(self.page.base)}<br>'
                                    f'{os.path.basename(self.page.base)}.{o["format"]}<br>'
                                    '(saving again overwrites these files)')
            return
        try:
            base = output.next_base(o['folder'], self.name_pattern(self.page))
            self.next_label.setText(f'<b>Saves to</b><br>{os.path.dirname(base)}<br>'
                                    f'<b>as</b> {os.path.basename(base)}.{o["format"]}'
                                    '<br>(boxes get _1, _2, ...)')
        except (KeyError, ValueError, IndexError) as e:
            self.next_label.setText(f'Invalid file name pattern: {e}')

    # ---- scanner button watcher
    def start_watcher(self):
        if self.watcher or self.worker or not self.cfg['continuous']['scanner_button']:
            if not self.cfg['continuous']['scanner_button']:
                self.scanner_label.setText('Scanner button: off')
            return
        self.watcher = ButtonWatcher(dict(self.cfg['scanner']))
        self.watcher.pressed.connect(self.scanner_button_pressed)
        self.watcher.status.connect(lambda s: self.scanner_label.setText(f'Scanner: {s}'))
        self.watcher.finished.connect(self.watcher_finished)
        self.watcher.start()

    def stop_watcher(self):
        if self.watcher:
            self.watcher.stop_flag = True
            self.watcher.wait(5000)
            self.watcher = None

    def watcher_finished(self):
        if self.watcher is not None and self.watcher.isFinished():
            self.watcher = None                    # ended after a press; restarted when idle

    def scanner_button_pressed(self):
        self.stop_watcher()                        # wait until it has released the USB interface
        if self.worker:
            return
        self.cancel_countdown()
        self.scan()

    # ---- background work
    def run_worker(self, fn, args, on_done, label, uses_scanner=False):
        if uses_scanner:
            self.stop_watcher()
        self.worker = Worker(fn, *args)
        self.worker.step.connect(self.step_label.setText)
        self.worker.bar.connect(self.set_bar)
        self.worker.done.connect(lambda r: self.worker_finished(on_done, r))
        self.worker.failed.connect(self.worker_failed)
        self.step_label.setText(label)
        self.progress.show()
        self.set_bar(-1)
        self.update_actions()
        self.worker.start()

    def set_bar(self, v):
        if v < 0:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(v * 1000))

    def worker_finished(self, on_done, result):
        self.worker.wait()
        self.worker = None
        self.progress.hide()
        self.update_actions()
        on_done(result)
        if self.worker is None:                  # nothing new started: idle again
            self.idle()

    def worker_failed(self, msg, details):
        self.worker.wait()
        self.worker = None
        self.progress.hide()
        self.step_label.setText('Failed')
        self.stop_continuous()
        self.update_actions()
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        box.setWindowTitle('SV600')
        box.setText(msg.split('\n\n')[0])
        if '\n\n' in msg:
            cmd = msg.split('\n\n', 1)[1]
            box.setInformativeText(cmd)
            copy = box.addButton('Copy command', QtWidgets.QMessageBox.ButtonRole.ActionRole)
            copy.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(cmd))
        box.setDetailedText(details)
        box.addButton(QtWidgets.QMessageBox.StandardButton.Close)
        box.exec()
        self.idle()

    def idle(self):
        self.start_watcher()

    # ---- continuous scanning
    def toggle_continuous(self, on):
        if on:
            c = self.cfg['continuous']
            how = (f'after each scan the next starts in {c["timer_s"]:g} s' if c['timer_s'] > 0 else
                   'press the Scan button on the scanner (or Space) for each page')
            self.step_label.setText(f'Continuous scanning on: {how}'
                                    + ('; every scan is saved automatically' if c['auto_save'] else '')
                                    + '. Esc stops.')
        else:
            self.cancel_countdown()
            self.step_label.setText('Continuous scanning off')

    def stop_continuous(self):
        if self.a_cont.isChecked():
            self.a_cont.setChecked(False)
        self.cancel_countdown()

    def after_scan_continuous(self):
        """Called when a scan in continuous mode has been processed (and saved, if enabled)."""
        t = self.cfg['continuous']['timer_s']
        if t > 0:
            self.countdown_end = time.time() + t
            self.progress.show()
            self.progress.setRange(0, int(t * 1000))
            self.countdown.start()
            self.countdown_tick()
        else:
            self.step_label.setText('Ready for the next page: press the Scan button on the scanner '
                                    '(or Space). Esc stops continuous scanning.')

    def countdown_tick(self):
        left = self.countdown_end - time.time()
        if left <= 0:
            self.cancel_countdown()
            self.scan()
            return
        self.progress.setValue(int(left * 1000))
        self.step_label.setText(f'Next scan in {left:.0f} s: turn the page. '
                                'Space = scan now, Esc = stop.')

    def cancel_countdown(self):
        if self.countdown.isActive():
            self.countdown.stop()
            self.progress.hide()

    # ---- scanning / loading
    def prefix(self):
        return output.safe_name(self.prefix_edit.text()) if hasattr(self, 'prefix_edit') else ''

    def prefix_changed(self, text):
        self.state['prefix'] = text
        save_state(self.state)
        self.update_actions()

    def scan_plain(self):
        """Scan button: normal file names (output.name pattern)."""
        self.use_prefix = False
        self.scan()

    def scan_prefixed(self):
        """Scan as: PREFIX_001, ...; continuous mode / scanner button keep this until Scan."""
        if not self.prefix():
            self.step_label.setText('Type a file name prefix first')
            self.prefix_edit.setFocus()
            return
        self.use_prefix = True
        self.scan()

    def name_pattern(self, page):
        if getattr(page, 'prefix', ''):
            return f'{page.prefix}_{{n:03}}'
        return self.cfg['output']['name']

    def scan(self):
        """Start a scan with the current naming mode (plain or prefix)."""
        if self.worker:
            return
        self.pending_prefix = self.prefix() if getattr(self, 'use_prefix', False) else ''
        self.cancel_countdown()
        self.scanner_label.setText('Scanner: scanning')
        cfg = json.loads(json.dumps(self.cfg))
        self.run_worker(scan_job, (cfg,), self.scan_done, 'Connecting to the scanner', uses_scanner=True)

    def scan_done(self, result):
        image, hint = result
        self.scan_count += 1
        prefix = getattr(self, 'pending_prefix', '')
        label = f'Scan {self.scan_count}  {time.strftime("%H:%M:%S")}'
        self.add_page(image, f'{prefix}: {label}' if prefix else label, hint, prefix)
        self.detect(then=self.after_detect_continuous if self.a_cont.isChecked() else None)

    def after_detect_continuous(self):
        if not self.a_cont.isChecked():
            return
        if self.cfg['continuous']['auto_save']:
            self.save(then=self.after_scan_continuous)
        else:
            self.after_scan_continuous()

    def open_image(self):
        if self.worker:
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, 'Open scan', self.state.get('open_dir', os.path.expanduser(self.cfg['output']['folder'])),
            'Images (*.jpg *.jpeg *.png *.tif *.tiff *.webp)')
        if not path:
            return
        self.state['open_dir'] = os.path.dirname(path)
        self.run_worker(load_job, (path, json.loads(json.dumps(self.cfg))), self.loaded, f'Opening {path}')

    def loaded(self, result):
        image, name, hint = result
        self.add_page(image, name, hint)
        self.detect()

    def add_page(self, image, label, hint=None, prefix=''):
        self.page_serial = getattr(self, 'page_serial', 0) + 1     # never reused: park files are per number
        page = Page(image, label, self.session_dir, self.page_serial)
        page.hint = hint                   # how to detect objects: see detect_job
        page.prefix = prefix               # file names PREFIX_001 ... ('' = output.name pattern)
        self.pages.append(page)
        self.mark_modified()
        it = QtWidgets.QListWidgetItem(QtGui.QIcon(page.thumb), '')
        it.setData(Qt.ItemDataRole.UserRole, page)
        self.session.addItem(it)
        self.show_page(page)
        self.session.blockSignals(True)
        self.session.setCurrentRow(len(self.pages) - 1)
        self.session.blockSignals(False)
        self.update_session_item(page)

    def update_session_item(self, page):
        for i in range(self.session.count()):
            it = self.session.item(i)
            if it.data(Qt.ItemDataRole.UserRole) is page:
                n = len(page.boxes_now())
                state = 'unsaved' if page.dirty else 'saved'
                it.setText(f'{page.label}\n{n} box(es), {state}')
                it.setForeground(QtGui.QColor('#c0392b') if page.dirty else QtGui.QColor('#27ae60'))
        self.update_actions()

    def session_clicked(self, row):
        if 0 <= row < len(self.pages) and self.pages[row] is not self.page and not self.worker:
            self.show_page(self.pages[row])

    def remove_page(self):
        row = self.session.currentRow()
        if not (0 <= row < len(self.pages)) or self.worker:
            return
        page = self.pages[row]
        if page.dirty:
            r = QtWidgets.QMessageBox.question(
                self, 'Remove scan', 'This scan has not been saved. Remove it from the session anyway?',
                QtWidgets.QMessageBox.StandardButton.Discard | QtWidgets.QMessageBox.StandardButton.Cancel)
            if r != QtWidgets.QMessageBox.StandardButton.Discard:
                return
        self.pages.pop(row)
        self.mark_modified()
        page.drop_files()
        self.session.blockSignals(True)
        self.session.takeItem(row)
        self.session.blockSignals(False)
        if page is self.page:
            self.page = None
            if self.pages:
                nxt = self.pages[min(row, len(self.pages) - 1)]
                self.session.setCurrentRow(self.pages.index(nxt))
                self.show_page(nxt)
            else:
                self.show_placeholder()
        self.update_actions()

    def show_page(self, page):
        if self.page is not None and self.page is not page:
            self.page.park()
        self.page = page
        image = page.image()
        self.boxes = []                  # drop references first: clear() deletes the items and
        self.work = None                 # emits selectionChanged while doing so
        self.scene.clear()
        w, h = page.size
        self.pixmap_item = self.scene.addPixmap(QtGui.QPixmap.fromImage(to_qimage(image)))
        self.pixmap_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self.scene.setSceneRect(QRectF(0, 0, w, h))
        self.work = WorkAreaItem(QRectF(0, 0, w, h))
        self.scene.addItem(self.work)
        saved = self.state.get('work_area')
        if saved and saved.get('size') == [w, h]:
            self.work.set_rect(QRectF(*saved['rect']))
        self.work.changed.connect(self.work_area_changed)
        if page.history:
            self.restore(page.snapshot())
        else:
            self.commit('new')                    # history[0]: no boxes yet
        self.view.fit()
        self.step_label.setText(f'{page.label}: {w} x {h} px = {w / DPI * 25.4:.0f} x {h / DPI * 25.4:.0f} mm')
        self.update_next_name()
        self.thumb_timer.start()
        self.update_actions()

    def work_area_changed(self):
        r = self.work.r
        self.state['work_area'] = {'size': list(self.page.size), 'rect': [r.x(), r.y(), r.width(), r.height()]}
        save_state(self.state)
        self.commit('area')
        self.step_label.setText('Work area changed: press Detect (D) to find objects in it again')

    def reset_work_area(self):
        if self.work:
            self.work.set_rect(self.work.image_rect)
            self.work_area_changed()

    # ---- undo / redo
    def snapshot(self):
        r = self.work.r if self.work else QRectF()
        boxes = tuple((round(b.pos().x(), 2), round(b.pos().y(), 2), round(b.w, 2), round(b.h, 2),
                       round(b.rotation(), 3), b.quarter, b.name, b.height_mm) for b in self.boxes)
        sel = tuple(i for i, b in enumerate(self.boxes) if b.isSelected())
        return ((r.x(), r.y(), r.width(), r.height()), boxes, sel)

    def commit(self, kind):
        """Record the current state as one undo step (merging quick repeats of nudges)."""
        p = self.page
        if p is None:
            return
        self.renumber_only()
        snap = self.snapshot()
        cur = p.snapshot()
        if cur is not None and cur[:2] == snap[:2]:
            p.history[p.pos] = snap                # only the selection changed
            return
        now = time.time()
        if (kind in ('nudge', 'finerot') and kind == p.last_kind and now - p.last_time < 1.0
                and p.pos == len(p.history) - 1 and p.pos > 0):
            p.history[p.pos] = snap
        else:
            del p.history[p.pos + 1:]
            p.history.append(snap)
            p.pos += 1
        p.last_kind, p.last_time = kind, now
        self.thumb_timer.start()
        self.update_session_item(p)
        if kind != 'new':
            self.mark_modified()

    def restore(self, snap):
        for b in self.boxes:
            self.scene.removeItem(b)
        self.boxes = []
        area, boxes, sel = snap
        if self.work is not None and area[2] > 0:
            self.work.set_rect(QRectF(*area))
        for i, (cx, cy, w, h, a, q, name, height) in enumerate(boxes):
            b = self.add_box(cx, cy, w, h, a, q, name, height)
            b.setSelected(i in sel)
        self.renumber_only()
        self.thumb_timer.start()
        if self.page:
            self.update_session_item(self.page)

    def undo(self):
        p = self.page
        if p is None or self.worker or p.pos <= 0:
            return
        p.pos -= 1
        p.last_kind = None
        self.restore(p.snapshot())
        self.step_label.setText(f'Undo ({p.pos} more step(s) available)')

    def redo(self):
        p = self.page
        if p is None or self.worker or p.pos >= len(p.history) - 1:
            return
        p.pos += 1
        p.last_kind = None
        self.restore(p.snapshot())
        self.step_label.setText('Redo')

    # ---- boxes
    def detect(self, then=None):
        if self.page is None or self.worker:
            return
        r = self.work.r
        roi = (r.left(), r.top(), r.right(), r.bottom())
        page = self.page

        def done(rects):
            if page is self.page:
                self.detected(rects)
            if then:
                then()
        self.run_worker(detect_job, (page.image(), self.cfg['detection'], roi, getattr(page, 'hint', None),
                                     self.cfg), done, 'Finding objects')

    def detected(self, rects):
        for b in self.boxes:
            self.scene.removeItem(b)
        self.boxes = []
        margin = self.cfg['split']['margin_mm'] / self.box_scale(self.cfg['split']['object_height_mm'])
        for r in rects:
            self.add_box(*crop.box_from_rect(r, margin))
        self.commit('detect')
        n = len(rects)
        self.step_label.setText(f'{n} object(s) found' if n else
                                'No objects found: draw boxes by dragging on the scan, or adjust the work area')

    def add_box(self, cx, cy, w, h, angle=0.0, quarter=0, name='', height=None):
        if height is None:
            height = self.cfg['split']['object_height_mm']
        b = BoxItem(cx, cy, w, h, angle, quarter, name, height)
        b.changed.connect(self.boxes_changed)
        b.edited.connect(lambda: self.commit('box'))
        b.open_preview.connect(lambda b=b: self.open_preview(b))
        self.scene.addItem(b)
        self.boxes.append(b)
        return b

    def add_box_from_rect(self, r):
        if self.page is None:
            return
        b = self.add_box(r.center().x(), r.center().y(), r.width(), r.height())
        self.scene.clearSelection()
        b.setSelected(True)
        self.commit('add')

    def renumber(self):
        self.renumber_only()
        self.thumb_timer.start()

    def boxes_changed(self):
        self.thumb_timer.start()

    def selected_boxes(self):
        return [b for b in self.boxes if b.isSelected()]

    def select_all(self):
        for b in self.boxes:
            b.setSelected(True)

    def turn(self, steps):
        sel = self.selected_boxes()
        for b in sel:
            b.turn(steps)
        if sel:
            self.commit('turn')

    def delete_selected(self):
        sel = self.selected_boxes()
        for b in sel:
            self.scene.removeItem(b)
            self.boxes.remove(b)
        if sel:
            self.commit('delete')

    def keyPressEvent(self, e):
        sel = self.selected_boxes()
        steps = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0), Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1)}
        if sel and e.key() in steps:
            dx, dy = steps[e.key()]
            k = 20 if e.modifiers() & Qt.KeyboardModifier.ShiftModifier else 3
            for b in sel:
                b.setPos(b.pos() + QPointF(dx * k, dy * k))
                b.changed.emit()
            self.commit('nudge')
            return
        if sel and e.key() in (Qt.Key.Key_BracketLeft, Qt.Key.Key_BracketRight):
            for b in sel:
                b.setRotation(b.rotation() + (0.25 if e.key() == Qt.Key.Key_BracketRight else -0.25))
                b.changed.emit()
            self.commit('finerot')
            return
        super().keyPressEvent(e)

    def toolbar_height_changed(self, v):
        v = round(v * 10, 1)                                # cm in the box, mm inside
        if v and self.box_scale(v) == 1.0:
            self.step_label.setText(f'{cm(v)} is too high for this scanner')
            return
        if self.cfg['split']['object_height_mm'] != v:      # default for the next scans
            self.cfg['split']['object_height_mm'] = v
            try:
                settings.save(self.cfg)
            except (OSError, ValueError):
                pass
        targets = self.selected_boxes() or self.boxes
        changed = [b for b in targets if b.height_mm != v]
        for b in changed:
            b.height_mm = v
        if changed:
            self.commit('height')
            self.step_label.setText(f'Height {cm(v)} for {len(changed)} box(es)'
                                    + ('' if self.selected_boxes() else ' (all on this scan)'))

    def sync_selection(self):
        sel = self.selected_boxes() if hasattr(self, 'height_box') else []
        if len(sel) == 1 and round(sel[0].height_mm / 10, 2) != round(self.height_box.value(), 2):
            self.height_box.blockSignals(True)
            self.height_box.setValue(sel[0].height_mm / 10)
            self.height_box.blockSignals(False)
        self.update_actions()
        self.thumbs.blockSignals(True)
        for i in range(self.thumbs.count()):
            it = self.thumbs.item(i)
            b = it.data(Qt.ItemDataRole.UserRole)
            it.setSelected(b in self.boxes and b.isSelected())
        self.thumbs.blockSignals(False)

    def thumb_selected(self):
        self.scene.blockSignals(True)
        self.scene.clearSelection()
        for it in self.thumbs.selectedItems():
            b = it.data(Qt.ItemDataRole.UserRole)
            if b in self.boxes:
                b.setSelected(True)
                self.view.ensureVisible(b)
        self.scene.blockSignals(False)
        self.update_actions()

    def update_thumbs(self):
        if self.page is None:
            return
        self.renumber_only()
        self.thumbs.blockSignals(True)
        self.thumbs.clear()
        fill = tuple(self.cfg['split']['fill_colour'])
        for b in self.boxes:
            cx, cy, w, h, a = b.box()
            k = self.box_scale(b.height_mm)
            img = crop.extract(self.page.small, (cx / 4, cy / 4, w / 4, h / 4, a), b.quarter, fill, k)
            w, h = w * k, h * k
            pm = QtGui.QPixmap.fromImage(to_qimage(img)).scaled(
                THUMB, THUMB, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            ow, oh = (w, h) if b.quarter % 2 == 0 else (h, w)
            text = f'{b.number}: {ow / DPI * 25.4:.0f} x {oh / DPI * 25.4:.0f} mm'
            if b.height_mm:
                text += f'  (h {cm(b.height_mm)})'
            if b.name:
                text += f'\n{b.name}'
            it = QtWidgets.QListWidgetItem(QtGui.QIcon(pm), text)
            it.setToolTip('Double-click: preview, rotate, name')
            it.setData(Qt.ItemDataRole.UserRole, b)
            self.thumbs.addItem(it)
            it.setSelected(b.isSelected())
        self.thumbs.blockSignals(False)

    def renumber_only(self):
        row = self.cfg['detection']['row_height_mm'] * DPI / 25.4
        self.boxes.sort(key=lambda b: (round(b.pos().y() / row), b.pos().x()))
        for i, b in enumerate(self.boxes, 1):
            if b.number != i:
                b.number = i
                b.update()

    def open_preview(self, box):
        if box not in self.boxes or self.worker:
            return
        ObjectPreview(self, box).exec()

    def head_height(self):
        """Viewpoint height above the desk (mm): the setting if set, else the calibration's."""
        h = self.cfg['correction']['head_height_mm']
        if h:
            return h
        if not hasattr(self, '_cal_head'):
            from . import correct as corr
            try:
                self._cal_head = corr.Calibration(
                    paths.calibration_file(self.cfg['correction']['calibration'])).head_height
            except (OSError, ValueError):
                self._cal_head = None
        return self._cal_head

    def box_scale(self, height_mm):
        try:
            return crop.height_scale(height_mm, self.head_height())
        except ValueError:
            return 1.0

    def default_name(self, box):
        o = self.cfg['output']
        base = self.page.base if self.page and self.page.base else output.next_base(o['folder'], self.name_pattern(self.page))
        return f'{os.path.basename(base)}_{box.number}'

    # ---- saving
    def save(self, then=None, page=None):
        page = page or self.page
        if page is None or self.worker:
            return
        o = self.cfg['output']
        if page.base is None:
            try:
                page.base = output.next_base(o['folder'], self.name_pattern(page))
            except (KeyError, ValueError, IndexError) as e:
                QtWidgets.QMessageBox.warning(self, 'Save', f'Invalid file name pattern: {e}')
                return
        if page is self.page:
            self.renumber_only()
            self.commit('save')
        jobs = [((cx, cy, w, h, a), q, name, self.box_scale(z))
                for cx, cy, w, h, a, q, name, z in page.boxes_now()]
        save_full = not jobs or self.cfg['split']['save_full_scan']
        boxes_at_save = page.boxes_now()

        def done(files):
            self.saved(page, files, boxes_at_save)
            if then:
                then()
        self.run_worker(save_job, (page.image(), jobs, page.base, json.loads(json.dumps(self.cfg)), save_full,
                                   list(page.files), getattr(page, 'hint', None) is not None), done, 'Saving')

    def saved(self, page, files, boxes_at_save):
        page.files = files
        page.saved_boxes = boxes_at_save
        full = [f for f in files if os.path.splitext(f)[0] == page.base]
        if full:                                   # reload from the saved scan; free the parked copy
            page.source = full[0]
            if page is not self.page:
                page.drop_files()
        self.mark_modified()
        self.update_session_item(page)
        self.update_next_name()
        folder = os.path.dirname(files[0]) if files else ''
        self.step_label.setText(f'Saved {len(files)} file(s) to {folder}: ' +
                                ', '.join(os.path.basename(f) for f in files))

    def save_all(self):
        todo = [p for p in self.pages if p.dirty]
        if not todo or self.worker:
            return

        def next_one():
            rest = [p for p in todo if p.dirty]
            if rest:
                self.save(then=next_one, page=rest[0])
            else:
                self.step_label.setText(f'Saved {len(todo)} scan(s)')
        next_one()

    # ---- settings
    def open_settings(self):
        d = SettingsDialog(self.cfg, self)
        if d.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            old = self.cfg
            self.cfg = d.cfg
            self.__dict__.pop('_cal_head', None)
            self.height_box.blockSignals(True)
            self.height_box.setValue(self.cfg['split']['object_height_mm'] / 10)
            self.height_box.blockSignals(False)
            self.apply_display_settings()
            self.thumb_timer.start()
            self.update_next_name()
            notes = []
            if old['correction'] != self.cfg['correction'] or old['brighten'] != self.cfg['brighten']:
                notes.append('correction/brighten changes apply to the next scan')
            if old['detection'] != self.cfg['detection'] or old['split']['margin_mm'] != self.cfg['split']['margin_mm'] \
                    or old['split']['object_height_mm'] != self.cfg['split']['object_height_mm']:
                notes.append('press Detect (D) to apply detection/margin/height changes')
            if old['continuous']['scanner_button'] != self.cfg['continuous']['scanner_button'] \
                    or old['scanner'] != self.cfg['scanner']:
                self.stop_watcher()
                self.start_watcher()
            self.step_label.setText('Settings saved' + (': ' + '; '.join(notes) if notes else ''))

    # ---- session files
    def mark_modified(self):
        if not self.session_modified:
            self.session_modified = True
        self.update_title()

    def update_title(self):
        name = os.path.basename(self.session_path) if self.session_path else 'unsaved session'
        star = ' *' if self.session_modified and self.pages else ''
        self.setWindowTitle(f'{name}{star} - SV600 Scanner {__version__}')

    def confirm_leave_session(self, what):
        """True if it is fine to drop the current session (saved, or the user says so)."""
        if not self.pages or not self.session_modified:
            return True
        unsaved = sum(1 for p in self.pages if p.dirty)
        text = 'This session has changes that are not in a session file.'
        if unsaved:
            text += f' {unsaved} scan(s) have also not been saved as images.'
        box = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Icon.Question, what, text, parent=self)
        save = box.addButton('Save session', QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QtWidgets.QMessageBox.StandardButton.Discard)
        box.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(save)
        box.exec()
        if box.clickedButton() is save:
            self.save_session(then=lambda: self.close() if what == 'Quit' else None)
            return False                       # continue once the save has finished
        return box.standardButton(box.clickedButton()) == QtWidgets.QMessageBox.StandardButton.Discard

    def session_folder(self):
        recent = self.state.get('recent_sessions') or []
        if recent:
            return os.path.dirname(recent[0])
        return os.path.join(os.path.expanduser(self.cfg['output']['folder']), 'sessions')

    def remember_session(self, path):
        recent = [p for p in self.state.get('recent_sessions', []) if p != path]
        self.state['recent_sessions'] = [path] + recent[:7]
        save_state(self.state)

    def fill_recent(self):
        self.recent_menu.clear()
        recent = [p for p in self.state.get('recent_sessions', []) if os.path.exists(p)]
        for p in recent:
            self.recent_menu.addAction(os.path.basename(p), lambda p=p: self.open_session(p)).setToolTip(p)
        if not recent:
            self.recent_menu.addAction('(none)').setEnabled(False)

    def save_session_as(self, then=None):
        if self.worker or not self.pages:
            return
        folder = self.session_folder()
        os.makedirs(folder, exist_ok=True)
        default = self.session_path or os.path.join(folder, time.strftime('session_%Y-%m-%d_%H%M') + session.EXT)
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, 'Save session', default,
                                                        f'SV600 sessions (*{session.EXT})')
        if not path:
            return
        if not path.endswith(session.EXT):
            path += session.EXT
        self.session_path = path
        self.save_session(then=then)

    def save_session(self, then=None):
        if self.worker or not self.pages:
            return
        if not self.session_path:
            self.save_session_as(then=then)
            return
        current = self.pages.index(self.page) if self.page in self.pages else 0

        def done(_):
            self.session_modified = False
            self.remember_session(self.session_path)
            self.update_title()
            self.step_label.setText(f'Session saved: {self.session_path}')
            if then:
                then()
        self.run_worker(save_session_job, (list(self.pages), self.page, self.session_path, current,
                                           self.session_dir), done, 'Saving session')

    def clear_session(self):
        for p in self.pages:
            p.drop_files()
        self.pages, self.page = [], None
        self.session.blockSignals(True)
        self.session.clear()
        self.session.blockSignals(False)
        self.scan_count = 0
        self.session_path, self.session_modified = None, False
        self.show_placeholder()
        self.update_next_name()
        self.update_title()

    def new_session(self):
        if self.worker or not self.confirm_leave_session('New session'):
            return
        self.clear_session()

    def open_session(self, path=None):
        if self.worker or not self.confirm_leave_session('Open session'):
            return
        if not path:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(self, 'Open session', self.session_folder(),
                                                            f'SV600 sessions (*{session.EXT})')
            if not path:
                return

        def done(result):
            pages, current = result
            self.clear_session()
            for i, (meta, png, det, small, size) in enumerate(pages, 1):
                self.page_serial = getattr(self, 'page_serial', 0) + 1
                page = Page.restored(meta, png, det, small, size, self.session_dir, self.page_serial)
                self.pages.append(page)
                it = QtWidgets.QListWidgetItem(QtGui.QIcon(page.thumb), '')
                it.setData(Qt.ItemDataRole.UserRole, page)
                self.session.addItem(it)
                self.update_session_item(page)
            self.scan_count = len(self.pages)
            if self.pages:
                cur = self.pages[min(max(current, 0), len(self.pages) - 1)]
                self.session.blockSignals(True)
                self.session.setCurrentRow(self.pages.index(cur))
                self.session.blockSignals(False)
                self.show_page(cur)
            self.session_path, self.session_modified = path, False
            self.remember_session(path)
            self.update_title()
            self.step_label.setText(f'Session opened: {len(self.pages)} scan(s) from {path}')
        self.run_worker(open_session_job, (path, self.session_dir), done, f'Opening session {path}')

    def closeEvent(self, e):
        if self.worker:
            e.ignore()
            return
        if not self.confirm_leave_session('Quit'):
            e.ignore()
            return
        self.cancel_countdown()
        self.stop_watcher()
        self.state['geometry'] = bytes(self.saveGeometry().toBase64()).decode()
        save_state(self.state)
        import shutil
        shutil.rmtree(self.session_dir, ignore_errors=True)
        e.accept()


# ----------------------------------------------------------------------------- jobs (run in threads)
def postprocess(progress, raw_rgb, cfg):
    """Correction + brighten, as configured. Returns (image, detection hint): the hint is a
    1/4-size copy in the scanner's own colours when the colour calibration was applied (object
    detection is tuned on those), else None."""
    from . import correct as corr
    img, hint = raw_rgb, None
    if cfg['correction']['enabled']:
        progress.step('Correcting geometry, colour and brightness')
        cal = corr.Calibration(paths.calibration_file(cfg['correction']['calibration']))
        colour = cfg['correction']['colour'] and cal.colour is not None
        img = corr.correct(raw_rgb, cal, cfg['correction']['area'], cfg['correction']['flat_field'],
                           colour, detect_copy=colour)
        if colour:
            img, hint = img
    if cfg['brighten']['enabled']:
        from PIL import Image, ImageOps
        progress.step('Brightening')
        img = np.asarray(ImageOps.autocontrast(Image.fromarray(img), cutoff=cfg['brighten']['cutoff_percent']))
    return np.ascontiguousarray(img), hint


def scan_job(progress, cfg):
    from .scanner import SV600
    progress.step('Connecting to the scanner over USB')
    sv = SV600(progress, cfg['scanner'])
    try:
        w, h, raw = sv.scan()
    finally:
        sv.close()
    rgb = np.frombuffer(raw, np.uint8).reshape(h, w, 3)
    return postprocess(progress, rgb, cfg)              # (image, detection hint)


def load_job(progress, path, cfg):
    from PIL import Image
    progress.step('Reading image')
    im = Image.open(path)
    marked = output.COLOUR_MARK in (im.info.get('comment') or b'')
    img = np.asarray(im.convert('RGB'))
    from . import correct as corr
    cal = corr.Calibration(paths.calibration_file(cfg['correction']['calibration']))
    hint = 'undo' if marked else None              # saved colour-calibrated: undo for detection
    if img.shape[:2] == (cal.H, cal.W):            # a raw frame (--no-correct): correct it now
        img, hint = postprocess(progress, img, cfg)
    return np.ascontiguousarray(img), os.path.basename(path), hint


def detect_job(progress, image, det, roi, hint=None, cfg=None):
    progress.step('Finding objects')
    small = hint if isinstance(hint, np.ndarray) else None
    prepare = None
    if isinstance(hint, str) and hint == 'undo' and cfg is not None:
        from . import correct as corr
        cal = corr.Calibration(paths.calibration_file(cfg['correction']['calibration']))
        prepare = lambda im: corr.unapply_colour(im, cal)
    rects, _ = crop.find_objects(image, det, roi=roi, prepare=prepare, small=small)
    return rects


def save_job(progress, image, jobs, base, cfg, save_full, old_files=(), coloured=False):
    from PIL import Image
    o = cfg['output']
    ext = '.' + o['format']
    fill = tuple(cfg['split']['fill_colour'])
    files = []
    total = len(jobs) + bool(save_full)
    k = 0
    if save_full:
        progress.step(f'Saving {os.path.basename(base)}{ext}')
        output.save_image(Image.fromarray(image), base + ext, o['jpeg_quality'], o['png_compression'],
                          output.COLOUR_MARK if coloured else None)
        files.append(base + ext)
        k += 1
        progress.bar(k, total)
    folder = os.path.dirname(base)
    for i, (box, quarter, name, scale) in enumerate(jobs, 1):
        stem = output.safe_name(name)
        path = output.unique_path(folder, stem, ext, files, old_files) if stem else f'{base}_{i}{ext}'
        progress.step(f'Saving {os.path.basename(path)}')
        cut = crop.extract(image, box, quarter, fill, scale)
        output.save_image(Image.fromarray(cut), path, o['jpeg_quality'], o['png_compression'])
        files.append(path)
        k += 1
        progress.bar(k, total)
    for f in old_files:                  # files this app wrote for the page earlier, now stale
        if f not in files:
            try:
                os.remove(f)
            except OSError:
                pass
    return files


def save_session_job(progress, pages, shown, path, current, cache_dir):
    """Encode scans not yet in the session cache (lossless PNG, once), then write the file."""
    todo = [p for p in pages if not (p.png_path and os.path.exists(p.png_path))]
    for i, p in enumerate(todo, 1):
        progress.step(f'Encoding scan {i} of {len(todo)} (lossless, once per scan)')
        progress.bar(i - 1, len(todo))
        path_png = os.path.join(cache_dir, f'session_page{p.number:04d}.png')
        was_loaded = p._image is not None
        session.encode_png(p.image(), path_png)
        p.png_path = path_png
        if not was_loaded and p is not shown:
            p._image = None                       # keep memory low: only the shown scan stays loaded
        if os.path.exists(p.park_path) and p is not shown:
            p.drop_files()                        # the PNG replaces the parked copy
    for p in pages:
        if isinstance(p.hint, np.ndarray) and not (p.det_png and os.path.exists(p.det_png)):
            p.det_png = os.path.join(cache_dir, f'session_page{p.number:04d}_det.png')
            session.encode_png(p.hint, p.det_png)
    progress.step('Writing the session file')
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    session.save(path, [{'meta': p.meta(), 'png': p.png_path,
                         'det_png': p.det_png if isinstance(p.hint, np.ndarray) else None} for p in pages],
                 current, lambda i, n: progress.bar(i, n))
    return path


def open_session_job(progress, path, cache_dir):
    """Read a session file: extract each scan, make its thumbnail copy, free the full image."""
    import cv2
    progress.step('Reading the session file')
    pages, current = session.load(path, cache_dir, lambda i, n: progress.bar(i, n))
    out = []
    for i, p in enumerate(pages, 1):
        progress.step(f'Loading scan {i} of {len(pages)}')
        progress.bar(i - 1, len(pages))
        img = session.read_png(p['png'])
        small = cv2.resize(img, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)
        out.append((p['meta'], p['png'], p['det'], small, (img.shape[1], img.shape[0])))
        del img
    return out, current


def main():
    QtWidgets.QApplication.setApplicationName('SV600 Scanner')
    QtWidgets.QApplication.setDesktopFileName('sv600-scanner')
    app = QtWidgets.QApplication(sys.argv)
    app.setWindowIcon(QtGui.QIcon(os.path.join(paths.DATA_DIR, 'sv600-scanner.png')))
    w = MainWindow()
    w.show()
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        w.run_worker(load_job, (sys.argv[1], w.cfg), w.loaded, f'Opening {sys.argv[1]}')
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
