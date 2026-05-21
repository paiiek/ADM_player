#!/usr/bin/env bash
# LOGO_White.png → ADMPlayer.icns (프로젝트 루트에서 실행 권장)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOGO="${ROOT}/LOGO_White.png"
if [[ ! -f "$LOGO" ]]; then
  LOGO="${ROOT}/adm_player/resources/LOGO_White.png"
fi
if [[ ! -f "$LOGO" ]]; then
  echo "Logo not found: LOGO_White.png" >&2
  exit 1
fi
OUTDIR="${ROOT}/packaging/AppIcon.iconset"
ICNS="${ROOT}/packaging/ADMPlayer.icns"
rm -rf "$OUTDIR"
mkdir -p "$OUTDIR"
sips -z 16 16 "$LOGO" --out "$OUTDIR/icon_16x16.png" >/dev/null
sips -z 32 32 "$LOGO" --out "$OUTDIR/icon_16x16@2x.png" >/dev/null
sips -z 32 32 "$LOGO" --out "$OUTDIR/icon_32x32.png" >/dev/null
sips -z 64 64 "$LOGO" --out "$OUTDIR/icon_32x32@2x.png" >/dev/null
sips -z 128 128 "$LOGO" --out "$OUTDIR/icon_128x128.png" >/dev/null
sips -z 256 256 "$LOGO" --out "$OUTDIR/icon_128x128@2x.png" >/dev/null
sips -z 256 256 "$LOGO" --out "$OUTDIR/icon_256x256.png" >/dev/null
sips -z 512 512 "$LOGO" --out "$OUTDIR/icon_256x256@2x.png" >/dev/null
sips -z 512 512 "$LOGO" --out "$OUTDIR/icon_512x512.png" >/dev/null
sips -z 1024 1024 "$LOGO" --out "$OUTDIR/icon_512x512@2x.png" >/dev/null
mkdir -p "$(dirname "$ICNS")"
iconutil -c icns "$OUTDIR" -o "$ICNS"
echo "Wrote $ICNS"
