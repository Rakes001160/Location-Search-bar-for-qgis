# -*- coding: utf-8 -*-
"""
Location Search Bar for QGIS: one search box for places, addresses,
coordinates in any format, UTM/MGRS, Plus Codes, map links and layer values.
"""


def classFactory(iface):
    from .location_searchbar import LocationSearchBarPlugin

    return LocationSearchBarPlugin(iface)
