# Location Search Bar 3.1 for QGIS

One search box for QGIS that works like Google Maps and Google Earth. It sits above the
Layers panel (Google Earth Pro layout). Press **Ctrl+L** to jump to it.

## Install
Plugins > Manage and Install Plugins > Install from ZIP > choose `location_searchbar_v3.1.2.zip`.
It replaces version 2.x in place and keeps your recent searches and saved places.

## What you can type
* **Places and addresses**: Sabarmati Ashram, MG Road Pune, petrol pump near Navrangpura.
  Suggestions appear while you type; results near the current map view rank first.
* **Coordinates in any style**: `23.0225, 72.5714`, `72.5714, 23.0225` (x, y order copied from QGIS is
  detected from the map position), `23.0225N 72.5714E`, `23°01'21"N 72°34'17"E`, `23 01 21 N 72 34 17 E`,
  `N 23° 01.350 E 072° 34.283`, `lat 23.02 lon 72.57`, Indian numerals, decimal commas.
* **Grids**: UTM `43Q 251102 2548074`, MGRS `43QBF5110248074`, `EPSG:32643 251102 2548074`,
  or bare numbers `251102 2548074` (tried in the project CRS, the map CRS and the UTM zone under the map).
* **Plus Codes**: `7JMJ2CXC+XX` or `2CXC+XX Ahmedabad`. **Geohash**: `gh:ts5dgpv10`.
* **WKT / GeoJSON points**: `POINT(72.57 23.02)`.
* **Links**: Google Maps (uses the exact place pin), Google Earth, OpenStreetMap, Bing, Apple Maps,
  Waze, `geo:` and `maps.app.goo.gl` short links.
* **Your data**: values from loaded vector layers (village names, survey/gat/plot numbers), spatial
  bookmarks, recent searches and saved places. Typos are tolerated.
* **Zoom hint**: add `1:2500` or `17z` after coordinates.

## On the map
When a place is found the map flies there and a Google Maps style pin drops in with a bounce,
sends out ripple rings and shows the name and address underneath in bold text with a white halo.
Areas get a red dashed outline, layer features are highlighted and flashed. Coordinates and pasted
map links are located automatically without pressing Enter, and moving through results with the
arrow keys previews each one on the map. All of this can be switched off in Settings > Map / Search.

## Keys
Up/Down choose a result, Enter opens the best match, Shift+Enter shows all results as numbered pins,
Esc closes the list. Right-click the map for **What's here?**, copy coordinates and Google Maps/Earth/Street View.

## Place card
Shows the result in every format (lat/lon, DMS, DDM, UTM, MGRS, Plus Code, map CRS, geohash) with copy
buttons, the address for coordinates, layer attributes for features, and buttons to open Google Maps,
Google Earth, Street View or OpenStreetMap, save the pin to a "Saved places" layer, star it, or bookmark the view.

## Sources (Settings > Sources)
| Source | When | Notes |
|---|---|---|
| Photon (OSM) | while typing | free |
| Esri World Geocoder | while typing and on Enter | free for searching; Esri's terms require a token if you store results |
| Nominatim (OSM) | on Enter only | its usage policy forbids type-ahead; throttled to 1 request/second; gives area outlines |
| OSM deep search (Overpass) | on Enter when little is found | searches names inside the visible area |
| Google Places (New) | optional | needs your own key with "Places API (New)" and "Geocoding API"; billed after the free allowance; Google's terms restrict showing its results on non-Google maps |

All requests go through the QGIS network manager, so proxy settings apply, they run in parallel,
are cancelled as you keep typing, and never freeze QGIS.

## Changes from 2.5
* Searches run in parallel instead of one after another, through QGIS proxy settings.
* Pins and labels are temporary map items (2.5 wrote annotations into the project file).
* The unofficial Google suggestion endpoint was removed; typo tolerance comes from Esri, Photon,
  Google Places (with a key) and the local fuzzy matcher.
* Zip packaged with standard paths (the 2.5 zip used Windows backslashes).

Compatible with QGIS 3.22 to 3.x and prepared for QGIS 4 (Qt6). Tested on QGIS 3.34.

## Licence
GNU General Public License v2 or later (see LICENSE). Author: RAKESH SOLANKI.
