#!/usr/bin/env bash
# Install the local coding agent dispatcher on TR4.
#
#   1. venv + editable install (already created: .venv)
#   2. ~/.config/local-dispatcher/config.toml (from config.example.toml,
#      only if not present — never overwrites an existing config)
#   3. systemd user unit + daemon-reload
#   4. optional: start the daemon, register the MCP forwarder
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
# cline launcher for openai-compatible worker backends (seeds the provider
# baseUrl into each run's isolated --data-dir; see bin/ccline):
if [ -w /usr/local/bin ]; then
    cp "$REPO/bin/ccline" /usr/local/bin/ccline
    chmod +x /usr/local/bin/ccline
else
    echo "    note: /usr/local/bin not writable — install the launcher manually:"
    echo "          sudo cp $REPO/bin/ccline /usr/local/bin/ccline"
fi

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
# sed, not head: sed drains its input, so a running systemctl cannot die of
# SIGPIPE and trip pipefail into the "not started" branch below.
systemctl --user status local-dispatcher --no-pager 2>/dev/null | sed -n '1,5p' || \
    echo "    daemon not started yet:  systemctl --user start local-dispatcher"

cat <<'EOF'

Next:
  systemctl --user start local-dispatcher
  ladd status
  ladd project add <id> <repo>      # or use config [[projects]]
  ladd mode <project> team          # only when a worker backend is free
  ladd submit "objective" <base> cc-worker --scope <path>

MCP forwarder for the primary session (user scope):
  claude mcp add dispatcher -- $VENV/bin/dispatcher-mcp

Docs: docs/setup.md  docs/recovery.md  docs/troubleshooting.md
      docs/commands.md  docs/cline-status.md  docs/mycelium-notes.md
EOF
