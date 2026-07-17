#!/usr/bin/env bash
# Install launchd agents for background sync + the always-on dashboard.
# Usage: bash scripts/install_launchd.sh [install|uninstall]
set -euo pipefail

ACTION="${1:-install}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${REPO_ROOT}/.venv/bin/python"
AGENTS_DIR="${HOME}/Library/LaunchAgents"
SYNC_LABEL="com.transcript-analyzer.sync"
WEB_LABEL="com.transcript-analyzer.dashboard"
SYNC_PLIST="${AGENTS_DIR}/${SYNC_LABEL}.plist"
WEB_PLIST="${AGENTS_DIR}/${WEB_LABEL}.plist"
# Earlier versions labelled the agents com.<user>.transcript-{sync,dashboard}.
# Derive those names so an existing install is torn down rather than left loaded
# alongside the new agents. On a machine that never ran the old version these
# match nothing and the unload is a silent no-op.
LEGACY_LABELS=("com.${USER}.transcript-sync" "com.${USER}.transcript-dashboard")
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
  for L in "${SYNC_LABEL}" "${WEB_LABEL}" "${LEGACY_LABELS[@]}"; do
    launchctl unload "${AGENTS_DIR}/${L}.plist" 2>/dev/null || true
    rm -f "${AGENTS_DIR}/${L}.plist"
  done
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

# Tear down agents from a previous install, including any under the legacy
# labels, so a re-install replaces them instead of running two copies.
for L in "${LEGACY_LABELS[@]}"; do
  if launchctl list 2>/dev/null | grep -q "${L}"; then
    echo "Removing legacy agent ${L}"
  fi
  launchctl unload "${AGENTS_DIR}/${L}.plist" 2>/dev/null || true
  rm -f "${AGENTS_DIR}/${L}.plist"
done
launchctl unload "${SYNC_PLIST}" 2>/dev/null || true
launchctl unload "${WEB_PLIST}" 2>/dev/null || true
launchctl load "${SYNC_PLIST}"
launchctl load "${WEB_PLIST}"

echo "Installed:"
echo "  sync      -> every ${INTERVAL}s   (${SYNC_PLIST})"
echo "  dashboard -> http://127.0.0.1:8787 (${WEB_PLIST})"
echo "Check:  launchctl list | grep transcript"
echo "Logs:   ${LOGS}/"
