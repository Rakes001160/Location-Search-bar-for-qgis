# -*- coding: utf-8 -*-
"""Plugin settings (QgsSettings) plus recent searches and favourites.

History and favourites keep the keys/format of version 2.x, so saved places
survive the upgrade.
"""

import json
import time

from qgis.core import QgsSettings

PREFIX = "LocationSearchBar/"

DEFAULTS = {
    # providers
    "photon/enabled": True,
    "photon/url": "https://photon.komoot.io/api/",
    "esri/enabled": True,
    "esri/live": True,
    "esri/token": "",
    "nominatim/enabled": True,
    "nominatim/url": "https://nominatim.openstreetmap.org",
    "nominatim/email": "",
    "overpass/enabled": True,
    "overpass/url": "https://overpass-api.de/api/interpreter",
    "google/enabled": False,
    "google/key": "",
    "google/live": True,
    # search behaviour
    "search/language": "en",
    "search/countries": "",
    "search/bias": True,
    "search/in_view_only": False,
    "search/debounce_ms": 280,
    "search/min_chars": 3,
    "search/max_results": 12,
    "search/auto_go": True,
    "search/timeout_s": 8,
    "search/local_layers": True,
    "search/local_fields": "smart",  # smart | text | all
    "search/local_max_features": 200000,
    "search/visible_layers_only": False,
    # map display
    "map/point_scale": 2500,
    "map/pin_label": True,
    "map/label_address": True,
    "map/ripple": True,
    "map/preview_on_select": True,
    "map/boundary": True,
    "map/flash": True,
    "map/select_feature": False,
    "map/animate": True,
    "map/pin_color": "#EA4335",
    "map/auto_clear_s": 0,
    "ui/details": True,
    "ui/copy_format": "dd",
}

HISTORY_MAX = 50


class Settings(object):

    def __init__(self):
        self._s = QgsSettings()

    def get(self, key):
        default = DEFAULTS.get(key)
        raw = self._s.value(PREFIX + key, default)
        if isinstance(default, bool):
            if isinstance(raw, str):
                return raw.strip().lower() in ("1", "true", "yes", "on")
            return bool(raw)
        if isinstance(default, int):
            try:
                return int(float(raw))
            except (TypeError, ValueError):
                return default
        if isinstance(default, float):
            try:
                return float(raw)
            except (TypeError, ValueError):
                return default
        return "" if raw is None else str(raw)

    def set(self, key, value):
        self._s.setValue(PREFIX + key, value)

    def countries(self):
        raw = self.get("search/countries")
        return [
            c.strip().lower()
            for c in raw.replace(";", ",").split(",")
            if c.strip()
        ]

    # ------------------------------------------------------------ json lists

    def _get_list(self, key):
        raw = self._s.value(PREFIX + key, "[]")
        try:
            data = json.loads(raw) if isinstance(raw, str) else (raw or [])
        except ValueError:
            data = []
        return [
            d
            for d in data
            if isinstance(d, dict) and "lat" in d and "lon" in d
        ]

    def _set_list(self, key, items):
        self._s.setValue(PREFIX + key, json.dumps(items))

    # history entries: query, name, subtitle, lat, lon, bbox, scale, kind, ts
    def history(self):
        return self._get_list("history")

    def add_history(self, entry):
        entry = dict(entry)
        entry["ts"] = int(time.time())
        items = [
            h
            for h in self.history()
            if not (
                h.get("name") == entry.get("name")
                and abs(h.get("lat", 0) - entry["lat"]) < 1e-6
                and abs(h.get("lon", 0) - entry["lon"]) < 1e-6
            )
        ]
        items.insert(0, entry)
        self._set_list("history", items[:HISTORY_MAX])

    def clear_history(self):
        self._set_list("history", [])

    def favorites(self):
        return self._get_list("favorites")

    def is_favorite(self, name, lat, lon):
        return any(
            f.get("name") == name
            and abs(f["lat"] - lat) < 1e-6
            and abs(f["lon"] - lon) < 1e-6
            for f in self.favorites()
        )

    def add_favorite(self, entry):
        items = [
            f
            for f in self.favorites()
            if not (
                f.get("name") == entry.get("name")
                and abs(f["lat"] - entry["lat"]) < 1e-6
            )
        ]
        items.insert(0, dict(entry))
        self._set_list("favorites", items)

    def remove_favorite(self, name, lat, lon):
        self._set_list(
            "favorites",
            [
                f
                for f in self.favorites()
                if not (
                    f.get("name") == name
                    and abs(f["lat"] - lat) < 1e-6
                    and abs(f["lon"] - lon) < 1e-6
                )
            ],
        )

    def clear_favorites(self):
        self._set_list("favorites", [])
