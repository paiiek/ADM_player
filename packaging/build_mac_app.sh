#!/usr/bin/env bash
# macOS .app 번들 빌드 (PyInstaller). 프로젝트 루트에서 실행.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

python3 -m pip install -q -e ".[gui,packaging]"

# 이전 빌드의 .app 안 파일이 읽기 전용이면 rm에서 "Permission denied"가 날 수 있음
chmod -R u+w "${ROOT}/build" "${ROOT}/dist" 2>/dev/null || true
rm -rf "${ROOT}/build" "${ROOT}/dist"

bash "${ROOT}/packaging/build_icon.sh"

ICON="${ROOT}/packaging/ADMPlayer.icns"
if [[ ! -f "$ICON" ]]; then
  echo "Missing $ICON after icon build" >&2
  exit 1
fi

python3 -m PyInstaller \
  --noconfirm \
  --clean \
  "${ROOT}/packaging/adm_player_mac.spec"

echo ""
echo "출력: ${ROOT}/dist/ADM Player.app"
echo "테스트: open \"${ROOT}/dist/ADM Player.app\""
