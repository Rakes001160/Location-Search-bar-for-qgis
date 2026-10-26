# -*- coding: utf-8 -*-
"""Small text helpers: digit normalisation, search folding, geohash."""

import re
import unicodedata

# ---------------------------------------------------------------- digits


def ascii_digits(text):
    """Convert any Unicode decimal digits (Devanagari, Gujarati, Arabic...) to
    0-9.
    """
    if not text:
        return text
    out = []
    for ch in text:
        if ch.isdigit() and not ("0" <= ch <= "9"):
            val = unicodedata.decimal(ch, None)
            out.append(str(val) if val is not None else ch)
        else:
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------- folding

_SEP_RE = re.compile(r"[\s_\-/\\,.;:()\[\]{}'\"`|]+")


def fold(text):
    """Case/accent-insensitive key for matching. Keeps Indic vowel signs
    intact.
    """
    if not text:
        return ""
    text = ascii_digits(str(text))
    decomposed = unicodedata.normalize("NFKD", text)
    out = []
    prev_base = ""
    for ch in decomposed:
        if unicodedata.combining(ch):
            # strip accents only from Latin letters (é -> e); keep matras etc.
            if prev_base and ord(prev_base) < 0x0250:
                continue
        else:
            prev_base = ch
        out.append(ch)
    text = unicodedata.normalize("NFC", "".join(out)).casefold()
    return _SEP_RE.sub(" ", text).strip()


def words(text):
    return fold(text).split()


# ---------------------------------------------------------------- geohash

# digits plus the lowercase letters except a, i, l and o
GEOHASH_ALPHABET = "".join(
    [str(d) for d in range(10)]
    + [c for c in map(chr, range(ord("a"), ord("z") + 1)) if c not in "ailo"]
)
_GH_INDEX = {c: i for i, c in enumerate(GEOHASH_ALPHABET)}
GEOHASH_RE = re.compile(r"^[0-9bcdefghjkmnpqrstuvwxyz]{1,12}$")


def geohash_decode(gh):
    """Return (lat, lon, (minlon, minlat, maxlon, maxlat)) or None."""
    gh = (gh or "").strip().lower()
    if not GEOHASH_RE.match(gh):
        return None
    lat_lo, lat_hi = -90.0, 90.0
    lon_lo, lon_hi = -180.0, 180.0
    even = True
    for ch in gh:
        val = _GH_INDEX[ch]
        for mask in (16, 8, 4, 2, 1):
            if even:
                mid = (lon_lo + lon_hi) / 2.0
                if val & mask:
                    lon_lo = mid
                else:
                    lon_hi = mid
            else:
                mid = (lat_lo + lat_hi) / 2.0
                if val & mask:
                    lat_lo = mid
                else:
                    lat_hi = mid
            even = not even
    return (
        (lat_lo + lat_hi) / 2.0,
        (lon_lo + lon_hi) / 2.0,
        (lon_lo, lat_lo, lon_hi, lat_hi),
    )


def geohash_encode(lat, lon, precision=9):
    lat_lo, lat_hi = -90.0, 90.0
    lon_lo, lon_hi = -180.0, 180.0
    out = []
    bit = 0
    ch = 0
    even = True
    while len(out) < precision:
        if even:
            mid = (lon_lo + lon_hi) / 2.0
            if lon >= mid:
                ch |= 16 >> bit
                lon_lo = mid
            else:
                lon_hi = mid
        else:
            mid = (lat_lo + lat_hi) / 2.0
            if lat >= mid:
                ch |= 16 >> bit
                lat_lo = mid
            else:
                lat_hi = mid
        even = not even
        if bit < 4:
            bit += 1
        else:
            out.append(GEOHASH_ALPHABET[ch])
            bit = 0
            ch = 0
    return "".join(out)
