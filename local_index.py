# -*- coding: utf-8 -*-
"""
Local search: attribute values of loaded vector layers, spatial bookmarks,
recent searches and favourites.

Layer values are read in a background QgsTask (feature sources are thread
safe), folded (case and accent insensitive) and kept in an in-memory word
index, so typing "gat 245" or "sabar" answers instantly without touching
the data provider again.
"""

import bisect
import difflib
import re
import time

from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal
from qgis.core import (
    QgsApplication,
    QgsFeatureRequest,
    QgsProject,
    QgsTask,
    QgsVectorLayer,
    QgsVectorLayerFeatureSource,
    QgsMessageLog,
)

from .compat import no_geometry_flag, task_can_cancel, enum_int, MSG_INFO
from .core.candidate import Candidate
from .core.textutil import fold
from . import geo

ID_FIELD_RE = re.compile(
    r"(^|_|\b)(id|no|num|number|code|plot|survey|sr|gat|khasra|cts|ward|zone|"
    r"pin|block|sector|parcel|kh|fid|uid|name)($|_|\b)",
    re.I,
)
TEXT_TYPES = {10}  # QVariant::String / QMetaType::QString
INT_TYPES = {2, 3, 4, 5}  # Int, UInt, LongLong, ULongLong
SKIP_TYPES = {
    12,
    7,
    8,
    9,
    11,
}  # ByteArray, Char? map, list, stringlist (rarely useful)
OWN_LAYER_PROP = "location_searchbar/places"


def pick_fields(layer, mode):
    fields = []
    for idx, f in enumerate(layer.fields()):
        try:
            t = enum_int(f.type())
        except Exception:
            t = -1
        name = f.name()
        if mode == "all":
            if t not in SKIP_TYPES:
                fields.append((idx, name))
        elif t in TEXT_TYPES or (f.typeName() or "").lower() in (
            "text",
            "string",
            "varchar",
            "char",
        ):
            fields.append((idx, name))
        elif mode == "smart" and t in INT_TYPES and ID_FIELD_RE.search(name):
            fields.append((idx, name))
    return fields[:30]


class IndexTask(QgsTask):

    def __init__(self, jobs):
        super(IndexTask, self).__init__(
            "Location Search: indexing layers", task_can_cancel()
        )
        self.jobs = jobs
        self.result = {}
        self.error = None

    def run(self):
        try:
            total = max(1, len(self.jobs))
            for n, job in enumerate(self.jobs):
                if self.isCanceled():
                    return False
                req = QgsFeatureRequest()
                req.setSubsetOfAttributes([i for i, _ in job["fields"]])
                if not job["with_geom"]:
                    req.setFlags(no_geometry_flag())
                req.setLimit(job["limit"])
                entries = []
                for i, f in enumerate(job["source"].getFeatures(req)):
                    if i % 2000 == 0:
                        if self.isCanceled():
                            return False
                        self.setProgress(
                            100.0
                            * (n + min(1.0, i / float(job["limit"])))
                            / total
                        )
                    cx = cy = None
                    if job["with_geom"]:
                        g = f.geometry()
                        if g is not None and not g.isEmpty():
                            c = g.boundingBox().center()
                            cx, cy = c.x(), c.y()
                    attrs = f.attributes()
                    for idx, fname in job["fields"]:
                        v = attrs[idx] if idx < len(attrs) else None
                        if v is None:
                            continue
                        try:
                            if v.isNull():
                                continue
                        except AttributeError:
                            pass
                        s = str(v).strip()
                        if not s or len(s) > 160 or s == "NULL":
                            continue
                        key = fold(s)
                        if key:
                            entries.append((key, s, fname, f.id(), cx, cy))
                self.result[job["layer_id"]] = entries
            return True
        except Exception as exc:
            self.error = str(exc)
            return False


