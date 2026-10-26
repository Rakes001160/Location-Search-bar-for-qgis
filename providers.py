# -*- coding: utf-8 -*-
"""
Online geocoders. Each provider turns a query into Candidates asynchronously.

  Photon     free, built for search-as-you-type (OSM data)       live + Enter
  Esri       World Geocoder suggest/find (good POI coverage)     live + Enter
  Nominatim  OSM reference geocoder, outlines for areas          Enter only *
 Overpass deep OSM name search inside the current view Enter, fallback
  Google     Places API (New), needs your own API key            live + Enter

* Nominatim's usage policy forbids autocomplete, so it only runs on Enter and
  is throttled to one request per second.
"""

import math
import time
import uuid

from qgis.PyQt.QtCore import QObject, QTimer

from .core.candidate import Candidate
from .core.textutil import fold

ISO3 = {
    "IND": "India",
    "USA": "United States",
    "GBR": "United Kingdom",
    "ARE": "United Arab Emirates",
    "NPL": "Nepal",
    "PAK": "Pakistan",
    "BGD": "Bangladesh",
    "LKA": "Sri Lanka",
    "CHN": "China",
    "ESP": "Spain",
    "NLD": "Netherlands",
    "DEU": "Germany",
    "FRA": "France",
    "ITA": "Italy",
    "AUS": "Australia",
    "CAN": "Canada",
    "SGP": "Singapore",
    "JPN": "Japan",
    "BTN": "Bhutan",
}

AREA_SCALE = {
    "continent": 40000000,
    "country": 8000000,
    "state": 2000000,
    "region": 2000000,
    "province": 2000000,
    "state_district": 400000,
    "county": 400000,
    "district": 250000,
    "city": 100000,
    "municipality": 100000,
    "town": 50000,
    "borough": 50000,
    "suburb": 25000,
    "city_district": 25000,
    "quarter": 15000,
    "neighbourhood": 10000,
    "village": 20000,
    "hamlet": 8000,
    "locality": 15000,
    "isolated_dwelling": 4000,
    "postcode": 30000,
    "street": 5000,
    "road": 5000,
    "house": 1500,
    "building": 1500,
}
AREA_TYPES = set(AREA_SCALE) - {"street", "road", "house", "building"}
POI_KEYS = {
    "amenity",
    "shop",
    "tourism",
    "leisure",
    "historic",
    "office",
    "craft",
    "healthcare",
    "aeroway",
    "railway",
    "public_transport",
    "man_made",
    "building",
    "sport",
    "emergency",
}


def humanize(value):
    if not value:
        return ""
    value = str(value).replace("_", " ").strip()
    return value[:1].upper() + value[1:]


def bbox_from(minlon, minlat, maxlon, maxlat):
    try:
        vals = [float(v) for v in (minlon, minlat, maxlon, maxlat)]
    except (TypeError, ValueError):
        return None
    lons = sorted((vals[0], vals[2]))
    lats = sorted((vals[1], vals[3]))
    if lons[0] == lons[1] and lats[0] == lats[1]:
        return None
    return (lons[0], lats[0], lons[1], lats[1])


def _iso3(text):
    if not text:
        return text
    parts = [p.strip() for p in text.split(",")]
    if parts and parts[-1] in ISO3:
        parts[-1] = ISO3[parts[-1]]
    return ", ".join(p for p in parts if p)


class SearchContext(object):
    """Where the user is looking, used to bias results like Google does."""

    def __init__(
        self,
        center_lat=None,
        center_lon=None,
        view_bbox=None,
        scale=None,
        language="en",
        countries=None,
        bias=True,
        in_view_only=False,
        limit=10,
        timeout_s=8,
        point_scale=2500,
    ):
        self.center_lat = center_lat
        self.center_lon = center_lon
        self.view_bbox = view_bbox  # (minlon, minlat, maxlon, maxlat)
        self.scale = scale
        self.language = language or "en"
        self.countries = countries or []
        self.bias = bias
        self.in_view_only = in_view_only
        self.limit = limit
        self.timeout_s = timeout_s
        self.point_scale = point_scale

    @property
    def has_center(self):
        return self.center_lat is not None and self.center_lon is not None

    def view_radius_m(self):
        if not self.view_bbox or not self.has_center:
            return 50000.0
        minlon, minlat, maxlon, maxlat = self.view_bbox
        dlat = (maxlat - minlat) * 111320.0 / 2.0
        dlon = (
            (maxlon - minlon)
            * 111320.0
            * math.cos(math.radians(self.center_lat))
            / 2.0
        )
        return max(500.0, min(50000.0, math.hypot(dlat, dlon)))

    def web_zoom(self):
        if not self.scale:
            return 12
        return max(
            1, min(18, int(round(math.log(591657527.591555 / self.scale, 2))))
        )

    def small_view(self, max_deg=3.0):
        if not self.view_bbox:
            return False
        minlon, minlat, maxlon, maxlat = self.view_bbox
        return (maxlon - minlon) <= max_deg and (maxlat - minlat) <= max_deg


