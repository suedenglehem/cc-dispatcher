"""PRD §14 test 16 — the worker and the primary share one Mycelium project
key: the worker's [worker-checkpoint] save lands in the same store the
primary reads (verified by the primary-side client recalling the checkpoint).

The daemon itself runs WITHOUT a [mycelium] section (isolation: it must not
need or touch the store); the fake worker spawns its own `mycelium serve`
child against the temp store via FAKE_MYCELIUM_CMD/FAKE_MYCELIUM_CONFIG —
the same stdio contract a real worker gets through --mcp-config."""

import asyncio
import json
import os

import pytest

from local_dispatcher.config import MyceliumConfig
from local_dispatcher.mycelium import MyceliumClient

from tests.helpers import MYCELIUM_BIN, make_mycelium_store, wait_job

pytestmark = pytest.mark.skipif(
    not os.path.exists(MYCELIUM_BIN),
    reason=f"mycelium binary not found at {MYCELIUM_BIN}")

KEY = "p16key"


def test_worker_and_primary_share_memory_key(env):
    store = make_mycelium_store(env.root)
    env.backend()
    h = env.daemon(
        projects=[env.project("p1", mycelium_project=KEY)],
        profiles=[dict(id="w", env_overrides={
            "FAKE_MYCELIUM_CMD": json.dumps(store["command"]),
            "FAKE_MYCELIUM_CONFIG": store["cfg"]})])
    c = h.client()
    c.set_mode("team", project_id="p1")

    jid = c.submit("p1", objective="worker saves a checkpoint",
                   base_commit=env.repo_head, allowed_scope=["src"],
                   profile_id="w")["job_id"]
    wait_job(c, jid, "succeeded")

    # primary side: bounded recall against the same store + project key
    async def _recall():
        mc = MyceliumClient(MyceliumConfig(
            command=store["command"], env=store["env"], enabled=True))
        try:
            return await mc.memory_section(
                {"id": "p1", "mycelium_project": KEY},
                {"memory_queries": json.dumps([jid])})
        finally:
            await mc.close()
    text = asyncio.run(_recall()) or ""
    assert jid in text, "primary cannot recall the worker's checkpoint"
    assert "[worker-checkpoint]" in text