class LocalIndex(QObject):
    indexChanged = pyqtSignal()

    def __init__(self, settings, iface, parent=None):
        super(LocalIndex, self).__init__(parent)
        self.settings = settings
        self.iface = iface
        self._layers = {}  # layer_id -> {"name", "crs", "entries"}
        self._dirty = set()
        self._task = None
        self._values = []  # folded distinct values
        self._sorted = []  # [(value, vid)]
        self._refs = []  # vid -> [(layer_id, original, field, fid, cx, cy)]
        self._words = {}  # 2-char word prefix -> set(vid)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(1500)
        self._timer.timeout.connect(self._start)
        self._connected = set()
        prj = QgsProject.instance()
        prj.layersAdded.connect(self._on_layers_added)
        prj.layersRemoved.connect(self._on_layers_removed)
        prj.cleared.connect(self.reset)

    # ------------------------------------------------------------ lifecycle

    def unload(self):
        self._timer.stop()
        if self._task is not None:
            try:
                self._task.cancel()
            except RuntimeError:
                pass
        prj = QgsProject.instance()
        for sig, slot in (
            (prj.layersAdded, self._on_layers_added),
            (prj.layersRemoved, self._on_layers_removed),
            (prj.cleared, self.reset),
        ):
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        for lid in list(self._connected):
            lyr = prj.mapLayer(lid)
            if lyr is not None:
                try:
                    lyr.dataChanged.disconnect(self._on_layer_changed)
                except (TypeError, RuntimeError):
                    pass
        self._connected.clear()

    def reset(self):
        self._layers.clear()
        self._dirty.clear()
        self._connected.clear()
        self._rebuild()

    def rebuild_all(self):
        self._layers.clear()
        self._dirty = set(QgsProject.instance().mapLayers().keys())
        self._timer.start(10)

    def _eligible(self, layer):
        return (
            isinstance(layer, QgsVectorLayer)
            and layer.isValid()
            and layer.customProperty("location_searchbar/exclude", False)
            not in (True, "true")
        )

    def _on_layers_added(self, layers):
        for lyr in layers:
            if self._eligible(lyr):
                self._dirty.add(lyr.id())
                if lyr.id() not in self._connected:
                    try:
                        lyr.dataChanged.connect(self._on_layer_changed)
                        self._connected.add(lyr.id())
                    except (TypeError, AttributeError):
                        pass
        if self._dirty:
            self._timer.start()

    def _on_layers_removed(self, ids):
        changed = False
        for lid in ids:
            self._connected.discard(lid)
            self._dirty.discard(lid)
            if self._layers.pop(lid, None) is not None:
                changed = True
        if changed:
            self._rebuild()

    def _on_layer_changed(self):
        lyr = self.sender()
        if lyr is not None:
            self._dirty.add(lyr.id())
            self._timer.start(3000)

    def ensure_started(self):
        if not self._layers and not self._dirty and not self._task:
            self.rebuild_all()

    def _start(self):
        if not self.settings.get("search/local_layers"):
            return
        if self._task is not None:
            self._timer.start(1000)
            return
        prj = QgsProject.instance()
        mode = self.settings.get("search/local_fields")
        limit = max(1000, self.settings.get("search/local_max_features"))
        jobs = []
        for lid in list(self._dirty):
            lyr = prj.mapLayer(lid)
            self._dirty.discard(lid)
            if not self._eligible(lyr):
                continue
            if lid not in self._connected:
                try:
                    lyr.dataChanged.connect(self._on_layer_changed)
                    self._connected.add(lid)
                except (TypeError, AttributeError):
                    pass
            fields = pick_fields(lyr, mode)
            if not fields:
                continue
            count = lyr.featureCount()
            jobs.append(
                {
                    "layer_id": lid,
                    "name": lyr.name(),
                    "crs": lyr.crs(),
                    "fields": fields,
                    "source": QgsVectorLayerFeatureSource(lyr),
                    "limit": limit,
                    "with_geom": lyr.isSpatial() and 0 <= count <= 150000,
                }
            )
        if not jobs:
            return
        meta = {j["layer_id"]: (j["name"], j["crs"]) for j in jobs}
        task = IndexTask(jobs)
        started = time.time()

        def finished():
            self._task = None
            if task.error:
                QgsMessageLog.logMessage(
                    "Layer index error: %s" % task.error,
                    "Location Search",
                    MSG_INFO,
                )
            for lid, entries in task.result.items():
                name, c = meta[lid]
                self._layers[lid] = {
                    "name": name,
                    "crs": c,
                    "entries": entries,
                }
            self._rebuild()
            QgsMessageLog.logMessage(
                "Indexed %d layer(s) in %.1f s"
                % (len(task.result), time.time() - started),
                "Location Search",
                MSG_INFO,
            )
            if self._dirty:
                self._timer.start(500)

        task.taskCompleted.connect(finished)
        task.taskTerminated.connect(finished)
        self._task = task
        QgsApplication.taskManager().addTask(task)

    def _rebuild(self):
        values, refs, lookup = [], [], {}
        for lid, info in self._layers.items():
            for key, orig, fname, fid, cx, cy in info["entries"]:
                vid = lookup.get(key)
                if vid is None:
                    vid = len(values)
                    lookup[key] = vid
                    values.append(key)
                    refs.append([])
                if len(refs[vid]) < 25:
                    refs[vid].append((lid, orig, fname, fid, cx, cy))
        words = {}
        for vid, v in enumerate(values):
            for w in set(v.split()):
                words.setdefault(w[:2], set()).add(vid)
        self._values, self._refs, self._words = values, refs, words
        self._sorted = sorted((v, i) for i, v in enumerate(values))
        self.indexChanged.emit()

    @property
    def size(self):
        return len(self._values)

    # ------------------------------------------------------------ search

    def search(self, query, limit=8):
        if not self.settings.get("search/local_layers") or not self._values:
            return []
        q = fold(query)
        if len(q) < 2:
            return []
        tokens = q.split()
        scored = {}

        def add(vid, score):
            if scored.get(vid, 0) < score:
                scored[vid] = score

        # exact / prefix over sorted values
        i = bisect.bisect_left(self._sorted, (q, -1))
        while (
            i < len(self._sorted)
            and self._sorted[i][0].startswith(q)
            and len(scored) < 200
        ):
            v, vid = self._sorted[i]
            add(
                vid,
                100.0 if v == q else 92.0 - min(6.0, (len(v) - len(q)) * 0.2),
            )
            i += 1
        # every token is the start of a word (any order)
        sets = [self._words.get(t[:2], set()) for t in tokens if len(t) >= 2]
        if sets:
            cands = min(sets, key=len)
            for vid in list(cands)[:20000]:
                v = self._values[vid]
                vw = v.split()
                if all(any(w.startswith(t) for w in vw) for t in tokens):
                    add(vid, 86.0)
                elif q in v:
                    add(vid, 80.0)
        # substring scan with a time budget (e.g. "bad" in "ahmedabad")
        if len(scored) < limit and len(q) >= 3:
            t0 = time.time()
            for vid, v in enumerate(self._values):
                if q in v or all(t in v for t in tokens):
                    add(vid, 76.0 if q in v else 72.0)
                if vid % 4000 == 0 and time.time() - t0 > 0.035:
                    break
        # typo tolerance
        if not scored and len(q) >= 4:
            pool = set()
            for key in (q[:2], q[1:3], q[2:4]):
                pool |= self._words.get(key, set())
            pool = list(pool)[:6000]
            words = [self._values[v] for v in pool]
            for m in difflib.get_close_matches(q, words, n=limit, cutoff=0.78):
                vid = pool[words.index(m)]
                add(
                    vid,
                    60.0 + 10.0 * difflib.SequenceMatcher(None, q, m).ratio(),
                )
        if not scored:
            return []
        return self._to_candidates(
            sorted(scored.items(), key=lambda kv: -kv[1])[:limit * 2],
            query,
            limit,
        )

    def _to_candidates(self, ranked, query, limit):
        prj = QgsProject.instance()
        root = prj.layerTreeRoot()
        active = self.iface.activeLayer() if self.iface else None
        visible_only = self.settings.get("search/visible_layers_only")
        out = []
        for vid, score in ranked:
            for lid, orig, fname, fid, cx, cy in self._refs[vid][:3]:
                info = self._layers.get(lid)
                lyr = prj.mapLayer(lid)
                if info is None or lyr is None:
                    continue
                node = root.findLayer(lid) if root else None
                visible = bool(node and node.isVisible())
                if visible_only and not visible:
                    continue
                s = (
                    score
                    + (3.0 if visible else 0.0)
                    + (
                        3.0
                        if active is not None and active.id() == lid
                        else 0.0
                    )
                )
                lat = lon = None
                if cx is not None:
                    ll = geo.to_wgs84(cx, cy, info["crs"])
                    if ll:
                        lat, lon = ll
                out.append(
                    Candidate(
                        "feature",
                        orig,
                        "in %s (%s)" % (info["name"], fname),
                        lat,
                        lon,
                        source="local",
                        score=s,
                        layer_id=lid,
                        fid=fid,
                        field=fname,
                        decisive=score >= 100.0,
                        type_label="",
                        key="local:%s:%s" % (lid, fid),
                        query=query,
                    )
                )
                if len(out) >= limit:
                    return out
        return out


