# -*- coding: utf-8 -*-
"""Coordinate formatting (pure Python) and links to web map viewers."""

import math

from . import olc
from .textutil import geohash_encode

try:
    from urllib.parse import quote
except ImportError:  # pragma: no cover
    from urllib import quote  # type: ignore


def fmt_dd(lat, lon, decimals=6):
    return "%.*f, %.*f" % (decimals, lat, decimals, lon)


def _split_dms(value, sec_decimals):
    value = abs(value)
    d = int(value)
    m_full = (value - d) * 60.0
    m = int(m_full)
    s = round((m_full - m) * 60.0, sec_decimals)
    if s >= 60.0:
        s -= 60.0
        m += 1
    if m >= 60:
        m -= 60
        d += 1
    return d, m, s


def fmt_dms(lat, lon, sec_decimals=1):
    """Google Maps style: 23°01'21.0"N 72°34'17.0"E"""
    out = []
    for val, pos, neg in ((lat, "N", "S"), (lon, "E", "W")):
        d, m, s = _split_dms(val, sec_decimals)
        out.append(
            "%d\u00b0%02d'%0*.*f\"%s"
            % (
                d,
                m,
                3 + sec_decimals if sec_decimals else 2,
                sec_decimals,
                s,
                pos if val >= 0 else neg,
            )
        )
    return " ".join(out)


def fmt_ddm(lat, lon, min_decimals=4):
    out = []
    for val, pos, neg in ((lat, "N", "S"), (lon, "E", "W")):
        a = abs(val)
        d = int(a)
        m = round((a - d) * 60.0, min_decimals)
        if m >= 60.0:
            m -= 60.0
            d += 1
        out.append(
            "%d\u00b0%0*.*f'%s"
            % (d, 3 + min_decimals, min_decimals, m, pos if val >= 0 else neg)
        )
    return " ".join(out)


def fmt_pluscode(lat, lon, length=11):
    try:
        return olc.encode(lat, lon, length)
    except ValueError:
        return ""


def fmt_short_pluscode(lat, lon, locality):
    """'2CXC+XXR Ahmedabad' style; needs a locality name to be meaningful."""
    full = fmt_pluscode(lat, lon)
    if not full or not locality:
        return ""
    short = olc.shorten(full, lat, lon)
    if short == full:
        return ""
    return "%s %s" % (short, locality)


def fmt_geohash(lat, lon, precision=9):
    return geohash_encode(lat, lon, precision)


def fmt_distance(km):
    if km is None:
        return ""
    if km < 1.0:
        return "%d m" % int(round(km * 1000.0, -1) or round(km * 1000.0))
    if km < 10.0:
        return "%.1f km" % km
    if km < 10000.0:
        return "%d km" % int(round(km))
    return "%d,%03d km" % divmod(int(round(km)), 1000)


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = (
        math.sin(dp / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    )
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def scale_to_zoom(scale):
    if not scale or scale <= 0:
        return 15
    return max(1, min(21, int(round(math.log(591657527.591555 / scale, 2)))))


# ------------------------------------------------------------------ links


def link_google_maps(lat, lon, scale=None):
    return "https://www.google.com/maps/search/?api=1&query=%.7f%%2C%.7f" % (
        lat,
        lon,
    )


def link_google_maps_view(lat, lon, scale=None):
    return "https://www.google.com/maps/@%.7f,%.7f,%dz" % (
        lat,
        lon,
        scale_to_zoom(scale),
    )


def link_google_earth(lat, lon, scale=None):
    dist = max(150.0, (scale or 5000.0) / 3.4)
    return "https://earth.google.com/web/@%.7f,%.7f,0a,%.0fd,35y,0h,0t,0r" % (
        lat,
        lon,
        dist,
    )


def link_street_view(lat, lon, scale=None):
    return (
        "https://www.google.com/maps/@?api=1&map_action=pano"
        "&viewpoint=%.7f%%2C%.7f" % (lat, lon)
    )


def link_osm(lat, lon, scale=None):
    z = scale_to_zoom(scale)
    return (
        "https://www.openstreetmap.org/?mlat=%.7f&mlon=%.7f#map=%d/%.6f/%.6f"
        % (lat, lon, z, lat, lon)
    )


def link_bing(lat, lon, scale=None):
    return (
        "https://www.bing.com/maps?cp=%.7f~%.7f&lvl=%d&sp=point.%.7f_%.7f"
        % (lat, lon, scale_to_zoom(scale), lat, lon)
    )


def link_directions(lat, lon, scale=None):
    return (
        "https://www.google.com/maps/dir/?api=1&destination=%.7f%%2C%.7f"
        % (lat, lon)
    )


def link_search_text(text):
    return "https://www.google.com/maps/search/?api=1&query=" + quote(
        text or ""
    )
