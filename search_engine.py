# -*- coding: utf-8 -*-
"""
The omnibox brain.

Typing:  instant results (coordinates, links, layers, bookmarks, history)
         appear immediately; online suggestions stream in after a short pause.
Enter:   a clear answer (coordinates, Plus Code, exact feature) is opened at
         once; otherwise every geocoder is asked in parallel and the best
         ranked result is opened when they reply (or the timeout passes).
Ranking: provider score + text match + distance from the current map view,
         so "MG Road" finds the one on your screen first, as Google does.
"""

import math

from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal

from .core import olc
from .core.candidate import Candidate
from .core.coord_parser import parse
from .core.formats import haversine_km, fmt_dd
from .core.textutil import fold
from .core.url_parser import parse_url
from .compat import log_exception
from . import geo
from .local_index import search_bookmarks, history_candidates
from .providers import SearchContext, text_match_bonus

DIRECT_KINDS = ("coordinate", "grid", "pluscode", "link")


class SearchEngine(QObject):
    resultsChanged = pyqtSignal()
    busyChanged = pyqtSignal(bool)
    statusChanged = pyqtSignal(str)
    goRequested = pyqtSignal(object, bool)  # candidate, show_all
    notify = pyqtSignal(str, str)  # text, level (info|warning)

    def __init__(
        self, canvas, settings, http, providers, local_index, parent=None
    ):
        super(SearchEngine, self).__init__(parent)
        self.canvas = canvas
        self.settings = settings
        self.http = http
        self.providers = {p.key: p for p in providers}
        self.order = [p.key for p in providers]
        self.local = local_index
        self.text = ""
        self._online_text = ""
        self._qid = 0
        self._results = {}
        self._pending = {}
        self._errors = []
        self._ran = set()
        self._go = (
            None  # None or show_all flag while waiting for Enter results
        )
        self._ctx = None
        self._live_timer = QTimer(self)
        self._live_timer.setSingleShot(True)
        self._live_timer.timeout.connect(self._run_live)
        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.timeout.connect(self._finalize_go)

    # ------------------------------------------------------------ context

    def context(self):
        lat, lon, bbox, scale = geo.canvas_context(self.canvas)
        s = self.settings
        return SearchContext(
            lat,
            lon,
            bbox,
            scale,
            s.get("search/language"),
            s.countries(),
            s.get("search/bias"),
            s.get("search/in_view_only"),
            s.get("search/max_results"),
            s.get("search/timeout_s"),
            s.get("map/point_scale"),
        )

    @property
    def results(self):
        return sorted(
            (c for c in self._results.values()), key=lambda c: -c.score
        )

    @property
    def busy(self):
        return bool(self._pending)

    # ------------------------------------------------------------ typing

    def cancel(self):
        self._live_timer.stop()
        self._deadline.stop()
        for h in list(self._pending.values()):
            if h is not None:
                try:
                    h.cancel()
                except Exception as exc:
                    log_exception("cancel request", exc)
        self._pending.clear()
        self._go = None
        self.busyChanged.emit(False)

    def set_text(self, text, schedule_online=True):
        self.cancel()
        self._qid += 1
        self.text = text.strip()
        self._online_text = self.text
        self._results = {}
        self._errors = []
        self._ran = set()
        self._ctx = self.context()
        t = self.text
        if not t:
            for c in history_candidates(self.settings, "", 10):
                self._add(c, adjust=False)
            self.statusChanged.emit("")
            self.resultsChanged.emit()
            return
        hits = parse(t)
        cands, specials = geo.hits_to_candidates(
            hits, self.canvas, self.settings.get("map/point_scale")
        )
        for h in specials:
            if h.kind == "query" and h.text:
                self._online_text = h.text
        for c in cands:
            self._add(c)
        self._rank_ambiguous(cands)
        direct = [c for c in cands if c.kind in DIRECT_KINDS]
        if not direct or specials:
            q = self._online_text
            for c in history_candidates(self.settings, q, 4):
                self._add(c)
            for c in search_bookmarks(q):
                self._add(c)
            self.local.ensure_started()
            for c in self.local.search(q, 8):
                self._add(c)
        if direct and not specials:
            self.statusChanged.emit(self._describe(direct[0]))
        elif hits and not cands and not specials:
            self.statusChanged.emit(
                "This looks like a coordinate but it is out of range"
            )
        else:
            self.statusChanged.emit("")
        self.resultsChanged.emit()
        if (
            schedule_online
            and (not direct or specials)
            and len(self._online_text) >= self.settings.get("search/min_chars")
        ):
            self._live_timer.start(self.settings.get("search/debounce_ms"))

    def _describe(self, c):
        return (
            "%s: %s" % (c.type_label or "Location", fmt_dd(c.lat, c.lon))
            if c.lat is not None
            else (c.type_label or "")
        )

    def _run_live(self):
        for key in self.order:
            p = self.providers[key]
            if p.live_enabled():
                self._start_provider(p, live=True)

    def _start_provider(self, p, live):
        tag = (p.key, live)
        if tag in self._ran or p.key in self._pending:
            return
        self._ran.add(tag)
        qid = self._qid

        def done(results, error, key=p.key):
            self._on_provider(qid, key, results, error)

        try:
            handle = p.search(self._online_text, self._ctx, live, done)
        except Exception as exc:
            self._errors.append("%s: %s" % (p.label, exc))
            return
        if handle is not None:
            self._pending[p.key] = handle
            self.busyChanged.emit(True)
            self.statusChanged.emit(
                "Searching %s"
                % ", ".join(self.providers[k].label for k in self._pending)
            )

    def _on_provider(self, qid, key, results, error):
        if qid != self._qid:
            return
        self._pending.pop(key, None)
        if error:
            self._errors.append(error)
        for c in results or []:
            self._add(c)
        if not self._pending:
            self.busyChanged.emit(False)
            n = len([c for c in self._results.values() if c.enabled])
            if self._errors and not n:
                self.statusChanged.emit(self._errors[-1])
            else:
                self.statusChanged.emit(
                    "%d result%s" % (n, "" if n == 1 else "s")
                    if n
                    else "No results yet, press Enter to search everywhere"
                )
        else:
            self.statusChanged.emit(
                "Searching %s"
                % ", ".join(self.providers[k].label for k in self._pending)
            )
        self.resultsChanged.emit()
        if self._go is not None and not self._pending:
            self._finalize_go()

    # ------------------------------------------------------------ ranking

    def _add(self, c, adjust=True):
        if adjust and c.lat is not None and self._ctx and self._ctx.has_center:
            km = haversine_km(
                self._ctx.center_lat, self._ctx.center_lon, c.lat, c.lon
            )
            c.distance_km = km if c.distance_km is None else c.distance_km
            if c.source not in ("parser",) and self._ctx.bias:
                inside = self._in_view(c.lat, c.lon)
                c.score += (3.0 if inside else 0.0) - min(
                    5.0, math.log10(1.0 + km) * 1.2
                )
            if c.source not in ("parser", "local", "bookmark"):
                c.score += text_match_bonus(self._online_text, c.title)
        elif (
            c.lat is not None
            and self._ctx
            and self._ctx.has_center
            and c.distance_km is None
        ):
            c.distance_km = haversine_km(
                self._ctx.center_lat, self._ctx.center_lon, c.lat, c.lon
            )
        old = self._results.get(c.key)
        if old is not None:
            if old.score >= c.score:
                self._merge_extra(old, c)
                return
            self._merge_extra(c, old)
        # near-duplicates from different geocoders
        if c.lat is not None and c.source not in ("parser",):
            ft = fold(c.title)
            for k, o in list(self._results.items()):
                if (
                    o.lat is None
                    or o.source in ("parser", "local")
                    or fold(o.title) != ft
                ):
                    continue
                if haversine_km(o.lat, o.lon, c.lat, c.lon) < 0.25:
                    if o.score >= c.score:
                        self._merge_extra(o, c)
                        return
                    self._merge_extra(c, o)
                    del self._results[k]
                    break
        self._results[c.key] = c

    @staticmethod
    def _merge_extra(keep, other):
        if not keep.geojson and other.geojson:
            keep.geojson = other.geojson
        if not keep.bbox and other.bbox and keep.kind == "area":
            keep.bbox = other.bbox
        if not keep.subtitle and other.subtitle:
            keep.subtitle = other.subtitle

    def _in_view(self, lat, lon):
        b = self._ctx.view_bbox if self._ctx else None
        return bool(b) and b[0] <= lon <= b[2] and b[1] <= lat <= b[3]

    def _rank_ambiguous(self, cands):
        """Several readings of the same numbers: prefer the one on/near the
        map.
        """
        group = [
            c
            for c in cands
            if c.lat is not None and c.kind in ("coordinate", "grid")
        ]
        if len(group) < 2 or not (self._ctx and self._ctx.has_center):
            return
        for c in group:
            inside = self._in_view(c.lat, c.lon)
            km = c.distance_km or haversine_km(
                self._ctx.center_lat, self._ctx.center_lon, c.lat, c.lon
            )
            c.score += (8.0 if inside else 0.0) - min(
                10.0, math.log10(1.0 + km) * 2.5
            )
        best = max(group, key=lambda c: c.score)
        for c in group:
            c.decisive = c is best
            if c is not best:
                c.note = "Other reading of the same numbers"

    # ------------------------------------------------------------ Enter

    def submit(self, text, show_all=False):
        t = text.strip()
        if not t:
            return
        if t != self.text:
            self.set_text(t, schedule_online=False)
        self._live_timer.stop()
        best = self.results[0] if self._results else None
        if (
            best is not None
            and best.decisive
            and not show_all
            and (best.kind in DIRECT_KINDS or best.score >= 95.0)
        ):
            self.goRequested.emit(best, False)
            return
        if best is not None and best.kind in DIRECT_KINDS and not show_all:
            self.goRequested.emit(best, False)
            return
        started = False
        for key in self.order:
            p = self.providers[key]
            if key == "overpass" or not p.enabled():
                continue
            if (key, False) in self._ran:
                continue
            before = len(self._pending)
            self._start_provider(p, live=False)
            started = started or len(self._pending) > before
        self._go = show_all
        if not self._pending:
            self._finalize_go()
        else:
            self._deadline.start(
                int((self.settings.get("search/timeout_s") + 1) * 1000)
            )

    def _finalize_go(self):
        if self._go is None:
            return
        online = [
            c
            for c in self._results.values()
            if c.source not in ("history", "favorite", "parser")
        ]
        ov = self.providers.get("overpass")
        if (
            len(online) < 2
            and ov
            and ov.enabled()
            and ("overpass", False) not in self._ran
            and self._ctx
            and self._ctx.view_bbox
        ):
            self._start_provider(ov, live=False)
            if self._pending:
                self._deadline.start(12000)
                return
        show_all = self._go
        self._go = None
        self._deadline.stop()
        res = [c for c in self.results if c.enabled]
        if not res:
            msg = "No places found for \u201c%s\u201d" % self.text
            if self._errors:
                msg += " (%s)" % self._errors[-1]
            self.notify.emit(msg, "warning")
            self.statusChanged.emit("No results")
            return
        self.goRequested.emit(res[0], show_all)

    # ------------------------------------------------------------ lazy results

    def resolve(self, cand, callback):
        """callback(candidate or None, error). Fetches coordinates for
        suggestions.
        """
        lazy = cand.lazy
        if not lazy:
            callback(cand, None)
            return
        ctx = self._ctx or self.context()
        typ = lazy.get("type")
        if typ == "shortlink":

            def got(ok, data, info):
                final = (info or {}).get("url") or ""
                hits = parse_url(final) or []
                if not hits and isinstance(data, str):
                    import re

                    m = re.search(
                        r"https://www\.google\.[a-z.]+/maps/[^\"'\s<>]+", data
                    )
                    if m:
                        hits = parse_url(m.group(0)) or []
                hits = [h for h in hits if h.kind == "url"]
                if not hits:
                    callback(None, "Could not read a location from this link")
                    return
                cs, _ = geo.hits_to_candidates(
                    hits, self.canvas, self.settings.get("map/point_scale")
                )
                callback(
                    cs[0] if cs else None,
                    None if cs else "No location in link",
                )

            self.http.get(
                lazy["url"],
                callback=got,
                timeout_s=ctx.timeout_s,
                want_json=False,
            )
            return
        if typ == "pluscode":
            provider = next(
                (
                    self.providers[k]
                    for k in ("google", "esri", "photon", "nominatim")
                    if self.providers[k].enabled()
                ),
                None,
            )
            if provider is None:
                callback(
                    None,
                    "Turn on a geocoder to resolve the Plus Code locality",
                )
                return

            def got_locality(results, error):
                if not results:
                    callback(
                        None, error or "Could not find %s" % lazy["locality"]
                    )
                    return

                def finish(loc, err):
                    if loc is None or loc.lat is None:
                        callback(
                            None, err or "Could not find %s" % lazy["locality"]
                        )
                        return
                    full = olc.recover_nearest(lazy["code"], loc.lat, loc.lon)
                    area = olc.decode(full)
                    callback(
                        Candidate(
                            "pluscode",
                            "%s %s" % (lazy["code"], lazy["locality"]),
                            "Plus Code %s" % full,
                            area.lat,
                            area.lng,
                            source="parser",
                            score=99.0,
                            bbox=(
                                area.lng_lo,
                                area.lat_lo,
                                area.lng_hi,
                                area.lat_hi,
                            ),
                            scale=self.settings.get("map/point_scale"),
                            type_label="Plus Code",
                            key="parser:pc:" + full,
                        ),
                        None,
                    )

                first = sorted(results, key=lambda c: -c.score)[0]
                if first.lazy:
                    provider.resolve(first, ctx, finish)
                else:
                    finish(first, None)

            provider.search(lazy["locality"], ctx, False, got_locality)
            return
        provider = self.providers.get(cand.source)
        if provider is None:
            callback(None, "Unknown provider")
            return
        provider.resolve(cand, ctx, callback)

    def reverse(self, lat, lon, callback):
        """Address for a point: first provider that answers wins."""
        ctx = self.context()
        chain = [
            self.providers[k]
            for k in ("google", "esri", "nominatim")
            if self.providers[k].enabled()
        ]

        def attempt(i, last_error=None):
            if i >= len(chain):
                callback(None, last_error or "No reverse geocoder enabled")
                return
            chain[i].reverse(
                lat,
                lon,
                ctx,
                lambda res, err: (
                    callback(res, None) if res else attempt(i + 1, err)
                ),
            )

        attempt(0)
