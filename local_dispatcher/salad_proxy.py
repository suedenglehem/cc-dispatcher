#!/usr/bin/env python3
"""salad_proxy.py — local Anthropic<->OpenAI bridge for a llama.cpp server behind a Salad public gateway.

Vendored verbatim from /usr/local/bin/sl_proxy.py (the cl_salad runner's proxy);
the dispatcher runs one of these per salad backend (see salad.py).

Claude Code speaks the Anthropic Messages API; the Salad container runs
llama-server, which is OpenAI-only. This stdlib-only proxy:

  * listens on 127.0.0.1 only (it holds a secret key — never exposed to the network)
  * serves POST /v1/messages, GET /v1/models, GET /healthz, and
    GET /__upstream/<path> — a keyed passthrough to the llama server (the
    wrapper uses it to interrogate the model, e.g. detect max context)
  * translates an Anthropic request -> OpenAI /v1/chat/completions
  * injects the Salad-Api-Key header on every upstream request (read from a file,
    never printed)
  * translates the OpenAI response back to Anthropic — streaming SSE or JSON
  * maps tool_use / tool_result  <->  tool_calls / role:tool
  * disables Qwen "thinking" by default so the model emits clean answers, not a
    long reasoning preamble (set --thinking 1 to change)

Run by the cl_salad wrapper. No third-party dependencies.
"""
import argparse
import http.client
import json
import os
import re
import sys
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _ev(name, obj):
    """One Anthropic SSE frame: 'event: <name>\\ndata: <json>\\n\\n'."""
    return "event: %s\ndata: %s\n\n" % (name, json.dumps(obj, separators=(",", ":")))


def _rand_id():
    return uuid.uuid4().hex[:24]


def _err_msg(status, raw):
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    if status == 404:
        return ("upstream 404: Salad container not ready (group stopped or still starting). "
                "If you set SALAD_NO_AUTOSTART=1, start the group first. Raw: %s" % text[:300])
    try:
        d = json.loads(text)
        e = d.get("error")
        if isinstance(e, dict):
            return "upstream error (HTTP %d): %s" % (status, e.get("message") or e)
        if isinstance(e, str):
            return "upstream error (HTTP %d): %s" % (status, e)
        if "detail" in d:
            return "upstream error (HTTP %d): %s" % (status, d["detail"])
    except Exception:
        pass
    return "upstream error (HTTP %d): %s" % (status, text[:300])


# --------------------------------------------------------------------------- #
# upstream client (talks to the Salad gateway, injects the key)
# --------------------------------------------------------------------------- #
class Upstream:
    def __init__(self, upstream_url, keyfile, model, thinking=False,
                 max_tokens_cap=12000, timeout=600, allow_http=False):
        u = urllib.parse.urlsplit(upstream_url)
        if u.scheme == "https":
            self.use_tls = True
        elif u.scheme == "http":
            # testing-only escape hatch (fake local gateways speak plain
            # HTTP); loopback hosts only, and only when explicitly asked
            if not allow_http or (u.hostname or "") not in \
                    ("127.0.0.1", "localhost", "::1"):
                raise SystemExit(
                    "upstream must be https for Salad gateways "
                    "(plain http is only allowed for loopback with "
                    "--allow-http-upstream)")
            self.use_tls = False
        else:
            raise SystemExit("upstream must be https for Salad gateways")
        self.host = u.hostname
        self.port = u.port or (443 if self.use_tls else 80)
        self.path = u.path or "/v1/chat/completions"
        self.model = model
        self.thinking = thinking
        self.max_tokens_cap = max_tokens_cap
        self.timeout = timeout
        try:
            with open(keyfile) as fh:
                self.key = fh.read().strip()
        except OSError as e:
            raise SystemExit("cannot read keyfile %s: %s" % (keyfile, e))
        if not self.key:
            raise SystemExit("keyfile %s is empty" % keyfile)

    def _conn(self, timeout):
        cls = http.client.HTTPSConnection if self.use_tls \
            else http.client.HTTPConnection
        return cls(self.host, self.port, timeout=timeout)

    def request(self, payload, stream):
        conn = self._conn(self.timeout)
        headers = {
            "Salad-Api-Key": self.key,
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }
        conn.request("POST", self.path, body=json.dumps(payload), headers=headers)
        return conn, conn.getresponse()

    def get(self, path, timeout=5):
        """Keyed GET passthrough (e.g. /__upstream/props -> upstream /props).
        Introspection only, so a short timeout: a dead group must not stall
        a session start."""
        conn = self._conn(timeout)
        conn.request("GET", path, headers={"Salad-Api-Key": self.key})
        return conn, conn.getresponse()


