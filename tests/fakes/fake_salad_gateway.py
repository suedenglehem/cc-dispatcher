"""Fake salad.cloud group: a threaded HTTP server emulating the public
gateway's observable behavior.

- EVERY request requires the Salad-Api-Key header (403 otherwise) — the
  defining property of a real group.
- GET /v1/models   → llama.cpp-shaped body (id + meta.n_ctx).
- GET /props       → default_generation_settings.n_ctx (the proxy's
                     /__upstream/props ctx fallback path).
- POST /v1/completions → 1-token completion (the daemon heartbeat target).
- POST /v1/chat/completions → minimal OpenAI chat completion (upstream
                     target of salad_proxy.py; exercised by live smoke,
                     here only for completeness). When the request is
                     streaming and `chat_mode` is set, responds with SSE
                     instead (see chat_mode).

`.stop_group()` emulates a stopped Salad group the way the Cloudflare
front presents it: the host still answers, with HTTP 503. `.stop()` shuts
the server down entirely (connection refused).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    def _key_ok(self) -> bool:
        gw = self.server.gw  # type: ignore[attr-defined]
        return self.headers.get("Salad-Api-Key", "") == gw.key

    def _json(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _group_down(self) -> bool:
        gw = self.server.gw  # type: ignore[attr-defined]
        if gw.online:
            return False
        payload = json.dumps({"error": "group stopped"}).encode("utf-8")
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        return True

    def do_GET(self):  # noqa: N802 (http.server API)
        gw = self.server.gw  # type: ignore[attr-defined]
        if self._group_down():
            return
        if not self._key_ok():
            self._json(403, {"error": "forbidden"})
            return
        p = self.path.split("?", 1)[0].rstrip("/")
        if p == "/v1/models":
            self._json(200, gw.models_body())
        elif p == "/props":
            self._json(200, {"default_generation_settings":
                             {"n_ctx": gw.ctx_max}})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        gw = self.server.gw  # type: ignore[attr-defined]
        if self._group_down():
            return
        if not self._key_ok():
            self._json(403, {"error": "forbidden"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        p = self.path.split("?", 1)[0].rstrip("/")
        if p == "/v1/completions":
            gw.completions += 1
            self._json(200, {"id": "cmpl-fake",
                             "choices": [{"text": ".", "index": 0}]})
        elif p == "/v1/chat/completions":
            gw.chat_calls += 1
            try:
                req = json.loads(raw or b"{}")
            except Exception:
                req = {}
            if req.get("stream") and gw.chat_mode != "json":
                self._chat_stream(gw)
            elif gw.chat_mode == "json_error":
                # 200 with an in-band error payload and no completion
                self._json(200, {"error": {"message": "engine died"}})
            elif gw.chat_mode == "json_empty":
                # 200 with no choices at all (zero-content completion)
                self._json(200, {"id": "chat-fake", "choices": [],
                                 "usage": {"prompt_tokens": 1,
                                           "completion_tokens": 0}})
            else:
                self._json(200, {
                    "id": "chat-fake",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant",
                                             "content": "fake-ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                              "total_tokens": 2}})
        else:
            self._json(404, {"error": "not found"})

    def _chat_stream(self, gw) -> None:
        """SSE responses per gw.chat_mode:

        - "stream":              normal two-delta completion + [DONE]
        - "stream_empty":        headers, then EOF — zero content (origin/
                                 edge killed the stream mid-flight)
        - "stream_partial":      some content, then EOF with no completion
                                 signal (truncated answer)
        - "stream_error_payload": one delta, then an in-band
                                 `data: {"error": ...}` payload, then EOF
        - "stream_trailing_garbage": a complete answer (content +
                                 finish_reason + [DONE]) with legal SSE
                                 id:/retry: lines and one junk line before
                                 the end — the answer must NOT be discarded
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def data(obj: dict) -> bytes:
            return b"data: " + json.dumps(obj).encode() + b"\n\n"

        def chunk(content: str | None = None,
                  finish: str | None = None) -> bytes:
            d: dict = {"role": "assistant"}
            if content is not None:
                d["content"] = content
            return data({"id": "chat-fake",
                         "choices": [{"index": 0, "delta": d,
                                      "finish_reason": finish}]})

        if gw.chat_mode == "stream":
            self.wfile.write(chunk("fake-"))
            self.wfile.write(chunk("ok"))
            self.wfile.write(chunk(None, "stop"))
            self.wfile.write(b"data: [DONE]\n\n")
        elif gw.chat_mode == "stream_empty":
            pass  # nothing: the stream dies right after the headers
        elif gw.chat_mode == "stream_partial":
            # content, then EOF with no finish_reason and no [DONE]: the
            # answer may be truncated — it must not be a clean success.
            self.wfile.write(chunk("partial"))
        elif gw.chat_mode == "stream_error_payload":
            self.wfile.write(chunk("partial"))
            self.wfile.write(data({"error": {"message": "upstream exploded"}}))
            # no [DONE]: the stream ends with the error
        elif gw.chat_mode == "stream_trailing_garbage":
            self.wfile.write(chunk("ok"))
            self.wfile.write(chunk(None, "stop"))
            # legal SSE fields then junk BEFORE the end marker: the turn is
            # already complete, so the proxy must finish normally.
            self.wfile.write(b"id: 42\n")
            self.wfile.write(b"retry: 3000\n")
            self.wfile.write(b"<html>edge noise</html>\n")
            self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *args):  # silence
        pass


class FakeSaladGateway:
    def __init__(self, key: str = "test-salad-key", model: str = "qwen",
                 ctx_max: int = 32768):
        self.key = key
        self.model = model
        self.ctx_max = ctx_max
        self.completions = 0     # heartbeat hits
        self.chat_calls = 0
        self.online = True
        self.chat_mode: str = "json"  # or stream/stream_empty/stream_error_payload
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int | None = None

    def models_body(self) -> dict:
        return {"data": [{"id": self.model,
                          "meta": {"n_ctx": self.ctx_max}}]}

    def start(self) -> "FakeSaladGateway":
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.gw = self  # type: ignore[attr-defined]
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True,
            name=f"fake-salad-{self.port}")
        self._thread.start()
        return self

    def stop_group(self) -> None:
        """Group stopped by the user: the gateway answers 503."""
        self.online = False

    def start_group(self) -> None:
        self.online = True

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def gateway_url(self) -> str:
        assert self.port is not None, "FakeSaladGateway.start() not called"
        return f"http://127.0.0.1:{self.port}"
