"""`ladd` — admin CLI. Equivalent operations to the MCP tools (PRD §8) plus
backend/project management the primary session should not do itself.

All operations go through the Unix-socket API; connection refused is rendered
as an actionable "daemon not running" message by ApiClient.
"""

from __future__ import annotations

import argparse
import json
import sys

from .api import ApiClient
from .config import DEFAULT_CONFIG_PATH, Paths, load_config
from .models import ApiError, ErrorCodes
from .salad import SALAD_KEYFILE_DEFAULT, find_salad_backend


def _client(args) -> ApiClient:
    # The socket path comes from the same config resolution the daemon uses.
    cfg = load_config(getattr(args, "config", None))
    if getattr(args, "socket", None):
        paths = Paths(root=cfg.paths.root, worktree_root=cfg.paths.worktree_root,
                      socket_path=args.socket, lock_path=cfg.paths.lock_path)
    else:
        paths = cfg.paths
    return ApiClient(paths.socket_path)


def _out(args, obj) -> None:
    if getattr(args, "json", False):
        print(json.dumps(obj, indent=2, sort_keys=False))
    else:
        _pretty(obj)


def _pretty(obj) -> None:
    """Compact human-readable rendering of the common result shapes."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                print(f"{k}:")
                for row in v:
                    print("  " + _row(row))
            elif isinstance(v, dict):
                print(f"{k}:")
                for kk, vv in v.items():
                    print(f"  {kk}: {_scalar(vv)}")
            else:
                print(f"{k}: {_scalar(v)}")
    else:
        print(obj)


def _scalar(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, (dict, list)):
        s = json.dumps(v)
        return s if len(s) <= 200 else s[:200] + " …"
    return str(v)


def _row(row: dict) -> str:
    keep = ("id", "status", "state", "objective", "model", "backend_id",
            "profile_id", "failure_class", "event", "endpoint", "health",
            "detail", "code", "message")
    parts = [f"{k}={_scalar(row[k])}" for k in keep
             if k in row and row[k] is not None]
    if "ts" in row:
        parts.insert(0, f"[{row['ts']}]")
    if not parts and row:
        parts = [f"{k}={_scalar(v)}"
                 for k, v in list(row.items())[:4] if v is not None]
    return "  ".join(parts) or json.dumps(row)


# -- commands -------------------------------------------------------------------
def c_status(args) -> int:
    _out(args, _client(args).status(all_jobs=args.all_jobs))
    return 0


def c_set_mode(args) -> int:
    r = _client(args).set_mode(args.mode, project_id=args.project,
                               policy=args.policy)
    _out(args, r)
    return 0


def c_submit(args) -> int:
    params = dict(
        objective=args.objective,
        base_commit=args.base,
        allowed_scope=args.scope or [],
        acceptance_criteria=args.criterion or [],
        relevant_files=args.file or [],
        profile_id=args.profile,
    )
    if args.backend:
        params["backend_id"] = args.backend
    if args.contracts:
        params["contracts_text"] = args.contracts
    if args.decisions:
        params["decisions_text"] = args.decisions
    if args.exclusions:
        params["exclusions_text"] = args.exclusions
    if args.memory_query:
        params["memory_queries"] = args.memory_query
    if args.dependency:
        params["dependencies"] = args.dependency
    if args.idempotency_key:
        params["idempotency_key"] = args.idempotency_key
    if args.memory_required:
        params["memory_required"] = True
    if args.project:
        params["project_id"] = args.project
    r = _client(args).submit(params.pop("project_id", None), **params)
    if args.json:
        _out(args, r)
    else:
        print(f"job {r['job_id']} {'created' if r['created'] else 'already existed (idempotency)'}")
    return 0


def c_job(args) -> int:
    _out(args, _client(args).job(args.job_id, full=args.full))
    return 0


def c_cancel(args) -> int:
    _out(args, _client(args).cancel(args.job_id))
    return 0


def c_resume(args) -> int:
    _out(args, _client(args).resume(args.job_id))
    return 0


def c_integrate(args) -> int:
    _out(args, _client(args).integrate(args.job_id, args.commit))
    return 0


def c_promote(args) -> int:
    _out(args, _client(args).promote(args.job_id))
    return 0


def c_workers(args) -> int:
    _out(args, _client(args).workers(refresh=args.refresh))
    return 0


def c_reconcile(args) -> int:
    _out(args, _client(args).reconcile())
    return 0


def c_backend_list(args) -> int:
    _out(args, _client(args).backend_list())
    return 0


def c_backend_set(args) -> int:
    kw = {}
    if args.action in ("enable", "disable"):
        kw["enabled"] = args.action == "enable"
    if args.action in ("reserve", "release"):
        kw["reserved"] = args.action == "reserve"
        note = getattr(args, "note", "")
        if note:
            kw["note"] = note
    _out(args, _client(args).backend_set(args.backend_id, **kw))
    return 0


def c_backend_add_salad(args) -> int:
    r = _client(args).backend_add_salad(
        args.url, id=args.id, port=args.port, keyfile=args.keyfile,
        model=args.model, thinking=args.thinking, concurrency=args.concurrency,
        resource_group=args.group, notes=args.notes)
    _out(args, r)
    return 0


def c_backend_remove_salad(args) -> int:
    _out(args, _client(args).backend_remove_salad(args.backend_id))
    return 0


def c_salad_shorthand(args) -> int:
    """`ladd -a URL` / `ladd -d URL` — the primary salad UX."""
    c = _client(args)
    if args.add_salad:
        r = c.backend_add_salad(args.add_salad)
        if args.json:
            _out(args, r)
        else:
            note = ""
            if r.get("key_rejected"):
                note = (" — gateway reachable but REJECTED the Salad key "
                        "(check the keyfile)")
            elif r["health"] != "online":
                d = r.get("offline_detail")
                note = f" — currently {r['health']}" + (f": {d}" if d else "")
            print(f"added salad backend {r['backend']['id']}: proxy "
                  f"{r['proxy']}, health {r['health']}{note}")
            if not r.get("proxy_up"):
                print(f"  proxy not up: {r.get('proxy_detail')}")
        return 0
    rows = c.backend_list()["backends"]
    try:
        row = find_salad_backend(rows, args.delete_salad)
    except ValueError as e:
        raise ApiError(ErrorCodes.SALAD_URL_INVALID, str(e))
    if row is None:
        known = [b["id"] for b in rows if b.get("kind") == "salad"]
        raise ApiError(ErrorCodes.SALAD_NOT_FOUND,
                       f"no registered salad backend matches "
                       f"{args.delete_salad!r}",
                       salad_backends=known)
    c.backend_remove_salad(row["id"])
    if args.json:
        _out(args, {"removed": row["id"], "endpoint": row.get("endpoint")})
    else:
        print(f"removed salad backend {row['id']} ({row.get('endpoint')})")
    return 0


def c_backend_check(args) -> int:
    """Force a probe, then show that backend's row + worker state."""
    c = _client(args)
    w = c.workers(refresh=True)
    r = c.backend_list()
    row = next((b for b in r["backends"] if b["id"] == args.backend_id), None)
    if row is None:
        raise ApiError("backend_unknown", f"unknown backend {args.backend_id!r}")
    _out(args, {"backend": row, "workers": w["workers"]})
    return 0


