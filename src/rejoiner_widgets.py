"""Custom-painted controls and animated navigation widgets."""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import (Qt, QPropertyAnimation, QEasingCurve, QRect, QRectF,
                            QPoint, QPointF, Property, Signal, QParallelAnimationGroup)
from PySide6.QtGui import (QColor, QPainter, QPainterPath, QFont, QPen, QBrush,
                           QPixmap)
from PySide6.QtWidgets import (QWidget, QFrame, QLabel, QGraphicsOpacityEffect,
                               QVBoxLayout, QHBoxLayout, QSizePolicy,
                               QPushButton, QCheckBox, QApplication, QComboBox)

from rejoiner_theme import (BG, CARD, CARD2, FIELD, BORDER, BORDER2, ACCENT, ACCENT_MID,
                      ACCENT_SOFT, TEXT, TEXT2, MUTED, GOOD, WARN, BAD, SIDEBAR,
                      SWITCH_OFF, SWITCH_ON, lerp_qcolor)


# Vector icons used by the navigation and account controls.
def draw_glyph(p: QPainter, name: str, r: QRectF, color: QColor) -> None:
    p.save()
    p.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(color, 1.8)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    cx, cy = r.center().x(), r.center().y()
    w, h = r.width(), r.height()

    if name == "setup":          # sliders / adjustments
        rows = [(-h * 0.26, 0.66), (0.0, 0.34), (h * 0.26, 0.58)]
        for dy, knob in rows:
            y = cy + dy
            p.drawLine(QPointF(r.left() + 2, y), QPointF(r.right() - 2, y))
            kx = r.left() + 2 + knob * (w - 4)
            p.setBrush(QBrush(color))
            p.drawEllipse(QPointF(kx, y), 2.6, 2.6)
            p.setBrush(Qt.NoBrush)
    elif name == "accounts":     # person
        head_r = w * 0.17
        p.setBrush(QBrush(color))
        p.drawEllipse(QPointF(cx, cy - h * 0.18), head_r, head_r)
        p.setBrush(Qt.NoBrush)
        body = QPainterPath()
        bw = w * 0.42
        body.moveTo(cx - bw, r.bottom() - 2)
        body.cubicTo(cx - bw, cy + h * 0.18, cx + bw, cy + h * 0.18,
                     cx + bw, r.bottom() - 2)
        p.drawPath(body)
    elif name == "monitor":      # activity pulse
        path = QPainterPath()
        path.moveTo(r.left() + 1, cy)
        path.lineTo(cx - w * 0.22, cy)
        path.lineTo(cx - w * 0.08, cy - h * 0.30)
        path.lineTo(cx + w * 0.04, cy + h * 0.30)
        path.lineTo(cx + w * 0.18, cy)
        path.lineTo(r.right() - 1, cy)
        p.drawPath(path)
    elif name == "roblox":       # gear (settings)
        import math
        rad = w * 0.27
        for k in range(8):
            a = math.radians(k * 45)
            ca, sa = math.cos(a), math.sin(a)
            p.drawLine(QPointF(cx + ca * rad, cy + sa * rad),
                       QPointF(cx + ca * (rad + w * 0.13),
                               cy + sa * (rad + w * 0.13)))
        p.drawEllipse(QPointF(cx, cy), rad, rad)
        p.setBrush(QBrush(color))
        p.drawEllipse(QPointF(cx, cy), w * 0.085, w * 0.085)
        p.setBrush(Qt.NoBrush)
    p.restore()


