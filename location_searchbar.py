# -*- coding: utf-8 -*-
"""
Location Search Bar 3 - main plugin class.

Search panel docked above the Layers panel (Google Earth Pro layout),
Ctrl+L to focus, "What's here?" map tool and right-click map menu.
"""

import os

from qgis.PyQt.QtCore import Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QIcon, QKeySequence, QDesktopServices
from qgis.PyQt.QtWidgets import QApplication, QDockWidget
from qgis.core import QgsSettings
from qgis.gui import QgsDockWidget, QgsMapToolEmitPoint

from .compat import log_exception, QAction, run_dialog
from .core import formats as F
from .settings import Settings
from .net import HttpClient
from .providers import make_providers
from .local_index import LocalIndex
from .search_engine import SearchEngine
from .map_display import MapPresenter
from . import geo

PLUGIN_DIR = os.path.dirname(__file__)


class WhatsHereTool(QgsMapToolEmitPoint):
    def __init__(self, canvas, on_point, on_done):
        super(WhatsHereTool, self).__init__(canvas)
        self._on_point = on_point
        self._on_done = on_done
        self.setCursor(Qt.CursorShape.CrossCursor)

    def canvasReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._on_point(e.mapPoint())

    def deactivate(self):
        super(WhatsHereTool, self).deactivate()
        self._on_done()