def search_bookmarks(query, limit=5):
    q = fold(query)
    if len(q) < 2:
        return []
    managers = []
    try:
        managers.append(QgsProject.instance().bookmarkManager())
        managers.append(QgsApplication.bookmarkManager())
    except AttributeError:
        return []
    out = []
    for mgr in managers:
        for bm in mgr.bookmarks():
            name = fold(bm.name())
            if not name:
                continue
            if name == q:
                score = 92.0
            elif name.startswith(q):
                score = 87.0
            elif q in name or all(t in name for t in q.split()):
                score = 77.0
            else:
                continue
            ext = bm.extent()
            center = ext.center()
            ll = geo.to_wgs84(center.x(), center.y(), ext.crs())
            out.append(
                Candidate(
                    "bookmark",
                    bm.name(),
                    "Spatial bookmark%s"
                    % (", " + bm.group() if bm.group() else ""),
                    ll[0] if ll else None,
                    ll[1] if ll else None,
                    source="bookmark",
                    score=score,
                    extent=(ext, ext.crs().authid()),
                    decisive=score >= 92.0,
                    key="bm:%s" % bm.id(),
                    type_label="Bookmark",
                )
            )
    return sorted(out, key=lambda c: -c.score)[:limit]


def history_candidates(settings, query="", limit=8):
    q = fold(query)
    out = []
    for kind, items, base in (
        ("favorite", settings.favorites(), 90.0),
        ("history", settings.history(), 84.0),
    ):
        for i, h in enumerate(items):
            name = h.get("name") or h.get("query") or ""
            if q:
                fn = fold(name)
                fq = fold(h.get("query") or "")
                if fn == q or fq == q:
                    score = base + 4
                elif fn.startswith(q) or fq.startswith(q):
                    score = base
                elif q in fn:
                    score = base - 8
                else:
                    continue
            else:
                score = base - i * 0.5
            bbox = h.get("bbox")
            out.append(
                Candidate(
                    kind,
                    name,
                    h.get("subtitle")
                    or (
                        "Saved place"
                        if kind == "favorite"
                        else "Recent search"
                    ),
                    h["lat"],
                    h["lon"],
                    source=kind,
                    score=score,
                    bbox=tuple(bbox) if bbox else None,
                    scale=h.get("scale"),
                    type_label=h.get("type") or "",
                    decisive=bool(q) and score >= base,
                    key="%s:%s:%.6f:%.6f" % (kind, name, h["lat"], h["lon"]),
                )
            )
    seen, uniq = set(), []
    for c in out:
        k = (c.title, round(c.lat, 6), round(c.lon, 6))
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq[:limit]