# --------------------------------------------------------------------------- #
# Anthropic -> OpenAI (request)
# --------------------------------------------------------------------------- #
def _system_text(system):
    if system is None:
        return None
    if isinstance(system, str):
        return system or None
    if isinstance(system, list):
        parts = []
        for b in system:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, str):
                parts.append(b)
        txt = "\n".join(p for p in parts if p)
        return txt or None
    return None


def _image_to_openai(b):
    src = b.get("source", {}) or {}
    st = src.get("type")
    if st == "base64":
        mt = src.get("media_type", "image/png")
        data = src.get("data", "")
        return {"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (mt, data)}}
    if st == "url":
        return {"type": "image_url", "image_url": {"url": src.get("url", "")}}
    return {"type": "text", "text": "[image]"}


def _translate_messages(msgs):
    out = []
    for m in msgs:
        role = m.get("role")
        content = m.get("content")

        if role == "system":
            txt = _system_text(content)
            if txt:
                out.append({"role": "system", "content": txt})
            continue

        if role == "assistant":
            text_parts, tool_calls = [], []
            if isinstance(content, str):
                text_parts.append(content)
            elif isinstance(content, list):
                for b in content:
                    bt = b.get("type")
                    if bt == "text":
                        text_parts.append(b.get("text", ""))
                    elif bt == "tool_use":
                        text_parts  # noqa: B018 (kept for clarity of intent)
                        tool_calls.append({
                            "id": b.get("id"),
                            "type": "function",
                            "function": {
                                "name": b.get("name"),
                                "arguments": json.dumps(b.get("input", {}),
                                                        separators=(",", ":")),
                            },
                        })
                    # "thinking" blocks are dropped
            txt = "\n".join(p for p in text_parts if p)
            msg = {"role": "assistant", "content": txt}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)
            continue

        if role == "user":
            tool_msgs, user_blocks = [], []
            if isinstance(content, str):
                user_blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for b in content:
                    bt = b.get("type")
                    if bt == "tool_result":
                        c = b.get("content")
                        if isinstance(c, list):
                            parts = []
                            for cb in c:
                                if isinstance(cb, dict):
                                    if cb.get("type") == "text":
                                        parts.append(cb.get("text", ""))
                                    elif cb.get("type") == "image":
                                        parts.append("[image]")
                            tool_content = "\n".join(parts)
                        else:
                            tool_content = "" if c is None else str(c)
                        tool_msgs.append({"role": "tool",
                                          "tool_call_id": b.get("tool_use_id"),
                                          "content": tool_content})
                    elif bt == "text":
                        user_blocks.append({"type": "text", "text": b.get("text", "")})
                    elif bt == "image":
                        user_blocks.append(_image_to_openai(b))
            # tool results come first (they answer the previous assistant tool_calls),
            # then any new user text/images
            out.extend(tool_msgs)
            if user_blocks:
                out.append({"role": "user", "content": user_blocks})
            continue

        # unknown role: skip
    return out