# Animated on/off switch used for Boolean settings.
class IOSSwitch(QWidget):
    toggled = Signal(bool)

    def __init__(self, checked: bool = False, parent=None):
        super().__init__(parent)
        self._checked = bool(checked)
        self._knob = 1.0 if self._checked else 0.0
        self.setFixedSize(46, 28)
        self.setCursor(Qt.PointingHandCursor)
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(200)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, v: bool, animate: bool = True):
        v = bool(v)
        self._checked = v
        target = 1.0 if v else 0.0
        self._anim.stop()
        if animate and self.isVisible():
            self._anim.setStartValue(self._knob)
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            self._knob = target
            self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.setChecked(not self._checked)
            self.toggled.emit(self._checked)

    def _get_knob(self):
        return self._knob

    def _set_knob(self, v):
        self._knob = v
        self.update()

    knob = Property(float, _get_knob, _set_knob)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(0, 0, self.width(), self.height())
        track = lerp_qcolor(QColor(SWITCH_OFF), QColor(SWITCH_ON), self._knob)
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        d = r.height() - 6
        x = 3 + self._knob * (r.width() - d - 6)
        # Add a subtle shadow to separate the switch knob from its track.
        p.setBrush(QColor(0, 0, 0, 55))
        p.drawEllipse(QRectF(x, 4.5, d, d))
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(QRectF(x, 3, d, d))


# Application wordmark shown in the navigation header.
class Wordmark(QWidget):
    def __init__(self, display_family: str, parent=None):
        super().__init__(parent)
        self._family = display_family
        base = getattr(
            sys, "_MEIPASS", os.path.dirname(os.path.dirname(__file__)))
        logo_path = os.path.join(base, "rejoiner_logo.png")
        if not os.path.exists(logo_path):
            logo_path = os.path.join(base, "assets", "rejoiner_logo.png")
        self._logo = QPixmap(logo_path)
        self.setFixedHeight(46)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        if not self._logo.isNull():
            side = self.height()
            scaled = self._logo.scaled(
                side, side, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            p.drawPixmap(0, (self.height() - scaled.height()) // 2, scaled)
        f = QFont(self._family, 0, QFont.Bold)
        f.setPixelSize(15)
        p.setFont(f)
        p.setPen(QColor(TEXT))
        p.drawText(QPointF(self.height() + 8, self.height() / 2 + 5),
                   "REJOINER")


# Button used to select a page in the top navigation.
class NavButton(QWidget):
    clicked = Signal(int)

    def __init__(self, index: int, icon: str, label: str, parent=None):
        super().__init__(parent)
        self._index = index
        self._icon = icon
        self._label = label
        self.setToolTip(label)
        self._selected = False
        self._hover = False
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(42)

    def setSelected(self, v: bool):
        self._selected = v
        self.update()

    def enterEvent(self, _):
        self._hover = True
        self.update()

    def leaveEvent(self, _):
        self._hover = False
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self._index)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        if not self._selected and self._hover:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 14))
            p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()), 4, 4)
        fg = QColor("#FFFFFF") if self._selected else QColor(TEXT2)
        if not self._selected and not self._hover:
            fg = QColor(MUTED)
        # Move the icon between the expanded and collapsed sidebar positions.
        raw = max(0.0, min(1.0, (self.width() - 96) / 64.0))
        ix_left = 14.0
        ix_center = (self.width() - 18) / 2.0
        ix = ix_center + (ix_left - ix_center) * raw
        draw_glyph(p, self._icon, QRectF(ix, self.height() / 2 - 9, 18, 18), fg)
        f = QFont()
        f.setPixelSize(14)
        f.setWeight(QFont.DemiBold if self._selected else QFont.Medium)
        p.setFont(f)
        # Fade the label as the sidebar collapses instead of clipping it.
        raw = max(0.0, min(1.0, (self.width() - 96) / 64.0))
        a = raw * raw * (3 - 2 * raw)
        if a > 0.02:
            fg2 = QColor(fg)
            fg2.setAlphaF(fg.alphaF() * a)
            p.setPen(fg2)
            text_rect = QRectF(44, 0, max(1.0, self.width() - 50),
                              self.height())
            p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft,
                       p.fontMetrics().elidedText(
                           self._label, Qt.ElideRight, int(text_rect.width())))


