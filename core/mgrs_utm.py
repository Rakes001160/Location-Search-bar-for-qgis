# -*- coding: utf-8 -*-
"""
UTM / MGRS helpers (pure Python).

Projection maths (lat/lon <-> UTM metres) is delegated to QGIS/PROJ by the
caller; this module only handles zones, latitude bands and the MGRS
100 km letter grid, so results are exactly as precise as PROJ itself.
"""

import re

BANDS = "CDEFGHJKLMNPQRSTUVWX"  # 8 degree bands from 80S, X is 12 degrees
COLUMN_SETS = ("ABCDEFGH", "JKLMNPQR", "STUVWXYZ")
ROW_LETTERS = "ABCDEFGHJKLMNPQRSTUV"  # 20 letters, I and O skipped

# Lowest northing (rounded down to 100 km) of each latitude band, used to
# undo the 2,000 km repetition of MGRS row letters.
MIN_NORTHING = {
    "C": 1100000,
    "D": 2000000,
    "E": 2800000,
    "F": 3700000,
    "G": 4600000,
    "H": 5500000,
    "J": 6400000,
    "K": 7300000,
    "L": 8200000,
    "M": 9100000,
    "N": 0,
    "P": 800000,
    "Q": 1700000,
    "R": 2600000,
    "S": 3500000,
    "T": 4400000,
    "U": 5300000,
    "V": 6200000,
    "W": 7000000,
    "X": 7900000,
}

MGRS_RE = re.compile(
    r"^\s*(\d{1,2})\s*([C-HJ-NP-X])\s*([A-HJ-NP-Z])\s*([A-HJ-NP-V])"
    r"\s*(\d{0,10})\s*(\d{0,5})\s*$",
    re.I,
)


def utm_zone(lat, lon):
    """UTM zone number for a WGS84 position, including the Norway/Svalbard
    exceptions.
    """
    lon = ((lon + 180.0) % 360.0) - 180.0
    zone = int((lon + 180.0) // 6.0) + 1
    if zone > 60:
        zone = 60
    if 56.0 <= lat < 64.0 and 3.0 <= lon < 12.0:
        zone = 32
    if 72.0 <= lat < 84.0:
        if 0.0 <= lon < 9.0:
            zone = 31
        elif 9.0 <= lon < 21.0:
            zone = 33
        elif 21.0 <= lon < 33.0:
            zone = 35
        elif 33.0 <= lon < 42.0:
            zone = 37
    return zone


def lat_band(lat):
    """MGRS latitude band letter, or None outside 80S..84N (polar UPS
    areas).
    """
    if lat < -80.0 or lat > 84.0:
        return None
    idx = int((lat + 80.0) // 8.0)
    return BANDS[min(idx, len(BANDS) - 1)]


def utm_epsg(zone, north):
    return (32600 if north else 32700) + int(zone)


def band_is_north(letter):
    return letter.upper() >= "N"


def _set_for_zone(zone):
    s = zone % 6
    return 6 if s == 0 else s


def parse_mgrs(text):
    """Parse an MGRS reference such as '43QBF4567845678' or '43Q BF 45678
    45678'.

    Returns dict(zone, band, north, easting, northing, precision_m) with the
    easting/northing at the centre of the referenced grid cell, or None.
    """
    m = MGRS_RE.match(text or "")
    if not m:
        return None
    zone = int(m.group(1))
    if not 1 <= zone <= 60:
        return None
    band = m.group(2).upper()
    col_letter = m.group(3).upper()
    row_letter = m.group(4).upper()
    d1, d2 = m.group(5) or "", m.group(6) or ""
    if d2:
        if len(d1) != len(d2) or len(d1) > 5:
            return None
        e_digits, n_digits = d1, d2
    else:
        if len(d1) % 2 or len(d1) > 10:
            return None
        half = len(d1) // 2
        e_digits, n_digits = d1[:half], d1[half:]
    set_ = _set_for_zone(zone)
    columns = COLUMN_SETS[(set_ - 1) % 3]
    if col_letter not in columns:
        return None
    east100k = (columns.index(col_letter) + 1) * 100000.0
    row_idx = ROW_LETTERS.index(row_letter)
    if set_ % 2 == 0:
        row_idx = (row_idx - 5) % 20
    north100k = row_idx * 100000.0
    while north100k < MIN_NORTHING[band]:
        north100k += 2000000.0
    k = len(e_digits)
    precision = 10.0 ** (5 - k)
    easting = (
        east100k
        + (float(e_digits) * precision if k else 0.0)
        + precision / 2.0
    )
    northing = (
        north100k
        + (float(n_digits) * precision if k else 0.0)
        + precision / 2.0
    )
    return {
        "zone": zone,
        "band": band,
        "north": band_is_north(band),
        "easting": easting,
        "northing": northing,
        "precision_m": precision,
    }


def to_mgrs(zone, band, easting, northing, digits=5, spaced=True):
    """Build an MGRS string from UTM metres (truncates, as the MGRS standard
    requires).
    """
    if band is None:
        return ""
    set_ = _set_for_zone(zone)
    columns = COLUMN_SETS[(set_ - 1) % 3]
    col = int(easting // 100000)
    if not 1 <= col <= 8:
        return ""
    row = int(northing // 100000) % 20
    if set_ % 2 == 0:
        row = (row + 5) % 20
    square = columns[col - 1] + ROW_LETTERS[row]
    digits = max(0, min(5, int(digits)))
    div = 10 ** (5 - digits)
    e = int((easting % 100000) // div)
    n = int((northing % 100000) // div)
    if digits == 0:
        nums = ""
    else:
        nums = (
            "%0*d" % (digits, e)
            + (" " if spaced else "")
            + "%0*d" % (digits, n)
        )
    if spaced:
        return ("%d%s %s %s" % (zone, band, square, nums)).strip()
    return "%d%s%s%s" % (zone, band, square, nums)
