#!/bin/sh
# Renders the app icon's raster versions from its one source,
# alisdr/web/static/icon.svg (edit that, then re-run this):
#   alisdr.png (512 px, Linux/README) and alisdr.ico (16-256 px, Windows .exe).
# Needs inkscape and ImageMagick.
set -eu
cd "$(dirname "$0")"
SVG=../../alisdr/web/static/icon.svg
TMP=$(mktemp -d)
for s in 16 24 32 48 64 128 256 512; do inkscape "$SVG" -w "$s" -h "$s" -o "$TMP/$s.png" >/dev/null 2>&1; done
cp "$TMP/512.png" alisdr.png
magick "$TMP/16.png" "$TMP/24.png" "$TMP/32.png" "$TMP/48.png" "$TMP/64.png" "$TMP/128.png" "$TMP/256.png" alisdr.ico
rm -r "$TMP"
echo "wrote packaging/icons/alisdr.png and alisdr.ico"