def c_project_list(args) -> int:
    _out(args, _client(args).project_list())
    return 0


def c_project_add(args) -> int:
    _out(args, _client(args).project_add(
        args.id, args.repo,
        integration_path=args.integration_path or "",
        integration_ref=args.integration_ref or "main",
        mycelium_project=args.mycelium_project or ""))
    return 0


def c_events(args) -> int:
    _out(args, _client(args).events(
        subject_type=args.subject_type, subject_id=args.subject_id,
        limit=args.limit))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ladd",
        description="local agent dispatcher admin CLI")
    ap.add_argument("--config", default=None,
                    help=f"config path (default: {DEFAULT_CONFIG_PATH} if present)")
    ap.add_argument("--socket", default=None,
                    help="override the daemon socket path")
    ap.add_argument("--json", action="store_true", help="raw JSON output")
    ap.add_argument("-a", "--add-salad", metavar="URL", default=None,
                    help="register a salad.cloud gateway (ladd -a "
                         "https://<group>.salad.cloud); id derived from the "
                         "group host, proxy port auto-assigned")
    ap.add_argument("-d", "--delete-salad", metavar="URL", default=None,
                    help="unregister a salad backend by its gateway URL")
    # `cmd` is no longer required at the argparse level: `ladd -a URL` is a
    # valid command without a subcommand; main() enforces one-or-the-other.
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("status", help="projects, jobs, workers, reservations")
    p.add_argument("--all-jobs", action="store_true",
                   help="list terminal (succeeded/failed/cancelled) jobs in "
                        "full; default shows only active/blocked plus a count "
                        "and the last five finished. This explicit full dump "
                        "is exempt from the 8k response cap (the bounded "
                        "default always stays under it)")
    p.set_defaults(fn=c_status)

    p = sub.add_parser("mode", help="switch a project's mode")
    p.add_argument("project", nargs="?", default=None)
    p.add_argument("mode", choices=["single", "team"])
    p.add_argument("--policy", choices=["drain", "cancel"], default=None)
    p.set_defaults(fn=c_set_mode)

    p = sub.add_parser("submit", help="submit a job (team mode required)")
    p.add_argument("objective")
    p.add_argument("base", help="base commit sha (7-64 hex)")
    p.add_argument("profile", help="worker profile id")
    p.add_argument("--scope", action="append",
                   help="allowed path prefix (repeatable)")
    p.add_argument("--criterion", action="append",
                   help="acceptance criterion (repeatable)")
    p.add_argument("--file", action="append",
                   help="relevant file (repeatable)")
    p.add_argument("--backend", default=None)
    p.add_argument("--contracts", default=None)
    p.add_argument("--decisions", default=None)
    p.add_argument("--exclusions", default=None)
    p.add_argument("--memory-query", action="append")
    p.add_argument("--dependency", action="append")
    p.add_argument("--idempotency-key", default=None)
    p.add_argument("--memory-required", action="store_true")
    p.add_argument("--project", default=None)
    p.set_defaults(fn=c_submit)

    p = sub.add_parser("job", help="job view")
    p.add_argument("job_id")
    p.add_argument("--full", action="store_true", help="include result JSON")
    p.set_defaults(fn=c_job)

    p = sub.add_parser("cancel", help="cancel a job")
    p.add_argument("job_id")
    p.set_defaults(fn=c_cancel)

    p = sub.add_parser("resume", help="explicitly resume a paused job")
    p.add_argument("job_id")
    p.set_defaults(fn=c_resume)

    p = sub.add_parser("integrate",
                       help="record that the primary merged the worker's work")
    p.add_argument("job_id")
    p.add_argument("--commit", required=True)
    p.set_defaults(fn=c_integrate)

    p = sub.add_parser("promote",
                       help="save the [decision] memory for an accepted job")
    p.add_argument("job_id")
    p.set_defaults(fn=c_promote)

    p = sub.add_parser("workers", help="worker backend availability")
    p.add_argument("--refresh", action="store_true", help="force probes")
    p.set_defaults(fn=c_workers)

    p = sub.add_parser("reconcile", help="release dead quarantined reservations")
    p.set_defaults(fn=c_reconcile)

    p = sub.add_parser("backend", help="backend management")
    bsub = p.add_subparsers(dest="bcmd", required=True)
    b = bsub.add_parser("list")
    b.set_defaults(fn=c_backend_list)
    for name in ("enable", "disable", "reserve", "release"):
        b = bsub.add_parser(name)
        b.add_argument("backend_id")
        if name == "reserve":
            b.add_argument("--note", default="")
        b.set_defaults(fn=c_backend_set, action=name)
    b = bsub.add_parser("check", help="force probe and show backend row")
    b.add_argument("backend_id")
    b.set_defaults(fn=c_backend_check)
    b = bsub.add_parser("add-salad",
                        help="register a salad.cloud gateway (full options; "
                             "shorthand: ladd -a URL)")
    b.add_argument("url")
    b.add_argument("--id", default=None,
                   help="backend id (default: first dot-label of the host)")
    b.add_argument("--port", type=int, default=None,
                   help="local proxy port (default: first free 18100-18199)")
    b.add_argument("--keyfile", default="",
                   help=f"Salad API key file (default: {SALAD_KEYFILE_DEFAULT})")
    b.add_argument("--model", default="qwen",
                   help="served model alias the proxy rewrites requests to")
    b.add_argument("--thinking", action="store_true",
                   help="request reasoning effort upstream")
    b.add_argument("--concurrency", type=int, default=1)
    b.add_argument("--group", default="", help="resource group id")
    b.add_argument("--notes", default="")
    b.set_defaults(fn=c_backend_add_salad)
    b = bsub.add_parser("remove-salad", help="unregister a salad backend")
    b.add_argument("backend_id")
    b.set_defaults(fn=c_backend_remove_salad)

    p = sub.add_parser("project", help="project management")
    psub = p.add_subparsers(dest="pcmd", required=True)
    b = psub.add_parser("list")
    b.set_defaults(fn=c_project_list)
    b = psub.add_parser("add")
    b.add_argument("id")
    b.add_argument("repo")
    b.add_argument("--integration-path", default=None)
    b.add_argument("--integration-ref", default=None)
    b.add_argument("--mycelium-project", default=None)
    b.set_defaults(fn=c_project_add)

    p = sub.add_parser("events", help="recent events (audit trail)")
    p.add_argument("--subject-type", default=None)
    p.add_argument("--subject-id", default=None)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(fn=c_events)
    return ap


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args, extras = parser.parse_known_args(argv)
    try:
        if args.add_salad or args.delete_salad:
            if extras:
                parser.error(f"unexpected extra arguments: {extras}")
            return c_salad_shorthand(args)
        if args.cmd is None:
            parser.error("a subcommand is required (or -a URL / -d URL)")
        # strict re-parse so subcommands still reject unrecognized arguments
        args = parser.parse_args(argv)
        return args.fn(args)
    except ApiError as e:
        print(f"error: {e.code}: {e.message}", file=sys.stderr)
        if e.extra:
            print(f"  detail: {json.dumps(e.extra)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
