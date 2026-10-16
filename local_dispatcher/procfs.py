"""/proc helpers: process start identity, marker scan, group liveness.

PID alone is not a reliable identity after restart (PID reuse). We record
(pid, pgid, starttime) at spawn — starttime is /proc/<pid>/stat field 22 —
and additionally tag worker processes with marker env vars
(DISPATCHER=1, DISPATCHER_JOB_ID, DISPATCHER_ATTEMPT_ID) so a restarted
daemon can discover every live worker regardless of DB state.
"""

from __future__ import annotations

import os
import signal


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def starttime(pid: int) -> int | None:
    """Field 22 of /proc/<pid>/stat (clock ticks since boot). None if unreadable."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read().decode("ascii", "replace")
    except OSError:
        return None
    # The comm field (2) is parenthesized and may contain spaces/parens.
    rpar = data.rfind(")")
    fields = data[rpar + 2:].split()
    # field 3 = state → index 0; starttime is field 22 → index 19
    if len(fields) >= 19:
        try:
            return int(fields[19])
        except ValueError:
            return None
    return None


def read_environ(pid: int) -> dict[str, str] | None:
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            raw = f.read()
    except OSError:
        return None
    env: dict[str, str] = {}
    for chunk in raw.split(b"\0"):
        if not chunk:
            continue
        # systemd-style quoting: VAR="value"
        if len(chunk) >= 2 and chunk.endswith(b'"') and b'="' in chunk:
            k, v = chunk.split(b'="', 1)
            env[k.decode()] = v[:-1].decode()
        else:
            if b"=" in chunk:
                k, v = chunk.split(b"=", 1)
                env[k.decode()] = v.decode()
    return env


def scan_marker_processes() -> dict[str, dict]:
    """Find live processes carrying DISPATCHER_ATTEMPT_ID.

    Returns {attempt_id: {"pid": pid, "pgid": pgid, "job_id": str}}.
    """
    found: dict[str, dict] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        env = read_environ(pid)
        if not env or env.get("DISPATCHER") != "1":
            continue
        attempt_id = env.get("DISPATCHER_ATTEMPT_ID", "")
        if not attempt_id:
            continue
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                data = f.read().decode("ascii", "replace")
            pgid = int(data[data.rfind(")") + 2:].split()[1])  # field 5
        except (OSError, ValueError, IndexError):
            continue
        found[attempt_id] = {"pid": pid, "pgid": pgid, "job_id": env.get("DISPATCHER_JOB_ID", "")}
    return found


def group_alive(pgid: int) -> bool:
    """True if any process in the group still exists.

    killpg(pgid, 0) raises ESRCH when the group is empty. As a belt-and-braces
    check (and to catch the edge where pgid==pid of a reparented member),
    also scan /proc for members.
    """
    if pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    except PermissionError:
        alive = True
    if not alive:
        return False
    # killpg(0) can succeed while our direct child is gone but a member
    # lingers — that's still "group alive" for our purposes.
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as f:
                data = f.read().decode("ascii", "replace")
        except OSError:
            continue
        parts = data[data.rfind(")") + 2:].split()
        if len(parts) > 1 and int(parts[1]) == pgid and int(entry) > 1:
            return True
    return alive


def kill_group(pgid: int, sig: int) -> bool:
    """Send sig to the group. Returns True if the group existed."""
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def signal_group(pgid: int, sig: int) -> bool:
    return kill_group(pgid, sig)


def pgid_of(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read().decode("ascii", "replace")
        return int(data[data.rfind(")") + 2:].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def make_identity(pid: int) -> str:
    st = starttime(pid)
    return f"{pid}:{pid}:{st if st is not None else '?'}"


def parse_identity(ident: str) -> tuple[int, int, int | None]:
    try:
        a, b, c = ident.split(":")
        return int(a), int(b), None if c == "?" else int(c)
    except (ValueError, AttributeError):
        return 0, 0, None


def identity_matches(pid: int, pgid: int, recorded: str) -> bool:
    r_pid, r_pgid, r_st = parse_identity(recorded)
    if r_pid != pid or r_pgid != pgid:
        return False
    if r_st is None:
        return True
    return starttime(pid) == r_st
