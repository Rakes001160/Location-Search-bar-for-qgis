# -*- coding: utf-8 -*-
"""
Non-blocking HTTP for the search providers.

All requests go through QgsNetworkAccessManager, so QGIS proxy, SSL and
authentication settings apply (important on office networks). Requests run
in parallel, never block the UI thread and can be cancelled the moment the
user types another character.
"""

import json

try:
    from urllib.parse import urlencode, quote
except ImportError:  # pragma: no cover
    from urllib import urlencode, quote  # type: ignore

from qgis.PyQt.QtCore import QObject, QTimer, QUrl, QByteArray
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply
from qgis.core import QgsNetworkAccessManager, QgsMessageLog

from .compat import qenum, MSG_INFO, log_exception

LOG_TAG = "Location Search"
USER_AGENT = "QGIS-LocationSearchBar/3.1 (QGIS plugin)"

_NO_ERROR = qenum(QNetworkReply, "NetworkError", "NoError")
_STATUS_ATTR = qenum(QNetworkRequest, "Attribute", "HttpStatusCodeAttribute")
_CONTENT_TYPE = qenum(QNetworkRequest, "KnownHeaders", "ContentTypeHeader")
try:
    _REDIRECT_ATTR = qenum(
        QNetworkRequest, "Attribute", "RedirectPolicyAttribute"
    )
    _REDIRECT_POLICY = qenum(
        QNetworkRequest, "RedirectPolicy", "NoLessSafeRedirectPolicy"
    )
except AttributeError:  # pragma: no cover - very old Qt
    _REDIRECT_ATTR = _REDIRECT_POLICY = None


def build_url(base, params=None):
    if not params:
        return base
    clean = [
        (k, v if isinstance(v, str) else str(v))
        for k, v in params.items()
        if v is not None and v != ""
    ]
    sep = "&" if "?" in base else "?"
    return base + sep + urlencode(clean, quote_via=quote)


class RequestHandle(object):
    __slots__ = (
        "reply",
        "timer",
        "callback",
        "cancelled",
        "timed_out",
        "want_json",
        "url",
    )

    def __init__(self, url, callback, want_json):
        self.url = url
        self.callback = callback
        self.want_json = want_json
        self.reply = None
        self.timer = None
        self.cancelled = False
        self.timed_out = False

    def cancel(self):
        if self.cancelled:
            return
        self.cancelled = True
        if self.timer is not None:
            self.timer.stop()
        try:
            if self.reply is not None and self.reply.isRunning():
                self.reply.abort()
        except RuntimeError:
            pass


class HttpClient(QObject):
    """callback(ok, data, info) — data is parsed JSON (or text) when ok, else
    an
    error string.
    """

    def __init__(self, parent=None):
        super(HttpClient, self).__init__(parent)
        self._active = set()

    def _prepare(self, url, headers):
        req = QNetworkRequest(
            QUrl.fromEncoded(QByteArray(url.encode("ascii", "ignore")))
        )
        req.setRawHeader(b"User-Agent", USER_AGENT.encode("ascii"))
        req.setRawHeader(b"Accept", b"application/json, text/plain, */*")
        for k, v in (headers or {}).items():
            req.setRawHeader(k.encode("ascii"), str(v).encode("utf-8"))
        if _REDIRECT_ATTR is not None:
            req.setAttribute(_REDIRECT_ATTR, _REDIRECT_POLICY)
        return req

    def get(
        self,
        url,
        params=None,
        headers=None,
        callback=None,
        timeout_s=8,
        want_json=True,
    ):
        full = build_url(url, params)
        req = self._prepare(full, headers)
        handle = RequestHandle(full, callback, want_json)
        reply = QgsNetworkAccessManager.instance().get(req)
        return self._track(handle, reply, timeout_s)

    def post_json(self, url, body, headers=None, callback=None, timeout_s=8):
        req = self._prepare(url, headers)
        req.setHeader(_CONTENT_TYPE, "application/json")
        handle = RequestHandle(url, callback, True)
        data = QByteArray(json.dumps(body).encode("utf-8"))
        reply = QgsNetworkAccessManager.instance().post(req, data)
        return self._track(handle, reply, timeout_s)

    def post_form(
        self, url, fields, headers=None, callback=None, timeout_s=10
    ):
        req = self._prepare(url, headers)
        req.setHeader(_CONTENT_TYPE, "application/x-www-form-urlencoded")
        handle = RequestHandle(url, callback, True)
        data = QByteArray(urlencode(fields).encode("utf-8"))
        reply = QgsNetworkAccessManager.instance().post(req, data)
        return self._track(handle, reply, timeout_s)

    def _track(self, handle, reply, timeout_s):
        handle.reply = reply
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda h=handle: self._on_timeout(h))
        timer.start(int(max(1, timeout_s) * 1000))
        handle.timer = timer
        reply.finished.connect(lambda h=handle: self._on_finished(h))
        self._active.add(handle)
        return handle

    def _on_timeout(self, handle):
        if handle.cancelled:
            return
        handle.timed_out = True
        try:
            handle.reply.abort()
        except RuntimeError:
            pass

    def _on_finished(self, handle):
        self._active.discard(handle)
        reply = handle.reply
        if handle.timer is not None:
            handle.timer.stop()
            handle.timer.deleteLater()
            handle.timer = None
        try:
            if handle.cancelled or handle.callback is None:
                return
            status = reply.attribute(_STATUS_ATTR)
            raw = bytes(reply.readAll())
            final_url = reply.url().toString()
            info = {"status": status, "url": final_url}
            if handle.timed_out:
                handle.callback(False, "timed out", info)
                return
            error = reply.error()
            if error != _NO_ERROR and not raw:
                handle.callback(False, reply.errorString(), info)
                return
            text = raw.decode("utf-8", "replace")
            if not handle.want_json:
                handle.callback(error == _NO_ERROR, text, info)
                return
            try:
                data = json.loads(text) if text.strip() else None
            except ValueError:
                handle.callback(
                    False, "unexpected response (HTTP %s)" % status, info
                )
                return
            if error != _NO_ERROR or (status and int(status) >= 400):
                handle.callback(
                    False, _error_message(data) or reply.errorString(), info
                )
                return
            handle.callback(True, data, info)
        except Exception as exc:  # never let a provider bug escape into Qt
            QgsMessageLog.logMessage(
                "HTTP callback error: %s" % exc, LOG_TAG, MSG_INFO
            )
        finally:
            handle.reply = None
            try:
                reply.deleteLater()
            except RuntimeError as exc:
                log_exception("release reply", exc)

    def cancel_all(self):
        for h in list(self._active):
            h.cancel()
        self._active.clear()


def _error_message(data):
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return err.get("message") or err.get("status") or str(err)
        if isinstance(err, str):
            return err
        if data.get("error_message"):
            return data.get("error_message")
        if data.get("message"):
            return data.get("message")
    return None
