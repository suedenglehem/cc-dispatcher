"""PRD §14 test 14 — a fact the primary saves to Mycelium before submission
appears in the worker's task packet (bounded recall at packet render time).

Real `mycelium serve` under MYCELIUM_CONFIG→temp store (the user's
~/.mycelium is never touched): the test client saves the fact, the daemon's
client recalls it at render time, and the fake worker (FAKE_ECHO_PACKET)
dumps the received packet to <artifacts>/echo_packet.txt."""

import asyncio
import os

import pytest

from local_dispatcher.config import MyceliumConfig
from local_dispatcher.mycelium import MyceliumClient

from tests.helpers import MYCELIUM_BIN, make_mycelium_store, wait_job

pytestmark = pytest.mark.skipif(
    not os.path.exists(MYCELIUM_BIN),
    reason=f"mycelium binary not found at {MYCELIUM_BIN}")

FACT = ("[project-note] p1: the deploy script has a --fast-path alias; "
        "use it for smoke runs")


def _save_fact(store: dict, project: str) -> bool:
    """The 'primary' side: one pinned save against the temp store."""
    async def _run():
        c = MyceliumClient(MyceliumConfig(
            command=store["command"], env=store["env"], enabled=True))
        try:
            return await c.save(FACT, project=project, pinned=True,
                                confidence=0.9)
        finally:
            await c.close()
    return asyncio.run(_run())


def test_worker_packet_contains_recalled_fact(env):
    store = make_mycelium_store(env.root)
    assert _save_fact(store, "p1"), "test client could not save the fact"

    env.backend()
    h = env.daemon(
        mycelium={"command": store["command"], "env": store["env"]},
        profiles=[dict(id="w", env_overrides={"FAKE_ECHO_PACKET": "1"})])
    c = h.client()
    c.set_mode("team", project_id="p1")

    jid = c.submit("p1",
                   objective="worker should see the primary's fact",
                   base_commit=env.repo_head, allowed_scope=["src"],
                   profile_id="w",
                   memory_queries=["deploy script fast-path alias"])["job_id"]
    v = wait_job(c, jid, "succeeded")

    with open(os.path.join(v["artifacts"], "echo_packet.txt"),
              encoding="utf-8") as f:
        echo = f.read()
    assert "MEMORY_RECALLED" in echo, "packet has no MEMORY_RECALLED section"
    assert "fast-path" in echo, "the recalled fact is missing from the packet"
