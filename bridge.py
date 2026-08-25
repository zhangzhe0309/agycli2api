#!/usr/bin/env python3
"""OpenAI-format <-> Gemini format bridge for agycli2api.

Listens on port 3404, translates OpenAI /chat/completions requests into
Gemini native /v1beta/models/{model}:generateContent, forwards to agycli2api
on 3403, and converts responses back.

v2: Added SSE streaming support (streamGenerateContent).
v3: Full tool/function-calling support:
    - OpenAI tools  -> Gemini functionDeclarations
    - assistant tool_calls (history) -> Gemini functionCall parts
    - tool result messages -> Gemini functionResponse parts
    - Gemini functionCall response -> OpenAI tool_calls (stream + non-stream)
    Without this, Hermes sent tools that the bridge dropped, so Gemini never
    saw the tool schema and produced MALFORMED_FUNCTION_CALL (logged by Hermes
    as "Empty response" -> retry/fallback).
v3.1: Force maxOutputTokens=65535 — gemini-3.6 thinking budget INCLUDES thought
    tokens; mapping Hermes max_tokens directly starves visible output.
v4: Transport layer rewrite:
    - ThreadingHTTPServer: a streaming request no longer blocks all others.
    - Native http.client instead of curl subprocesses: no per-request fork,
      no stdin pipe deadlock on large bodies, socket timeout on every read.
    - Catch BrokenPipeError/ConnectionResetError: client disconnects during
      SSE no longer dump tracebacks (was flooding journald).
    - _forward now passes through the upstream status code (was always 200).
    - BRIDGE_DEBUG env gates diagnostic logging (was always on).
    - BRIDGE_PROXY_URL / BRIDGE_API_KEY / BRIDGE_UPSTREAM_TIMEOUT env config.
    - Malformed JSON bodies get a clean 400 instead of a crashed handler.
"""
import http.client
import json
import os
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PROXY_URL = os.environ.get("BRIDGE_PROXY_URL", "http://127.0.0.1:3403")
API_KEY = os.environ.get("BRIDGE_API_KEY", "hermes-agy-proxy-2026")
DEBUG = os.environ.get("BRIDGE_DEBUG", "").lower() in ("1", "true", "yes")
# Socket-level timeout per upstream read; for SSE this bounds the gap between
# events (long thinking pauses included), not the total request duration.
UPSTREAM_TIMEOUT = float(os.environ.get("BRIDGE_UPSTREAM_TIMEOUT", "300"))

_parsed_proxy = urlparse(PROXY_URL)
PROXY_HOST = _parsed_proxy.hostname or "127.0.0.1"
PROXY_PORT = _parsed_proxy.port or 80

# Map short model names to full model names supported by agycli2api/Antigravity.
_MODEL_MAP = {
    "gemini-3.7-flash": "gemini-3.7-flash-medium",
    "gemini-3.6-flash": "gemini-3.6-flash-medium",
    "gemini-2.5-flash": "gemini-2.5-flash",
    "gemini-2.5-pro": "gemini-2.5-pro",
}

def _resolve_model(name: str, reasoning_effort: str = None) -> str:
    """Resolve a model name + optional reasoning_effort to Antigravity model name."""
    if not name:
        return "gemini-3.7-flash-medium"
    if "/" in name:
        name = name.split("/")[-1]
    if name.endswith(("-low", "-medium", "-high", "-off")):
        return name
    effort = (reasoning_effort or "").strip().lower()
    if effort in ("low", "medium", "high"):
        if "3.7-flash" in name:
            return f"gemini-3.7-flash-{effort}"
        if "3.6-flash" in name:
            return f"gemini-3.6-flash-{effort}"
    return _MODEL_MAP.get(name, f"{name}-medium" if "flash" in name and ("3.7" in name or "3.6" in name) else name)

# JSON-schema keys Gemini's functionDeclarations accepts.
_SCHEMA_KEYS = ("type", "properties", "required", "items", "enum", "description",
                "format", "anyOf", "allOf", "minimum", "maximum", "default")


