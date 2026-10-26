# -*- coding: utf-8 -*-
"""A search result from any source (coordinates, layers, geocoders,
history).
"""

ICON_FOR_KIND = {
    "coordinate": "coord",
    "grid": "grid",
    "pluscode": "plus",
    "link": "link",
    "feature": "layer",
    "bookmark": "bookmark",
    "history": "history",
    "favorite": "star",
    "place": "pin",
    "area": "area",
    "road": "road",
    "poi": "poi",
    "status": "info",
}

# Sources shown as a small tag on each row
SOURCE_LABELS = {
    "parser": "Coordinates",
    "local": "Layer",
    "bookmark": "Bookmark",
    "history": "Recent",
    "favorite": "Saved",
    "photon": "Photon",
    "esri": "Esri",
    "nominatim": "OSM",
    "overpass": "OSM deep",
    "google": "Google",
    "whatshere": "Map",
}


class Candidate(object):
    """Everything the UI and the map need to show one result."""

    def __init__(self, kind, title, subtitle="", lat=None, lon=None, **kw):
        self.kind = kind  # see ICON_FOR_KIND
        self.title = title or ""
        self.subtitle = subtitle or ""
        self.lat = lat
        self.lon = lon
        self.source = kw.get("source", "")
        self.score = float(kw.get("score", 50.0))
        self.bbox = kw.get("bbox")  # WGS84 (minlon, minlat, maxlon, maxlat)
        self.scale = kw.get("scale")  # preferred map scale when no bbox
        self.type_label = kw.get("type_label", "")
        self.precision_m = kw.get("precision_m")
        self.geojson = kw.get("geojson")  # outline from the provider
        self.decisive = kw.get(
            "decisive", False
        )  # Enter may jump straight here
        self.lazy = kw.get(
            "lazy"
        )  # dict for providers that need a 2nd request
        self.layer_id = kw.get("layer_id")
        self.fid = kw.get("fid")
        self.field = kw.get("field")
        self.extent = kw.get("extent")  # (QgsRectangle, authid) for bookmarks
        self.address = kw.get("address") or {}
        self.attributes = kw.get("attributes")
        self.note = kw.get("note", "")  # e.g. "Interpreted as UTM 43N"
        self.origin = kw.get("origin")  # parser Hit / raw dict for details
        self.query = kw.get("query", "")
        self.distance_km = None
        self.key = kw.get("key") or self._default_key()
        self.enabled = kw.get("enabled", True)

    def _default_key(self):
        if self.lat is not None and self.lon is not None:
            return "%s:%s:%.6f:%.6f" % (
                self.source,
                self.kind,
                self.lat,
                self.lon,
            )
        return "%s:%s:%s" % (self.source, self.kind, self.title)

    @property
    def icon(self):
        return ICON_FOR_KIND.get(self.kind, "pin")

    @property
    def source_label(self):
        return SOURCE_LABELS.get(self.source, self.source.title())

    @property
    def has_location(self):
        return (
            (self.lat is not None and self.lon is not None)
            or self.lazy is not None
            or self.layer_id is not None
            or self.extent is not None
        )

    def to_history(self):
        return {
            "query": self.query or self.title,
            "name": self.title,
            "subtitle": self.subtitle,
            "lat": self.lat,
            "lon": self.lon,
            "bbox": list(self.bbox) if self.bbox else None,
            "scale": self.scale,
            "kind": self.kind,
            "source": self.source,
            "type": self.type_label,
        }

    def __repr__(self):
        return "<Candidate %s %r %.1f (%s, %s) %s>" % (
            self.kind,
            self.title,
            self.score,
            self.lat,
            self.lon,
            self.source,
        )