class _Delayed(object):
    """Cancellable delayed start (used to throttle Nominatim)."""

    def __init__(self):
        self.cancelled = False
        self.inner = None

    def cancel(self):
        self.cancelled = True
        if self.inner is not None:
            self.inner.cancel()


class Provider(QObject):
    key = ""
    label = ""

    def __init__(self, http, settings, parent=None):
        super(Provider, self).__init__(parent)
        self.http = http
        self.settings = settings

    def enabled(self):
        return bool(self.settings.get(self.key + "/enabled"))

    def live_enabled(self):
        return False

    def search(self, query, ctx, live, done):
        raise NotImplementedError

    def resolve(self, cand, ctx, done):
        done(cand, None)

    def reverse(self, lat, lon, ctx, done):
        done(None, "reverse geocoding not supported")

    def _base_score(self, idx, base):
        return base - 1.5 * idx


# ---------------------------------------------------------------------- Photon


class PhotonProvider(Provider):
    key = "photon"
    label = "Photon"

    def live_enabled(self):
        return self.enabled()

    def search(self, query, ctx, live, done):
        params = {"q": query, "limit": max(5, min(15, ctx.limit))}
        if ctx.language in ("en", "de", "fr", "it"):
            params["lang"] = ctx.language
        if ctx.bias and ctx.has_center:
            params["lat"] = "%.5f" % ctx.center_lat
            params["lon"] = "%.5f" % ctx.center_lon
            params["zoom"] = ctx.web_zoom()
            params["location_bias_scale"] = "0.3"
        if ctx.in_view_only and ctx.view_bbox:
            params["bbox"] = "%.5f,%.5f,%.5f,%.5f" % ctx.view_bbox

        def cb(ok, data, info):
            if not ok:
                done([], "Photon: %s" % data)
                return
            done(self._parse(data or {}, ctx), None)

        return self.http.get(
            self.settings.get("photon/url"),
            params,
            callback=cb,
            timeout_s=ctx.timeout_s,
        )

    def _parse(self, data, ctx):
        out = []
        for i, f in enumerate(data.get("features") or []):
            coords = (f.get("geometry") or {}).get("coordinates") or []
            if len(coords) < 2:
                continue
            p = f.get("properties") or {}
            cc = (p.get("countrycode") or "").lower()
            if ctx.countries and cc and cc not in ctx.countries:
                continue
            lon, lat = float(coords[0]), float(coords[1])
            street_line = " ".join(
                x for x in (p.get("housenumber"), p.get("street")) if x
            )
            title = (
                p.get("name")
                or street_line
                or p.get("city")
                or p.get("state")
                or p.get("country")
                or ""
            )
            parts = []
            for v in (
                street_line if p.get("name") else None,
                p.get("locality"),
                p.get("district"),
                p.get("city"),
                p.get("county"),
                p.get("state"),
                p.get("country"),
            ):
                if v and v != title and v not in parts:
                    parts.append(v)
            ptype = p.get("type") or ""
            osm_key, osm_value = (
                p.get("osm_key") or "",
                p.get("osm_value") or "",
            )
            kind = "place"
            if ptype in AREA_TYPES or osm_key in ("place", "boundary"):
                kind = "area"
            elif osm_key == "highway" or ptype == "street":
                kind = "road"
            elif osm_key in POI_KEYS:
                kind = "poi"
            ext = p.get("extent")
            bbox = (
                bbox_from(*ext)
                if isinstance(ext, list) and len(ext) == 4
                else None
            )
            scale = (
                AREA_SCALE.get(osm_value)
                or AREA_SCALE.get(ptype)
                or ctx.point_scale
            )
            osm_type = (p.get("osm_type") or "")[:1].upper()
            out.append(
                Candidate(
                    kind,
                    title,
                    ", ".join(parts),
                    lat,
                    lon,
                    source="photon",
                    score=self._base_score(i, 70.0),
                    bbox=bbox,
                    scale=scale,
                    type_label=humanize(
                        osm_value if osm_value not in ("yes", "") else osm_key
                    ),
                    key=(
                        "osm:%s%s" % (osm_type, p.get("osm_id"))
                        if p.get("osm_id")
                        else None
                    ),
                    address=p,
                )
            )
        return out


