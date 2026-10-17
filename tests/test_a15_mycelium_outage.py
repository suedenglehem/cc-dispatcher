"""PRD §14 test 15 — when Mycelium is unavailable, launches proceed without
the memory section (degrade) unless memory_required=1, in which case the job
is blocked (needs a decision).

Real `mycelium serve` child, killed mid-run twice: spawn-once + respawn-once
means job 1 and job 2 still get the section, job 3 degrades to a plain
packet, and job 4 (memory_required) is blocked with
memory_required_unavailable."""

import asyncio
import os
import signal
import time

import pytest

from local_dispatcher.config import MyceliumConfig
from local_dispatcher.mycelium import MyceliumClient

from tests.helpers import MYCELIUM_BIN, make_mycelium_store, wait_job

pytestmark = pytest.mark.skipif(
    not os.path.exists(MYCELIUM_BIN),
    reason=f"mycelium binary not found at {MYCELIUM_BIN}")

FACT = "[project-note] p1: outage drill marker FACT-A15"


def _save_fact(store: dict, project: str) -> bool:
    async def _run():
        c = MyceliumClient(MyceliumConfig(
            command=store["command"], env=store["env"], enabled=True))
        try:
            return await c.save(FACT, project=project, pinned=True,
                                confidence=0.9)
        finally:
            await c.close()
    return asyncio.run(_run())


def test_mycelium_outage_degrades_or_blocks(env):
    store = make_mycelium_store(env.root)
    assert _save_fact(store, "p1")

    env.backend()
    h = env.daemon(
        mycelium={"command": store["command"], "env": store["env"]},
        profiles=[dict(id="w", env_overrides={"FAKE_ECHO_PACKET": "1"})])
    c = h.client()
    c.set_mode("team", project_id="p1")

    def submit(memory_required: bool = False) -> str:
        p = dict(objective="outage drill", base_commit=env.repo_head,
                 allowed_scope=["src"], profile_id="w",
                 memory_queries=["outage drill marker"])
        if memory_required:
            p["memory_required"] = True
        return c.submit("p1", **p)["job_id"]

    def echoed(jid: str) -> str:
        v = wait_job(c, jid, "succeeded")
        with open(os.path.join(v["artifacts"], "echo_packet.txt"),
                  encoding="utf-8") as f:
            return f.read()

    # job 1: healthy client (spawns the child) → section present
    j1 = submit()
    assert "MEMORY_RECALLED" in echoed(j1), "healthy run lost the section"

    # job 2: first death → the one allowed respawn → section present
    os.kill(h.mycelium.pid, signal.SIGKILL)
    time.sleep(0.5)  # let the daemon loop reap the child
    j2 = submit()
    assert "MEMORY_RECALLED" in echoed(j2), "respawn did not restore the section"

    # job 3: second death → budget exhausted → no section, job still succeeds
    os.kill(h.mycelium.pid, signal.SIGKILL)
    time.sleep(0.5)
    j3 = submit()
    assert "MEMORY_RECALLED" not in echoed(j3), \
        "exhausted client must not recall anything"
    assert h.mycelium.available is False, "client must be permanently unavailable"

    # job 4: memory_required ⇒ the prefilter blocks the launch
    j4 = submit(memory_required=True)
    v = wait_job(c, j4, "blocked")
    assert v["status_detail"] == "memory_required_unavailable"
