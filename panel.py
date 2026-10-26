# -*- coding: utf-8 -*-
"""Search panel (docked above the Layers panel, like Google Earth Pro)."""

import os
import time

from qgis.PyQt.QtCore import Qt, QEvent, QSize, QRect, QUrl, pyqtSignal
from qgis.PyQt.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QPalette,
    QStandardItem,
    QStandardItemModel,
    QDesktopServices,
)
from qgis.PyQt.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListView,
    QMenu,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QAbstractItemView,
    QHeaderView,
    QMessageBox,
)
from qgis.core import (
    QgsProject,
    QgsVectorLayer,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsReferencedRectangle,
)
from qgis.gui import QgsFilterLineEdit

from .compat import run_dialog, MSG_INFO, MSG_WARNING, MSG_SUCCESS
from .core import formats as F
from .core.candidate import Candidate
from . import geo
from .map_display import BLUE

ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")
KEY_ROLE = int(Qt.ItemDataRole.UserRole) + 1
PLACES_PROP = "location_searchbar/places"


def icon(name):
    return QIcon(os.path.join(ICON_DIR, name + ".svg"))


class ResultDelegate(QStyledItemDelegate):
    """Two-line rows: icon, bold title, muted subtitle, source tag and
    distance.
    """

    def __init__(self, panel):
        super(ResultDelegate, self).__init__(panel)
        self.panel = panel

    def sizeHint(self, option, index):
        fm = QFontMetrics(option.font)
        return QSize(200, int(fm.height() * 2 + 12))

    def paint(self, painter, option, index):
        cand = self.panel.candidate(index.data(KEY_ROLE))
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        opt.icon = QIcon()
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(
            QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget
        )
        if cand is None:
            return
        painter.save()
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        pal = opt.palette
        text_c = pal.color(
            QPalette.ColorRole.HighlightedText
            if selected
            else QPalette.ColorRole.Text
        )
        muted = QColor(text_c)
        muted.setAlpha(150)
        r = opt.rect.adjusted(6, 4, -6, -4)
        ic = icon(cand.icon)
        ic.paint(
            painter, QRect(r.left(), r.top() + (r.height() - 18) // 2, 18, 18)
        )
        left = r.left() + 26
        bold = QFont(opt.font)
        bold.setBold(True)
        small = QFont(opt.font)
        small.setPointSizeF(max(7.0, opt.font.pointSizeF() - 1.0))
        fm_small = QFontMetrics(small)
        tag = cand.source_label
        if cand.distance_km is not None and cand.source not in ("parser",):
            tag = "%s  %s" % (F.fmt_distance(cand.distance_km), tag)
        tag_w = fm_small.boundingRect(tag).width() + 4
        line_h = r.height() // 2
        painter.setFont(bold)
        painter.setPen(text_c if cand.enabled else muted)
        title_rect = QRect(left, r.top(), r.right() - left - tag_w - 6, line_h)
        painter.drawText(
            title_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            QFontMetrics(bold).elidedText(
                cand.title, Qt.TextElideMode.ElideRight, title_rect.width()
            ),
        )
        painter.setFont(small)
        painter.setPen(muted)
        painter.drawText(
            QRect(r.right() - tag_w, r.top(), tag_w, line_h),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            tag,
        )
        sub = cand.subtitle
        if (
            cand.type_label
            and cand.source not in ("parser",)
            and cand.type_label.lower() not in sub.lower()
        ):
            sub = "%s, %s" % (cand.type_label, sub) if sub else cand.type_label
        if cand.note:
            sub = "%s. %s" % (cand.note, sub)
        sub_rect = QRect(
            left, r.top() + line_h, r.right() - left, r.height() - line_h
        )
        painter.drawText(
            sub_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            fm_small.elidedText(
                sub, Qt.TextElideMode.ElideRight, sub_rect.width()
            ),
        )
        painter.restore()


class SearchPanel(QWidget):
    whatsHereToggled = pyqtSignal(bool)

    def __init__(self, plugin, parent=None):
        super(SearchPanel, self).__init__(parent)
        self.plugin = plugin
        self.iface = plugin.iface
        self.canvas = plugin.iface.mapCanvas()
        self.settings = plugin.settings
        self.engine = plugin.engine
        self.presenter = plugin.presenter
        self._cands = {}
        self._user_nav = False
        self._current = None
        self._reverse_info = None
        self._typed = False
        self._auto_key = None
        self._auto_cand = None
        self._build()
        from qgis.PyQt.QtCore import QTimer

        self._auto_timer = QTimer(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.setInterval(650)
        self._auto_timer.timeout.connect(self._auto_open)
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(260)
        self._preview_timer.timeout.connect(self._preview_current)
        e = self.engine
        e.resultsChanged.connect(self._fill)
        e.busyChanged.connect(self._busy)
        e.statusChanged.connect(self._status)
        e.goRequested.connect(self.open_candidate)
        e.notify.connect(self._notify)

    # ------------------------------------------------------------ UI

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(3)
        row = QHBoxLayout()
        row.setSpacing(3)
        self.input = QgsFilterLineEdit()
        self.input.setShowSearchIcon(True)
        self.input.setPlaceholderText(
            "Search places, coordinates, Plus Codes, links, layers"
        )
        self.input.setToolTip(
            "Type a place, address, coordinates (any format), UTM/MGRS, Plus"
            " Code,\na Google Maps / Earth / OSM link or a value from your"
            " layers.\nEnter opens the best match, Shift+Enter shows all"
            " results on the map."
        )
        self.input.textEdited.connect(self._edited)
        try:
            self.input.cleared.connect(lambda: self._edited(""))
        except AttributeError:
            pass
        self.input.installEventFilter(self)
        self.go_btn = QPushButton("Search")
        self.go_btn.setObjectName("LSBGo")
        self.go_btn.clicked.connect(
            lambda: self.engine.submit(self.input.text())
        )
        self.here_btn = QToolButton()
        self.here_btn.setIcon(icon("whatshere"))
        self.here_btn.setCheckable(True)
        self.here_btn.setToolTip(
            "What's here? Click the map to get the address and coordinates"
        )
        self.here_btn.toggled.connect(self.whatsHereToggled.emit)
        self.menu_btn = QToolButton()
        self.menu_btn.setIcon(icon("menu"))
        self.menu_btn.setToolTip("Saved places, recent searches and options")
        self.menu_btn.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.menu = QMenu(self)
        self.menu.aboutToShow.connect(self._build_menu)
        self.menu_btn.setMenu(self.menu)
        row.addWidget(self.input, 1)
        row.addWidget(self.go_btn)
        row.addWidget(self.here_btn)
        row.addWidget(self.menu_btn)
        lay.addLayout(row)

        self.status = QLabel("")
        self.status.setObjectName("LSBStatus")
        self.status.setWordWrap(True)
        self.status.setVisible(False)
        lay.addWidget(self.status)

        self.model = QStandardItemModel(self)
        self.list = QListView()
        self.list.setObjectName("LSBList")
        self.list.setModel(self.model)
        self.list.setItemDelegate(ResultDelegate(self))
        self.list.setUniformItemSizes(True)
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.list.setMouseTracking(True)
        self.list.clicked.connect(lambda idx: self._open_index(idx))
        self.list.activated.connect(lambda idx: self._open_index(idx))
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._list_menu)
        self.list.setVisible(False)
        self.list.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        lay.addWidget(self.list)

        self.card = self._build_card()
        self.card.setVisible(False)
        lay.addWidget(self.card)
        lay.addStretch(1)
        self.setStyleSheet(
            "#LSBGo { background:#1A73E8; color:white; border:none;"
            " border-radius:4px; padding:4px 10px; font-weight:600; }"
            " #LSBGo:hover { background:#1765CC; } #LSBGo:pressed {"
            " background:#1557B0; } #LSBStatus { color:palette(mid); padding:0"
            " 2px; } #LSBCard { background:palette(base); border:1px solid"
            " palette(mid); border-radius:6px; } #LSBTitle { font-size:12pt;"
            " font-weight:600; }"
        )

    def _build_card(self):
        card = QFrame()
        card.setObjectName("LSBCard")
        v = QVBoxLayout(card)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(3)
        top = QHBoxLayout()
        self.c_title = QLabel()
        self.c_title.setObjectName("LSBTitle")
        self.c_title.setWordWrap(True)
        self.c_title.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        close = QToolButton()
        close.setIcon(icon("close"))
        close.setAutoRaise(True)
        close.setToolTip("Close and remove the pin")
        close.clicked.connect(self.clear_all)
        top.addWidget(self.c_title, 1)
        top.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        v.addLayout(top)
        self.c_sub = QLabel()
        self.c_sub.setWordWrap(True)
        self.c_sub.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.c_meta = QLabel()
        self.c_meta.setStyleSheet("color: palette(mid);")
        self.c_meta.setWordWrap(True)
        v.addWidget(self.c_sub)
        v.addWidget(self.c_meta)

        acts = QHBoxLayout()
        acts.setSpacing(2)

        def tb(name, tip, slot):
            b = QToolButton()
            b.setIcon(icon(name))
            b.setToolTip(tip)
            b.setAutoRaise(True)
            b.clicked.connect(slot)
            acts.addWidget(b)
            return b

        tb(
            "zoom",
            "Zoom to this place again",
            lambda: self._current
            and self.open_candidate(self._current, False, True),
        )
        tb("whatshere", "Flash the location again", self.presenter.pulse)
        self.open_btn = QToolButton()
        self.open_btn.setIcon(icon("globe"))
        self.open_btn.setToolTip(
            "Open in Google Maps, Google Earth, Street View or OpenStreetMap"
        )
        self.open_btn.setAutoRaise(True)
        self.open_btn.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        om = QMenu(self)
        for label, fn in (
            ("Google Maps", F.link_google_maps),
            ("Google Earth", F.link_google_earth),
            ("Street View", F.link_street_view),
            ("Directions in Google Maps", F.link_directions),
            ("OpenStreetMap", F.link_osm),
            ("Bing Maps", F.link_bing),
        ):
            a = om.addAction(label)
            a.triggered.connect(lambda _=False, f=fn: self._open_link(f))
        om.addSeparator()
        om.addAction("Copy Google Maps link").triggered.connect(
            lambda: self._current
            and self._copy(
                F.link_google_maps(self._current.lat, self._current.lon)
            )
        )
        self.open_btn.setMenu(om)
        acts.addWidget(self.open_btn)
        tb(
            "savepin",
            "Save this place to the 'Saved places' layer",
            self._save_to_layer,
        )
        self.fav_btn = tb(
            "star", "Add to saved places in this panel", self._toggle_favorite
        )
        tb(
            "bookmark",
            "Create a spatial bookmark of the current view",
            self._add_bookmark,
        )
        tb("copy", "Copy all details", self._copy_all)
        acts.addStretch(1)
        v.addLayout(acts)

        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(6)
        self.grid.setVerticalSpacing(1)
        self.grid.setColumnStretch(1, 1)
        v.addLayout(self.grid)
        self._rows = []
        self.attrs = QTableWidget(0, 2)
        self.attrs.horizontalHeader().setVisible(False)
        self.attrs.verticalHeader().setVisible(False)
        self.attrs.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.attrs.horizontalHeader().setStretchLastSection(True)
        self.attrs.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.attrs.setMaximumHeight(170)
        self.attrs.setVisible(False)
        v.addWidget(self.attrs)
        return card

    # ------------------------------------------------------------ input

    def focus_search(self):
        self.input.setFocus()
        self.input.selectAll()
        if not self.input.text().strip():
            self.engine.set_text("")

    def _edited(self, text):
        self._user_nav = False
        self._typed = True
        self._auto_timer.stop()
        self.engine.set_text(text)

    def _auto_open(self):
        c = self._auto_cand
        if c is not None and self.engine.text and c.key in self._cands:
            self.open_candidate(c, keep_text=True, keep_list=True)

    def _preview_current(self):
        idx = self.list.currentIndex()
        c = self.candidate(idx.data(KEY_ROLE)) if idx.isValid() else None
        if c is not None and c.enabled and not c.lazy and c.has_location:
            self.open_candidate(c, keep_text=True, preview=True)

    def eventFilter(self, obj, event):
        if obj is self.input:
            et = event.type()
            if et == QEvent.Type.FocusIn and not self.input.text().strip():
                self.engine.set_text("")
            elif et == QEvent.Type.KeyPress:
                key = event.key()
                n = self.model.rowCount()
                if key in (Qt.Key.Key_Down, Qt.Key.Key_Up) and n:
                    if not self.list.isVisible():
                        self.list.setVisible(True)
                    row = (
                        self.list.currentIndex().row()
                        if self.list.currentIndex().isValid()
                        else -1
                    )
                    row = (
                        (row + 1) % n
                        if key == Qt.Key.Key_Down
                        else (n - 1 if row <= 0 else row - 1)
                    )
                    self.list.setCurrentIndex(self.model.index(row, 0))
                    self._user_nav = True
                    self._auto_timer.stop()
                    if self.settings.get("map/preview_on_select"):
                        self._preview_timer.start()
                    return True
                if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    shift = bool(
                        event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                    )
                    idx = self.list.currentIndex()
                    if (
                        self._user_nav
                        and idx.isValid()
                        and self.list.isVisible()
                        and not shift
                    ):
                        self._open_index(idx)
                    else:
                        self.engine.submit(self.input.text(), show_all=shift)
                    return True
                if key == Qt.Key.Key_Escape:
                    if self.list.isVisible():
                        self.list.setVisible(False)
                    elif self.card.isVisible():
                        self.card.setVisible(False)
                    else:
                        self.input.clear()
                        self.engine.set_text("")
                    return True
        return super(SearchPanel, self).eventFilter(obj, event)

    # ------------------------------------------------------------ results

    def candidate(self, key):
        return self._cands.get(key)

    def _fill(self):
        prev = (
            self.list.currentIndex().data(KEY_ROLE)
            if self.list.currentIndex().isValid()
            else None
        )
        res = self.engine.results[
            : max(4, self.settings.get("search/max_results"))
        ]
        self._cands = {c.key: c for c in res}
        self.model.clear()
        sel_row = -1
        for i, c in enumerate(res):
            it = QStandardItem(c.title)
            it.setData(c.key, KEY_ROLE)
            it.setToolTip(
                "%s\n%s" % (c.title, c.subtitle) if c.subtitle else c.title
            )
            self.model.appendRow(it)
            if c.key == prev:
                sel_row = i
        if sel_row >= 0 and self._user_nav:
            self.list.setCurrentIndex(self.model.index(sel_row, 0))
        else:
            self.list.clearSelection()
            self.list.setCurrentIndex(self.model.index(-1, 0))
        self._resize_list()
        if res and self._typed and self.settings.get("search/auto_go"):
            top = res[0]
            if (
                top.decisive
                and top.kind in ("coordinate", "grid", "pluscode", "link")
                and top.key != self._auto_key
            ):
                self._auto_key = top.key
                self._auto_cand = top
                self._auto_timer.start()

    def _resize_list(self):
        n = self.model.rowCount()
        if not n:
            self.list.setVisible(False)
            return
        rows = min(n, 5 if self.card.isVisible() else 8)
        h = self.list.sizeHintForRow(0) if n else 36
        self.list.setFixedHeight(rows * h + 4)
        self.list.setVisible(True)

    def _busy(self, busy):
        try:
            self.input.setShowSpinner(busy)
        except AttributeError:
            pass

    def _status(self, text):
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def _notify(self, text, level):
        self.iface.messageBar().pushMessage(
            "Location Search",
            text,
            MSG_WARNING if level == "warning" else MSG_INFO,
            5,
        )

    def _open_index(self, idx):
        c = self.candidate(idx.data(KEY_ROLE))
        if c is not None and c.enabled:
            self.open_candidate(c)

    def _list_menu(self, pos):
        idx = self.list.indexAt(pos)
        c = self.candidate(idx.data(KEY_ROLE)) if idx.isValid() else None
        if c is None:
            return
        m = QMenu(self)
        m.addAction("Go there").triggered.connect(
            lambda: self.open_candidate(c)
        )
        m.addAction("Show all results on map").triggered.connect(
            lambda: self.open_candidate(self.engine.results[0], True)
        )
        if c.lat is not None:
            m.addAction("Copy coordinates").triggered.connect(
                lambda: self._copy(F.fmt_dd(c.lat, c.lon, 7))
            )
            m.addAction("Open in Google Maps").triggered.connect(
                lambda: QDesktopServices.openUrl(
                    QUrl(F.link_google_maps(c.lat, c.lon))
                )
            )
        m.addAction("Copy name").triggered.connect(lambda: self._copy(c.title))
        if c.source == "favorite":
            m.addAction("Remove from saved places").triggered.connect(
                lambda: (
                    self.settings.remove_favorite(c.title, c.lat, c.lon),
                    self.engine.set_text(self.input.text()),
                )
            )
        if c.source == "history":
            m.addAction("Clear recent searches").triggered.connect(
                lambda: (
                    self.settings.clear_history(),
                    self.engine.set_text(self.input.text()),
                )
            )
        run_dialog(m, self.list.viewport().mapToGlobal(pos))

    # --- open a result

    def open_candidate(
        self,
        cand,
        show_all=False,
        keep_text=False,
        preview=False,
        keep_list=False,
    ):
        if cand is None:
            return
        if cand.lazy:
            self._status("Locating %s" % cand.title)

            def done(res, err):
                if res is None:
                    self._notify(
                        err or "Could not locate this result", "warning"
                    )
                    self._status(err or "")
                    return
                if not res.query:
                    res.query = cand.query or self.input.text()
                self.open_candidate(
                    res, show_all, keep_text, preview, keep_list
                )

            self.engine.resolve(cand, done)
            return
        p = self.presenter
        p.clear()
        geom = layer = None
        if cand.layer_id is not None:
            layer = QgsProject.instance().mapLayer(cand.layer_id)
            if layer is not None:
                feat = layer.getFeature(cand.fid)
                if feat.isValid():
                    cand.attributes = [
                        (f.name(), feat.attribute(f.name()))
                        for f in layer.fields()
                    ]
                    if feat.hasGeometry():
                        geom = QgsGeometry(feat.geometry())
                        try:
                            pt = geom.pointOnSurface().asPoint()
                        except Exception:
                            pt = geom.centroid().asPoint()
                        ll = geo.to_wgs84(pt.x(), pt.y(), layer.crs())
                        if ll:
                            cand.lat, cand.lon = ll
                        p.add_highlight(geom, layer)
                        if self.settings.get("map/select_feature"):
                            layer.selectByIds([cand.fid])
        if cand.lat is None and cand.extent is None:
            self._notify("This result has no location", "warning")
            return
        outline = None
        if cand.geojson and self.settings.get("map/boundary"):
            from .map_display import geometry_from_geojson

            outline = p.add_outline(geometry_from_geojson(cand.geojson))
        others = []
        if show_all:
            others = [
                c
                for c in self.engine.results
                if c.lat is not None
                and c.key != cand.key
                and not c.lazy
                and c.source not in ("history", "favorite")
            ][:19]
            for i, o in enumerate(others):
                p.add_pin(o.lat, o.lon, "", None, i + 2, 22)
        if cand.lat is not None:
            delay = 300 if self.settings.get("map/animate") else 0
            sub = "" if cand.source == "parser" else cand.subtitle
            if cand.source == "local" and cand.field:
                sub = self._feature_label(cand) or sub
            p.add_pin(
                cand.lat,
                cand.lon,
                cand.title,
                None,
                1 if show_all else None,
                34,
                sublabel=sub,
                delay_ms=delay,
                primary=True,
            )
        if show_all and others:
            rect = p.fit_points([cand] + others)
        else:
            rect = p.target_extent(
                cand, geom, layer.crs() if layer is not None else None
            )
        p.go_to(rect)
        if geom is not None:
            p.flash(geom, layer.crs())
        elif outline is not None:
            p.flash(outline, geo.canvas_crs(self.canvas))
        elif cand.lat is not None:
            p.flash(
                QgsGeometry.fromPointXY(
                    geo.from_wgs84(
                        cand.lat, cand.lon, geo.canvas_crs(self.canvas)
                    )
                ),
                geo.canvas_crs(self.canvas),
            )
        p.schedule_auto_clear()
        if not keep_text and cand.source not in ("parser",):
            self.input.setText(cand.title)
        if (
            not preview
            and cand.source not in ("whatshere",)
            and cand.lat is not None
        ):
            entry = cand.to_history()
            entry["query"] = cand.query or self.input.text()
            self.settings.add_history(entry)
        self.list.setVisible(
            (bool(show_all) or preview or keep_list)
            and self.model.rowCount() > 0
        )
        self.show_details(cand)
        if show_all:
            self._status(
                "%d results on the map, numbered by rank" % (len(others) + 1)
            )

    def _feature_label(self, cand):
        """Second line under the pin for a layer feature: other text
        attributes +
        layer name.
        """
        parts = []
        for name, val in cand.attributes or []:
            if name == cand.field or val is None:
                continue
            sval = str(val).strip()
            if (
                sval
                and sval != "NULL"
                and not sval.replace(".", "", 1).isdigit()
                and len(sval) < 40
            ):
                parts.append(sval)
            if len(parts) >= 2:
                break
        lyr = (
            QgsProject.instance().mapLayer(cand.layer_id)
            if cand.layer_id
            else None
        )
        if lyr is not None:
            parts.append(lyr.name())
        return ", ".join(parts)

    # ------------------------------------------------------------ details card

    def show_point(self, lat, lon):
        c = Candidate(
            "coordinate",
            F.fmt_dd(lat, lon),
            "Point on the map",
            lat,
            lon,
            source="whatshere",
            type_label="Dropped pin",
        )
        self.presenter.clear()
        self.presenter.add_pin(
            lat, lon, F.fmt_dd(lat, lon, 5), BLUE, primary=True
        )
        self.show_details(c)

    def show_details(self, cand):
        self._current = cand
        self._reverse_info = None
        if not self.settings.get("ui/details"):
            self.card.setVisible(False)
            return
        self.c_title.setText(cand.title)
        self.c_sub.setText(cand.subtitle)
        self.c_sub.setVisible(bool(cand.subtitle))
        src = (
            (
                "from layer"
                if cand.source == "local"
                else "from %s" % cand.source_label
            )
            if cand.source not in ("parser", "whatshere")
            else ""
        )
        meta = [
            x
            for x in (
                cand.type_label,
                src,
                (
                    (
                        "about %s accurate"
                        % F.fmt_distance(cand.precision_m / 1000.0)
                    )
                    if cand.precision_m and cand.precision_m > 5
                    else ""
                ),
            )
            if x
        ]
        self.c_meta.setText(", ".join(meta))
        self.c_meta.setVisible(bool(meta))
        self._fill_rows(cand)
        if cand.attributes:
            self.attrs.setRowCount(0)
            for name, val in cand.attributes[:60]:
                r = self.attrs.rowCount()
                self.attrs.insertRow(r)
                self.attrs.setItem(r, 0, QTableWidgetItem(name))
                self.attrs.setItem(
                    r,
                    1,
                    QTableWidgetItem(
                        "" if val is None or str(val) == "NULL" else str(val)
                    ),
                )
            self.attrs.resizeRowsToContents()
            rh = self.attrs.rowHeight(0) if self.attrs.rowCount() else 20
            self.attrs.setFixedHeight(min(170, rh * self.attrs.rowCount() + 4))
            self.attrs.setVisible(True)
        else:
            self.attrs.setVisible(False)
        self._update_fav_icon()
        self.card.setVisible(True)
        self._resize_list()
        if (
            cand.lat is not None
            and cand.kind in ("coordinate", "grid", "pluscode", "link")
            and cand.source in ("parser", "whatshere")
        ):
            key = cand.key

            def got(info, err):
                if (
                    self._current is None
                    or self._current.key != key
                    or not info
                ):
                    return
                self._reverse_info = info
                if cand.source == "whatshere" and info.get("title"):
                    self.c_title.setText(info["title"])
                if info.get("title") or info.get("address"):
                    self.presenter.set_primary_label(
                        info.get("title") or cand.title,
                        info.get("address", ""),
                    )
                self.c_sub.setText(info.get("address", ""))
                self.c_sub.setVisible(bool(info.get("address")))
                self.c_meta.setText(
                    ", ".join(
                        x
                        for x in (
                            self.c_meta.text(),
                            "address from %s" % info.get("source"),
                        )
                        if x
                    )
                )
                self.c_meta.setVisible(True)
                self._fill_rows(cand)

            self.engine.reverse(cand.lat, cand.lon, got)

    def _format_rows(self, cand):
        lat, lon = cand.lat, cand.lon
        if lat is None:
            return []
        rows = [
            ("Lat, lon", F.fmt_dd(lat, lon, 7)),
            ("DMS", F.fmt_dms(lat, lon)),
            ("DDM", F.fmt_ddm(lat, lon)),
        ]
        zone, north, epsg, e, n, band = geo.utm_for(lat, lon)
        if e is not None:
            rows.append(
                (
                    "UTM",
                    "%d%s %.0f E %.0f N"
                    % (zone, band or ("N" if north else "S"), e, n),
                )
            )
            m = geo.mgrs_for(lat, lon)
            if m:
                rows.append(("MGRS", m))
        pc = F.fmt_pluscode(lat, lon, 11)
        locality = (self._reverse_info or {}).get("locality")
        short = F.fmt_short_pluscode(lat, lon, locality) if locality else ""
        rows.append(("Plus Code", "%s  (%s)" % (short, pc) if short else pc))
        c = geo.canvas_crs(self.canvas)
        if c.authid() not in ("EPSG:4326", "EPSG:%d" % epsg):
            p = geo.from_wgs84(lat, lon, c)
            dec = 7 if c.isGeographic() else 2
            rows.append(
                (
                    "Map CRS",
                    "%.*f, %.*f  %s" % (dec, p.x(), dec, p.y(), c.authid()),
                )
            )
        rows.append(("Geohash", F.fmt_geohash(lat, lon, 9)))
        return rows

    def _fill_rows(self, cand):
        for w in self._rows:
            w.setParent(None)
            w.deleteLater()
        self._rows = []
        small = QFont(self.font())
        small.setPointSizeF(max(7.5, small.pointSizeF() - 0.5))
        for r, (label, value) in enumerate(self._format_rows(cand)):
            lbl = QLabel(label)
            lbl.setFont(small)
            lbl.setStyleSheet("color: palette(mid);")
            val = QLabel(value)
            val.setFont(small)
            val.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            b = QToolButton()
            b.setIcon(icon("copy"))
            b.setAutoRaise(True)
            b.setIconSize(QSize(13, 13))
            b.setToolTip("Copy %s" % label)
            copy_value = (
                value.split("  (")[0]
                if label != "Map CRS"
                else value.split("  ")[0]
            )
            b.clicked.connect(lambda _=False, v=copy_value: self._copy(v))
            self.grid.addWidget(lbl, r, 0)
            self.grid.addWidget(val, r, 1)
            self.grid.addWidget(b, r, 2)
            self._rows += [lbl, val, b]

    def _copy(self, text):
        QApplication.clipboard().setText(text)
        self.iface.statusBarIface().showMessage("Copied: %s" % text, 3000)

    def _copy_all(self):
        c = self._current
        if c is None:
            return
        lines = [self.c_title.text()]
        if self.c_sub.text():
            lines.append(self.c_sub.text())
        lines += ["%s: %s" % kv for kv in self._format_rows(c)]
        if c.lat is not None:
            lines.append("Google Maps: %s" % F.link_google_maps(c.lat, c.lon))
        self._copy("\n".join(lines))

    def _open_link(self, fn):
        c = self._current
        if c is not None and c.lat is not None:
            QDesktopServices.openUrl(
                QUrl(fn(c.lat, c.lon, self.canvas.scale()))
            )

    def _update_fav_icon(self):
        c = self._current
        fav = (
            c is not None
            and c.lat is not None
            and self.settings.is_favorite(self.c_title.text(), c.lat, c.lon)
        )
        self.fav_btn.setIcon(icon("star_on" if fav else "star"))
        self.fav_btn.setToolTip(
            "Remove from saved places"
            if fav
            else "Add to saved places in this panel"
        )

    def _toggle_favorite(self):
        c = self._current
        if c is None or c.lat is None:
            return
        name = self.c_title.text()
        if self.settings.is_favorite(name, c.lat, c.lon):
            self.settings.remove_favorite(name, c.lat, c.lon)
        else:
            entry = c.to_history()
            entry["name"] = name
            entry["subtitle"] = self.c_sub.text()
            self.settings.add_favorite(entry)
            self.iface.messageBar().pushMessage(
                "Location Search", "Saved %s" % name, MSG_SUCCESS, 3
            )
        self._update_fav_icon()

    def _places_layer(self):
        prj = QgsProject.instance()
        for lyr in prj.mapLayers().values():
            if lyr.customProperty(PLACES_PROP, False) in (True, "true"):
                return lyr
        uri = (
            "Point?crs=EPSG:4326&field=name:string(254)"
            "&field=address:string(254)&field=lat:double"
            "&field=lon:double&field=source:string(40)&field=type:string(80)"
            "&field=query:string(254)"
            "&field=saved:string(25)"
        )
        lyr = QgsVectorLayer(uri, "Saved places", "memory")
        lyr.setCustomProperty(PLACES_PROP, True)
        prj.addMapLayer(lyr)
        return lyr

    def _save_to_layer(self):
        c = self._current
        if c is None or c.lat is None:
            return
        lyr = self._places_layer()
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(c.lon, c.lat)))
        f.setAttributes(
            [
                self.c_title.text()[:254],
                self.c_sub.text()[:254],
                c.lat,
                c.lon,
                c.source_label,
                c.type_label[:80],
                (c.query or "")[:254],
                time.strftime("%Y-%m-%d %H:%M"),
            ]
        )
        lyr.dataProvider().addFeatures([f])
        lyr.updateExtents()
        lyr.triggerRepaint()
        self.iface.messageBar().pushMessage(
            "Location Search",
            "Added to 'Saved places'. Use Make Permanent on the layer to"
            " keep it.",
            MSG_SUCCESS,
            5,
        )

    def _add_bookmark(self):
        try:
            from qgis.core import QgsBookmark

            bm = QgsBookmark()
            bm.setName(self.c_title.text() or "Search result")
            bm.setGroup("Location Search")
            bm.setExtent(
                QgsReferencedRectangle(
                    self.canvas.extent(), geo.canvas_crs(self.canvas)
                )
            )
            QgsProject.instance().bookmarkManager().addBookmark(bm)
            self.iface.messageBar().pushMessage(
                "Location Search",
                "Bookmark added to this project",
                MSG_SUCCESS,
                3,
            )
        except Exception as exc:
            self._notify("Could not add bookmark: %s" % exc, "warning")

    def clear_all(self):
        self.presenter.clear()
        self.card.setVisible(False)
        self._current = None
        self._resize_list()

    # ------------------------------------------------------------ menu

    def _build_menu(self):
        m = self.menu
        m.clear()
        fav = m.addMenu(icon("star_on"), "Saved places")
        favs = self.settings.favorites()
        for f in favs[:30]:
            a = fav.addAction(f.get("name") or "Place")
            a.triggered.connect(
                lambda _=False, e=f: self._open_entry(e, "favorite")
            )
        if not favs:
            fav.addAction("Star a result to save it here").setEnabled(False)
        hist = m.addMenu(icon("history"), "Recent searches")
        items = self.settings.history()
        for h in items[:15]:
            a = hist.addAction(h.get("name") or h.get("query") or "Place")
            a.triggered.connect(
                lambda _=False, e=h: self._open_entry(e, "history")
            )
        if items:
            hist.addSeparator()
            hist.addAction("Clear recent searches").triggered.connect(
                self.settings.clear_history
            )
        else:
            hist.addAction("Nothing yet").setEnabled(False)
        m.addSeparator()
        a = m.addAction("Search only in the visible map area")
        a.setCheckable(True)
        a.setChecked(self.settings.get("search/in_view_only"))
        a.toggled.connect(
            lambda on: (
                self.settings.set("search/in_view_only", on),
                self.engine.set_text(self.input.text()),
            )
        )
        m.addAction(
            "Show all results on the map (Shift+Enter)"
        ).triggered.connect(
            lambda: self.engine.submit(self.input.text(), show_all=True)
        )
        m.addAction(
            icon("close"), "Remove pins and outlines"
        ).triggered.connect(self.clear_all)
        m.addAction(
            "Re-read layers for search (%d values indexed)"
            % self.plugin.local.size
        ).triggered.connect(self.plugin.local.rebuild_all)
        m.addSeparator()
        m.addAction(icon("settings"), "Settings").triggered.connect(
            self.plugin.show_settings
        )
        m.addAction("What can I type here?").triggered.connect(self.show_help)

    def _open_entry(self, e, kind):
        bbox = e.get("bbox")
        c = Candidate(
            kind,
            e.get("name") or "",
            e.get("subtitle") or "",
            e["lat"],
            e["lon"],
            source=kind,
            bbox=tuple(bbox) if bbox else None,
            scale=e.get("scale"),
            type_label=e.get("type") or "",
        )
        self.open_candidate(c)

    def show_help(self):
        QMessageBox.information(
            self, "Location Search: what you can type", HELP_HTML
        )