# ---------------------------------------------------------------------- Esri

ESRI_BASE = (
    "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/"
)
ESRI_FIELDS = (
    "PlaceName,LongLabel,Place_addr,Type,Addr_type,City,Region,Country,Score"
)
ESRI_AREA = {
    "Locality",
    "PostalLoc",
    "Postal",
    "District",
    "City",
    "Subregion",
    "Region",
    "Country",
    "Zone",
    "Neighborhood",
    "LatLong",
}
ESRI_ROAD = {"StreetName", "StreetAddress", "StreetInt", "StreetAddressExt"}
ESRI_SCALE = {
    "PointAddress": 1500,
    "Subaddress": 1500,
    "POI": 2500,
    "StreetAddress": 2500,
    "StreetName": 5000,
    "StreetInt": 3000,
}


class EsriProvider(Provider):
    key = "esri"
    label = "Esri"

    def live_enabled(self):
        return self.enabled() and self.settings.get("esri/live")

    def _common(self, ctx):
        params = {"f": "json", "langCode": ctx.language.upper()}
        token = self.settings.get("esri/token")
        if token:
            params["token"] = token
        if ctx.bias and ctx.has_center:
            params["location"] = "%.6f,%.6f" % (ctx.center_lon, ctx.center_lat)
        if ctx.in_view_only and ctx.view_bbox:
            params["searchExtent"] = "%.6f,%.6f,%.6f,%.6f" % ctx.view_bbox
        return params

    def search(self, query, ctx, live, done):
        if live:
            params = self._common(ctx)
            params.update({"text": query, "maxSuggestions": 6})
            if ctx.countries:
                params["countryCode"] = ",".join(
                    c.upper() for c in ctx.countries
                )

            def cb(ok, data, info):
                if not ok:
                    done([], "Esri: %s" % data)
                    return
                out = []
                for i, s in enumerate((data or {}).get("suggestions") or []):
                    if s.get("isCollection"):
                        continue
                    text = s.get("text") or ""
                    parts = [p.strip() for p in text.split(",")]
                    out.append(
                        Candidate(
                            "place",
                            parts[0],
                            _iso3(", ".join(parts[1:])),
                            source="esri",
                            score=self._base_score(i, 72.0),
                            key="esri:s:" + text,
                            lazy={"magicKey": s.get("magicKey"), "text": text},
                            query=query,
                        )
                    )
                done(out, None)

            return self.http.get(
                ESRI_BASE + "suggest",
                params,
                callback=cb,
                timeout_s=ctx.timeout_s,
            )
        params = self._common(ctx)
        params.update(
            {
                "SingleLine": query,
                "outFields": ESRI_FIELDS,
                "maxLocations": max(5, min(20, ctx.limit)),
                "forStorage": "false",
            }
        )
        if ctx.countries:
            params["sourceCountry"] = ",".join(
                c.upper() for c in ctx.countries
            )

        def cb2(ok, data, info):
            if not ok:
                done([], "Esri: %s" % data)
                return
            done(self._parse_candidates(data or {}, ctx, query), None)

        return self.http.get(
            ESRI_BASE + "findAddressCandidates",
            params,
            callback=cb2,
            timeout_s=ctx.timeout_s,
        )

    def _parse_candidates(self, data, ctx, query, limit=None):
        out = []
        for i, c in enumerate(data.get("candidates") or []):
            loc = c.get("location") or {}
            if loc.get("x") is None or loc.get("y") is None:
                continue
            score = float(c.get("score") or 0)
            if score < 55 and i > 0:
                continue
            a = c.get("attributes") or {}
            name = (a.get("PlaceName") or "").strip()
            long_label = (a.get("LongLabel") or c.get("address") or "").strip()
            place_addr = (a.get("Place_addr") or "").strip()
            title = name or long_label.split(",")[0]
            if name and place_addr and place_addr.lower() != name.lower():
                sub = place_addr
            else:
                sub = ", ".join(p.strip() for p in long_label.split(",")[1:])
            addr_type = a.get("Addr_type") or ""
            kind = (
                "poi"
                if addr_type == "POI"
                else (
                    "area"
                    if addr_type in ESRI_AREA
                    else ("road" if addr_type in ESRI_ROAD else "place")
                )
            )
            ext = c.get("extent") or {}
            bbox = (
                bbox_from(
                    ext.get("xmin"),
                    ext.get("ymin"),
                    ext.get("xmax"),
                    ext.get("ymax"),
                )
                if ext
                else None
            )
            if kind != "area" and bbox and (bbox[2] - bbox[0]) < 0.02:
                bbox = None  # tiny POI envelopes: zoom by scale instead
            out.append(
                Candidate(
                    kind,
                    title,
                    _iso3(sub),
                    float(loc["y"]),
                    float(loc["x"]),
                    source="esri",
                    score=self._base_score(i, 74.0) + (score - 90.0) / 10.0,
                    bbox=bbox,
                    scale=ESRI_SCALE.get(addr_type, ctx.point_scale),
                    type_label=a.get("Type") or humanize(addr_type),
                    address=a,
                    query=query,
                )
            )
            if limit and len(out) >= limit:
                break
        return out

    def resolve(self, cand, ctx, done):
        lazy = cand.lazy or {}
        params = self._common(ctx)
        params.update(
            {
                "SingleLine": lazy.get("text", cand.title),
                "magicKey": lazy.get("magicKey"),
                "outFields": ESRI_FIELDS,
                "maxLocations": 1,
                "forStorage": "false",
            }
        )

        def cb(ok, data, info):
            if not ok:
                done(None, "Esri: %s" % data)
                return
            res = self._parse_candidates(data or {}, ctx, cand.query, limit=1)
            if not res:
                done(None, "Esri could not locate this suggestion")
                return
            r = res[0]
            r.title = cand.title or r.title
            r.subtitle = cand.subtitle or r.subtitle
            done(r, None)

        return self.http.get(
            ESRI_BASE + "findAddressCandidates",
            params,
            callback=cb,
            timeout_s=ctx.timeout_s,
        )

    def reverse(self, lat, lon, ctx, done):
        params = {
            "f": "json",
            "location": "%.7f,%.7f" % (lon, lat),
            "langCode": ctx.language.upper(),
        }
        token = self.settings.get("esri/token")
        if token:
            params["token"] = token

        def cb(ok, data, info):
            if not ok or not isinstance(data, dict) or "address" not in data:
                done(
                    None, "Esri: %s" % (data if not ok else "no address here")
                )
                return
            a = data.get("address") or {}
            done(
                {
                    "title": (
                        a.get("PlaceName")
                        or a.get("Address")
                        or a.get("Neighborhood")
                        or a.get("City")
                        or ""
                    ),
                    "address": _iso3(
                        a.get("LongLabel") or a.get("Match_addr") or ""
                    ),
                    "locality": a.get("City") or a.get("Subregion") or "",
                    "source": "Esri",
                },
                None,
            )

        return self.http.get(
            ESRI_BASE + "reverseGeocode",
            params,
            callback=cb,
            timeout_s=ctx.timeout_s,
        )


