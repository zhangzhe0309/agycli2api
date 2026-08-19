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
"""
import json
import sys
import subprocess
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler

PROXY_URL = "http://127.0.0.1:3403"
API_KEY = "hermes-agy-proxy-2026"

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
    """Diagnostic to stderr (visible via: journalctl -u hermes-gemini-bridge)."""
    try:
        sys.stderr.write(f"[BRIDGE_DBG] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


class BridgeHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self._forward(PROXY_URL + self.path, "GET")

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        parsed_path = self.path.split("?")[0]
        # Pass through native Gemini paths directly.
        if parsed_path.startswith("/v1beta/models") or "/generateContent" in self.path:
            self._forward(PROXY_URL + self.path, "POST", body)
            return

        if parsed_path in ("/chat/completions", "/v1/chat/completions"):
            data = json.loads(body) if body else {}
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
            self._forward(PROXY_URL + self.path, "POST", body)

    def _handle_non_streaming(self, model, req_data, effort=None):
        """Non-streaming: call :generateContent, return full OpenAI JSON."""
        resolved = _resolve_model(model, effort)
        target = f"{PROXY_URL}/v1beta/models/{resolved}:generateContent?key={API_KEY}"
        proc = subprocess.run(
            ["curl", "-s", "-X", "POST", target,
             "-H", "Content-Type: application/json",
             "--data-binary", "@-"],
            input=req_data, capture_output=True, timeout=120,
        )
        try:
            resp_data = json.loads(proc.stdout) if proc.stdout else {}
        except json.JSONDecodeError:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "upstream returned non-JSON",
                                         "raw": proc.stdout[:500].decode("utf-8", "replace")}).encode())
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
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(openai_resp, ensure_ascii=False).encode())
        else:
            status_code = resp_data.get("error", {}).get("code", 500) or 500
            self.send_response(status_code if isinstance(status_code, int) else 500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(proc.stdout)

    def _handle_streaming(self, model, req_data, effort=None):
        """Streaming: call :streamGenerateContent, translate SSE events."""
        resolved = _resolve_model(model, effort)
        target = f"{PROXY_URL}/v1beta/models/{resolved}:streamGenerateContent?key={API_KEY}&alt=sse"
        proc = subprocess.Popen(
            ["curl", "-s", "-N", "-X", "POST", target,
             "-H", "Content-Type: application/json",
             "--data-binary", "@-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        proc.stdin.write(req_data)
        proc.stdin.close()

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        acc_text = ""
        finish_reason = None
        saw_tool_call = False
        _ev_log = []

        for raw_line in proc.stdout:
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

        proc.wait()
        self.close_connection = True

    def _forward(self, url, method, body=None):
        cmd = ["curl", "-s", "-X", method, url, "-H", "Content-Type: application/json"]
        auth_header = self.headers.get("Authorization")
        if auth_header:
            cmd += ["-H", f"Authorization: {auth_header}"]
        try:
            if body:
                proc = subprocess.run(cmd, input=body, capture_output=True, timeout=120)
            else:
                proc = subprocess.run(cmd, capture_output=True, timeout=120)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(proc.stdout)
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, format, *args):
        pass  # suppress default access log


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 3404
    server = HTTPServer(("127.0.0.1", port), BridgeHandler)
    print(f"Bridge running on :{port} → proxy on {PROXY_URL} (v3.1, tools+stream)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