# Control for collapsing and expanding the navigation sidebar.
class SidebarToggle(QWidget):
    """Small chevron button that collapses / expands the sidebar."""
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._collapsed = False
        self._hover = False
        self.setFixedSize(26, 26)
        self.setCursor(Qt.PointingHandCursor)

    def set_collapsed(self, v: bool):
        self._collapsed = bool(v)
        self.update()

    def enterEvent(self, _):
        self._hover = True
        self.update()

    def leaveEvent(self, _):
        self._hover = False
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        if self._hover:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 16))
            p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()), 7, 7)
        col = QColor(TEXT2) if self._hover else QColor(MUTED)
        pen = QPen(col, 1.7)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        cx, cy = self.width() / 2, self.height() / 2
        path = QPainterPath()
        if not self._collapsed:                 # "‹"  → will collapse
            path.moveTo(cx + 3, cy - 5)
            path.lineTo(cx - 3, cy)
            path.lineTo(cx + 3, cy + 5)
        else:                                    # "›"  → will expand
            path.moveTo(cx - 3, cy - 5)
            path.lineTo(cx + 3, cy)
            path.lineTo(cx - 3, cy + 5)
        p.drawPath(path)


# Navigation sidebar containing page links and application status.
class NavRail(QFrame):
    navigated = Signal(int)

    def __init__(self, display_family: str, items, parent=None):
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.EXPANDED_W = 270
        self.COLLAPSED_W = 72
        self._collapsed = False
        self._rw = self.EXPANDED_W
        self.setFixedWidth(self.EXPANDED_W)
        self._wordmark = Wordmark(display_family, self)
        self._subtitle = QLabel("multi-account + game monitor", self)
        self._subtitle.setObjectName("RowSub")
        self._pill = QFrame(self)
        self._pill.setStyleSheet(
            f"background:{ACCENT}; border-radius:1px;")
        self._pill.lower()
        self._buttons = []
        for i, (icon, label) in enumerate(items):
            b = NavButton(i, icon, label, self)
            b.clicked.connect(self.navigated.emit)
            self._buttons.append(b)
        self._footer = QLabel("Roblox Rejoiner", self)
        self._footer.setObjectName("RowSub")
        self._status_cap = QLabel("STATUS", self)
        self._status_cap.setObjectName("Section")
        self._siderule = QFrame(self)
        self._siderule.setObjectName("SideRule")
        self._siderule.setFixedHeight(1)
        f = self._status_cap.font()
        f.setLetterSpacing(QFont.AbsoluteSpacing, 0.8)
        self._status_cap.setFont(f)
        self._status_dot = StatusDot(MUTED, self)
        self._status = QLabel("Idle", self)
        self._status.setObjectName("RowTitle")
        self._collapse_btn = SidebarToggle(self)
        self._collapse_btn.clicked.connect(self.toggle_collapsed)
        self._current = 0
        self._pill_anim = QPropertyAnimation(self._pill, b"geometry", self)
        self._pill_anim.setDuration(340)
        self._pill_anim.setEasingCurve(QEasingCurve.OutQuint)
        self._rail_anim = QPropertyAnimation(self, b"railWidth", self)
        self._rail_anim.setDuration(280)
        self._rail_anim.setEasingCurve(QEasingCurve.OutCubic)

    # Animate sidebar width and child positions between collapsed and expanded.
    def _get_rail_w(self) -> int:
        return self._rw

    def _set_rail_w(self, w: int):
        self._rw = int(w)
        self.setFixedWidth(int(w))

    railWidth = Property(int, _get_rail_w, _set_rail_w)

    def toggle_collapsed(self):
        self.set_collapsed(not self._collapsed)

    def set_collapsed(self, collapsed: bool):
        collapsed = bool(collapsed)
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        self._collapse_btn.set_collapsed(collapsed)
        self._rail_anim.stop()
        self._rail_anim.setStartValue(self.width())
        self._rail_anim.setEndValue(
            self.COLLAPSED_W if collapsed else self.EXPANDED_W)
        self._rail_anim.start()

    def set_status(self, text: str, running: bool = False):
        self._status.setText(text)
        self._status_dot.set_color(GOOD if running else MUTED)

    def apply_theme(self):
        self._pill.setStyleSheet(
            f"background:{ACCENT}; border-radius:1px;")
        self._wordmark.update()
        self.update()

    def _btn_rect(self, i: int) -> QRect:
        top, gap, h = 128, 6, 42
        w = self.width()
        raw = max(0.0, min(1.0, (w - 96) / 64.0))
        if raw >= 0.999:
            return QRect(12, top + i * (h + gap), w - 24, h)
        # Move the active-page marker behind the centered icon when collapsed.
        sq = 46
        fx, fw = 12, w - 24
        cx = int((w - sq) / 2 + (fx - (w - sq) / 2) * raw)
        cw = int(sq + (fw - sq) * raw)
        return QRect(cx, top + i * (h + gap), cw, h)

    def _indicator_rect(self, i: int) -> QRect:
        rect = self._btn_rect(i)
        return QRect(0, rect.y(), 3, rect.height())

    def _fx(self, w: QWidget) -> QGraphicsOpacityEffect:
        fx = w.graphicsEffect()
        if not isinstance(fx, QGraphicsOpacityEffect):
            fx = QGraphicsOpacityEffect(w)
            w.setGraphicsEffect(fx)
        return fx

    def resizeEvent(self, _):
        w = self.width()
        h = self.height()
        # Normalize the width to drive smooth label and icon transitions.
        raw = max(0.0, min(1.0, (w - 120) / float(self.EXPANDED_W - 120)))
        op = raw * raw * (3 - 2 * raw)
        for lbl in (self._wordmark, self._subtitle, self._status_cap,
                    self._status, self._siderule, self._footer):
            lbl.setVisible(True)
            self._fx(lbl).setOpacity(op)
        self._status_dot.setVisible(True)        # dot stays even when collapsed
        self._wordmark.setGeometry(18, 50, max(10, w - 56), 46)
        self._subtitle.setGeometry(18, 90, max(10, w - 32), 16)
        for i, b in enumerate(self._buttons):
            b.setGeometry(self._btn_rect(i))
        self._pill.setGeometry(self._indicator_rect(self._current))
        self._siderule.setGeometry(18, h - 96, max(10, w - 36), 1)
        self._status_cap.setGeometry(18, h - 78, max(10, w - 36), 14)
        self._status.setGeometry(36, h - 61, max(10, w - 52), 20)
        self._footer.setGeometry(18, h - 30, max(10, w - 32), 16)
        # Reposition the status indicator and sidebar toggle with the layout.
        ex_dx, ex_dy = 18.0, h - 57.0
        co_dx, co_dy = (w - 12) / 2.0, h - 40.0
        self._status_dot.move(int(co_dx + (ex_dx - co_dx) * raw),
                              int(co_dy + (ex_dy - co_dy) * raw))
        ex_cx, co_cx = w - 38.0, (w - 26) / 2.0
        self._collapse_btn.move(int(co_cx + (ex_cx - co_cx) * raw), 60)

    def setCurrent(self, i: int, animate: bool = True):
        if not (0 <= i < len(self._buttons)):
            return
        self._current = i
        for j, b in enumerate(self._buttons):
            b.setSelected(j == i)
        target = self._indicator_rect(i)
        self._pill_anim.stop()
        if animate and self.isVisible():
            self._pill_anim.setStartValue(self._pill.geometry())
            self._pill_anim.setEndValue(target)
            self._pill_anim.start()
        else:
            self._pill.setGeometry(target)


