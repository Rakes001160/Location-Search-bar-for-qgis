# -*- coding: utf-8 -*-
"""
Everything drawn on the map. Nothing is written into the project: pins and
outlines are temporary canvas items (v2.x used project annotations, which
ended up saved inside the .qgz).
"""

import json

from qgis.PyQt.QtCore import Qt, QPointF, QRectF, QTimer, QElapsedTimer
from qgis.PyQt.QtGui import (
    QColor,
    QPainter,
    QPainterPath,
    QPen,
    QFont,
    QFontMetricsF,
)
from qgis.core import (
    QgsGeometry,
    QgsRectangle,
    QgsJsonUtils,
    QgsWkbTypes,
)
from qgis.gui import QgsMapCanvasItem, QgsRubberBand, QgsHighlight

from .compat import log_exception, GEOM_POLYGON, GEOM_LINE, is_deleted
from . import geo

RED = QColor("#EA4335")
BLUE = QColor("#1A73E8")


def _bounce(t):
    """easeOutBounce, 0..1 -> 0..1"""
    n, d = 7.5625, 2.75
    if t < 1 / d:
        return n * t * t
    if t < 2 / d:
        t -= 1.5 / d
        return n * t * t + 0.75
    if t < 2.5 / d:
        t -= 2.25 / d
        return n * t * t + 0.9375
    t -= 2.625 / d
    return n * t * t + 0.984375


def _wrap(text, font, max_w, max_lines):
    fm = QFontMetricsF(font)

    def width(x):
        return fm.horizontalAdvance(x)

    lines, cur = [], ""
    for word in (text or "").replace("\n", " ").split():
        trial = (cur + " " + word).strip()
        if width(trial) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and width(last + "\u2026") > max_w:
            last = last[:-1]
        lines[-1] = last.rstrip(" ,") + "\u2026"
    return [(ln, width(ln)) for ln in lines]