def _upstream_request(method, path, body=None, timeout=UPSTREAM_TIMEOUT):
    """Open an http.client connection to the local proxy and send a request.

    Returns (conn, response); the caller owns both and must conn.close().
    Raises OSError subclasses on connection/socket failures.
    """
    conn = http.client.HTTPConnection(PROXY_HOST, PROXY_PORT, timeout=timeout)
    try:
        conn.request(method, path, body=body,
                     headers={"Content-Type": "application/json"})
        return conn, conn.getresponse()
    except Exception:
        conn.close()
        raise


# v4.1: Google's daily-cloudcode-pa frontends intermittently reject datacenter
# IPs with "User location is not supported" (FAILED_PRECONDITION 400). Retrying
# after a short backoff lands on a different frontend and usually succeeds.
_LOCATION_RETRY_MAX = int(os.environ.get("BRIDGE_LOCATION_RETRY_MAX", "4"))
_LOCATION_BACKOFFS = [2.0, 5.0, 9.0, 15.0]  # seconds, cumulative ~31s worst case


def _is_location_block(resp) -> bool:
    """Peek at an error response without destroying the stream: True when the
    upstream 'User location is not supported' soft-block fired."""
    if resp.status != 400:
        return False
    try:
        raw = resp.read(4096)
    except OSError:
        return False
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return False
    msg = ""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            msg = str(err.get("message", "")) + str(err.get("status", ""))
        else:
            msg = str(err or "")
    return "location is not supported" in msg


def _upstream_request_with_location_retry(method, path, body=None,
                                          timeout=UPSTREAM_TIMEOUT):
    """Like _upstream_request but transparently retries location soft-blocks.

    Returns (conn, response) of the last attempt; all blocked attempts are
    closed. Raises OSError subclasses on connection/socket failures.
    """
    last_conn = None
    last_resp = None
    for attempt in range(_LOCATION_RETRY_MAX + 1):
        conn, resp = _upstream_request(method, path, body=body, timeout=timeout)
        if attempt == _LOCATION_RETRY_MAX or not _is_location_block(resp):
            return conn, resp
        # Soft-blocked: close and back off before hitting another frontend.
        try:
            resp.read()
        except OSError:
            pass
        conn.close()
        delay = _LOCATION_BACKOFFS[min(attempt, len(_LOCATION_BACKOFFS) - 1)]
        print(f"[bridge] location soft-block (attempt {attempt + 1}/"
              f"{_LOCATION_RETRY_MAX}), retrying in {delay:.0f}s",
              flush=True)
        time.sleep(delay)
    return last_conn, last_resp  # unreachable, keeps linters quiet


def _extract_text(candidates):
    """Extract non-thought text from Gemini candidates."""
    texts = []
    for cand in candidates or []:
        for part in cand.get("content", {}).get("parts", []):
            if "text" in part and not part.get("thought"):
                texts.append(part["text"])
    return "".join(texts)


def _convert_openai_tools_to_gemini(tools):
    """OpenAI tools -> Gemini functionDeclarations wrapper.

    Returns {"functionDeclarations": [...]} or None when no usable tool.
    """
    decls = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if t.get("type") == "function" else t
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        decl = {"name": fn["name"], "description": fn.get("description", "")}
        params = fn.get("parameters")
        if isinstance(params, dict):
            clean = {k: v for k, v in params.items() if k in _SCHEMA_KEYS}
            decl["parameters"] = clean or {"type": "object"}
        else:
            decl["parameters"] = {"type": "object"}
        decls.append(decl)
    return {"functionDeclarations": decls} if decls else None


def _tool_call_from_function_call(part, index):
    """Gemini part (functionCall + optional thoughtSignature) -> OpenAI tool_call.

    The thoughtSignature is carried in tool_call.extra_content (nested under
    'google') so Hermes replays it on the next turn — gemini-3 rejects
    functionCall parts missing thought_signature with HTTP 400.
    """
    if not isinstance(part, dict):
        return None
    fc = part.get("functionCall")
    if not isinstance(fc, dict):
        return None
    args = fc.get("args", {})
    try:
        args_str = json.dumps(args, ensure_ascii=False)
    except Exception:
        args_str = str(args)
    tc = {
        "index": index,
        "id": fc.get("id") or f"call_{uuid.uuid4().hex[:12]}",
        "type": "function",
        "function": {"name": fc.get("name", ""), "arguments": args_str},
    }
    sig = part.get("thoughtSignature")
    if isinstance(sig, str) and sig:
        tc["extra_content"] = {"google": {"thought_signature": sig}}
    return tc


