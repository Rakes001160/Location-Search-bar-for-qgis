# -*- coding: utf-8 -*-
"""
Coordinate parser for the Location Search Bar (no QGIS imports).

``parse(text)`` returns a list of ``Hit`` objects: every plausible reading of
the text as a location. Ambiguous input (lat/lon order, UTM "S" band versus
southern hemisphere, projected numbers without a CRS) yields several hits;
the search engine ranks them against the current map view.

Understood formats
 * Decimal degrees 23.0225, 72.5714 | 23.0225N 72.5714E | N23.0225 E72.5714
  * Degrees/minutes     23°01.350'N 72°34.284'E
 * DMS 23°01'21"N 72°34'17"E | 23 01 21 N 72 34 17 E | 23d01m21sN
  * UTM                 43N 345678 2545678 | 43Q 345678E 2545678N | Zone 43 ...
  * MGRS                43QBF4567845678 | 43Q BF 45678 45678
  * Plus Codes          7JMJ2CXC+XX | 2CXC+XX Ahmedabad
  * Geohash             gh:tsj4ty (or a bare geohash with digits)
  * CRS tagged          EPSG:32643 345678 2545678 | 345678,2545678 (EPSG:24378)
 * WKT / GeoJSON POINT(72.57 23.02) | {"type":"Point","coordinates":[..]}
 * Bare projected XY 345678.2 2545678.9 (resolved against project CRS / UTM)
  * Map links           see url_parser.py
  * View hints          "... 1:5000" or "... 15z" appended to any of the above
"""

import json
import re

from . import olc
from .mgrs_utm import parse_mgrs, utm_epsg, band_is_north
from .textutil import ascii_digits, geohash_decode, GEOHASH_RE

DEG, MIN, SEC = 1, 2, 3
METERS_PER_DEGREE = 111320.0


class Hit(object):
    """One possible interpretation of the search text."""

    def __init__(self, kind, **kw):
        self.kind = kind  # latlon utm mgrs pluscode pluscode_short geohash
        #                                     xy crs_xy url query shortlink
        self.lat = kw.get("lat")
        self.lon = kw.get("lon")
        self.x = kw.get("x")
        self.y = kw.get("y")
        self.crs = kw.get("crs")
        self.zone = kw.get("zone")
        self.north = kw.get("north")
        self.band = kw.get("band")
        self.confidence = float(kw.get("confidence", 90.0))
        self.label = kw.get("label", "")
        self.precision_m = kw.get("precision_m")
        self.bbox = kw.get("bbox")  # (minlon, minlat, maxlon, maxlat)
        self.zoom = kw.get("zoom")
        self.scale = kw.get("scale")
        self.swapped = kw.get("swapped", False)
        self.order_known = kw.get("order_known", False)
        self.code = kw.get("code")
        self.locality = kw.get("locality")
        self.text = kw.get("text")
        self.url = kw.get("url")
        self.name = kw.get("name")

    def __repr__(self):
        return "Hit(%s, lat=%s, lon=%s, x=%s, y=%s, crs=%s, conf=%s, %s)" % (
            self.kind,
            self.lat,
            self.lon,
            self.x,
            self.y,
            self.crs,
            self.confidence,
            self.label,
        )


def valid_latlon(lat, lon):
    return (
        lat is not None
        and lon is not None
        and -90.0 <= lat <= 90.0
        and -180.0 <= lon <= 180.0
    )


def zoom_to_scale(zoom):
    return 591657527.591555 / (2.0 ** float(zoom))


# ------------------------------------------------------------------ normalise

_SYMBOLS = {
    "\u00b0": "\u00b0",
    "\u00ba": "\u00b0",
    "\u02da": "\u00b0",
    "\u2218": "\u00b0",
    "\u1d52": "\u00b0",
    "\u2070": "\u00b0",
    "\u2032": "'",
    "\u2019": "'",
    "\u2018": "'",
    "\u00b4": "'",
    "`": "'",
    "\u02b9": "'",
    "\u02bc": "'",
    "\u201b": "'",
    "\u2033": '"',
    "\u201d": '"',
    "\u201c": '"',
    "\u02ba": '"',
    "\u3003": '"',
    "\u201f": '"',
    "\u201e": '"',
    "\u2212": "-",
    "\u2013": "-",
    "\u2014": "-",
    "\u2010": "-",
    "\u2011": "-",
    "\u00a0": " ",
    "\u2009": " ",
    "\u202f": " ",
    "\t": " ",
    "\n": " ",
    "\r": " ",
    "\uff0c": ",",
    "\uff1b": ";",
    "\uff1a": ":",
}

