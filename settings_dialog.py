# -*- coding: utf-8 -*-
"""Settings dialog."""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QListWidget,
    QListWidgetItem,
)
from qgis.core import QgsProject, QgsVectorLayer


class SettingsDialog(QDialog):

    def __init__(self, settings, parent=None):
        super(SettingsDialog, self).__init__(parent)
        self.settings = settings
        self.setWindowTitle("Location Search settings")
        self.setMinimumWidth(460)
        self._w = {}
        tabs = QTabWidget()
        tabs.addTab(self._providers_tab(), "Sources")
        tabs.addTab(self._search_tab(), "Search")
        tabs.addTab(self._map_tab(), "Map")
        tabs.addTab(self._layers_tab(), "Layers")
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        v = QVBoxLayout(self)
        v.addWidget(tabs)
        v.addWidget(bb)

    def _check(self, form, key, text, tip=""):
        w = QCheckBox(text)
        w.setChecked(self.settings.get(key))
        w.setToolTip(tip)
        form.addRow(w)
        self._w[key] = w
        return w

    def _line(self, form, key, label, secret=False, placeholder=""):
        w = QLineEdit(self.settings.get(key))
        if secret:
            w.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        w.setPlaceholderText(placeholder)
        form.addRow(label, w)
        self._w[key] = w
        return w

    def _spin(self, form, key, label, lo, hi, step=1, suffix=""):
        w = QSpinBox()
        w.setRange(lo, hi)
        w.setSingleStep(step)
        w.setSuffix(suffix)
        w.setValue(self.settings.get(key))
        form.addRow(label, w)
        self._w[key] = w
        return w

    @staticmethod
    def _note(form, text):
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color: palette(mid);")
        form.addRow(lbl)

    def _providers_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        self._check(
            f,
            "photon/enabled",
            "Photon (OpenStreetMap, free): suggestions while typing",
        )
        self._check(
            f,
            "esri/enabled",
            "Esri World Geocoder (free for searching): strong on shops and"
            " POIs",
        )
        self._check(
            f, "esri/live", "    Ask Esri for suggestions while typing"
        )
        self._line(
            f,
            "esri/token",
            "Esri token (optional)",
            secret=True,
            placeholder="needed only to store Esri results",
        )
        self._check(
            f,
            "nominatim/enabled",
            "Nominatim (OpenStreetMap): on Enter, gives area outlines",
        )
        self._line(
            f,
            "nominatim/email",
            "Contact email (optional)",
            placeholder="sent to Nominatim as their policy asks",
        )
        self._check(
            f,
            "overpass/enabled",
            "OSM deep search in the visible area when little is found",
        )
        self._check(
            f, "google/enabled", "Google Places (needs your own API key)"
        )
        self._line(
            f,
            "google/key",
            "Google API key",
            secret=True,
            placeholder="enable 'Places API (New)' and 'Geocoding API'",
        )
        self._check(
            f,
            "google/live",
            "    Use Google for suggestions while typing (uses quota)",
        )
        self._note(
            f,
            "Google charges per request after its free monthly allowance and"
            " its terms limit showing results on non-Google maps. Esri"
            " requires a token if you store results.",
        )
        self._line(f, "photon/url", "Photon server")
        self._line(f, "nominatim/url", "Nominatim server")
        return w

    def _search_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        self._line(
            f,
            "search/language",
            "Result language",
            placeholder="en, hi, gu, mr ...",
        )
        self._line(
            f,
            "search/countries",
            "Limit to countries",
            placeholder="e.g. in  (blank = whole world)",
        )
        self._check(
            f, "search/bias", "Prefer results near the current map view"
        )
        self._check(
            f, "search/in_view_only", "Only search inside the visible map area"
        )
        self._check(
            f,
            "search/auto_go",
            "Locate automatically when coordinates or a map link are typed or"
            " pasted",
        )
        self._check(
            f,
            "map/preview_on_select",
            "Preview results on the map while moving through them with the"
            " arrow keys",
        )
        self._spin(
            f,
            "search/debounce_ms",
            "Pause before online suggestions",
            100,
            1500,
            20,
            " ms",
        )
        self._spin(
            f, "search/min_chars", "Characters before searching online", 1, 8
        )
        self._spin(f, "search/max_results", "Results to show", 4, 30)
        self._spin(f, "search/timeout_s", "Network timeout", 3, 30, 1, " s")
        return w

    def _map_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        self._spin(
            f,
            "map/point_scale",
            "Zoom scale for a point  1:",
            250,
            500000,
            250,
        )
        self._check(f, "map/animate", "Fly smoothly to results")
        self._check(f, "map/pin_label", "Show the name under the pin")
        self._check(f, "map/label_address", "Show the address under the name")
        self._check(f, "map/ripple", "Ripple rings around the found location")
        self._check(
            f, "map/boundary", "Draw outlines of cities, districts and roads"
        )
        self._check(
            f,
            "map/flash",
            "Animate the pin (drop in) and flash the result when arriving",
        )
        self._check(f, "map/select_feature", "Select matched layer features")
        self._check(f, "ui/details", "Show the place card with coordinates")
        self._spin(
            f,
            "map/auto_clear_s",
            "Remove the pin after (0 = keep)",
            0,
            3600,
            5,
            " s",
        )
        c = QComboBox()
        for name in (
            "#EA4335",
            "#1A73E8",
            "#188038",
            "#F9AB00",
            "#9334E6",
            "#202124",
        ):
            c.addItem(name, name)
        c.setCurrentIndex(
            max(0, c.findData(self.settings.get("map/pin_color")))
        )
        f.addRow("Pin colour", c)
        self._w["map/pin_color"] = c
        return w

    def _layers_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        self._check(
            f, "search/local_layers", "Search values in loaded vector layers"
        )
        self._check(
            f, "search/visible_layers_only", "Only layers that are switched on"
        )
        c = QComboBox()
        c.addItem(
            "Text fields plus ID-like number fields (gat, survey, plot, ward)",
            "smart",
        )
        c.addItem("Text fields only", "text")
        c.addItem("All fields", "all")
        c.setCurrentIndex(
            max(0, c.findData(self.settings.get("search/local_fields")))
        )
        f.addRow("Fields", c)
        self._w["search/local_fields"] = c
        self._spin(
            f,
            "search/local_max_features",
            "Max features read per layer",
            1000,
            5000000,
            10000,
        )
        self.layers = QListWidget()
        for lyr in QgsProject.instance().mapLayers().values():
            if isinstance(lyr, QgsVectorLayer):
                it = QListWidgetItem(lyr.name())
                it.setData(Qt.ItemDataRole.UserRole, lyr.id())
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                excluded = lyr.customProperty(
                    "location_searchbar/exclude", False
                ) in (True, "true")
                it.setCheckState(
                    Qt.CheckState.Unchecked
                    if excluded
                    else Qt.CheckState.Checked
                )
                self.layers.addItem(it)
        f.addRow(QLabel("Layers to search in this project"))
        f.addRow(self.layers)
        return w

    def _save(self):
        for key, w in self._w.items():
            if isinstance(w, QCheckBox):
                self.settings.set(key, w.isChecked())
            elif isinstance(w, QSpinBox):
                self.settings.set(key, w.value())
            elif isinstance(w, QComboBox):
                self.settings.set(key, w.currentData())
            else:
                self.settings.set(key, w.text().strip())
        prj = QgsProject.instance()
        for i in range(self.layers.count()):
            it = self.layers.item(i)
            lyr = prj.mapLayer(it.data(Qt.ItemDataRole.UserRole))
            if lyr is not None:
                lyr.setCustomProperty(
                    "location_searchbar/exclude",
                    it.checkState() != Qt.CheckState.Checked,
                )
        self.accept()
