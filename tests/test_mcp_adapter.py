"""P2 — dispatcher-mcp forwarder: a real stdio MCP client drives the 8
tools against the in-process daemon over the real socket wire.

The forwarder subprocess is spawned with a minimal env (HOME/PATH +
LOCAL_DISPATCHER_SOCKET only) — it must work without inheriting anything
else from the launching session.
"""

import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tests.helpers import wait_job

REPO = Path(__file__).resolve().parent.parent

EXPECTED_TOOLS = {
    "dispatch_status", "dispatch_set_mode", "dispatch_submit",
    "dispatch_job", "dispatch_cancel", "dispatch_resume",
    "dispatch_workers", "dispatch_integrate",
}


def _params(sock: str, cwd: str) -> StdioServerParameters:
    envd = {"HOME": os.environ.get("HOME", "/"),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LOCAL_DISPATCHER_SOCKET": str(sock)}
    return StdioServerParameters(command=sys.executable,
                                 args=["-m", "local_dispatcher.mcp_adapter"],
                                 env=envd, cwd=str(cwd))


@asynccontextmanager
async def _session(sock: str, cwd: str):
    async with stdio_client(_params(sock, cwd)) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            yield s


def _text(res) -> str:
    return "".join(c.text for c in res.content if getattr(c, "text", None))


@pytest.fixture
def handle(env):
    env.backend()  # a real (fake) backend so jobs actually launch
    # hold workers 8s so cancel tests can catch a running attempt
    return env.daemon(
        worker_mode="hold",
        profiles=[dict(id="w", env_overrides={"FAKE_HOLD_S": "8"})])


def _job_status(handle, job_id: str) -> str:
    return handle.query("SELECT status FROM jobs WHERE id=?",
                        (job_id,))[0]["status"]


async def test_tools_listed(tmp_path):
    async with _session(str(tmp_path / "nope.sock"), REPO) as s:
        tools = await s.list_tools()
        assert {t.name for t in tools.tools} == EXPECTED_TOOLS


async def test_daemon_down_is_actionable(tmp_path):
    async with _session(str(tmp_path / "nope.sock"), REPO) as s:
        res = await s.call_tool("dispatch_status", {})
        assert res.isError
        assert "not running" in _text(res)


async def test_status_shape(handle):
    async with _session(handle.cfg.paths.socket_path, REPO) as s:
        res = await s.call_tool("dispatch_status", {})
        assert not res.isError, _text(res)
        data = json.loads(_text(res))
        assert {"projects", "jobs", "workers", "reservations"} <= set(data)
        assert data["projects"][0]["id"] == "p1"


async def test_submit_job_cancel_via_mcp(handle, env):
    sock = handle.cfg.paths.socket_path
    async with _session(sock, REPO) as s:
        res = await s.call_tool("dispatch_set_mode",
                                {"mode": "team", "project_id": "p1"})
        assert not res.isError, _text(res)
        res = await s.call_tool("dispatch_submit", {
            "objective": "via mcp", "base_commit": env.repo_head,
            "profile_id": "w", "allowed_scope": ["src"],
            "project_id": "p1"})
        assert not res.isError, _text(res)
        job_id = json.loads(_text(res))["job_id"]

        res = await s.call_tool("dispatch_cancel",
                                {"job_id": job_id, "project_id": "p1"})
        assert not res.isError, _text(res)
        view = wait_job(handle.client(), job_id, "cancelled")
        assert view["status"] == "cancelled"

        res = await s.call_tool("dispatch_job",
                                {"job_id": job_id, "project_id": "p1"})
        assert not res.isError, _text(res)
        assert json.loads(_text(res))["status"] == "cancelled"


async def test_cwd_binds_project(handle, env):
    # no project_id anywhere; the server's cwd IS the project repo
    async with _session(handle.cfg.paths.socket_path, env.repo) as s:
        res = await s.call_tool("dispatch_set_mode", {"mode": "team"})
        assert not res.isError, _text(res)
        res = await s.call_tool("dispatch_submit", {
            "objective": "cwd bound", "base_commit": env.repo_head,
            "profile_id": "w", "allowed_scope": ["src"]})
        assert not res.isError, _text(res)
        job_id = json.loads(_text(res))["job_id"]
        row = handle.query("SELECT project_id FROM jobs WHERE id=?",
                           (job_id,))[0]
        assert row["project_id"] == "p1"
        res = await s.call_tool("dispatch_cancel", {"job_id": job_id})
        assert not res.isError, _text(res)
        wait_job(handle.client(), job_id, "cancelled")


async def test_ambiguous_project_error(env):
    env.backend()
    h = env.daemon(projects=[env.project("p1"),
                             env.project("p2", repo=env.tmp_path / "other")])
    async with _session(h.cfg.paths.socket_path, env.tmp_path) as s:
        res = await s.call_tool("dispatch_submit", {
            "objective": "x", "base_commit": env.repo_head,
            "profile_id": "w", "allowed_scope": ["src"]})
        assert res.isError
        txt = _text(res)
        assert "p1" in txt and "p2" in txt  # actionable: lists known ids
