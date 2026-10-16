"""dispatcher-mcp — stdio MCP forwarder over the daemon socket (PRD §8).

Phase 1 stub: the socket API already exists; the 8 FastMCP tools land in
Phase 2 (plan: mcp_adapter.py, `claude mcp add dispatcher`).
"""

from __future__ import annotations

import sys


def main() -> int:
    print("dispatcher-mcp: not implemented yet (Phase 2). "
          "Use the `ladd` CLI or the socket API for now.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