class PinItem(QgsMapCanvasItem):
    """Google-style marker: drops in with a bounce, sends ripple rings out from
    the spot and shows the name and address under the pin with a white halo."""

    DROP_MS = 480
    DROP_H = 70.0
    RIPPLE_MS = 1100
    RIPPLE_GAP = 420
    RIPPLES = 3
    LABEL_W = 175.0

    def __init__(
        self,
        canvas,
        lat,
        lon,
        label="",
        color=RED,
        number=None,
        size=34,
        sublabel="",
        animate=True,
        delay_ms=0,
        ripple=True,
    ):
        super(PinItem, self).__init__(canvas)
        self._canvas = canvas
        self._lat, self._lon = lat, lon
        self._color = QColor(color)
        self._number = number
        self._size = float(size)
        self._animate = bool(animate)
        self._ripple = bool(ripple and animate)
        # space reserved for the drop and ripple animations; fixed for the
        # item's lifetime so the scene index never goes stale
        self._reserve_drop = True
        self._reserve_ripple = True
        self._removed = False
        self._waiting = self._animate and delay_ms > 0
        self._force_t = None
        self._clock = QElapsedTimer()
        self._running = False
        self._timer = QTimer()
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self._title_font = QFont()
        self._title_font.setPointSizeF(10.0)
        self._title_font.setBold(True)
        self._sub_font = QFont()
        self._sub_font.setPointSizeF(9.0)
        self._sub_font.setBold(True)
        self._lines = []
        self._label_w = self._label_h = 0.0
        self.setZValue(1000 if number is None else 900)
        self.set_label(label, sublabel)
        self.updatePosition()
        if self._animate:
            if delay_ms > 0:
                QTimer.singleShot(int(delay_ms), self._start)
            else:
                self._start()

    # --- animation

    def _start(self):
        if self._removed:
            return
        try:
            self._waiting = False
            self._clock.start()
            self._running = True
            self._timer.start()
            self.update()
        except RuntimeError:
            pass

    def pulse(self):
        """Replay the ripple (used when re-zooming to the same place)."""
        self._animate = True
        self._ripple = True
        self._start()

    def _total_ms(self):
        if self._ripple:
            return (
                self.DROP_MS
                + self.RIPPLE_GAP * (self.RIPPLES - 1)
                + self.RIPPLE_MS
                + 60
            )
        return self.DROP_MS + 300

    def _t(self):
        if self._force_t is not None:
            return self._force_t
        if not self._animate or not self._running:
            return 1e9
        return float(self._clock.elapsed())

    def _tick(self):
        try:
            if self._removed:
                self._timer.stop()
                return
            if self._t() > self._total_ms():
                self._timer.stop()
                self._running = False
                self._animate = False
            self.update()
        except RuntimeError:
            try:
                self._timer.stop()
            except RuntimeError:
                pass

    def stop(self):
        """Called before the item leaves the scene: no more repaints."""
        self._removed = True
        try:
            self._timer.stop()
        except RuntimeError:
            pass

    # ---------------------------------------------------------------- label

    def set_label(self, label, sublabel=""):
        if self._removed:
            return
        self.prepareGeometryChange()
        lines = [
            (t, w, self._title_font)
            for t, w in _wrap(label, self._title_font, self.LABEL_W, 2)
        ]
        sub = (sublabel or "").strip()
        if sub and label and sub.lower().startswith(label.lower()):
            sub = sub[len(label):].lstrip(" ,")
        lines += [
            (t, w, self._sub_font)
            for t, w in _wrap(sub, self._sub_font, self.LABEL_W, 3)
        ]
        self._lines = lines
        self._label_w = max([w for _, w, _ in lines] or [0.0])
        self._label_h = sum(QFontMetricsF(f).height() for _, _, f in lines)
        self.update()

    def lat_lon(self):
        return self._lat, self._lon

    def updatePosition(self):
        """Keep the pin tip exactly on its WGS84 point in the current
        map view.
        """
        try:
            pt = geo.from_wgs84(
                self._lat, self._lon, geo.canvas_crs(self._canvas)
            )
            self.setPos(self.toCanvasCoordinates(pt))
        except (
            Exception
        ) as exc:  # report instead of silently leaving the pin at 0,0
            from qgis.core import QgsMessageLog

            QgsMessageLog.logMessage(
                "Pin position failed: %s" % exc, "Location Search"
            )

    def boundingRect(self):
        """Constant apart from the label, which calls prepareGeometryChange.

        Qt's scene index caches this rectangle; changing it without
        prepareGeometryChange() leaves a stale entry that crashes QGIS when
        the pin is later removed and the canvas repaints.
        """
        s = self._size
        half = max(s * 0.6, self._label_w / 2.0 + 6.0)
        if self._reserve_ripple:
            half = max(half, 52.0)
        top = -s * 1.25 - (self.DROP_H if self._reserve_drop else 0.0) - 6.0
        bottom = 12.0
        if self._lines:
            bottom = max(bottom, 10.0 + self._label_h + 6.0)
        if self._reserve_ripple:
            bottom = max(bottom, 32.0)
        return QRectF(-half, top, 2 * half, bottom - top)

    # ---------------------------------------------------------------- paint

    def paint(self, painter, option=None, widget=None):
        if self._waiting:
            return
        t = self._t()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        s = self._size
        if self._ripple:
            for k in range(self.RIPPLES):
                tt = (t - (self.DROP_MS - 80) - k * self.RIPPLE_GAP) / float(
                    self.RIPPLE_MS
                )
                if 0.0 <= tt <= 1.0:
                    e = 1.0 - (1.0 - tt) ** 2
                    rad = 5.0 + 44.0 * e
                    ring = QColor(self._color)
                    ring.setAlphaF(0.7 * (1.0 - tt))
                    fill = QColor(self._color)
                    fill.setAlphaF(0.16 * (1.0 - tt))
                    painter.setPen(QPen(ring, 2.4))
                    painter.setBrush(fill)
                    painter.drawEllipse(QPointF(0, 0), rad, rad * 0.55)
        drop = 0.0
        if t < self.DROP_MS:
            drop = self.DROP_H * (1.0 - _bounce(max(0.0, t) / self.DROP_MS))
        shrink = 1.0 - 0.55 * drop / self.DROP_H
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, int(70 * shrink)))
        painter.drawEllipse(
            QPointF(0, 0), s * 0.17 * shrink, s * 0.065 * shrink
        )
        painter.save()
        painter.translate(0, -drop)
        r = s * 0.34
        cy = -s * 0.88
        path = QPainterPath(QPointF(0, 0))
        path.cubicTo(
            QPointF(-r * 0.12, -r * 0.6),
            QPointF(-r, cy + r * 0.8),
            QPointF(-r, cy),
        )
        path.arcTo(QRectF(-r, cy - r, 2 * r, 2 * r), 180.0, -180.0)
        path.cubicTo(
            QPointF(r, cy + r * 0.8),
            QPointF(r * 0.12, -r * 0.6),
            QPointF(0, 0),
        )
        path.closeSubpath()
        painter.setPen(QPen(self._color.darker(155), 1.3))
        painter.setBrush(self._color)
        painter.drawPath(path)
        painter.setPen(Qt.PenStyle.NoPen)
        if self._number is None:
            painter.setBrush(self._color.darker(165))
            painter.drawEllipse(QPointF(0, cy), r * 0.36, r * 0.36)
        else:
            f = QFont(self._title_font)
            f.setPointSizeF(max(6.5, s * 0.25))
            painter.setFont(f)
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(
                QRectF(-r, cy - r, 2 * r, 2 * r),
                int(Qt.AlignmentFlag.AlignCenter),
                str(self._number),
            )
        painter.restore()
        if not self._lines:
            return
        alpha = 1.0
        if t < 1e8:
            alpha = max(0.0, min(1.0, (t - self.DROP_MS * 0.7) / 260.0))
        if alpha <= 0.0:
            return
        y = 9.0
        halo = QColor(255, 255, 255)
        halo.setAlphaF(0.94 * alpha)
        for i, (text, w, font) in enumerate(self._lines):
            fm = QFontMetricsF(font)
            tp = QPainterPath()
            tp.addText(QPointF(-w / 2.0, y + fm.ascent()), font, text)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(
                QPen(
                    halo,
                    3.8,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                    Qt.PenJoinStyle.RoundJoin,
                )
            )
            painter.drawPath(tp)
            ink = (
                QColor("#202124")
                if font is self._title_font
                else QColor("#3C4043")
            )
            ink.setAlphaF(alpha)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(ink)
            painter.drawPath(tp)
            y += fm.height() * 0.98