_WORD_SUBS = [
    (re.compile(r"(?<![A-Za-z])lat(?:itude)?(?![A-Za-z])", re.I), " LAT "),
    (
        re.compile(
            r"(?<![A-Za-z])(?:longitude|long|lng|lon)(?![A-Za-z])", re.I
        ),
        " LON ",
    ),
    (re.compile(r"(?<![A-Za-z])north(?![A-Za-z])", re.I), " N "),
    (re.compile(r"(?<![A-Za-z])south(?![A-Za-z])", re.I), " S "),
    (re.compile(r"(?<![A-Za-z])east(?![A-Za-z])", re.I), " E "),
    (re.compile(r"(?<![A-Za-z])west(?![A-Za-z])", re.I), " W "),
    (re.compile(r"(?<![A-Za-z])deg(?:rees?|s)?(?![A-Za-z])", re.I), "\u00b0"),
    (re.compile(r"(?<![A-Za-z])min(?:utes?|s)?(?![A-Za-z])", re.I), "'"),
    (re.compile(r"(?<![A-Za-z])sec(?:onds?|s)?(?![A-Za-z])", re.I), '"'),
]


def normalise(text):
    s = ascii_digits(text or "")
    s = "".join(_SYMBOLS.get(ch, ch) for ch in s)
    s = s.replace("''", '"')
    for rx, rep in _WORD_SUBS:
        s = rx.sub(rep, s)
    if "." not in s:
        # European decimal commas: "23,0225 72,5714" or "23,0225; 72,5714"
        if len(re.findall(r"\d,\d", s)) >= 2 and re.search(r"[;\s]|,\s", s):
            s = re.sub(r"(?<=\d),(?=\d)", ".", s)
    if re.search(r"[NSEWnsew]", s) and re.search(r"\d-\d", s):
        s = re.sub(r"(?<=\d)-(?=\d)", " ", s)  # 23-01-21N style
    return re.sub(r"\s+", " ", s).strip()


# ------------------------------------------------------------------ view spec

_SCALE_RE = re.compile(
    r"(?:^|[\s,;@])1\s*:\s*(\d{1,3}(?:[,\s]\d{3})+|\d+)\s*$"
)
_ZOOM_RE = re.compile(
    r"(?:^|[\s,;@])(?:(?:zoom|z)\s*=?\s*(\d{1,2}(?:\.\d+)?)"
    r"|(\d{1,2}(?:\.\d+)?)\s*z)\s*$",
    re.I,
)


def extract_view_spec(text):
    """Strip a trailing '1:5000' scale or '15z' zoom. Returns (scale, zoom,
    text).
    """
    scale = zoom = None
    m = _SCALE_RE.search(text)
    if m and m.start() > 0:
        try:
            scale = float(re.sub(r"[,\s]", "", m.group(1)))
        except ValueError:
            scale = None
        if scale and scale >= 50:
            text = text[:m.start()].rstrip(" ,;@")
        else:
            scale = None
    m = _ZOOM_RE.search(text)
    if m and m.start() > 0:
        val = m.group(1) or m.group(2)
        try:
            z = float(val)
        except ValueError:
            z = None
        if z is not None and 0 <= z <= 23:
            zoom = z
            text = text[:m.start()].rstrip(" ,;@")
    return scale, zoom, text


# ------------------------------------------------------------------ lat / lon


class _Tok(object):
    __slots__ = ("type", "value", "text")

    def __init__(self, type_, value, text=""):
        self.type = type_
        self.value = value
        self.text = text


_TOK_RE = re.compile(
    r"(?P<num>[-+]?(?:\d+(?:\.\d*)?|\.\d+))"
    "|(?P<unit>[\u00b0'\"])"
    r"|(?P<word>[A-Za-z]+)"
    r"|(?P<sep>[,;/|])"
    r"|(?P<skip>[\s:=()\[\]{}@<>])"
    r"|(?P<other>.)"
)