class LocationSearchBarPlugin(object):
    PLUGIN_NAME = "Location Search Bar"

    def __init__(self, iface):
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.actions = []
        self.dock = None
        self.panel = None
        self.tool = None
        self._prev_tool = None

    # ------------------------------------------------------------ setup

    def initGui(self):
        from .panel import SearchPanel, icon

        self.settings = Settings()
        self.http = HttpClient()
        self.providers = make_providers(self.http, self.settings)
        self.local = LocalIndex(self.settings, self.iface)
        self.engine = SearchEngine(
            self.canvas, self.settings, self.http, self.providers, self.local
        )
        self.presenter = MapPresenter(self.canvas, self.settings)

        mw = self.iface.mainWindow()
        self.panel = SearchPanel(self)
        self.dock = QgsDockWidget("Search", mw)
        self.dock.setObjectName("LocationSearchDock")
        self.dock.setWidget(self.panel)
        self.dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.iface.addDockWidget(
            Qt.DockWidgetArea.LeftDockWidgetArea, self.dock
        )

        self.toggle_action = QAction(
            QIcon(os.path.join(PLUGIN_DIR, "icon.png")),
            "Location Search Bar",
            mw,
        )
        self.toggle_action.setCheckable(True)
        self.toggle_action.setChecked(True)
        self.toggle_action.setToolTip("Show the location search panel")
        self.toggle_action.triggered.connect(self._toggle)
        self.dock.visibilityChanged.connect(self._on_visibility)
        self.iface.addToolBarIcon(self.toggle_action)
        self.iface.addPluginToWebMenu(self.PLUGIN_NAME, self.toggle_action)

        self.focus_action = QAction(icon("search"), "Search location", mw)
        self.focus_action.setObjectName("LocationSearchFocus")
        self.focus_action.triggered.connect(self.focus)
        try:
            self.iface.registerMainWindowAction(self.focus_action, "Ctrl+L")
        except Exception:
            self.focus_action.setShortcut(QKeySequence("Ctrl+L"))
        mw.addAction(self.focus_action)
        self.iface.addPluginToWebMenu(self.PLUGIN_NAME, self.focus_action)

        self.here_action = QAction(icon("whatshere"), "What's here?", mw)
        self.here_action.setCheckable(True)
        self.here_action.triggered.connect(self.panel.here_btn.setChecked)
        self.iface.addPluginToWebMenu(self.PLUGIN_NAME, self.here_action)
        self.settings_action = QAction(icon("settings"), "Settings", mw)
        self.settings_action.triggered.connect(self.show_settings)
        self.iface.addPluginToWebMenu(self.PLUGIN_NAME, self.settings_action)
        self.actions = [
            self.toggle_action,
            self.focus_action,
            self.here_action,
            self.settings_action,
        ]

        self.tool = WhatsHereTool(
            self.canvas, self._whats_here, self._tool_done
        )
        self.panel.whatsHereToggled.connect(self._set_tool)
        try:
            self.canvas.contextMenuAboutToShow.connect(self._canvas_menu)
            self._has_ctx = True
        except AttributeError:
            self._has_ctx = False

        if not QgsSettings().value(
            "LocationSearchBar/ui/docked_v3", False, type=bool
        ):
            QTimer.singleShot(400, self._dock_above_layers)
        if QgsSettings().value("LocationSearchBar/visible", "true") == "false":
            self.dock.hide()
        QTimer.singleShot(2500, self.local.ensure_started)

    def unload(self):
        try:
            if self._has_ctx:
                self.canvas.contextMenuAboutToShow.disconnect(
                    self._canvas_menu
                )
        except (TypeError, RuntimeError, AttributeError):
            pass
        if self.tool is not None and self.canvas.mapTool() is self.tool:
            self.canvas.unsetMapTool(self.tool)
        self.engine.cancel()
        self.http.cancel_all()
        self.local.unload()
        self.presenter.unload()
        for a in self.actions:
            self.iface.removePluginWebMenu(self.PLUGIN_NAME, a)
        self.iface.removeToolBarIcon(self.toggle_action)
        try:
            self.iface.unregisterMainWindowAction(self.focus_action)
        except Exception as exc:
            log_exception("unregister shortcut", exc)
        self.iface.mainWindow().removeAction(self.focus_action)
        if self.dock is not None:
            self.iface.removeDockWidget(self.dock)
            self.dock.setWidget(None)
            self.dock.deleteLater()
            self.dock = None

    # ------------------------------------------------------------ dock

    def _find_layers_dock(self):
        try:
            p = self.iface.layerTreeView()
            while p is not None:
                if isinstance(p, QDockWidget):
                    return p
                p = p.parent()
        except Exception as exc:
            log_exception("find Layers panel", exc)
        for dw in self.iface.mainWindow().findChildren(QDockWidget):
            if (dw.objectName() or "").lower() in ("layers", "legend"):
                return dw
        return None

    def _dock_above_layers(self):
        mw = self.iface.mainWindow()
        layers = self._find_layers_dock()
        if layers is not None and layers.isVisible() and self.dock is not None:
            area = mw.dockWidgetArea(layers)
            if area == Qt.DockWidgetArea.NoDockWidgetArea:
                area = Qt.DockWidgetArea.LeftDockWidgetArea
            mw.addDockWidget(area, self.dock)
            mw.splitDockWidget(self.dock, layers, Qt.Orientation.Vertical)
            try:
                mw.resizeDocks(
                    [self.dock, layers], [300, 420], Qt.Orientation.Vertical
                )
            except Exception as exc:
                log_exception("resize docks", exc)
        QgsSettings().setValue("LocationSearchBar/ui/docked_v3", True)

    def _toggle(self, checked):
        self.dock.setVisible(checked)
        if checked:
            self.panel.focus_search()

    def _on_visibility(self, visible):
        self.toggle_action.setChecked(visible)
        QgsSettings().setValue(
            "LocationSearchBar/visible", "true" if visible else "false"
        )

    def focus(self):
        if not self.dock.isVisible():
            self.dock.show()
        self.dock.raise_()
        self.panel.focus_search()

    def show_settings(self):
        from .settings_dialog import SettingsDialog

        dlg = SettingsDialog(self.settings, self.iface.mainWindow())
        if run_dialog(dlg):
            self.local.rebuild_all()
            self.engine.set_text(self.panel.input.text())

    # ------------------------------------------------------------ what's here

    def _set_tool(self, on):
        if on:
            if self.canvas.mapTool() is not self.tool:
                self._prev_tool = self.canvas.mapTool()
                self.canvas.setMapTool(self.tool)
        elif self.canvas.mapTool() is self.tool:
            if self._prev_tool is not None:
                self.canvas.setMapTool(self._prev_tool)
            else:
                self.canvas.unsetMapTool(self.tool)
        self.here_action.setChecked(on)

    def _tool_done(self):
        self.panel.here_btn.blockSignals(True)
        self.panel.here_btn.setChecked(False)
        self.panel.here_btn.blockSignals(False)
        self.here_action.setChecked(False)

    def _to_latlon(self, map_point):
        return geo.to_wgs84(
            map_point.x(), map_point.y(), geo.canvas_crs(self.canvas)
        )

    def _whats_here(self, map_point):
        ll = self._to_latlon(map_point)
        if ll:
            if not self.dock.isVisible():
                self.dock.show()
            self.panel.show_point(ll[0], ll[1])

    def _canvas_menu(self, menu, event):
        ll = self._to_latlon(event.mapPoint())
        if not ll:
            return
        lat, lon = ll
        menu.addSeparator()
        menu.addAction(
            QIcon(os.path.join(PLUGIN_DIR, "icons", "whatshere.svg")),
            "What's here?",
        ).triggered.connect(lambda: self._whats_here(event.mapPoint()))
        sub = menu.addMenu(
            QIcon(os.path.join(PLUGIN_DIR, "icons", "copy.svg")),
            "Copy coordinates",
        )
        mgrs = geo.mgrs_for(lat, lon)
        for label, value in (
            ("%s" % F.fmt_dd(lat, lon), F.fmt_dd(lat, lon)),
            (F.fmt_dms(lat, lon), F.fmt_dms(lat, lon)),
            (
                "Plus Code %s" % F.fmt_pluscode(lat, lon),
                F.fmt_pluscode(lat, lon),
            ),
            ("MGRS %s" % mgrs, mgrs),
        ):
            if value:
                sub.addAction(label).triggered.connect(
                    lambda _=False, v=value: QApplication.clipboard().setText(
                        v
                    )
                )
        scale = self.canvas.scale()
        menu.addAction("Open in Google Maps").triggered.connect(
            lambda: QDesktopServices.openUrl(
                QUrl(F.link_google_maps(lat, lon))
            )
        )
        menu.addAction("Open in Google Earth").triggered.connect(
            lambda: QDesktopServices.openUrl(
                QUrl(F.link_google_earth(lat, lon, scale))
            )
        )
        menu.addAction("Street View here").triggered.connect(
            lambda: QDesktopServices.openUrl(
                QUrl(F.link_street_view(lat, lon))
            )
        )