def _translate_tools(tools):
    if not tools:
        return None
    out = []
    for t in tools:
        out.append({
            "type": "function",
            "function": {
                "name": t.get("name"),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        })
    return out


def _translate_tool_choice(tc):
    if tc is None:
        return None
    t = tc.get("type") if isinstance(tc, dict) else tc
    if t == "auto":
        return "auto"
    if t == "any":
        return "required"
    if t == "none":
        return "none"
    if t == "tool":
        return {"type": "function", "function": {"name": tc.get("name")}}
    return None


def anthropic_to_openai(body, up):
    om = []
    sys_txt = _system_text(body.get("system"))
    if sys_txt:
        om.append({"role": "system", "content": sys_txt})
    om.extend(_translate_messages(body.get("messages") or []))

    payload = {"model": up.model, "messages": om}
    try:
        mt = int(body.get("max_tokens"))
    except (TypeError, ValueError):
        mt = 0
    payload["max_tokens"] = max(1, min(mt or up.max_tokens_cap, up.max_tokens_cap))

    for k in ("temperature", "top_p"):
        if isinstance(body.get(k), (int, float)):
            payload[k] = body[k]
    stop = body.get("stop_sequences")
    if isinstance(stop, list) and stop:
        payload["stop"] = stop
    elif isinstance(stop, str) and stop:
        payload["stop"] = stop

    tools = _translate_tools(body.get("tools"))
    if tools:
        payload["tools"] = tools
    tc = _translate_tool_choice(body.get("tool_choice"))
    if tc is not None:
        payload["tool_choice"] = tc

    if body.get("stream"):
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    if not up.thinking:
        # keep the Qwen answer clean; the smoke test (sl_curl.sh) used this too
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    return payload


# --------------------------------------------------------------------------- #
# Empty think-block stripper
# --------------------------------------------------------------------------- #
# With enable_thinking=false the chat template ends the prompt with the CLOSED
# empty block "<think>\n\n</think>\n\n" as the generation prefix. Groups whose
# EXTRA_ARGS carry "--reasoning-format none" (the ATX operating point) do not
# strip think blocks server-side, and the model echoes that prefix — Claude
# Code then shows a literal "<think>\n\n</think>" in every answer. The proxy
# removes EMPTY think blocks from the response text; real (non-empty) think
# blocks pass through untouched, so --thinking 1 keeps working.
_EMPTY_THINK = re.compile(r"<think>[ \t\n\r]*</think>\n*")
# streaming: text AFTER the consumed "<think>" that completes an empty block
_EMPTY_CLOSE = re.compile(r"[ \t\n\r]*</think>\n*")
_THINK_OPEN = "<think>"


def _viable_empty_think(rest):
    """True if `rest` (text right after `<think>`) can still grow into an
    empty block: whitespace, then `</think>`, then trailing newlines."""
    i = 0
    while i < len(rest) and rest[i] in " \t\n\r":
        i += 1
    return ("</think>\n\n").startswith(rest[i:])


class EmptyThinkStripper:
    """Streaming filter: removes empty think blocks from a text stream fed in
    arbitrary chunks. Buffers only text that could still become an empty block
    (a prefix of "<think>" / its whitespace / "</think>" / trailing newlines);
    everything else is emitted immediately."""

    def __init__(self):
        self.buf = ""
        self.in_tag = False
        self.after_block = False  # just stripped: consume the prefix newlines

    def feed(self, text):
        self.buf += text
        return self._drain(False)

    def flush(self):
        """End of stream: release whatever was buffered (a block that never
        closed is real content and must not be swallowed)."""
        return self._drain(True)

    def _drain(self, final):
        out = []
        buf = self.buf
        while buf:
            if self.after_block:
                self.after_block = False
                j = 0
                while j < len(buf) and buf[j] == "\n":
                    j += 1
                buf = buf[j:]
                if not buf:
                    break
                continue
            if not self.in_tag:
                i = buf.find("<")
                if i < 0:
                    out.append(buf)
                    buf = ""
                    break
                out.append(buf[:i])
                buf = buf[i:]
                self.in_tag = True
            # in_tag: buf starts with '<'
            if len(buf) < len(_THINK_OPEN) and _THINK_OPEN.startswith(buf):
                if final:  # stream ended mid-tag — the text is real content
                    out.append(buf)
                    buf = ""
                    self.in_tag = False
                break  # partial tag ("<thi") — need more data
            if not buf.startswith(_THINK_OPEN):
                out.append("<")  # not a think tag — emit it, rescan
                buf = buf[1:]
                self.in_tag = False
                continue
            rest = buf[len(_THINK_OPEN):]
            m = _EMPTY_CLOSE.match(rest)
            if m:
                buf = rest[m.end():]  # empty block — stripped
                self.in_tag = False
                self.after_block = True
                continue
            if not final and _viable_empty_think(rest):
                break  # could still close as an empty block — keep buffering
            out.append(_THINK_OPEN)  # real content inside — pass through
            buf = rest
            self.in_tag = False
        self.buf = buf
        return "".join(out)


# --------------------------------------------------------------------------- #
# OpenAI -> Anthropic (non-streaming response)
# --------------------------------------------------------------------------- #
def openai_to_anthropic(resp, model):
    choices = resp.get("choices") or [{}]
    ch = choices[0]
    msg = ch.get("message") or {}
    content = []
    txt = msg.get("content")
    if txt:
        txt = _EMPTY_THINK.sub("", txt)
    if txt:
        content.append({"type": "text", "text": txt})
    for tc in (msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        try:
            inp = json.loads(args) if args else {}
        except Exception:
            inp = {"_raw": args}
        content.append({"type": "tool_use",
                        "id": tc.get("id") or ("toolu_%s" % _rand_id()),
                        "name": fn.get("name"), "input": inp})
    if not content:
        content = [{"type": "text", "text": ""}]
    fr = ch.get("finish_reason")
    stop = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens",
            "content_filter": "end_turn"}.get(fr, "end_turn")
    usage = resp.get("usage") or {}
    return {
        "id": "msg_" + (resp.get("id") or _rand_id()),
        "type": "message", "role": "assistant", "model": model,
        "content": content, "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                  "output_tokens": usage.get("completion_tokens", 0)},
    }


# --------------------------------------------------------------------------- #
# OpenAI streaming -> Anthropic streaming (incremental)
# --------------------------------------------------------------------------- #
def _iter_sse(resp):
    """Yield parsed JSON objects from an upstream 'data: ...' SSE body until [DONE]."""
    while True:
        raw = resp.readline()
        if not raw:
            return
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except Exception:
            continue


class StreamBuilder:
    """Accumulates OpenAI chat-completion chunks and emits Anthropic SSE frames."""

    def __init__(self, model, uid):
        self.model = model
        self.uid = uid
        self.next_index = 0
        self.text_open = False
        self.text_index = None
        self.tools = {}
        self.finish_reason = None
        self.usage_out = 0
        self.strip = EmptyThinkStripper()  # see the stripper section above

    def _aidx(self):
        i = self.next_index
        self.next_index += 1
        return i

    def message_start(self):
        return _ev("message_start", {"type": "message_start", "message": {
            "id": "msg_" + self.uid, "type": "message", "role": "assistant",
            "model": self.model, "content": [], "stop_reason": None,
            "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})

    def handle(self, chunk):
        out = []
        if isinstance(chunk.get("usage"), dict):
            self.usage_out = chunk["usage"].get("completion_tokens") or self.usage_out
        choices = chunk.get("choices") or []
        if not choices:
            return out
        delta = choices[0].get("delta") or {}
        fr = choices[0].get("finish_reason")
        if fr:
            self.finish_reason = fr
        if isinstance(delta.get("content"), str) and delta["content"]:
            out.extend(self._text(delta["content"]))
        tcs = delta.get("tool_calls")
        if tcs:
            for tc in tcs:
                out.extend(self._tool(tc))
        return out

    def _text(self, t):
        t = self.strip.feed(t)  # empty think blocks are swallowed here
        if not t:
            return []
        return self._text_raw(t)

    def _text_raw(self, t):
        out = []
        if not self.text_open:
            self.text_open = True
            self.text_index = self._aidx()
            out.append(_ev("content_block_start", {"type": "content_block_start",
                        "index": self.text_index,
                        "content_block": {"type": "text", "text": ""}}))
        out.append(_ev("content_block_delta", {"type": "content_block_delta",
                   "index": self.text_index,
                   "delta": {"type": "text_delta", "text": t}}))
        return out

    def _close_text(self):
        if self.text_open:
            self.text_open = False
            return _ev("content_block_stop",
                       {"type": "content_block_stop", "index": self.text_index})
        return None

    def _tool(self, tc):
        out = []
        i = tc.get("index", 0)
        fn = tc.get("function") or {}
        if i not in self.tools:
            tail = self.strip.flush()  # release buffered text before closing
            if tail:
                out.extend(self._text_raw(tail))
            ct = self._close_text()
            if ct:
                out.append(ct)
            aidx = self._aidx()
            tid = tc.get("id") or ("toolu_%s_%d" % (self.uid, i))
            self.tools[i] = {"aidx": aidx, "id": tid,
                             "name": fn.get("name"), "args": ""}
            out.append(_ev("content_block_start", {"type": "content_block_start",
                "index": aidx,
                "content_block": {"type": "tool_use", "id": tid,
                                   "name": fn.get("name"), "input": {}}}))
        args = fn.get("arguments") or ""
        if args:
            self.tools[i]["args"] += args
        return out

    def finish(self):
        out = []
        tail = self.strip.flush()  # a block that never closed is real content
        if tail:
            out.extend(self._text_raw(tail))
        ct = self._close_text()
        if ct:
            out.append(ct)
        for i in sorted(self.tools):
            t = self.tools[i]
            out.append(_ev("content_block_delta", {"type": "content_block_delta",
                "index": t["aidx"],
                "delta": {"type": "input_json_delta",
                          "partial_json": t["args"] or "{}"}}))
            out.append(_ev("content_block_stop",
                           {"type": "content_block_stop", "index": t["aidx"]}))
        stop = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens",
                "content_filter": "end_turn"}.get(self.finish_reason, "end_turn")
        out.append(_ev("message_delta", {"type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {"output_tokens": self.usage_out}}))
        out.append(_ev("message_stop", {"type": "message_stop"}))
        return out


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "sl_proxy/1.0"

    def log_message(self, *a):
        pass  # quiet; the wrapper captures stderr to a log file

    # ---- helpers ----
    def _json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _upstream(self):
        return self.server.upstream

    def _write(self, s):
        try:
            self.wfile.write(s.encode("utf-8"))
            self.wfile.flush()
        except Exception:
            pass

    # ---- GET ----
    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        if path.startswith("/__upstream"):
            # /__upstream/<p> -> upstream GET /<p> with the key injected:
            # querying the llama server goes through the proxy, the only
            # component that holds the Salad-Api-Key. Upstream response
            # (status, content-type, body) is relayed as-is.
            self._upstream_passthrough(path)
        elif path in ("/healthz", "/health"):
            # The nonce is the wrapper's ownership probe: a second cl_salad session
            # on the same port gets THIS proxy's answer, which carries a different
            # nonce, so it can tell "my proxy bound" from "someone else's proxy".
            self._json(200, {"ok": True, "nonce": getattr(self.server, "nonce", "")})
        elif path.endswith("/models"):
            up = self._upstream()
            self._json(200, {"data": [{"id": up.model, "type": "model",
                                        "display_name": up.model, "created_at": 0}]})
        else:
            self._json(404, {"type": "error", "error": {"type": "not_found",
                       "message": "unknown path %s" % path}})

    def _upstream_passthrough(self, path):
        up_path = path[len("/__upstream"):] or "/"
        try:
            conn, resp = self._upstream().get(up_path)
        except Exception as e:
            self._json(502, {"type": "error", "error": {"type": "api_error",
                            "message": "upstream connect error: %s" % e}})
            return
        body = resp.read()
        conn.close()
        self.send_response(resp.status)
        self.send_header("Content-Type",
                         resp.getheader("Content-Type") or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    # ---- POST ----
    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        if not path.endswith("/messages"):
            self._json(404, {"type": "error", "error": {"type": "not_found",
                       "message": "unknown path %s" % path}})
            return
        try:
            self._handle_messages()
        except Exception as e:  # last-resort guard so a thread never dies silently
            self._json(500, {"type": "error", "error": {"type": "api_error",
                       "message": "proxy error: %s" % e}})

    def _handle_messages(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            self._json(400, {"type": "error", "error": {"type": "invalid_request_error",
                       "message": "request body is not valid JSON"}})
            return
        up = self._upstream()
        payload = anthropic_to_openai(body, up)
        stream = bool(body.get("stream"))

        try:
            conn, resp = up.request(payload, stream)
        except Exception as e:
            self._json(502, {"type": "error", "error": {"type": "api_error",
                       "message": "upstream connect error: %s" % e}})
            return

        if resp.status != 200:
            err_raw = resp.read()
            status = resp.status if resp.status >= 400 else 502
            self._json(status, {"type": "error", "error": {"type": "api_error",
                          "message": _err_msg(resp.status, err_raw)}})
            conn.close()
            return

        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            b = StreamBuilder(up.model, _rand_id())
            self._write(b.message_start())
            try:
                for chunk in _iter_sse(resp):
                    for frame in b.handle(chunk):
                        self._write(frame)
                for frame in b.finish():
                    self._write(frame)
            except Exception as e:
                self._write(_ev("error", {"type": "error",
                             "error": {"type": "api_error",
                                       "message": "stream error: %s" % e}}))
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
        else:
            data = resp.read()
            try:
                obj = json.loads(data)
            except Exception:
                self._json(502, {"type": "error", "error": {"type": "api_error",
                           "message": "upstream returned non-JSON"}})
                conn.close()
                return
            self._json(200, openai_to_anthropic(obj, up.model))
            conn.close()


def main():
    ap = argparse.ArgumentParser(description="Anthropic->OpenAI proxy for a Salad gateway")
    ap.add_argument("--port", type=int, default=8093)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--upstream", required=True,
                    help="full /v1/chat/completions URL, e.g. https://<host>/v1/chat/completions")
    ap.add_argument("--model", required=True, help="served model alias sent upstream")
    ap.add_argument("--keyfile", required=True, help="file containing the Salad-Api-Key")
    ap.add_argument("--thinking", default="0", help="1 to enable Qwen thinking (default off)")
    ap.add_argument("--max-tokens-cap", type=int, default=12000,
                    help="hard ceiling on output tokens forwarded upstream")
    ap.add_argument("--timeout", type=int, default=600, help="upstream read timeout (s)")
    ap.add_argument("--nonce", default="",
                    help="echoed by /healthz so the wrapper can verify it is talking to THIS "
                         "proxy (two sessions on one port otherwise look identical)")
    ap.add_argument("--allow-http-upstream", action="store_true",
                    help="testing only: accept a plain-HTTP loopback upstream "
                         "(fake gateways); real salad.cloud gateways stay https-only")
    a = ap.parse_args()

    up = Upstream(a.upstream, a.keyfile, a.model,
                  thinking=a.thinking == "1",
                  max_tokens_cap=a.max_tokens_cap, timeout=a.timeout,
                  allow_http=a.allow_http_upstream)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.upstream = up
    srv.nonce = a.nonce
    sys.stderr.write("sl_proxy: http://%s:%d -> %s (model=%s) pid=%d\n" %
                     (a.host, a.port, a.upstream, a.model, os.getpid()))
    sys.stderr.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