_UNIT_OF = {"\u00b0": DEG, "'": MIN, '"': SEC}


def _tokenize(s):
    toks = []
    run_units = set()
    for m in _TOK_RE.finditer(s):
        kind = m.lastgroup
        val = m.group(kind)
        if kind == "skip":
            continue
        if kind == "other":
            return None
        if kind == "num":
            toks.append(_Tok("num", float(val), val))
        elif kind == "unit":
            if not (toks and toks[-1].type == "num"):
                return None
            u = _UNIT_OF[val]
            toks.append(_Tok("unit", u))
            run_units.add(u)
        elif kind == "sep":
            toks.append(_Tok("sep", val))
            run_units = set()
        elif kind == "word":
            up = val.upper()
            if up in ("LAT", "LON"):
                toks.append(_Tok("label", up.lower()))
                run_units = set()
                continue
            if len(val) > 3:
                return None
            for i, c in enumerate(val):
                nxt = val[i + 1] if i + 1 < len(val) else ""
                prev_num = bool(toks) and toks[-1].type == "num"
                if c in "dD":
                    if not prev_num:
                        return None
                    toks.append(_Tok("unit", DEG))
                    run_units.add(DEG)
                elif c in "mM":
                    if not prev_num:
                        return None
                    toks.append(_Tok("unit", MIN))
                    run_units.add(MIN)
                elif (
                    c in "sS"
                    and prev_num
                    and run_units & {DEG, MIN}
                    and (c == "s" or nxt.upper() in ("N", "S", "E", "W"))
                ):
                    toks.append(_Tok("unit", SEC))
                    run_units.add(SEC)
                elif c.upper() in "NSEW":
                    toks.append(_Tok("hemi", c.upper()))
                    run_units = set()
                else:
                    return None
    return toks


class _Comp(object):
    __slots__ = ("nums", "texts", "units", "hemi", "label")

    def __init__(self):
        self.nums = []
        self.texts = []
        self.units = []
        self.hemi = None
        self.label = None


def _components(toks):
    """Group tokens into coordinate components (one per axis)."""
    if not toks:
        return None
    first_hemi = next(
        (i for i, t in enumerate(toks) if t.type == "hemi"), None
    )
    first_num = next((i for i, t in enumerate(toks) if t.type == "num"), None)
    if first_num is None:
        return None
    prefix_style = first_hemi is not None and first_hemi < first_num
    comps = []
    cur = _Comp()

    def close():
        comps.append(cur)
        return _Comp()

    for t in toks:
        if t.type == "num":
            cur.nums.append(t.value)
            cur.texts.append(t.text)
            cur.units.append(None)
        elif t.type == "unit":
            if not cur.nums or cur.units[-1] is not None:
                return None
            assigned = [u for u in cur.units[:-1] if u is not None]
            if assigned and t.value <= max(assigned):
                # e.g. 23°01'21" 72°... -> the 72 starts the next component
                num, txt = cur.nums.pop(), cur.texts.pop()
                cur.units.pop()
                if not cur.nums:
                    return None
                cur = close()
                cur.nums.append(num)
                cur.texts.append(txt)
                cur.units.append(t.value)
            else:
                cur.units[-1] = t.value
        elif t.type == "hemi":
            if cur.nums:
                if cur.hemi is None and not prefix_style:
                    cur.hemi = t.value
                    cur = close()
                else:
                    cur = close()
                    cur.hemi = t.value
            else:
                if cur.hemi is not None:
                    return None
                cur.hemi = t.value
        elif t.type == "sep":
            if cur.nums:
                cur = close()
        elif t.type == "label":
            if cur.nums:
                cur = close()
            if cur.label is not None:
                return None
            cur.label = t.value
    if cur.nums:
        comps.append(cur)
    elif cur.hemi is not None or cur.label is not None:
        return None
    return comps


def _split_single(comp):
    """'23.02 72.57' or '23 1 21 72 34 17' without separators -> two
    components.
    """
    n = len(comp.nums)
    if any(u is not None for u in comp.units) or comp.hemi or comp.label:
        return None
    split = {2: 1, 4: 2, 6: 3}.get(n)
    if not split:
        return None
    a, b = _Comp(), _Comp()
    a.nums, a.texts, a.units = (
        comp.nums[:split],
        comp.texts[:split],
        [None] * split,
    )
    b.nums, b.texts, b.units = (
        comp.nums[split:],
        comp.texts[split:],
        [None] * (n - split),
    )
    return [a, b]


