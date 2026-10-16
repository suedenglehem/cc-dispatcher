"""Fake inference backend: a threaded HTTP server answering GET /v1/models
with the shape of one of the real providers, on an ephemeral 127.0.0.1 port.

- shape="llamacpp"  → {"data":[{"id": ..., "meta": {"n_ctx": N}}]}
- shape="vllm"      → {"data":[{"id": ..., "max_model_len": N}]}

`.stop()` makes the next probe report offline (connection refused), which is
how tests drive backend_offline transitions.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (http.server API)
        if self.path.rstrip("/") == "/v1/models":
            body = self.server.fake_body()  # type: ignore[attr-defined]
            payload = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):  # silence
        pass


class FakeBackend:
    def __init__(self, served_id: str = "fake-model", ctx_max: int = 32000,
                 shape: str = "llamacpp"):
        self.served_id = served_id
        self.ctx_max = ctx_max
        self.shape = shape
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int | None = None

    def _body(self) -> dict:
        if self.shape == "vllm":
            return {"data": [{"id": self.served_id,
                              "max_model_len": self.ctx_max}]}
        return {"data": [{"id": self.served_id,
                          "meta": {"n_ctx": self.ctx_max}}]}

    def start(self) -> "FakeBackend":
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.fake_body = self._body  # type: ignore[attr-defined]
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True,
            name=f"fake-backend-{self.port}")
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def endpoint(self) -> str:
        assert self.port is not None, "FakeBackend.start() not called"
        return f"http://127.0.0.1:{self.port}"