# --- Nominatim


class NominatimProvider(Provider):
    key = "nominatim"
    label = "OpenStreetMap"
    _last_request = 0.0

    def _throttled(self, start):
        wait = 1.05 - (time.time() - NominatimProvider._last_request)
        holder = _Delayed()

        def fire():
            if holder.cancelled:
                return
            NominatimProvider._last_request = time.time()
            holder.inner = start()

        if wait > 0:
            QTimer.singleShot(int(wait * 1000), fire)
        else:
            fire()
        return holder

    def _base_params(self, ctx):
        params = {
            "format": "jsonv2",
            "addressdetails": 1,
            "accept-language": ctx.language,
        }
        email = self.settings.get("nominatim/email")
        if email:
            params["email"] = email
        return params

    def search(self, query, ctx, live, done):
        if live:
            return None
        params = self._base_params(ctx)
        params.update(
            {"q": query, "limit": max(5, min(25, ctx.limit)), "dedupe": 1}
        )
        if self.settings.get("map/boundary"):
            params["polygon_geojson"] = 1
            params["polygon_threshold"] = 0.0004
        if ctx.countries:
            params["countrycodes"] = ",".join(ctx.countries)
        if ctx.view_bbox and (
            ctx.in_view_only or (ctx.bias and ctx.small_view(15.0))
        ):
            params["viewbox"] = "%.5f,%.5f,%.5f,%.5f" % ctx.view_bbox
            params["bounded"] = 1 if ctx.in_view_only else 0
        url = self.settings.get("nominatim/url").rstrip("/") + "/search"

        def cb(ok, data, info):
            if not ok:
                done([], "OpenStreetMap: %s" % data)
                return
            done(self._parse(data or [], ctx, query), None)

        return self._throttled(
            lambda: self.http.get(
                url, params, callback=cb, timeout_s=ctx.timeout_s
            )
        )

    def _parse(self, data, ctx, query):
        out = []
        if not isinstance(data, list):
            return out
        for i, r in enumerate(data):
            try:
                lat, lon = float(r["lat"]), float(r["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            display = r.get("display_name") or ""
            name = r.get("name") or display.split(",")[0].strip()
            sub = (
                display[len(name):].lstrip(" ,")
                if display.startswith(name)
                else display
            )
            category = r.get("category") or r.get("class") or ""
            typ = r.get("type") or ""
            addresstype = r.get("addresstype") or ""
            type_label = (
                humanize(addresstype or typ)
                if category in ("boundary", "place")
                else humanize(typ)
            )
            rank = int(r.get("place_rank") or 30)
            kind = (
                "area"
                if rank < 26 and category in ("boundary", "place", "landuse")
                else (
                    "road"
                    if category == "highway"
                    else ("poi" if category in POI_KEYS else "place")
                )
            )
            bb = r.get("boundingbox") or []
            bbox = (
                bbox_from(bb[2], bb[0], bb[3], bb[1]) if len(bb) == 4 else None
            )
            if rank >= 26 and bbox and (bbox[2] - bbox[0]) < 0.01:
                bbox = None
            geo = r.get("geojson")
            if not (
                isinstance(geo, dict)
                and geo.get("type")
                in ("Polygon", "MultiPolygon", "LineString", "MultiLineString")
            ):
                geo = None
            scale = (
                AREA_SCALE.get(addresstype)
                or AREA_SCALE.get(typ)
                or ctx.point_scale
            )
            importance = float(r.get("importance") or 0.0)
            osm_type = (r.get("osm_type") or "")[:1].upper()
            out.append(
                Candidate(
                    kind,
                    name,
                    sub,
                    lat,
                    lon,
                    source="nominatim",
                    score=self._base_score(i, 73.0) + importance * 4.0,
                    bbox=bbox,
                    scale=scale,
                    geojson=geo,
                    type_label=type_label,
                    address=r.get("address") or {},
                    query=query,
                    key=(
                        "osm:%s%s" % (osm_type, r.get("osm_id"))
                        if r.get("osm_id")
                        else None
                    ),
                )
            )
        return out

    def reverse(self, lat, lon, ctx, done):
        params = self._base_params(ctx)
        params.update({"lat": "%.7f" % lat, "lon": "%.7f" % lon, "zoom": 18})
        url = self.settings.get("nominatim/url").rstrip("/") + "/reverse"

        def cb(ok, data, info):
            if not ok or not isinstance(data, dict) or data.get("error"):
                done(
                    None,
                    "OpenStreetMap: %s"
                    % (data if not ok else data.get("error")),
                )
                return
            a = data.get("address") or {}
            locality = (
                a.get("city")
                or a.get("town")
                or a.get("village")
                or a.get("county")
                or ""
            )
            done(
                {
                    "title": (
                        data.get("name")
                        or a.get("road")
                        or a.get("neighbourhood")
                        or a.get("suburb")
                        or locality
                    ),
                    "address": data.get("display_name") or "",
                    "locality": locality,
                    "source": "OpenStreetMap",
                },
                None,
            )

        return self._throttled(
            lambda: self.http.get(
                url, params, callback=cb, timeout_s=ctx.timeout_s
            )
        )


# --- Overpass


class OverpassProvider(Provider):
    key = "overpass"
    label = "OSM deep search"

    def search(self, query, ctx, live, done):
        import re

        if live or not ctx.view_bbox:
            return None
        tokens = re.findall(r"\w+", query, re.UNICODE)
        if not tokens or len("".join(tokens)) < 3:
            return None
        minlon, minlat, maxlon, maxlat = ctx.view_bbox
        if (maxlon - minlon) > 0.6 or (maxlat - minlat) > 0.6:
            if not ctx.has_center:
                return None
            minlon, maxlon = ctx.center_lon - 0.3, ctx.center_lon + 0.3
            minlat, maxlat = ctx.center_lat - 0.3, ctx.center_lat + 0.3
        pattern = ".*".join(tokens).replace('"', "")
        bbox = "%.5f,%.5f,%.5f,%.5f" % (minlat, minlon, maxlat, maxlon)
        keys = "^(name|name:.*|alt_name|old_name|official_name)$"
        q = (
            "[out:json][timeout:9];("
            'node[~"%(k)s"~"%(p)s",i](%(b)s);'
            'way[~"%(k)s"~"%(p)s",i](%(b)s);'
            'relation[~"%(k)s"~"%(p)s",i](%(b)s););'
            "out center tags 20;"
        ) % {"k": keys, "p": pattern, "b": bbox}

        def cb(ok, data, info):
            if not ok:
                done([], "Overpass: %s" % data)
                return
            done(self._parse(data or {}, ctx, query), None)

        return self.http.post_form(
            self.settings.get("overpass/url"),
            {"data": q},
            callback=cb,
            timeout_s=max(10, ctx.timeout_s),
        )

    def _parse(self, data, ctx, query):
        out = []
        for i, el in enumerate(data.get("elements") or []):
            tags = el.get("tags") or {}
            name = (
                tags.get("name:" + ctx.language)
                or tags.get("name")
                or tags.get("official_name")
            )
            if not name:
                continue
            c = el.get("center") or el
            if c.get("lat") is None or c.get("lon") is None:
                continue
            typ = ""
            for k in (
                "amenity",
                "shop",
                "tourism",
                "leisure",
                "historic",
                "office",
                "place",
                "highway",
                "building",
                "landuse",
                "natural",
                "waterway",
                "railway",
                "man_made",
            ):
                if tags.get(k):
                    typ = tags[k] if tags[k] != "yes" else k
                    break
            parts = [
                tags.get(k)
                for k in (
                    "addr:street",
                    "addr:suburb",
                    "addr:city",
                    "addr:district",
                    "addr:state",
                    "is_in",
                )
                if tags.get(k)
            ]
            kind = (
                "road"
                if "highway" in tags
                else ("area" if "place" in tags else "poi")
            )
            out.append(
                Candidate(
                    kind,
                    name,
                    ", ".join(parts),
                    float(c["lat"]),
                    float(c["lon"]),
                    source="overpass",
                    score=self._base_score(i, 62.0),
                    scale=AREA_SCALE.get(typ, ctx.point_scale),
                    type_label=humanize(typ),
                    query=query,
                    key="osm:%s%s"
                    % ((el.get("type") or "")[:1].upper(), el.get("id")),
                )
            )
        return out


# ---------------------------------------------------------------------- Google

GOOGLE_PLACES = "https://places.googleapis.com/v1/"
GOOGLE_AREA_TYPES = {
    "locality",
    "sublocality",
    "sublocality_level_1",
    "neighborhood",
    "administrative_area_level_1",
    "administrative_area_level_2",
    "administrative_area_level_3",
    "country",
    "postal_code",
    "colloquial_area",
}
GOOGLE_GENERIC = {
    "point_of_interest",
    "establishment",
    "geocode",
    "political",
    "premise",
}
GOOGLE_FIELDS = "id,displayName,formattedAddress,location,viewport,types"


class GoogleProvider(Provider):
    key = "google"
    label = "Google"

    def __init__(self, http, settings, parent=None):
        super(GoogleProvider, self).__init__(http, settings, parent)
        self._session = None

    def enabled(self):
        return bool(
            self.settings.get("google/enabled")
            and self.settings.get("google/key")
        )

    def live_enabled(self):
        return self.enabled() and self.settings.get("google/live")

    def _headers(self, field_mask=None):
        h = {"X-Goog-Api-Key": self.settings.get("google/key")}
        if field_mask:
            h["X-Goog-FieldMask"] = field_mask
        return h

    def _bias(self, ctx, restrict=False):
        if ctx.view_bbox and ctx.small_view(20.0):
            minlon, minlat, maxlon, maxlat = ctx.view_bbox
            rect = {
                "rectangle": {
                    "low": {"latitude": minlat, "longitude": minlon},
                    "high": {"latitude": maxlat, "longitude": maxlon},
                }
            }
            return rect
        if ctx.has_center and not restrict:
            return {
                "circle": {
                    "center": {
                        "latitude": ctx.center_lat,
                        "longitude": ctx.center_lon,
                    },
                    "radius": ctx.view_radius_m(),
                }
            }
        return None

    def new_session(self):
        self._session = str(uuid.uuid4())

    def _types(self, types):
        types = list(types or [])
        specific = [t for t in types if t not in GOOGLE_GENERIC]
        return humanize((specific or types or [""])[0])

    def _kind(self, types):
        types = set(types or [])
        if types & GOOGLE_AREA_TYPES:
            return "area"
        if "route" in types:
            return "road"
        if "street_address" in types or "premise" in types:
            return "place"
        return "poi"

    def search(self, query, ctx, live, done):
        if live:
            if self._session is None:
                self.new_session()
            body = {
                "input": query,
                "languageCode": ctx.language,
                "sessionToken": self._session,
            }
            bias = self._bias(ctx)
            if bias:
                body["locationBias"] = bias
            if ctx.countries:
                body["includedRegionCodes"] = ctx.countries[:15]
            if ctx.has_center:
                body["origin"] = {
                    "latitude": ctx.center_lat,
                    "longitude": ctx.center_lon,
                }

            def cb(ok, data, info):
                if not ok:
                    done([], "Google: %s" % data)
                    return
                out = []
                for i, s in enumerate((data or {}).get("suggestions") or []):
                    p = s.get("placePrediction")
                    if not p:
                        continue
                    sf = p.get("structuredFormat") or {}
                    main = (sf.get("mainText") or {}).get("text") or (
                        p.get("text") or {}
                    ).get("text", "")
                    sec = (sf.get("secondaryText") or {}).get("text", "")
                    c = Candidate(
                        self._kind(p.get("types")),
                        main,
                        sec,
                        source="google",
                        score=self._base_score(i, 78.0),
                        key="google:" + p.get("placeId", main),
                        lazy={"placeId": p.get("placeId")},
                        query=query,
                        type_label=self._types(p.get("types")),
                    )
                    if p.get("distanceMeters") is not None:
                        c.distance_km = float(p["distanceMeters"]) / 1000.0
                    out.append(c)
                done(out, None)

            return self.http.post_json(
                GOOGLE_PLACES + "places:autocomplete",
                body,
                headers=self._headers(),
                callback=cb,
                timeout_s=ctx.timeout_s,
            )

        body = {"textQuery": query, "languageCode": ctx.language}
        if ctx.in_view_only and ctx.view_bbox:
            minlon, minlat, maxlon, maxlat = ctx.view_bbox
            body["locationRestriction"] = {
                "rectangle": {
                    "low": {"latitude": minlat, "longitude": minlon},
                    "high": {"latitude": maxlat, "longitude": maxlon},
                }
            }
        elif ctx.bias:
            bias = self._bias(ctx)
            if bias:
                body["locationBias"] = bias
        if len(ctx.countries) == 1:
            body["regionCode"] = ctx.countries[0]

        def cb2(ok, data, info):
            if not ok:
                done([], "Google: %s" % data)
                return
            out = []
            for i, p in enumerate((data or {}).get("places") or []):
                c = self._from_place(p, ctx, query)
                if c:
                    c.score = self._base_score(i, 80.0)
                    out.append(c)
            done(out, None)

        fields = ",".join("places." + f for f in GOOGLE_FIELDS.split(","))
        return self.http.post_json(
            GOOGLE_PLACES + "places:searchText",
            body,
            headers=self._headers(fields),
            callback=cb2,
            timeout_s=ctx.timeout_s,
        )

    def _from_place(self, p, ctx, query):
        loc = p.get("location") or {}
        if loc.get("latitude") is None:
            return None
        vp = p.get("viewport") or {}
        bbox = None
        if vp.get("low") and vp.get("high"):
            bbox = bbox_from(
                vp["low"].get("longitude"),
                vp["low"].get("latitude"),
                vp["high"].get("longitude"),
                vp["high"].get("latitude"),
            )
        kind = self._kind(p.get("types"))
        if kind != "area" and bbox and (bbox[2] - bbox[0]) < 0.02:
            bbox = None
        name = (p.get("displayName") or {}).get("text") or ""
        addr = p.get("formattedAddress") or ""
        sub = addr[len(name):].lstrip(" ,") if addr.startswith(name) else addr
        return Candidate(
            kind,
            name or addr.split(",")[0],
            sub,
            float(loc["latitude"]),
            float(loc["longitude"]),
            source="google",
            score=80.0,
            bbox=bbox,
            scale=ctx.point_scale,
            type_label=self._types(p.get("types")),
            key="google:" + (p.get("id") or name),
            query=query,
        )

    def resolve(self, cand, ctx, done):
        pid = (cand.lazy or {}).get("placeId")
        if not pid:
            done(None, "Google: missing place id")
            return None
        params = {"languageCode": ctx.language}
        if self._session:
            params["sessionToken"] = self._session

        def cb(ok, data, info):
            self._session = None  # a details call closes the billing session
            if not ok:
                done(None, "Google: %s" % data)
                return
            c = self._from_place(data or {}, ctx, cand.query)
            if not c:
                done(None, "Google returned no location for this place")
                return
            c.title = cand.title or c.title
            c.subtitle = cand.subtitle or c.subtitle
            c.score = cand.score
            done(c, None)

        return self.http.get(
            GOOGLE_PLACES + "places/" + pid,
            params,
            headers=self._headers(GOOGLE_FIELDS),
            callback=cb,
            timeout_s=ctx.timeout_s,
        )

    def reverse(self, lat, lon, ctx, done):
        params = {
            "latlng": "%.7f,%.7f" % (lat, lon),
            "key": self.settings.get("google/key"),
            "language": ctx.language,
        }

        def cb(ok, data, info):
            if (
                not ok
                or not isinstance(data, dict)
                or data.get("status") not in ("OK",)
            ):
                msg = (
                    data
                    if not ok
                    else (data.get("error_message") or data.get("status"))
                )
                done(None, "Google: %s" % msg)
                return
            res = (data.get("results") or [{}])[0]
            locality = ""
            for comp in res.get("address_components") or []:
                if "locality" in comp.get("types", []):
                    locality = comp.get("long_name", "")
                    break
            done(
                {
                    "title": res.get("formatted_address", "").split(",")[0],
                    "address": res.get("formatted_address", ""),
                    "locality": locality,
                    "source": "Google",
                },
                None,
            )

        return self.http.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params,
            callback=cb,
            timeout_s=ctx.timeout_s,
        )


def make_providers(http, settings, parent=None):
    """Providers in priority order (also the order used for reverse
    geocoding).
    """
    return [
        GoogleProvider(http, settings, parent),
        EsriProvider(http, settings, parent),
        PhotonProvider(http, settings, parent),
        NominatimProvider(http, settings, parent),
        OverpassProvider(http, settings, parent),
    ]


def text_match_bonus(query, title):
    q, t = fold(query), fold(title)
    if not q or not t:
        return 0.0
    if q == t:
        return 6.0
    if t.startswith(q):
        return 3.0
    if q in t:
        return 1.5
    return 0.0