def _extract_tool_calls(candidates):
    """All functionCall parts across candidates -> OpenAI tool_calls list."""
    calls = []
    for cand in candidates or []:
        for part in cand.get("content", {}).get("parts", []):
            if "functionCall" in part:
                tc = _tool_call_from_function_call(part, len(calls))
                if tc:
                    calls.append(tc)
    return calls


def _parse_args(arguments):
    """Best-effort parse of an OpenAI function.arguments (JSON string) -> dict."""
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        s = arguments.strip()
        if not s:
            return {}
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, dict) else {"_value": parsed}
        except Exception:
            return {"_raw": s}
    return {}


def _build_gemini_body(data):
    """Translate an OpenAI chat-completion body into Gemini's native schema."""
    contents = []
    system_text_parts = []

    # Collect tool_call id -> function name. Gemini's functionResponse REQUIRES
    # the real function name (matching a functionDeclaration); using the
    # OpenAI tool_call_id makes Gemini return an empty stream on multi-tool turns.
    id_to_name = {}
    for _msg in data.get("messages", []):
        if _msg.get("role") == "assistant" and isinstance(_msg.get("tool_calls"), list):
            for _tc in _msg["tool_calls"]:
                if isinstance(_tc, dict) and _tc.get("id"):
                    _fn = _tc.get("function", {})
                    if isinstance(_fn, dict) and _fn.get("name"):
                        id_to_name[_tc["id"]] = _fn["name"]

    for msg in data.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content")

        # System -> Gemini systemInstruction (collected, emitted once below)
        if role == "system":
            if isinstance(content, list):
                txt = " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("text"))
            else:
                txt = str(content)
            system_text_parts.append(txt)
            continue

        # assistant tool_calls (prior turns) -> model functionCall parts
        if role == "assistant" and msg.get("tool_calls"):
            parts = []
            if content:
                parts.append({"text": content if isinstance(content, str) else str(content)})
            for tc in msg["tool_calls"]:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function", {}) if isinstance(tc.get("function"), dict) else {}
                fc = {"name": fn.get("name", ""), "args": _parse_args(fn.get("arguments"))}
                if tc.get("id"):
                    fc["id"] = tc["id"]
                part = {"functionCall": fc}
                # Replay gemini-3 thought_signature (carried in extra_content)
                # so the API does not reject the functionCall with HTTP 400.
                extra = tc.get("extra_content")
                if isinstance(extra, dict):
                    _google = extra.get("google")
                    _sig = None
                    if isinstance(_google, dict):
                        _sig = _google.get("thought_signature") or _google.get("thoughtSignature")
                    elif isinstance(_google, str):
                        _sig = _google
                    if not _sig:
                        _sig = extra.get("thought_signature")
                    if isinstance(_sig, str) and _sig:
                        part["thoughtSignature"] = _sig
                parts.append(part)
            contents.append({"role": "model", "parts": parts})
            continue

        # tool result -> functionResponse (Gemini requires the function name,
        # which must match a functionDeclaration; map id -> name)
        if role == "tool":
            _tc_id = msg.get("tool_call_id")
            name = msg.get("name") or id_to_name.get(_tc_id) or _tc_id or "tool"
            result = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            contents.append({
                "role": "user",
                "parts": [{"functionResponse": {"name": name, "response": {"result": result}}}],
            })
            continue

        g_role = "model" if role == "assistant" else "user"

        # Multimodal content list
        if isinstance(content, list):
            parts = []
            for p in content:
                if not isinstance(p, dict):
                    continue
                ptype = p.get("type")
                if ptype == "text":
                    parts.append({"text": p.get("text", "")})
                elif ptype == "image_url":
                    url = (p.get("image_url") or {}).get("url", "")
                    if url.startswith("data:"):
                        header, _, data_b64 = url.partition(",")
                        try:
                            mime_type = header.split(";")[1] if ";" in header else "image/png"
                        except Exception:
                            mime_type = "image/png"
                        parts.append({"inline_data": {"mime_type": mime_type, "data": data_b64}})
                    else:
                        parts.append({"file_url": url})
            if parts:
                contents.append({"role": g_role, "parts": parts})
            else:
                contents.append({"role": g_role, "parts": [{"text": ""}]})
        else:
            contents.append({"role": g_role, "parts": [{"text": "" if content is None else str(content)}]})

    gemini_body = {"contents": contents}

    if system_text_parts:
        gemini_body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_text_parts)}]}

    # generationConfig: force a large output ceiling so thinking cannot starve
    # the visible response (gemini-3.6 maxOutputTokens includes thought tokens).
    gen_config = {"maxOutputTokens": 65535}
    temperature = data.get("temperature")
    if temperature is not None:
        gen_config["temperature"] = temperature
    top_p = data.get("top_p")
    if top_p is not None:
        gen_config["topP"] = top_p
    gemini_body["generationConfig"] = gen_config

    # Tool schema passthrough (the core fix).
    tools = _convert_openai_tools_to_gemini(data.get("tools"))
    if tools:
        gemini_body["tools"] = [tools]
        gemini_body["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}

    return gemini_body


def _build_openai_chunk(model, text="", finish_reason=None, tool_calls=None, index=0):
    """Build an OpenAI SSE data chunk."""
    delta = {}
    if text:
        delta["content"] = text
    if tool_calls:
        delta["tool_calls"] = tool_calls
    chunk = {
        "id": f"chatcmpl-{int(time.time() * 1000)}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": index,
            "delta": delta,
            "finish_reason": finish_reason,
        }],
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def _parse_gemini_sse_line(line):
    """Parse a single SSE line from Gemini's streamGenerateContent."""
    line = line.strip()
    if not line or line.startswith(":"):
        return None
    if not line.startswith("data: "):
        return None
    payload = line[6:].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


