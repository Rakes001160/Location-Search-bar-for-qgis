# -*- coding: utf-8 -*-
"""QGIS-side helpers: CRS transforms, map context, parser hits ->
candidates.
"""

import math

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProject,
    QgsPointXY,
    QgsRectangle,
    QgsCsException,
)

from .core import olc
from .core.candidate import Candidate
from .core.formats import fmt_dd
from .core.mgrs_utm import utm_zone, utm_epsg, lat_band, to_mgrs
from .core.url_parser import zoom_from_hit

WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
_XF_CACHE = {}


def crs(authid):
    return QgsCoordinateReferenceSystem(authid)


def xform(src, dst):
    key = (src.authid() or src.toWkt(), dst.authid() or dst.toWkt())
    xf = _XF_CACHE.get(key)
    if xf is None:
        xf = QgsCoordinateTransform(src, dst, QgsProject.instance())
        if len(_XF_CACHE) > 64:
            _XF_CACHE.clear()
        _XF_CACHE[key] = xf
    return xf


def clear_cache():
    _XF_CACHE.clear()


def to_wgs84(x, y, src):
    try:
        if src.authid() == "EPSG:4326":
            return y, x
        p = xform(src, WGS84).transform(QgsPointXY(x, y))
        if not (math.isfinite(p.x()) and math.isfinite(p.y())):
            return None
        return p.y(), p.x()
    except (QgsCsException, Exception):
        return None


def from_wgs84(lat, lon, dst):
    if dst.authid() == "EPSG:4326":
        return QgsPointXY(lon, lat)
    return xform(WGS84, dst).transform(QgsPointXY(lon, lat))


def bbox_to_crs(bbox, dst):
    rect = QgsRectangle(bbox[0], bbox[1], bbox[2], bbox[3])
    if dst.authid() == "EPSG:4326":
        return rect
    return xform(WGS84, dst).transformBoundingBox(rect)


def canvas_crs(canvas):
    return canvas.mapSettings().destinationCrs()


def canvas_context(canvas):
    """Returns (center_lat, center_lon, view_bbox_wgs84, scale)."""
    try:
        c = canvas_crs(canvas)
        center = canvas.center()
        ll = to_wgs84(center.x(), center.y(), c)
        ext = canvas.extent()
        if c.authid() == "EPSG:4326":
            r = ext
        else:
            r = xform(c, WGS84).transformBoundingBox(ext)
        bbox = (
            max(-180.0, r.xMinimum()),
            max(-90.0, r.yMinimum()),
            min(180.0, r.xMaximum()),
            min(90.0, r.yMaximum()),
        )
        if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
            bbox = None
        return (
            (ll[0] if ll else None),
            (ll[1] if ll else None),
            bbox,
            canvas.scale(),
        )
    except Exception:
        return None, None, None, None


def utm_for(lat, lon):
    """(zone, north, epsg, easting, northing, band) for a WGS84 point."""
    zone = utm_zone(lat, lon)
    north = lat >= 0
    epsg = utm_epsg(zone, north)
    try:
        p = xform(WGS84, crs("EPSG:%d" % epsg)).transform(QgsPointXY(lon, lat))
        return zone, north, epsg, p.x(), p.y(), lat_band(lat)
    except Exception:
        return zone, north, epsg, None, None, lat_band(lat)


def mgrs_for(lat, lon, digits=5):
    zone, north, epsg, e, n, band = utm_for(lat, lon)
    if e is None or band is None:
        return ""
    return to_mgrs(zone, band, round(e, 3), round(n, 3), digits)


def within_crs_bounds(c, lat, lon, tolerance=1.0):
    try:
        b = c.bounds()
        if b.isEmpty():
            return True
        return (
            b.xMinimum() - tolerance <= lon <= b.xMaximum() + tolerance
            and b.yMinimum() - tolerance <= lat <= b.yMaximum() + tolerance
        )
    except Exception:
        return True


def scale_for_precision(precision_m, point_scale):
    if not precision_m:
        return point_scale
    return max(point_scale, min(2000000.0, precision_m * 25.0))


# --- hits -> candidates

_KIND = {
    "latlon": "coordinate",
    "url": "link",
    "pluscode": "pluscode",
    "geohash": "coordinate",
    "utm": "grid",
    "mgrs": "grid",
    "crs_xy": "grid",
    "xy": "grid",
}


def _cand_from_latlon(
    h, lat, lon, kind, subtitle, point_scale, note="", score=None, key_extra=""
):
    scale = zoom_from_hit(h) or scale_for_precision(h.precision_m, point_scale)
    title = h.name or fmt_dd(lat, lon)
    if h.name:
        subtitle = "%s, %s" % (fmt_dd(lat, lon), subtitle)
    return Candidate(
        kind,
        title,
        subtitle,
        lat,
        lon,
        source="parser",
        score=h.confidence if score is None else score,
        bbox=h.bbox,
        scale=scale,
        precision_m=h.precision_m,
        decisive=True,
        note=note,
        origin=h,
        type_label=h.label,
        key="parser:%s:%.7f:%.7f%s" % (h.kind, lat, lon, key_extra),
    )


