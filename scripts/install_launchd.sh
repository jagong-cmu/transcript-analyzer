#!/usr/bin/env bash
# Install launchd agents for background sync + the always-on dashboard.
# Usage: bash scripts/install_launchd.sh [install|uninstall]
set -euo pipefail

ACTION="${1:-install}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${REPO_ROOT}/.venv/bin/python"
AGENTS_DIR="${HOME}/Library/LaunchAgents"
SYNC_LABEL="com.jonathang.transcript-sync"
WEB_LABEL="com.jonathang.transcript-dashboard"
SYNC_PLIST="${AGENTS_DIR}/${SYNC_LABEL}.plist"
WEB_PLIST="${AGENTS_DIR}/${WEB_LABEL}.plist"
LOGS="${REPO_ROOT}/data/logs"

if [ ! -x "${PY}" ]; then
  echo "ERROR: venv python not found at ${PY}. Create it first:"
  echo "  python3 -m venv .venv && ./.venv/bin/pip install -e ."
  exit 1
fi

# Read sync interval from config.toml (fallback 1200s).
INTERVAL=$("${PY}" - <<'PYEOF'
from transcript_analyzer.config import load_config
print(load_config().sync.interval_seconds)
PYEOF
)

mkdir -p "${AGENTS_DIR}" "${LOGS}"

uninstall() {
  for L in "${SYNC_LABEL}" "${WEB_LABEL}"; do
    launchctl unload "${AGENTS_DIR}/${L}.plist" 2>/dev/null || true
  done
  rm -f "${SYNC_PLIST}" "${WEB_PLIST}"
  echo "Uninstalled launchd agents."
}

if [ "${ACTION}" = "uninstall" ]; then
  uninstall
  exit 0
fi

cat > "${SYNC_PLIST}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${SYNC_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PY}</string>
    <string>${REPO_ROOT}/scripts/run_sync.py</string>
  </array>
  <key>WorkingDirectory</key><string>${REPO_ROOT}</string>
  <key>StartInterval</key><integer>${INTERVAL}</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>${LOGS}/sync.out.log</string>
  <key>StandardErrorPath</key><string>${LOGS}/sync.err.log</string>
</dict>
</plist>
PLIST

cat > "${WEB_PLIST}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${WEB_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PY}</string>
    <string>-m</string>
    <string>transcript_analyzer.web.app</string>
  </array>
  <key>WorkingDirectory</key><string>${REPO_ROOT}</string>
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>${LOGS}/web.out.log</string>
  <key>StandardErrorPath</key><string>${LOGS}/web.err.log</string>
</dict>
</plist>
PLIST

launchctl unload "${SYNC_PLIST}" 2>/dev/null || true
launchctl unload "${WEB_PLIST}" 2>/dev/null || true
launchctl load "${SYNC_PLIST}"
launchctl load "${WEB_PLIST}"

echo "Installed:"
echo "  sync      -> every ${INTERVAL}s   (${SYNC_PLIST})"
echo "  dashboard -> http://127.0.0.1:8787 (${WEB_PLIST})"
echo "Check:  launchctl list | grep transcript"
echo "Logs:   ${LOGS}/"