def _dbg(msg):
    """Diagnostic to stderr (visible via: journalctl -u hermes-gemini-bridge).

    Gated by BRIDGE_DEBUG=1 to keep journald quiet in production.
    """
    if not DEBUG:
        return
    try:
        sys.stderr.write(f"[BRIDGE_DBG] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


class BridgeHandler(BaseHTTPRequestHandler):

    @staticmethod
    def _add_key(path):
        if "key=" in path or path.split("?")[0].rstrip("/") in ("/v1/models", "/models"):
            return path
        sep = "&" if "?" in path else "?"
        return f"{path}{sep}key={API_KEY}"

    def _send_json(self, status, payload):
        """Send a JSON (or raw passthrough) response with Content-Length."""
        raw = payload if isinstance(payload, (bytes, bytearray)) else \
            json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _client_gone(self, err):
        """True when the error means the client disconnected mid-stream."""
        return isinstance(err, (BrokenPipeError, ConnectionResetError,
                                ConnectionAbortedError))

    def do_GET(self):
        if self.path.split("?")[0].rstrip("/") in ("/v1/models", "/models"):
            self._list_models_openai()
            return
        self._forward(self.path, "GET")

    def _list_models_openai(self):
        """Translate native /v1beta/models into OpenAI GET /v1/models."""
        try:
            conn, resp = _upstream_request("GET", f"/v1beta/models?key={API_KEY}")
            raw = json.loads(resp.read())
        except (OSError, ValueError) as e:
            self._send_json(502, {"error": str(e)})
            return
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass
        models = [
            {"id": m.get("name", "").removeprefix("models/"), "object": "model",
             "owned_by": "agycli2api"}
            for m in raw.get("models", [])
        ]
        self._send_json(200, {"object": "list", "data": models})

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        parsed_path = self.path.split("?")[0]
        # Pass through native Gemini paths directly.
        if parsed_path.startswith("/v1beta/models") or "/generateContent" in self.path:
            self._forward(self.path, "POST", body)
            return

        if parsed_path in ("/chat/completions", "/v1/chat/completions"):
            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError as e:
                self._send_json(400, {"error": f"invalid JSON body: {e}"})
                return
            model = data.get("model", "gemini-3.6-flash-medium")
            is_stream = data.get("stream", False)

            _dbg(f"req model={model} stream={is_stream} max_tokens={data.get('max_tokens')} "
                 f"nmsgs={len(data.get('messages', []))} ntools={len(data.get('tools', []))}")
            # diagnostic: message role/shape sequence
            _roles = []
            for m in data.get("messages", []):
                r = m.get("role", "?")
                if r == "tool":
                    c = m.get("content", "")
                    _roles.append(f"tool(len={len(c) if isinstance(c, str) else 'obj'})")
                elif r == "assistant" and m.get("tool_calls"):
                    _roles.append("asst.tc")
                elif r == "assistant":
                    _roles.append("asst.txt")
                else:
                    _roles.append(r)
            _dbg(f"  roles: {' | '.join(_roles)}")

            gemini_body = _build_gemini_body(data)
            req_data = json.dumps(gemini_body).encode()
            effort = data.get("reasoning_effort")

            if is_stream:
                self._handle_streaming(model, req_data, effort=effort)
            else:
                self._handle_non_streaming(model, req_data, effort=effort)
        else:
            self._forward(self.path, "POST", body)

    def _handle_non_streaming(self, model, req_data, effort=None):
        """Non-streaming: call :generateContent, return full OpenAI JSON."""
        resolved = _resolve_model(model, effort)
        path = f"/v1beta/models/{resolved}:generateContent?key={API_KEY}"
        try:
            conn, resp = _upstream_request_with_location_retry("POST", path, body=req_data)
        except OSError as e:
            self._send_json(502, {"error": f"upstream connection failed: {e}"})
            return

        try:
            raw = resp.read()
            status = resp.status
        except OSError as e:
            self._send_json(502, {"error": f"upstream read failed: {e}"})
            return
        finally:
            conn.close()

        try:
            resp_data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            self._send_json(502, {"error": "upstream returned non-JSON",
                                  "raw": raw[:500].decode("utf-8", "replace")})
            return

        if status != 200:
            # Pass the upstream error through with its real status code.
            self._send_json(status, resp_data if isinstance(resp_data, dict) else raw)
            return

        if "candidates" in resp_data:
            text = _extract_text(resp_data["candidates"])
            tool_calls = _extract_tool_calls(resp_data["candidates"])
            cand0 = resp_data["candidates"][0] if resp_data else {}
            fr = cand0.get("finishReason")

            if tool_calls:
                finish_reason = "tool_calls"
            elif fr == "MAX_TOKENS":
                finish_reason = "length"
            elif fr in ("SAFETY", "RECITATION"):
                finish_reason = "content_filter"
            else:
                finish_reason = "stop"

            _dbg(f"resp finish={finish_reason} text_len={len(text)} tool_calls={len(tool_calls)} upstream_fr={fr}")

            message = {"role": "assistant", "content": text if text else None}
            if tool_calls:
                message["tool_calls"] = tool_calls

            usage = resp_data.get("usageMetadata", {})
            openai_resp = {
                "id": f"chatcmpl-{int(time.time())}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }],
                "usage": {
                    "prompt_tokens": usage.get("promptTokenCount", 0),
                    "completion_tokens": usage.get("candidatesTokenCount", 0),
                    "total_tokens": usage.get("totalTokenCount", 0),
                },
            }
            self._send_json(200, openai_resp)
        else:
            status_code = resp_data.get("error", {}).get("code", 500) or 500
            self._send_json(status_code if isinstance(status_code, int) else 500, resp_data)

    def _handle_streaming(self, model, req_data, effort=None):
        """Streaming: call :streamGenerateContent, translate SSE events."""
        resolved = _resolve_model(model, effort)
        path = f"/v1beta/models/{resolved}:streamGenerateContent?key={API_KEY}&alt=sse"
        try:
            conn, resp = _upstream_request_with_location_retry("POST", path, body=req_data)
        except OSError as e:
            self._send_json(502, {"error": f"upstream connection failed: {e}"})
            return

        if resp.status != 200:
            raw = resp.read()
            conn.close()
            ct = resp.getheader("Content-Type", "application/json")
            self.send_response(resp.status)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        acc_text = ""
        finish_reason = None
        saw_tool_call = False
        _ev_log = []

        try:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace")
                gemini_event = _parse_gemini_sse_line(line)
                if gemini_event is None:
                    continue
                candidates = gemini_event.get("candidates", [])
                if not candidates:
                    continue

                parts = candidates[0].get("content", {}).get("parts", [])
                _ev_log.append(f"{candidates[0].get('finishReason') or '-'}/"
                               f"{'+'.join(sorted(set(k for p in parts for k in p.keys())))}")
                for part in parts:
                    # Function call -> OpenAI tool_calls delta
                    if "functionCall" in part:
                        tc = _tool_call_from_function_call(part, 0)
                        if tc:
                            chunk = _build_openai_chunk(model, tool_calls=[tc])
                            self.wfile.write(chunk.encode())
                            self.wfile.flush()
                            saw_tool_call = True
                        continue
                    # Visible text delta (skip pure-thought chunks)
                    if "text" in part and not part.get("thought"):
                        delta = part["text"]
                        if not delta:
                            continue
                        # Gemini sends cumulative text; emit only the new suffix.
                        if delta.startswith(acc_text):
                            new_text = delta[len(acc_text):]
                            if new_text:
                                self.wfile.write(_build_openai_chunk(model, new_text).encode())
                                self.wfile.flush()
                            acc_text = delta
                        else:
                            self.wfile.write(_build_openai_chunk(model, delta).encode())
                            self.wfile.flush()
                            acc_text = delta

                fr = candidates[0].get("finishReason")
                if fr and fr != "FINISH_REASON_UNSPECIFIED":
                    _finish_map = {
                        "STOP": "tool_calls" if saw_tool_call else "stop",
                        "MAX_TOKENS": "length",
                        "SAFETY": "content_filter",
                        "RECITATION": "content_filter",
                        "MALFORMED_FUNCTION_CALL": "stop",  # let caller retry; emit as stop
                    }
                    finish_reason = _finish_map.get(fr, "stop")

            _dbg(f"stream done finish={finish_reason} text_len={len(acc_text)} tool_call={saw_tool_call} events={_ev_log}")

            # Final chunk carries the finish_reason.
            self.wfile.write(_build_openai_chunk(model, "", finish_reason=finish_reason).encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Client hung up mid-stream: abort quietly, drop the upstream too.
            _dbg(f"client disconnected mid-stream after {len(acc_text)} chars")
        except OSError as e:
            # Upstream read failure (timeout / reset): end the SSE stream so
            # the client can retry instead of hanging forever.
            _dbg(f"upstream stream error: {e!r}")
            try:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except OSError:
                pass
        finally:
            conn.close()
            self.close_connection = True

    def _forward(self, path, method, body=None):
        conn = None
        try:
            path = self._add_key(path)
            conn, resp = _upstream_request(method, path, body=body)
            raw = resp.read()
            status = resp.status
            ct = resp.getheader("Content-Type", "application/json")
        except OSError as e:
            self._send_json(502, {"error": str(e)})
            return
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

        self.send_response(status)
        if ct:
            self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format, *args):
        pass  # suppress default access log


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError,
                            ConnectionAbortedError)):
            return  # routine client disconnects, not worth a traceback
        sys.stderr.write(f"[BRIDGE] error handling {client_address}: {exc!r}\n")


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 3404
    server = BridgeServer(("127.0.0.1", port), BridgeHandler)
    print(f"Bridge running on :{port} → proxy on {PROXY_URL} "
          f"(v4, threaded, tools+stream, debug={'on' if DEBUG else 'off'})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
