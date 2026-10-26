# -*- coding: utf-8 -*-
"""
Map-link parser. Paste a link from Google Maps, Google Earth, OpenStreetMap,
Bing, Apple Maps, Waze, HERE or a geo: URI and get the exact location.

For Google place links the "!3d<lat>!4d<lon>" data block (the place pin) is
preferred over the "@lat,lon" view centre, which is only where the map was
looking when the link was copied.
"""

import re

try:  # Python 3
    from urllib.parse import urlsplit, parse_qs, unquote, unquote_plus
except ImportError:  # pragma: no cover
    from urlparse import urlsplit, parse_qs  # type: ignore
    from urllib import unquote, unquote_plus  # type: ignore

from .coord_parser import Hit, parse_latlon, valid_latlon, zoom_to_scale

SHORTENER_HOSTS = (
    "maps.app.goo.gl",
    "goo.gl",
    "g.co",
    "g.page",
    "bit.ly",
    "tinyurl.com",
    "maps.apple",
    "waze.to",
    "osm.org/go",
    "here.com/l",
)

_DOMAIN_RE = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}(?:[/?#]|$)", re.I)
_NUM = r"-?\d+(?:\.\d+)?"


def looks_like_url(text):
    t = (text or "").strip().lower()
    if not t:
        return False
    if t.startswith(("http://", "https://", "www.", "geo:")):
        return True
    if " " in t:
        return False
    return bool(_DOMAIN_RE.match(t)) and "/" in t


def _hit(lat, lon, label, conf=99.0, zoom=None, scale=None, name=None):
    if not valid_latlon(lat, lon):
        return None
    return Hit(
        "url",
        lat=lat,
        lon=lon,
        label=label,
        confidence=conf,
        zoom=zoom,
        scale=scale,
        order_known=True,
        name=name,
        precision_m=1.0,
    )


def _coords_from_value(value):
    """Parse a URL parameter value as 'lat,lon' (lat first, as all map URLs
    use).
    """
    if not value:
        return None
    v = value.strip()
    m = re.match(r"^\s*(%s)\s*[,~ ]\s*(%s)" % (_NUM, _NUM), v)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        return (lat, lon) if valid_latlon(lat, lon) else None
    hits = parse_latlon(v, url_order=True)
    if hits and not hits[0].swapped:
        return hits[0].lat, hits[0].lon
    return None


def _parse_geo_uri(s):
    # geo:23.0225,72.5714;u=35  or  geo:0,0?q=23.0225,72.5714(Label)
    body = unquote(s[4:])
    q = re.search(r"[?&]q=([^&]+)", body)
    if q:
        val = q.group(1)
        m = re.match(
            r"^\s*(%s)\s*,\s*(%s)\s*(?:\((.*)\))?" % (_NUM, _NUM), val
        )
        if m:
            h = _hit(
                float(m.group(1)),
                float(m.group(2)),
                "geo: link",
                name=m.group(3),
            )
            if h:
                return [h]
        elif val.strip():
            return [
                Hit(
                    "query",
                    text=unquote_plus(val).strip(),
                    confidence=85.0,
                    label="Place from geo: link",
                )
            ]
    m = re.match(r"^\s*(%s)\s*,\s*(%s)" % (_NUM, _NUM), body)
    if m:
        h = _hit(float(m.group(1)), float(m.group(2)), "geo: link")
        return [h] if h and not (h.lat == 0 and h.lon == 0) else []
    return []


def parse_url(text):
    """Return None when the text is not a link, else a list of hits (may be
    empty).
    """
    s = (text or "").strip()
    if s.lower().startswith("geo:"):
        return _parse_geo_uri(s)
    if not looks_like_url(s):
        return None
    if "://" not in s:
        s = "https://" + s
    try:
        parts = urlsplit(s)
    except ValueError:
        return []
    host = parts.netloc.lower().split("@")[-1].split(":")[0]
    path = unquote_plus(parts.path or "")
    qs = parse_qs(parts.query or "")
    frag = unquote(parts.fragment or "")
    full = unquote_plus(s)
    hits = []
    name = None

    m = re.search(r"/maps/place/([^/@]+)", path)
    if m:
        cand = m.group(1).replace("+", " ").strip()
        if cand and not re.match(r"^[-\d.,\s\u00b0'\"NSEW]+$", cand):
            name = cand

    # view zoom from "@lat,lon,17z" / "@lat,lon,500m" / Earth
    # "@lat,lon,12a,850d"
    view = re.search(
        r"@(%s),\s*(%s)(?:,\s*(%s)([zma]))?(?:,\s*(\d+(?:\.\d+)?)d)?"
        % (_NUM, _NUM, _NUM),
        full,
    )
    zoom = scale = None
    if view:
        if view.group(4) == "z":
            zoom = float(view.group(3))
        elif view.group(4) == "m":
            scale = max(500.0, float(view.group(3)) * 5.4)
        if view.group(5):
            scale = max(500.0, float(view.group(5)) * 3.4)

    # 1. Google place pin (exact)
    pins = re.findall(r"!3d(%s)!4d(%s)" % (_NUM, _NUM), full)
    if pins:
        h = _hit(
            float(pins[-1][0]),
            float(pins[-1][1]),
            "Google Maps place pin",
            99.5,
            zoom=zoom,
            scale=scale,
            name=name,
        )
        if h:
            hits.append(h)

    # 2. Query parameters used by the main map sites (lat,lon order)
    keys = (
        "q",
        "query",
        "ll",
        "sll",
        "center",
        "cp",
        "coordinate",
        "coordinates",
        "destination",
        "daddr",
        "saddr",
        "viewpoint",
        "latlng",
        "point",
        "pos",
        "loc",
        "location",
        "map",
        "c",
    )
    text_query = None
    for key in keys:
        for value in qs.get(key, []):
            val = value.replace("~", ",") if key == "cp" else value
            if key == "map" and "/" in val:
                continue
            c = _coords_from_value(val)
            if c:
                if key == "map":
                    z = re.match(
                        r"^\s*%s\s*,\s*%s\s*,\s*(\d+(?:\.\d+)?)"
                        % (_NUM, _NUM),
                        val,
                    )
                    if z:
                        zoom = float(z.group(1))
                label = (
                    "Viewpoint" if key == "viewpoint" else "Location from link"
                )
                h = _hit(
                    c[0], c[1], label, 98.0, zoom=zoom, scale=scale, name=name
                )
                if h:
                    hits.append(h)
            elif (
                key in ("q", "query", "destination", "daddr")
                and val.strip()
                and not text_query
            ):
                text_query = val.strip()
    if "mlat" in qs and ("mlon" in qs or "mlng" in qs):
        try:
            lat = float(qs["mlat"][0])
            lon = float((qs.get("mlon") or qs.get("mlng"))[0])
            h = _hit(lat, lon, "OpenStreetMap marker", 99.0, zoom=zoom)
            if h:
                hits.append(h)
        except (TypeError, ValueError):
            pass
    for lat_key, lon_key in (
        ("lat", "lon"),
        ("lat", "lng"),
        ("latitude", "longitude"),
    ):
        if lat_key in qs and lon_key in qs:
            try:
                h = _hit(
                    float(qs[lat_key][0]),
                    float(qs[lon_key][0]),
                    "Location from link",
                    97.0,
                )
                if h:
                    hits.append(h)
            except (TypeError, ValueError):
                pass
    if "lvl" in qs and zoom is None:  # Bing
        try:
            zoom = float(qs["lvl"][0])
        except (TypeError, ValueError):
            pass

    # 3. OpenStreetMap / Leaflet style #map=zoom/lat/lon
    m = re.search(
        r"map=(\d+(?:\.\d+)?)/(%s)/(%s)" % (_NUM, _NUM), frag or full
    )
    if m:
        zoom = float(m.group(1))
        h = _hit(
            float(m.group(2)),
            float(m.group(3)),
            "Map view centre",
            96.0,
            zoom=zoom,
        )
        if h:
            hits.append(h)

    # 4. Coordinates written into the path (/search/23.02,+72.57 or
    # /place/23°01'21"N...)
    if not hits:
        for seg in re.split(r"/", path):
            seg = seg.replace("+", " ").strip()
            if not seg or not re.search(r"\d", seg) or len(seg) > 80:
                continue
            if seg.startswith("@") or seg.startswith("data="):
                continue
            c = _coords_from_value(seg)
            if c:
                h = _hit(
                    c[0],
                    c[1],
                    "Location from link",
                    98.0,
                    zoom=zoom,
                    scale=scale,
                    name=name,
                )
                if h:
                    hits.append(h)
                    break

    # 5. GeoHack "params=23_01_21_N_72_34_17_E"
    m = re.search(r"params=([\d._]+_[NS]_[\d._]+_[EW])", full)
    if m and not hits:
        c = _coords_from_value(m.group(1).replace("_", " "))
        if c:
            h = _hit(c[0], c[1], "GeoHack link", 98.0)
            if h:
                hits.append(h)

    # 6. Fallback: the "@lat,lon" view centre
    if view and not hits:
        h = _hit(
            float(view.group(1)),
            float(view.group(2)),
            "Map view centre",
            95.0,
            zoom=zoom,
            scale=scale,
            name=name,
        )
        if h:
            hits.append(h)

    for h in hits:
        if h.zoom is None and zoom is not None:
            h.zoom = zoom
        if h.scale is None and scale is not None:
            h.scale = scale
        if name and not h.name:
            h.name = name

    # de-duplicate (same point reported by several URL parts)
    unique = []
    for h in sorted(hits, key=lambda x: -x.confidence):
        if all(
            abs(h.lat - u.lat) > 1e-7 or abs(h.lon - u.lon) > 1e-7
            for u in unique
        ):
            unique.append(h)
    hits = unique

    if not hits:
        if name or text_query:
            hits.append(
                Hit(
                    "query",
                    text=name or text_query,
                    confidence=85.0,
                    label="Place name from link",
                )
            )
        elif any(
            host == h or host.endswith("." + h) or (h in s.lower())
            for h in SHORTENER_HOSTS
        ):
            hits.append(
                Hit(
                    "shortlink", url=s, confidence=92.0, label="Short map link"
                )
            )
    return hits


def zoom_from_hit(hit):
    if hit.scale:
        return hit.scale
    if hit.zoom is not None:
        return zoom_to_scale(hit.zoom)
    return None