def hits_to_candidates(hits, canvas, point_scale):
    """Convert parser hits into candidates; returns (candidates, specials)."""
    out, specials = [], []
    center_lat, center_lon, _, _ = canvas_context(canvas)
    for h in hits:
        k = h.kind
        if k in ("latlon", "url", "pluscode", "geohash"):
            sub = h.label
            if h.swapped:
                sub = "%s, read as longitude, latitude" % h.label
            elif k == "latlon" and not h.order_known:
                sub = "%s, latitude first" % h.label
            if k == "pluscode":
                sub = "Plus Code %s" % h.code
            out.append(
                _cand_from_latlon(h, h.lat, h.lon, _KIND[k], sub, point_scale)
            )
        elif k in ("utm", "mgrs", "crs_xy"):
            src = crs(h.crs)
            if not src.isValid():
                continue
            ll = to_wgs84(h.x, h.y, src)
            if not ll or not (-90 <= ll[0] <= 90 and -180 <= ll[1] <= 180):
                continue
            if not within_crs_bounds(src, ll[0], ll[1], tolerance=3.0):
                continue
            if k == "mgrs":
                sub = "MGRS, UTM %d%s" % (h.zone, "N" if h.north else "S")
            elif k == "utm":
                sub = "%s, E %.0f N %.0f" % (h.label, h.x, h.y)
            else:
                sub = "%s %s, X %.2f Y %.2f" % (
                    src.authid(),
                    src.description(),
                    h.x,
                    h.y,
                )
            score = h.confidence
            if h.band and k == "utm":
                band = lat_band(ll[0])
                if band and band != h.band:
                    score -= 6.0  # latitude band does not match the result
            out.append(
                _cand_from_latlon(
                    h,
                    ll[0],
                    ll[1],
                    "grid",
                    sub,
                    point_scale,
                    score=score,
                    key_extra=h.crs,
                )
            )
        elif k == "xy":
            out.extend(
                _xy_candidates(h, canvas, center_lat, center_lon, point_scale)
            )
        elif k == "pluscode_short":
            if center_lat is not None:
                try:
                    full = olc.recover_nearest(h.code, center_lat, center_lon)
                    area = olc.decode(full)
                    near = Candidate(
                        "pluscode",
                        h.code,
                        "Plus Code %s, nearest to the map view" % full,
                        area.lat,
                        area.lng,
                        source="parser",
                        score=97.0 if not h.locality else 84.0,
                        bbox=(
                            area.lng_lo,
                            area.lat_lo,
                            area.lng_hi,
                            area.lat_hi,
                        ),
                        scale=point_scale,
                        decisive=not h.locality,
                        key="parser:pc:%s" % full,
                        type_label="Plus Code",
                    )
                    out.append(near)
                except ValueError:
                    pass
            if h.locality:
                out.append(
                    Candidate(
                        "pluscode",
                        "%s %s" % (h.code, h.locality),
                        "Plus Code near %s" % h.locality,
                        source="parser",
                        score=98.0,
                        decisive=True,
                        lazy={
                            "type": "pluscode",
                            "code": h.code,
                            "locality": h.locality,
                        },
                        key="parser:pcl:%s:%s" % (h.code, h.locality),
                        type_label="Plus Code",
                    )
                )
        elif k == "shortlink":
            out.append(
                Candidate(
                    "link",
                    "Open short map link",
                    h.url,
                    source="parser",
                    score=95.0,
                    decisive=True,
                    lazy={"type": "shortlink", "url": h.url},
                    key="parser:short:" + h.url,
                    type_label="Short link",
                )
            )
        elif k == "query":
            specials.append(h)
    return out, specials


def _xy_candidates(h, canvas, center_lat, center_lon, point_scale):
    """Bare numbers: try project CRS, map CRS and the UTM zone under the
    map.
    """
    tried, out = set(), []
    systems = []
    for c in (QgsProject.instance().crs(), canvas_crs(canvas)):
        if c.isValid() and not c.isGeographic():
            systems.append(c)
    if center_lat is not None:
        zone = utm_zone(center_lat, center_lon)
        systems.append(crs("EPSG:%d" % utm_epsg(zone, center_lat >= 0)))
    orders = [(h.x, h.y, False)]
    if not h.order_known:
        orders.append((h.y, h.x, True))
    for c in systems:
        if c.authid() in tried:
            continue
        tried.add(c.authid())
        for x, y, swapped in orders:
            ll = to_wgs84(x, y, c)
            if not ll or not (-90 <= ll[0] <= 90 and -180 <= ll[1] <= 180):
                continue
            if not within_crs_bounds(c, ll[0], ll[1], tolerance=0.5):
                continue
            sub = "Read as %s %s%s" % (
                c.authid(),
                c.description(),
                " (numbers swapped)" if swapped else "",
            )
            cand = _cand_from_latlon(
                h,
                ll[0],
                ll[1],
                "grid",
                sub,
                point_scale,
                score=82.0 - (4.0 if swapped else 0.0),
                key_extra=c.authid(),
            )
            cand.decisive = False
            out.append(cand)
    return out
