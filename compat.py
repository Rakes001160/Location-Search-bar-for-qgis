# -*- coding: utf-8 -*-
"""Compatibility shims so one code base runs on QGIS 3.22+ (PyQt5) and QGIS 4
(PyQt6).
"""

from qgis.core import (
    Qgis,
    QgsFeatureRequest,
    QgsWkbTypes,
    QgsTask,
    QgsMessageLog,
)

try:
    from qgis.PyQt.QtWidgets import QAction  # noqa: F401  (PyQt5)
except ImportError:  # pragma: no cover - PyQt6 moved QAction to QtGui
    from qgis.PyQt.QtGui import QAction  # noqa: F401

try:
    from qgis.PyQt import sip  # noqa: F401
except ImportError:  # pragma: no cover
    import sip  # noqa: F401


def qenum(owner, enum_name, member):
    """Return owner.enum_name.member (scoped) or owner.member (old style)."""
    scope = getattr(owner, enum_name, None)
    if scope is not None and hasattr(scope, member):
        return getattr(scope, member)
    return getattr(owner, member)


def _geom_type(new, old):
    gt = getattr(Qgis, "GeometryType", None)
    if gt is not None and hasattr(gt, new):
        return getattr(gt, new)
    return getattr(QgsWkbTypes, old)


GEOM_POINT = _geom_type("Point", "PointGeometry")
GEOM_LINE = _geom_type("Line", "LineGeometry")
GEOM_POLYGON = _geom_type("Polygon", "PolygonGeometry")


def _msg(name):
    ml = getattr(Qgis, "MessageLevel", None)
    if ml is not None and hasattr(ml, name):
        return getattr(ml, name)
    return getattr(Qgis, name)


MSG_INFO = _msg("Info")
MSG_WARNING = _msg("Warning")
MSG_CRITICAL = _msg("Critical")
MSG_SUCCESS = _msg("Success")


def no_geometry_flag():
    """QgsFeatureRequest flag to skip geometries (scoped, Qt6 safe)."""
    flag = getattr(Qgis, "FeatureRequestFlag", None)
    if flag is not None and hasattr(flag, "NoGeometry"):
        return flag.NoGeometry
    return QgsFeatureRequest.Flag.NoGeometry


def task_can_cancel():
    """QgsTask flag that lets the user cancel a background task."""
    return QgsTask.Flag.CanCancel


def run_dialog(obj, *args):
    """Show a modal dialog or menu (QDialog.exec / QMenu.exec)."""
    return obj.exec(*args)


def enum_int(value):
    """int() of a Qt/QGIS enum on both PyQt5 (int-like) and PyQt6
    (enum.Enum).
    """
    try:
        return int(value)
    except TypeError:
        return int(getattr(value, "value", -1))


def is_deleted(obj):
    try:
        return obj is None or sip.isdeleted(obj)
    except Exception:  # pragma: no cover
        return obj is None


def log_exception(where, exc):
    """Record a non-fatal problem in the QGIS log (Location Search tab)."""
    QgsMessageLog.logMessage(
        "%s: %s" % (where, exc), "Location Search", MSG_INFO
    )
