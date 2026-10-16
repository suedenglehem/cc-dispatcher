#!/usr/bin/env bash
# Install the local coding agent dispatcher on TR4.
#
#   1. venv + editable install (already created: .venv)
#   2. ~/.config/local-dispatcher/config.toml (from config.example.toml,
#      only if not present — never overwrites an existing config)
#   3. systemd user unit + daemon-reload
#   4. optional: start the daemon, register the MCP forwarder (Phase 2)
#
# Safe to re-run. Never touches ~/.cline, ~/.mycelium, or running sessions.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$REPO/.venv"
CFG_DIR="$HOME/.config/local-dispatcher"
CFG="$CFG_DIR/config.toml"
UNIT_SRC="$REPO/systemd/local-dispatcher.service"
UNIT_DST="$HOME/.config/systemd/user/local-dispatcher.service"

echo "==> [1/4] venv + package"
if [ ! -x "$VENV/bin/python" ]; then
    python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install -q -e "$REPO"

echo "==> [2/4] config"
mkdir -p "$CFG_DIR"
if [ -f "$CFG" ]; then
    echo "    $CFG exists — leaving it untouched"
else
    cp "$REPO/config.example.toml" "$CFG"
    echo "    wrote $CFG (review backends/projects before first start)"
fi

echo "==> [3/4] systemd user unit"
mkdir -p "$HOME/.config/systemd/user"
sed "s|^ExecStart=.*|ExecStart=$VENV/bin/local-dispatcher|" "$UNIT_SRC" > "$UNIT_DST"
systemctl --user daemon-reload
echo "    installed $UNIT_DST"
echo "    (survive logout:  loginctl enable-linger \$USER  — one-time)"

echo "==> [4/4] status"
systemctl --user status local-dispatcher --no-pager 2>/dev/null | head -5 || \
    echo "    daemon not started yet:  systemctl --user start local-dispatcher"

cat <<'EOF'

Next:
  systemctl --user start local-dispatcher
  ladd status
  ladd project add <id> <repo>      # or use config [[projects]]
  ladd mode <project> team          # only when a worker backend is free
  ladd submit "objective" <base> cc-worker --scope <path>

The MCP forwarder (dispatcher-mcp) lands in Phase 2:
  claude mcp add dispatcher -- /dd2/andrei/claude/dispatcher/.venv/bin/dispatcher-mcp
EOF