def _decimals(txt):
    txt = txt.lstrip("+-")
    return len(txt.split(".", 1)[1]) if "." in txt else 0


def _comp_value(c):
    """Return (degrees, precision_deg, form) for one component, or None."""
    n = len(c.nums)
    if n == 0 or n > 3:
        return None
    order = []
    nxt = DEG
    for u in c.units:
        u = nxt if u is None else u
        if order and u <= order[-1]:
            return None
        if u > SEC:
            return None
        order.append(u)
        nxt = u + 1
    if order[0] != DEG:
        return None
    vals = dict(zip(order, c.nums))
    txts = dict(zip(order, c.texts))
    negative = txts[DEG].strip().startswith("-")
    d = abs(vals[DEG])
    m = vals.get(MIN, 0.0)
    s = vals.get(SEC, 0.0)
    if m < 0 or s < 0:
        return None
    if (MIN in vals or SEC in vals) and d != int(d):
        return None
    if SEC in vals and MIN in vals and m != int(m):
        return None
    if m >= 60 or s >= 60:
        return None
    value = d + m / 60.0 + s / 3600.0
    last = order[-1]
    prec = 10.0 ** -_decimals(txts[last])
    prec_deg = (
        prec
        if last == DEG
        else (prec / 60.0 if last == MIN else prec / 3600.0)
    )
    form = {
        DEG: "Decimal degrees",
        MIN: "Degrees, decimal minutes",
        SEC: "Degrees, minutes, seconds",
    }[last]
    if c.hemi in ("S", "W"):
        negative = True
    elif c.hemi in ("N", "E"):
        negative = False
    return (-value if negative else value), prec_deg, form


def _axis(c):
    if c.hemi in ("N", "S"):
        return "lat"
    if c.hemi in ("E", "W"):
        return "lon"
    return c.label


def parse_latlon(text, url_order=False):
    """Parse geographic coordinates. Returns a list of 'latlon' hits."""
    s = normalise(text)
    if not s or not re.search(r"\d", s):
        return []
    toks = _tokenize(s)
    if not toks:
        return []
    comps = _components(toks)
    if not comps:
        return []
    penalty = 0.0
    if len(comps) == 1:
        comps = _split_single(comps[0])
        if not comps:
            return []
    if (
        len(comps) == 3
        and len(comps[2].nums) == 1
        and comps[2].hemi is None
        and comps[2].label is None
        and comps[2].units[0] is None
    ):
        comps = comps[:2]  # trailing altitude / accuracy value
        penalty = 4.0
    if len(comps) != 2:
        return []
    a, b = comps
    va, vb = _comp_value(a), _comp_value(b)
    if va is None or vb is None:
        return []
    ax_a, ax_b = _axis(a), _axis(b)
    if ax_a and ax_b and ax_a == ax_b:
        return []
    hinted = bool(ax_a or ax_b) or url_order
    if ax_a == "lon" or ax_b == "lat":
        lat, lon = vb[0], va[0]
    else:
        lat, lon = va[0], vb[0]
    prec_m = max(va[1], vb[1]) * METERS_PER_DEGREE
    form = va[2] if va[2] == vb[2] else "Mixed degree formats"
    hits = []
    if valid_latlon(lat, lon):
        hits.append(
            Hit(
                "latlon",
                lat=lat,
                lon=lon,
                precision_m=prec_m,
                label=form,
                order_known=hinted,
                confidence=(98.0 if hinted else 95.0) - penalty,
            )
        )
    if not hinted and valid_latlon(lon, lat) and lat != lon:
        conf = (88.0 if hits else 96.0) - penalty
        hits.append(
            Hit(
                "latlon",
                lat=lon,
                lon=lat,
                precision_m=prec_m,
                label=form,
                swapped=True,
                confidence=conf,
            )
        )
    return hits


# --- bare / tagged XY

_XY_TAGS = {
    "E": "E",
    "ME": "E",
    "X": "E",
    "N": "N",
    "MN": "N",
    "Y": "N",
    "M": None,
}