def geometry_from_geojson(gj):
    try:
        g = QgsJsonUtils.geometryFromGeoJson(json.dumps(gj))
        if g is not None and not g.isEmpty():
            return g
    except Exception as exc:
        log_exception("read GeoJSON outline", exc)

    def ring(coords):
        return "(" + ", ".join("%r %r" % (c[0], c[1]) for c in coords) + ")"

    t, c = gj.get("type"), gj.get("coordinates")
    try:
        if t == "Polygon":
            wkt = "POLYGON(" + ", ".join(ring(r) for r in c) + ")"
        elif t == "MultiPolygon":
            wkt = (
                "MULTIPOLYGON("
                + ", ".join(
                    "(" + ", ".join(ring(r) for r in p) + ")" for p in c
                )
                + ")"
            )
        elif t == "LineString":
            wkt = "LINESTRING" + ring(c)
        elif t == "MultiLineString":
            wkt = "MULTILINESTRING(" + ", ".join(ring(r) for r in c) + ")"
        else:
            return None
        return QgsGeometry.fromWkt(wkt)
    except Exception:
        return None


class MapPresenter(object):

    def __init__(self, canvas, settings):
        self.canvas = canvas
        self.settings = settings
        self._pins = []
        self._bands = []
        self._highlights = []
        self._anim = None
        self._primary = None
        self._graveyard = []
        self._clear_timer = QTimer()
        self._clear_timer.setSingleShot(True)
        self._clear_timer.timeout.connect(self.clear)
        try:
            canvas.destinationCrsChanged.connect(self._refresh_items)
            canvas.extentsChanged.connect(self._reposition)
        except (AttributeError, TypeError):
            pass

    def unload(self):
        self.clear()
        try:
            self.canvas.destinationCrsChanged.disconnect(self._refresh_items)
            self.canvas.extentsChanged.disconnect(self._reposition)
        except (AttributeError, TypeError, RuntimeError):
            pass

    def _reposition(self):
        for p in self._pins:
            if not is_deleted(p):
                p.updatePosition()

    def _refresh_items(self):
        geo.clear_cache()
        for p in self._pins:
            if not is_deleted(p):
                p.updatePosition()

    # ------------------------------------------------------------ items

    def clear(self):
        self._clear_timer.stop()
        self._stop_anim()
        scene = self.canvas.scene()
        items = self._pins + self._bands + self._highlights
        self._pins, self._bands, self._highlights = [], [], []
        self._primary = None
        for item in items:
            if is_deleted(item):
                continue
            if isinstance(item, PinItem):
                item.stop()
            item.hide()
            if item.scene() is scene:
                scene.removeItem(item)
        self._graveyard = items      # freed on the next clear, never mid-paint
        self.canvas.refresh()

    def has_items(self):
        return bool(self._pins or self._bands or self._highlights)

    def add_pin(
        self,
        lat,
        lon,
        label="",
        color=None,
        number=None,
        size=34,
        sublabel="",
        delay_ms=0,
        primary=False,
    ):
        if color is None:
            color = QColor(self.settings.get("map/pin_color") or "#EA4335")
        show = self.settings.get("map/pin_label")
        animate = self.settings.get("map/flash")
        pin = PinItem(
            self.canvas,
            lat,
            lon,
            label if show else "",
            color,
            number,
            size,
            sublabel=(
                sublabel
                if (show and self.settings.get("map/label_address"))
                else ""
            ),
            animate=animate,
            delay_ms=delay_ms + (0 if primary else 40 * (number or 0)),
            ripple=primary and self.settings.get("map/ripple"),
        )
        self._pins.append(pin)
        if primary or self._primary is None:
            self._primary = pin
        return pin

    def set_primary_label(self, label, sublabel=""):
        if (
            self._primary is not None
            and not is_deleted(self._primary)
            and self.settings.get("map/pin_label")
        ):
            self._primary.set_label(
                label,
                sublabel if self.settings.get("map/label_address") else "",
            )

    def pulse(self):
        if self._primary is not None and not is_deleted(self._primary):
            self._primary.pulse()

    def add_outline(self, geom_wgs84):
        if geom_wgs84 is None or geom_wgs84.isEmpty():
            return None
        g = QgsGeometry(geom_wgs84)
        c = geo.canvas_crs(self.canvas)
        if c.authid() != "EPSG:4326":
            try:
                g.transform(geo.xform(geo.WGS84, c))
            except Exception:
                return None
        is_poly = QgsWkbTypes.geometryType(g.wkbType()) == GEOM_POLYGON
        rb = QgsRubberBand(self.canvas, GEOM_POLYGON if is_poly else GEOM_LINE)
        rb.setStrokeColor(QColor(234, 67, 53, 235))
        rb.setFillColor(
            QColor(234, 67, 53, 22) if is_poly else QColor(0, 0, 0, 0)
        )
        rb.setWidth(2 if is_poly else 4)
        if is_poly:
            rb.setLineStyle(Qt.PenStyle.DashLine)
        rb.setToGeometry(g, None)
        rb.show()
        self._bands.append(rb)
        return g

    def add_highlight(self, geom, layer):
        h = QgsHighlight(self.canvas, geom, layer)
        h.setColor(QColor(234, 67, 53, 230))
        h.setFillColor(QColor(234, 67, 53, 45))
        h.setWidth(3)
        try:
            h.setBuffer(0.6)
        except AttributeError:
            pass
        h.show()
        self._highlights.append(h)

    def flash(self, geom, crs):
        if not self.settings.get("map/flash"):
            return
        try:
            self.canvas.flashGeometries(
                [geom],
                crs,
                QColor(234, 67, 53, 255),
                QColor(234, 67, 53, 0),
                3,
                400,
            )
        except (AttributeError, TypeError):
            pass

    def schedule_auto_clear(self):
        s = self.settings.get("map/auto_clear_s")
        if s and s > 0:
            self._clear_timer.start(int(s * 1000))

    # ------------------------------------------------------------ navigation

    def target_extent(self, cand, feature_geom=None, feature_crs=None):
        """Extent in canvas CRS for a candidate."""
        dst = geo.canvas_crs(self.canvas)
        point_scale = float(self.settings.get("map/point_scale"))
        if feature_geom is not None and not feature_geom.isEmpty():
            try:
                rect = (
                    geo.xform(feature_crs, dst).transformBoundingBox(
                        feature_geom.boundingBox()
                    )
                    if feature_crs.authid() != dst.authid()
                    else QgsRectangle(feature_geom.boundingBox())
                )
            except Exception:
                rect = None
            if rect is not None and (rect.width() > 0 or rect.height() > 0):
                rect.scale(1.25)
                return self._at_least(rect, point_scale * 0.6)
            if rect is not None:
                return self._extent_at(rect.center(), point_scale)
        if cand.extent is not None:
            rect, authid = cand.extent
            try:
                r = (
                    geo.xform(geo.crs(authid), dst).transformBoundingBox(rect)
                    if authid != dst.authid()
                    else QgsRectangle(rect)
                )
                return r
            except Exception as exc:
                log_exception("bookmark extent", exc)
        if cand.bbox and cand.kind not in ("coordinate", "grid", "link"):
            try:
                rect = geo.bbox_to_crs(cand.bbox, dst)
                rect.scale(1.12)
                return self._at_least(rect, point_scale)
            except Exception as exc:
                log_exception("result extent", exc)
        if cand.lat is None:
            return None
        pt = geo.from_wgs84(cand.lat, cand.lon, dst)
        return self._extent_at(pt, float(cand.scale or point_scale))

    def _extent_at(self, center, scale):
        cur = self.canvas.scale() or 1.0
        ext = self.canvas.extent()
        f = scale / cur
        w, h = ext.width() * f, ext.height() * f
        return QgsRectangle(
            center.x() - w / 2,
            center.y() - h / 2,
            center.x() + w / 2,
            center.y() + h / 2,
        )

    def _at_least(self, rect, min_scale):
        """Avoid zooming in further than min_scale on tiny extents."""
        cur = self.canvas.scale() or 1.0
        ext = self.canvas.extent()
        if ext.width() <= 0:
            return rect
        f = max(
            rect.width() / ext.width(),
            rect.height() / max(ext.height(), 1e-12),
        )
        if f * cur < min_scale:
            return self._extent_at(rect.center(), min_scale)
        return rect

    def go_to(self, rect):
        if rect is None or rect.isEmpty() and rect.width() == 0:
            return
        self._stop_anim()
        start = self.canvas.extent()
        if not self.settings.get("map/animate") or start.width() <= 0:
            self.canvas.setExtent(rect)
            self.canvas.refresh()
            return
        steps = 9
        c0, c1 = start.center(), rect.center()
        w0, h0, w1, h1 = (
            start.width(),
            start.height(),
            rect.width(),
            rect.height(),
        )
        state = {"i": 0}
        timer = QTimer(self.canvas)
        timer.setInterval(30)

        def step():
            state["i"] += 1
            t = state["i"] / float(steps)
            e = 1 - (1 - t) ** 3
            cx = c0.x() + (c1.x() - c0.x()) * e
            cy = c0.y() + (c1.y() - c0.y()) * e
            w = w0 * (w1 / w0) ** e if w0 > 0 and w1 > 0 else w1
            h = h0 * (h1 / h0) ** e if h0 > 0 and h1 > 0 else h1
            if state["i"] >= steps:
                self._stop_anim()
                self.canvas.setExtent(rect)
            else:
                self.canvas.setExtent(
                    QgsRectangle(
                        cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
                    )
                )
            self.canvas.refresh()

        timer.timeout.connect(step)
        self._anim = timer
        timer.start()

    def _stop_anim(self):
        if self._anim is not None:
            try:
                self._anim.stop()
                self._anim.deleteLater()
            except RuntimeError as exc:
                log_exception("stop animation", exc)
            self._anim = None

    def fit_points(self, cands):
        dst = geo.canvas_crs(self.canvas)
        pts = [
            geo.from_wgs84(c.lat, c.lon, dst)
            for c in cands
            if c.lat is not None
        ]
        if not pts:
            return None
        rect = QgsRectangle(pts[0], pts[0])
        for p in pts[1:]:
            rect.combineExtentWith(p.x(), p.y())
        if rect.width() == 0 and rect.height() == 0:
            return self._extent_at(
                pts[0], float(self.settings.get("map/point_scale"))
            )
        rect.scale(1.3)
        return self._at_least(
            rect, float(self.settings.get("map/point_scale"))
        )
