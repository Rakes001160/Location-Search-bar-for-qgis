# -*- coding: utf-8 -*-
"""
Open Location Code (Google "Plus Codes") for the Location Search Bar.

Pure-Python implementation of the public OLC specification
(https://github.com/google/open-location-code, Apache-2.0). Integer
arithmetic is used for encoding so codes match Google Maps exactly.
"""

import math
import re

ALPHABET = "23456789CFGHJMPQRVWX"
SEPARATOR = "+"
SEPARATOR_POSITION = 8
PADDING = "0"
BASE = 20
LAT_MAX = 90
LNG_MAX = 180
MAX_DIGITS = 15
PAIR_LENGTH = 10
PAIR_FIRST_PLACE_VALUE = BASE ** (PAIR_LENGTH // 2 - 1)  # 160000
PAIR_PRECISION = BASE**3  # 8000
PAIR_RESOLUTIONS = (20.0, 1.0, 0.05, 0.0025, 0.000125)
GRID_LENGTH = MAX_DIGITS - PAIR_LENGTH  # 5
GRID_COLUMNS = 4
GRID_ROWS = 5
GRID_LAT_FIRST_PLACE_VALUE = GRID_ROWS ** (GRID_LENGTH - 1)  # 625
GRID_LNG_FIRST_PLACE_VALUE = GRID_COLUMNS ** (GRID_LENGTH - 1)  # 256
FINAL_LAT_PRECISION = PAIR_PRECISION * GRID_ROWS**GRID_LENGTH  # 25,000,000
FINAL_LNG_PRECISION = PAIR_PRECISION * GRID_COLUMNS**GRID_LENGTH  # 8,192,000

_INDEX = {c: i for i, c in enumerate(ALPHABET)}
CODE_RE = re.compile(
    r"^[23456789CFGHJMPQRVWX0]{2,8}\+[23456789CFGHJMPQRVWX]*$", re.I
)


class CodeArea(object):
    """Decoded area of a Plus Code (south-west / north-east corners)."""

    __slots__ = ("lat_lo", "lng_lo", "lat_hi", "lng_hi", "code_length")

    def __init__(self, lat_lo, lng_lo, lat_hi, lng_hi, code_length):
        self.lat_lo = lat_lo
        self.lng_lo = lng_lo
        self.lat_hi = lat_hi
        self.lng_hi = lng_hi
        self.code_length = code_length

    @property
    def lat(self):
        return min(self.lat_lo + (self.lat_hi - self.lat_lo) / 2.0, LAT_MAX)

    @property
    def lng(self):
        return min(self.lng_lo + (self.lng_hi - self.lng_lo) / 2.0, LNG_MAX)

    def __repr__(self):
        return "CodeArea(%.7f, %.7f, %.7f, %.7f, %d)" % (
            self.lat_lo,
            self.lng_lo,
            self.lat_hi,
            self.lng_hi,
            self.code_length,
        )


def is_valid(code):
    if not code or not isinstance(code, str):
        return False
    if code.count(SEPARATOR) != 1:
        return False
    sep = code.find(SEPARATOR)
    if len(code) == 1 or sep > SEPARATOR_POSITION or sep % 2 == 1:
        return False
    pad = code.find(PADDING)
    if pad != -1:
        if sep < SEPARATOR_POSITION:  # short codes cannot be padded
            return False
        if pad == 0:
            return False
        rpad = code.rfind(PADDING) + 1
        pads = code[pad:rpad]
        if len(pads) % 2 == 1 or pads.count(PADDING) != len(pads):
            return False
        if not code.endswith(SEPARATOR):
            return False
    if len(code) - sep - 1 == 1:  # a single char after "+" is illegal
        return False
    for ch in code:
        if ch.upper() not in _INDEX and ch not in (SEPARATOR, PADDING):
            return False
    return True


def is_short(code):
    if not is_valid(code):
        return False
    sep = code.find(SEPARATOR)
    return 0 <= sep < SEPARATOR_POSITION


def is_full(code):
    if not is_valid(code) or is_short(code):
        return False
    first_lat = _INDEX[code[0].upper()] * BASE
    if first_lat >= LAT_MAX * 2:
        return False
    if len(code) > 1:
        first_lng = _INDEX[code[1].upper()] * BASE
        if first_lng >= LNG_MAX * 2:
            return False
    return True


def _clip_lat(lat):
    return min(LAT_MAX, max(-LAT_MAX, lat))


def _norm_lng(lng):
    while lng < -LNG_MAX:
        lng += 360
    while lng >= LNG_MAX:
        lng -= 360
    return lng


def _lat_precision(code_length):
    if code_length <= PAIR_LENGTH:
        return BASE ** (code_length // -2 + 2)
    return BASE**-3 / (GRID_ROWS ** (code_length - PAIR_LENGTH))


def encode(lat, lng, code_length=PAIR_LENGTH):
    """Encode a WGS84 position into a full Plus Code (default 10 digits
    ~14 m).
    """
    if code_length < 2 or (code_length < PAIR_LENGTH and code_length % 2 == 1):
        raise ValueError("Invalid Plus Code length %s" % code_length)
    code_length = min(code_length, MAX_DIGITS)
    lat = _clip_lat(lat)
    lng = _norm_lng(lng)
    if lat == LAT_MAX:
        lat = lat - _lat_precision(code_length)
    lat_val = int(round((lat + LAT_MAX) * FINAL_LAT_PRECISION, 6))
    lng_val = int(round((lng + LNG_MAX) * FINAL_LNG_PRECISION, 6))
    code = ""
    if code_length > PAIR_LENGTH:
        for _ in range(GRID_LENGTH):
            lat_digit = lat_val % GRID_ROWS
            lng_digit = lng_val % GRID_COLUMNS
            code = ALPHABET[lat_digit * GRID_COLUMNS + lng_digit] + code
            lat_val //= GRID_ROWS
            lng_val //= GRID_COLUMNS
    else:
        lat_val //= GRID_ROWS**GRID_LENGTH
        lng_val //= GRID_COLUMNS**GRID_LENGTH
    for _ in range(PAIR_LENGTH // 2):
        code = ALPHABET[lng_val % BASE] + code
        code = ALPHABET[lat_val % BASE] + code
        lat_val //= BASE
        lng_val //= BASE
    code = code[:SEPARATOR_POSITION] + SEPARATOR + code[SEPARATOR_POSITION:]
    if code_length >= SEPARATOR_POSITION:
        return code[:code_length + 1]
    return (
        code[:code_length]
        + PADDING * (SEPARATOR_POSITION - code_length)
        + SEPARATOR
    )


def decode(code):
    """Decode a full Plus Code into a CodeArea."""
    if not is_full(code):
        raise ValueError("Not a full Plus Code: %r" % code)
    code = re.sub(r"[+0]", "", code).upper()[:MAX_DIGITS]
    normal_lat = -LAT_MAX * PAIR_PRECISION
    normal_lng = -LNG_MAX * PAIR_PRECISION
    grid_lat = 0
    grid_lng = 0
    digits = min(len(code), PAIR_LENGTH)
    pv = PAIR_FIRST_PLACE_VALUE
    for i in range(0, digits, 2):
        normal_lat += _INDEX[code[i]] * pv
        normal_lng += _INDEX[code[i + 1]] * pv
        if i < digits - 2:
            pv //= BASE
    lat_precision = float(pv) / PAIR_PRECISION
    lng_precision = float(pv) / PAIR_PRECISION
    if len(code) > PAIR_LENGTH:
        row_pv = GRID_LAT_FIRST_PLACE_VALUE
        col_pv = GRID_LNG_FIRST_PLACE_VALUE
        digits = min(len(code), MAX_DIGITS)
        for i in range(PAIR_LENGTH, digits):
            val = _INDEX[code[i]]
            grid_lat += (val // GRID_COLUMNS) * row_pv
            grid_lng += (val % GRID_COLUMNS) * col_pv
            if i < digits - 1:
                row_pv //= GRID_ROWS
                col_pv //= GRID_COLUMNS
        lat_precision = float(row_pv) / FINAL_LAT_PRECISION
        lng_precision = float(col_pv) / FINAL_LNG_PRECISION
    lat = (
        float(normal_lat) / PAIR_PRECISION
        + float(grid_lat) / FINAL_LAT_PRECISION
    )
    lng = (
        float(normal_lng) / PAIR_PRECISION
        + float(grid_lng) / FINAL_LNG_PRECISION
    )
    return CodeArea(
        round(lat, 14),
        round(lng, 14),
        round(lat + lat_precision, 14),
        round(lng + lng_precision, 14),
        min(len(code), MAX_DIGITS),
    )


def recover_nearest(short_code, ref_lat, ref_lng):
    """Turn a short code (e.g. '2CXC+XX') into the full code nearest a
    reference
    point.
    """
    if is_full(short_code):
        return short_code.upper()
    if not is_short(short_code):
        raise ValueError("Not a valid short Plus Code: %r" % short_code)
    ref_lat = _clip_lat(ref_lat)
    ref_lng = _norm_lng(ref_lng)
    short_code = short_code.upper()
    padding_length = SEPARATOR_POSITION - short_code.find(SEPARATOR)
    resolution = math.pow(20, 2 - (padding_length / 2.0))
    half = resolution / 2.0
    area = decode(encode(ref_lat, ref_lng)[0:padding_length] + short_code)
    lat_c, lng_c = area.lat, area.lng
    if ref_lat + half < lat_c and lat_c - resolution >= -LAT_MAX:
        lat_c -= resolution
    elif ref_lat - half > lat_c and lat_c + resolution <= LAT_MAX:
        lat_c += resolution
    if ref_lng + half < lng_c:
        lng_c -= resolution
    elif ref_lng - half > lng_c:
        lng_c += resolution
    return encode(lat_c, lng_c, area.code_length)


def shorten(code, ref_lat, ref_lng):
    """Remove leading digits when the reference point is close enough (Google
    style).
    """
    if not is_full(code) or PADDING in code:
        return code
    code = code.upper()
    area = decode(code)
    rng = max(
        abs(area.lat - _clip_lat(ref_lat)), abs(area.lng - _norm_lng(ref_lng))
    )
    for i in range(len(PAIR_RESOLUTIONS) - 2, 0, -1):
        if rng < PAIR_RESOLUTIONS[i] * 0.3:
            return code[(i + 1) * 2:]
    return code