def _tagged_numbers(text):
    """Numbers with optional E/N/X/Y tags. Returns [(value, tag)] or None."""
    s = text.upper()
    s = re.sub(r"(?<![A-Z])EASTINGS?(?![A-Z])", " E ", s)
    s = re.sub(r"(?<![A-Z])NORTHINGS?(?![A-Z])", " N ", s)
    toks = re.findall(r"[-+]?\d+(?:\.\d+)?|[A-Z]+|[^\s\dA-Z]", s)
    toks = [
        t
        for t in toks
        if t not in (",", ";", "/", "|", ":", "=", "(", ")", "[", "]", "@")
    ]
    for t in toks:
        if not (t[0].isdigit() or t[0] in "+-") and t not in _XY_TAGS:
            return None
    first_tag = next(
        (i for i, t in enumerate(toks) if t in _XY_TAGS and _XY_TAGS[t]), None
    )
    first_num = next(
        (i for i, t in enumerate(toks) if t[0].isdigit() or t[0] in "+-"), None
    )
    if first_num is None:
        return None
    prefix_style = first_tag is not None and first_tag < first_num
    vals = []
    pending = None
    for t in toks:
        if t[0].isdigit() or t[0] in "+-":
            try:
                vals.append([float(t), pending if prefix_style else None])
            except ValueError:
                return None
            pending = None
        else:
            tag = _XY_TAGS[t]
            if tag is None:
                continue
            if prefix_style:
                pending = tag
            elif vals and vals[-1][1] is None:
                vals[-1][1] = tag
    return vals


def _order_xy(vals):
    (a, ta), (b, tb) = vals
    if ta == "N" or tb == "E":
        return b, a, True
    if ta == "E" or tb == "N":
        return a, b, True
    return a, b, False


def parse_xy(text):
    """Two plain numbers that cannot be lat/lon -> projected candidate (CRS
    decided later).
    """
    s = normalise(text)
    vals = _tagged_numbers(s)
    if not vals or len(vals) != 2:
        return []
    x, y, known = _order_xy(vals)
    if valid_latlon(y, x) or valid_latlon(x, y):
        return []  # plain degrees; parse_latlon owns this case
    return [
        Hit(
            "xy",
            x=x,
            y=y,
            order_known=known,
            confidence=80.0,
            label="Projected coordinates",
        )
    ]


_CRS_RE = re.compile(
    r"(?:(?<![A-Za-z])(EPSG|ESRI|IGNF|OGC)\s*[:\s]\s*([A-Za-z0-9_.]+))"
    r"|(?:(?<![A-Za-z])SRID\s*=\s*(\d+)\s*;?)"
    r"|(?:(?<![A-Za-z])CRS\s*[:=]\s*(?:EPSG\s*[:\s]\s*)?(\d+))",
    re.I,
)


def parse_crs_tagged(text):
    m = _CRS_RE.search(text)
    if not m:
        return []
    if m.group(1):
        authid = "%s:%s" % (m.group(1).upper(), m.group(2))
    else:
        authid = "EPSG:%s" % (m.group(3) or m.group(4))
    rest = (text[:m.start()] + " " + text[m.end():]).strip()
    rest = re.sub(r"(?i)\bPOINT\b", " ", rest)
    vals = _tagged_numbers(normalise(rest))
    if authid.upper() in ("EPSG:4326", "EPSG:4979", "OGC:CRS84", "EPSG:4258"):
        hits = []
        if vals and len(vals) == 2:
            x, y, known = _order_xy(vals)
            if valid_latlon(y, x):
                hits.append(
                    Hit(
                        "latlon",
                        lat=y,
                        lon=x,
                        order_known=known,
                        confidence=96.0,
                        label="%s (x = longitude)" % authid,
                    )
                )
            if not known and valid_latlon(x, y) and x != y:
                hits.append(
                    Hit(
                        "latlon",
                        lat=x,
                        lon=y,
                        swapped=True,
                        confidence=88.0 if hits else 95.0,
                        label=authid,
                    )
                )
        else:
            hits = [h for h in parse_latlon(rest)]
        return hits
    if not vals or len(vals) != 2:
        return []
    x, y, known = _order_xy(vals)
    hits = [
        Hit(
            "crs_xy",
            x=x,
            y=y,
            crs=authid,
            order_known=known,
            confidence=97.0,
            label=authid,
        )
    ]
    if not known and x != y:
        hits.append(
            Hit(
                "crs_xy",
                x=y,
                y=x,
                crs=authid,
                swapped=True,
                confidence=86.0,
                label=authid,
            )
        )
    return hits