class TopNavBar(QFrame):
    """Top-level page selector with the app brand and monitor status."""

    navigated = Signal(int)

    def __init__(self, display_family: str, items, parent=None):
        super().__init__(parent)
        self.setObjectName("TopBar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(26, 10, 26, 10)
        layout.setSpacing(16)

        self._wordmark = Wordmark(display_family, self)
        self._wordmark.setFixedWidth(170)
        layout.addWidget(self._wordmark)

        divider = QFrame(self)
        divider.setObjectName("TopBarDivider")
        divider.setFixedSize(1, 38)
        layout.addWidget(divider)

        page_label = QLabel("PAGE", self)
        page_label.setObjectName("Section")
        layout.addWidget(page_label)

        self._selector = QComboBox(self)
        self._selector.setObjectName("PageSelector")
        self._selector.setMinimumWidth(300)
        self._selector.setMaximumWidth(360)
        for _, label in items:
            self._selector.addItem(label)
        self._selector.currentIndexChanged.connect(self.navigated.emit)
        layout.addWidget(self._selector)
        layout.addStretch(1)

        self._status_dot = StatusDot(MUTED, self)
        layout.addWidget(self._status_dot)
        self._status = QLabel("Idle", self)
        self._status.setObjectName("RowTitle")
        layout.addWidget(self._status)

    def setCurrent(self, index: int, animate: bool = True):
        del animate
        if 0 <= index < self._selector.count():
            self._selector.setCurrentIndex(index)

    def set_status(self, text: str, running: bool = False):
        self._status.setText(text)
        self._status_dot.set_color(GOOD if running else MUTED)

    def apply_theme(self):
        self._wordmark.update()
        self.update()


# Page stack that animates transitions between sections.
class AnimatedStack(QWidget):
    """Cross-fades and lightly slides between pages.

    Qt composites the already-painted page widget, so animating its position +
    opacity is smooth and does not re-rasterise the page's contents per frame.
    Each page keeps one persistent opacity effect (free at opacity 1.0, which Qt
    short-circuits), and any in-flight transition is snapped to its end before a
    new one starts — so rapid nav clicks never leave a half-faded page or fire
    animations at a deleted target."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pages = []
        self._effects = {}
        self._current = -1
        self._group = None
        self._leaving = None
        self._entering = None

    def _effect(self, w: QWidget) -> QGraphicsOpacityEffect:
        fx = self._effects.get(w)
        if fx is None:
            fx = QGraphicsOpacityEffect(w)
            fx.setOpacity(1.0)
            w.setGraphicsEffect(fx)
            self._effects[w] = fx
        return fx

    def addPage(self, w: QWidget):
        w.setParent(self)
        w.setGeometry(self.rect())
        self._effect(w).setOpacity(1.0)
        w.hide()
        self._pages.append(w)
        if self._current == -1:
            self._current = 0
            w.show()

    def count(self):
        return len(self._pages)

    def currentIndex(self):
        return self._current

    def resizeEvent(self, _):
        for w in self._pages:
            w.setGeometry(self.rect())

    def _snap(self):
        """Finish any running transition instantly."""
        if self._group is not None:
            self._group.stop()
            self._group = None
        if self._leaving is not None:
            self._leaving.hide()
            self._effect(self._leaving).setOpacity(1.0)
            self._leaving.move(0, 0)
            self._leaving = None
        if self._entering is not None:
            self._entering.move(0, 0)
            self._entering.setGeometry(self.rect())
            self._effect(self._entering).setOpacity(1.0)
            self._entering = None

    def setCurrent(self, index: int):
        if index == self._current or not (0 <= index < len(self._pages)):
            return
        self._snap()
        old = self._pages[self._current]
        new = self._pages[index]
        forward = index > self._current
        self._current = index

        off = 14 if forward else -14
        new.setGeometry(self.rect())
        new.move(0, off)
        self._effect(new).setOpacity(0.0)
        new.show()
        new.raise_()
        self._effect(old).setOpacity(1.0)
        self._leaving, self._entering = old, new

        dur = 300
        a_new_pos = QPropertyAnimation(new, b"pos", self)
        a_new_pos.setDuration(dur)
        a_new_pos.setEasingCurve(QEasingCurve.OutQuint)
        a_new_pos.setStartValue(QPoint(0, off))
        a_new_pos.setEndValue(QPoint(0, 0))
        a_new_op = QPropertyAnimation(self._effect(new), b"opacity", self)
        a_new_op.setDuration(dur)
        a_new_op.setEasingCurve(QEasingCurve.OutQuint)
        a_new_op.setStartValue(0.0)
        a_new_op.setEndValue(1.0)
        a_old_op = QPropertyAnimation(self._effect(old), b"opacity", self)
        a_old_op.setDuration(int(dur * 0.7))
        a_old_op.setEasingCurve(QEasingCurve.OutQuint)
        a_old_op.setStartValue(1.0)
        a_old_op.setEndValue(0.0)

        grp = QParallelAnimationGroup(self)
        grp.addAnimation(a_new_pos)
        grp.addAnimation(a_new_op)
        grp.addAnimation(a_old_op)
        self._group = grp

        def done():
            old.hide()
            self._effect(old).setOpacity(1.0)
            old.move(0, 0)
            new.move(0, 0)
            new.setGeometry(self.rect())
            self._effect(new).setOpacity(1.0)
            self._group = None
            self._leaving = self._entering = None

        grp.finished.connect(done)
        grp.start()


# Reusable cards, separators, and page layout components.
def make_card() -> QFrame:
    c = QFrame()
    c.setObjectName("Card")
    return c


def hsep() -> QFrame:
    s = QFrame()
    s.setObjectName("Sep")
    s.setFixedHeight(1)
    return s


class StatusDot(QWidget):
    """A filled status dot painted directly (so colour changes always repaint —
    a QLabel's stylesheet colour can fail to refresh live)."""

    def __init__(self, color: str = MUTED, parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self.setFixedSize(12, 12)

    def set_color(self, color: str):
        c = QColor(color)
        if c != self._color:
            self._color = c
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(self._color)
        d = 9.0
        p.drawEllipse(QRectF((self.width() - d) / 2, (self.height() - d) / 2, d, d))


class Avatar(QWidget):
    """Round account avatar: shows a Roblox headshot once loaded, otherwise a
    coloured initial. A small presence dot can be overlaid bottom-right."""

    def __init__(self, name: str = "?", size: int = 38, parent=None):
        super().__init__(parent)
        self._pix = None
        self._name = name or "?"
        self._presence = None            # None / "out" / "online" / "ingame"
        self.setFixedSize(size, size)

    def set_name(self, name: str):
        self._name = name or "?"
        self.update()

    def set_pixmap(self, pix):
        self._pix = pix
        self.update()

    def set_presence(self, state):
        self._presence = state
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        d = self.width()
        rect = QRectF(0, 0, d, d)
        path = QPainterPath()
        path.addEllipse(rect)
        p.save()
        p.setClipPath(path)
        if self._pix is not None and not self._pix.isNull():
            sp = self._pix.scaled(d, d, Qt.KeepAspectRatioByExpanding,
                                  Qt.SmoothTransformation)
            p.drawPixmap(0, 0, sp)
        else:
            hue = abs(hash(self._name)) % 360
            p.setPen(Qt.NoPen)
            p.setBrush(QColor.fromHsv(hue, 110, 150))
            p.drawEllipse(rect)
            p.setPen(QColor("white"))
            f = QFont()
            f.setPixelSize(int(d * 0.42))
            f.setBold(True)
            p.setFont(f)
            p.drawText(rect, Qt.AlignCenter, (self._name[:1] or "?").upper())
        p.restore()
        p.setPen(QPen(QColor(255, 255, 255, 22), 1))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(rect.adjusted(0.5, 0.5, -0.5, -0.5))
        if self._presence:
            col = {"ingame": QColor(GOOD), "online": QColor(ACCENT_MID),
                   "out": QColor(MUTED)}.get(self._presence, QColor(MUTED))
            dd = max(8, int(d * 0.30))
            p.setPen(QPen(QColor(BG), 2))
            p.setBrush(col)
            p.drawEllipse(QRectF(d - dd, d - dd, dd, dd))


class StatTile(QFrame):
    def __init__(self, label: str, value: str = "—", parent=None):
        super().__init__(parent)
        self.setObjectName("Inset")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(2)
        self.value_lbl = QLabel(value)
        self.value_lbl.setObjectName("Stat")
        self.label_lbl = QLabel(label)
        self.label_lbl.setObjectName("StatLabel")
        lay.addWidget(self.value_lbl)
        lay.addWidget(self.label_lbl)

    def set_value(self, v: str):
        self.value_lbl.setText(v)