HELP_HTML = """
<p><b>Places and addresses</b>: Sabarmati Ashram, MG Road Pune, petrol pump
near Navrangpura.
Results near the current map view rank first.</p>
<p><b>Coordinates</b>, any order or style:<br>
23.0225, 72.5714 &nbsp; 72.5714, 23.0225 (x, y copied from QGIS)<br>
23.0225N 72.5714E &nbsp; 23&deg;01'21"N 72&deg;34'17"E &nbsp; 23 01 21 N 72 34
17 E<br>
N 23&deg; 01.350 E 072&deg; 34.283 &nbsp; lat 23.02 lon 72.57 &nbsp;
&#2408;&#2409;.&#2406;&#2408;, &#2413;&#2408;.&#2411;&#2413;</p>
<p><b>Grids</b>: 43Q 251102 2548074 (UTM), 43QBF5110248074 (MGRS),
EPSG:32643 251102 2548074, or bare numbers like 251102 2548074 (read in the
project CRS or the UTM zone under the map).</p>
<p><b>Plus Codes</b>: 7JMJ2CXC+XX, or a short code with a place: 2CXC+XX
Ahmedabad.</p>
<p><b>Links</b>: paste a Google Maps, Google Earth, OpenStreetMap, Bing, Apple
Maps or Waze link,
including maps.app.goo.gl short links.</p>
<p><b>Your data</b>: any value in your loaded layers (village names, survey or
gat numbers) and spatial bookmarks.</p>
<p><b>Zoom hint</b>: add 1:2500 or 17z after coordinates.</p>
<p><b>Keys</b>: Ctrl+L focus, Up/Down choose, Enter open, Shift+Enter all
results on the map, Esc close.
Right-click the map for "What's here?".</p>
"""