# ------------------------------------------------------------------ UTM


def parse_utm(text):
    s = " " + ascii_digits(text).upper() + " "
    s = re.sub(r"(?<![A-Z])(?:UTM|ZONE|WGS\s*-?\s*84)(?![A-Z])", " ", s)
    s = re.sub(r"(?<![A-Z])NORTH(?:ERN)?(?![A-Z])", " N ", s)
    s = re.sub(r"(?<![A-Z])SOUTH(?:ERN)?(?![A-Z])", " S ", s)
    s = re.sub(r"(?<![A-Z])EASTINGS?(?![A-Z])", " E ", s)
    s = re.sub(r"(?<![A-Z])NORTHINGS?(?![A-Z])", " N ", s)
    m = re.search(r"(?<![\d.])(\d{1,2})\s?([C-HJ-NP-X])(?![A-Z])", s)
    letter = None
    if m:
        zone = int(m.group(1))
        letter = m.group(2)
        rest = s[:m.start()] + " " + s[m.end():]
    else:
        m = re.match(r"^\s*(\d{1,2})\s+(?=\S*\d{5})", s)
        if not m:
            return []
        zone = int(m.group(1))
        rest = s[m.end():]
    if not 1 <= zone <= 60:
        return []
    vals = _tagged_numbers(rest)
    if not vals or len(vals) != 2:
        return []
    e, n, known = _order_xy(vals)

    def ok(ee, nn):
        return 100000.0 <= ee <= 900000.0 and 0.0 <= nn <= 10000000.0

    if not ok(e, n):
        if not known and ok(n, e):
            e, n = n, e
        else:
            return []
    if letter is None:
        hemis = [(True, 90.0), (False, 84.0)]
        band = None
    elif letter == "N":
        hemis = [(True, 97.0)]
        band = None
    elif letter == "S":
        hemis = [
            (True, 93.0),
            (False, 90.0),
        ]  # band S (32-40N) or southern hemisphere
        band = "S"
    else:
        hemis = [(band_is_north(letter), 97.0)]
        band = letter
    hits = []
    for north, conf in hemis:
        label = "UTM zone %d%s" % (zone, "N" if north else "S")
        if letter == "S" and north:
            label = "UTM zone %dS (latitude band S, north)" % zone
        hits.append(
            Hit(
                "utm",
                x=e,
                y=n,
                zone=zone,
                north=north,
                band=band,
                crs="EPSG:%d" % utm_epsg(zone, north),
                confidence=conf,
                label=label,
                precision_m=1.0,
                order_known=True,
            )
        )
    return hits


def parse_mgrs_hit(text):
    t = ascii_digits(text or "").strip()
    r = parse_mgrs(t)
    if not r:
        return []
    return [
        Hit(
            "mgrs",
            x=r["easting"],
            y=r["northing"],
            zone=r["zone"],
            north=r["north"],
            band=r["band"],
            crs="EPSG:%d" % utm_epsg(r["zone"], r["north"]),
            precision_m=r["precision_m"],
            confidence=99.0,
            order_known=True,
            label="MGRS (%s m grid)" % ("%g" % r["precision_m"]),
        )
    ]


# --- Plus Codes / geohash

_PLUS_RE = re.compile(
    r"(?<![\w+])([23456789CFGHJMPQRVWX]{2,8}0*\+"
    r"[23456789CFGHJMPQRVWX]{0,7})(?![\w+])",
    re.I,
)


def parse_pluscode(text):
    t = (text or "").strip()
    m = _PLUS_RE.search(t)
    if not m:
        return []
    code = m.group(1).upper()
    if not olc.is_valid(code):
        return []
    locality = (t[:m.start()] + " " + t[m.end():]).strip(" ,;")
    if olc.is_full(code):
        area = olc.decode(code)
        size = (
            max(area.lat_hi - area.lat_lo, area.lng_hi - area.lng_lo)
            * METERS_PER_DEGREE
        )
        return [
            Hit(
                "pluscode",
                lat=area.lat,
                lon=area.lng,
                code=code,
                confidence=99.0,
                bbox=(area.lng_lo, area.lat_lo, area.lng_hi, area.lat_hi),
                precision_m=size / 2.0,
                label="Plus Code",
                order_known=True,
            )
        ]
    if olc.is_short(code):
        return [
            Hit(
                "pluscode_short",
                code=code,
                locality=locality or None,
                confidence=97.0,
                label="Plus Code (short)",
                order_known=True,
            )
        ]
    return []


def parse_geohash(text):
    t = (text or "").strip()
    m = re.match(r"^(?:geohash|gh)\s*[:=]?\s*([0-9a-z]{1,12})$", t, re.I)
    explicit = bool(m)
    gh = m.group(1).lower() if m else t
    if not explicit:
        if not (6 <= len(gh) <= 12 and GEOHASH_RE.match(gh)):
            return []
        if (
            sum(c.isdigit() for c in gh) < 1
            or sum(c.isalpha() for c in gh) < 2
        ):
            return []
    res = geohash_decode(gh)
    if not res:
        return []
    lat, lon, bbox = res
    size = max(bbox[2] - bbox[0], bbox[3] - bbox[1]) * METERS_PER_DEGREE
    return [
        Hit(
            "geohash",
            lat=lat,
            lon=lon,
            bbox=bbox,
            code=gh,
            precision_m=size / 2.0,
            confidence=98.0 if explicit else 52.0,
            label="Geohash",
            order_known=True,
        )
    ]


# --- WKT / GeoJSON

_WKT_RE = re.compile(
    r"^\s*(?:SRID\s*=\s*(\d+)\s*;)?\s*POINT\s*(?:ZM|Z|M)?\s*\("
    r"\s*([-+]?\d+(?:\.\d+)?)"
    r"\s+([-+]?\d+(?:\.\d+)?)",
    re.I,
)


def _point_hits(x, y, crs, label):
    if crs and crs not in ("EPSG:4326", "OGC:CRS84"):
        return [
            Hit(
                "crs_xy",
                x=x,
                y=y,
                crs=crs,
                confidence=98.0,
                label=label,
                order_known=True,
            )
        ]
    if valid_latlon(y, x):
        return [
            Hit(
                "latlon",
                lat=y,
                lon=x,
                confidence=98.0,
                label=label,
                order_known=True,
            )
        ]
    return [
        Hit("xy", x=x, y=y, confidence=85.0, label=label, order_known=True)
    ]


def parse_wkt(text):
    m = _WKT_RE.match(text or "")
    if not m:
        return []
    crs = "EPSG:%s" % m.group(1) if m.group(1) else None
    return _point_hits(float(m.group(2)), float(m.group(3)), crs, "WKT point")


def parse_geojson(text):
    t = (text or "").strip()
    if not t.startswith("{"):
        return []
    try:
        obj = json.loads(t)
    except ValueError:
        return []
    geom = obj.get("geometry") if obj.get("type") == "Feature" else obj
    if not isinstance(geom, dict) or geom.get("type") != "Point":
        return []
    coords = geom.get("coordinates") or []
    if len(coords) < 2:
        return []
    try:
        return _point_hits(
            float(coords[0]), float(coords[1]), None, "GeoJSON point"
        )
    except (TypeError, ValueError):
        return []


# --- entry point


def parse(text):
    """Return every plausible location reading of ``text`` (best first)."""
    from .url_parser import (
        parse_url,
    )  # local import: url_parser uses parse_latlon

    raw = (text or "").strip()
    if not raw:
        return []
    t = ascii_digits(raw)
    scale, zoom, t = extract_view_spec(t)
    hits = parse_url(t)
    if hits is None:
        hits = []
        exclusive = (
            parse_geojson,
            parse_wkt,
            parse_pluscode,
            parse_mgrs_hit,
            parse_utm,
            parse_crs_tagged,
        )
        for fn in exclusive:
            found = fn(t)
            if found:
                hits = found
                break
        if not hits:
            hits = parse_latlon(t)
            if not hits:
                hits = parse_xy(t)
            if not hits:
                hits = parse_geohash(t)
    for h in hits:
        if scale and not h.scale:
            h.scale = scale
        if zoom is not None and h.zoom is None:
            h.zoom = zoom
    hits.sort(key=lambda h: -h.confidence)
    return hits
