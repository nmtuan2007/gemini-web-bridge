"""Gemini StreamGenerate protocol implementation with httpx streaming."""
import base64
import json
import threading
import time
import uuid
import re
import urllib.request
import urllib.parse
import ssl
import os
import hashlib
from collections import deque

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

from .config import CONFIG, get_config_path, save_config

_ssl_ctx = None
_cookie_cache = {"str": "", "sapisid": None, "mtime": 0}
_cookie_lock = threading.RLock()
_session_state = {"authenticated": None, "last_refresh_at": None, "last_error": None}
_refresher_started = False
_httpx_client = None
_log_lock = threading.Lock()
_log_buffer = deque(maxlen=200)
_telemetry_lock = threading.Lock()
_telemetry_state = {
    "started_at": int(time.time()),
    "seq": 0,
    "total_requests": 0,
    "active_requests": 0,
    "success_requests": 0,
    "error_requests": 0,
    "tool_calls_emitted": 0,
    "total_latency_ms": 0,
    "last_request": None,
    "recent_events": deque(maxlen=30),
}
CLIENT_CAPABILITIES = (4, 33, 22, 5, 6, 8, 16, 19, 20, 38, 41)


def _encode_varint(value: int) -> bytes:
    """Encode a non-negative integer as a protobuf varint."""
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _pb_varint(field_num: int, value: int) -> bytes:
    """Encode a protobuf varint field."""
    return _encode_varint((field_num << 3) | 0) + _encode_varint(value)


def _pb_bytes(field_num: int, data: bytes) -> bytes:
    """Encode a protobuf length-delimited field."""
    return _encode_varint((field_num << 3) | 2) + _encode_varint(len(data)) + data


def _build_mode_ext_header(model_id: int, think_mode: int) -> str:
    """Build base64-encoded x-goog-ext-525001261-bin header for Gemini 4 mode routing."""
    parts = [_pb_varint(1, 1)]
    parts.extend(_pb_varint(9, cap) for cap in CLIENT_CAPABILITIES)
    parts.append(_pb_varint(15, model_id))
    if think_mode:
        parts.append(_pb_varint(16, think_mode))
    bl = CONFIG.get("gemini_bl")
    if bl:
        parts.append(_pb_bytes(18, _pb_bytes(1, bl.encode("utf-8"))))
    return base64.b64encode(b"".join(parts)).decode("ascii")


def log(msg: str):
    entry = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with _log_lock:
        _log_buffer.append(entry)
    if CONFIG["log_requests"]:
        import sys
        sys.stderr.write(f"{entry}\n")
        sys.stderr.flush()


def get_recent_logs(limit: int = 80) -> list:
    """Return recent formatted log lines from the in-memory ring buffer."""
    bounded = max(1, min(int(limit or 80), 200))
    with _log_lock:
        lines = list(_log_buffer)
    return lines[-bounded:]


def begin_request(endpoint: str, model: str, stream: bool = False, has_tools: bool = False) -> dict:
    """Record the start of an API generation request for telemetry."""
    with _telemetry_lock:
        _telemetry_state["seq"] += 1
        _telemetry_state["total_requests"] += 1
        _telemetry_state["active_requests"] += 1
        return {
            "id": _telemetry_state["seq"],
            "at": int(time.time()),
            "started_perf": time.perf_counter(),
            "endpoint": endpoint,
            "model": model,
            "stream": bool(stream),
            "has_tools": bool(has_tools),
        }


def finish_request(
    token: dict,
    status: int = 200,
    ok: bool = True,
    tool_calls_count: int = 0,
    prompt_chars: int = 0,
    response_chars: int = 0,
    error: str = None,
) -> None:
    """Record completion metrics and append a secret-free activity entry."""
    if not token:
        return
    duration_ms = max(0, int((time.perf_counter() - token["started_perf"]) * 1000))
    event = {
        "id": token["id"],
        "at": token["at"],
        "endpoint": token["endpoint"],
        "model": token["model"],
        "stream": token["stream"],
        "has_tools": token["has_tools"],
        "status": status,
        "ok": bool(ok),
        "duration_ms": duration_ms,
        "prompt_chars": int(prompt_chars or 0),
        "response_chars": int(response_chars or 0),
        "tool_calls": int(tool_calls_count or 0),
        "error": (str(error)[:220] if error else None),
    }
    with _telemetry_lock:
        _telemetry_state["active_requests"] = max(0, _telemetry_state["active_requests"] - 1)
        if ok:
            _telemetry_state["success_requests"] += 1
        else:
            _telemetry_state["error_requests"] += 1
        _telemetry_state["tool_calls_emitted"] += event["tool_calls"]
        _telemetry_state["total_latency_ms"] += duration_ms
        _telemetry_state["last_request"] = event
        _telemetry_state["recent_events"].appendleft(event)


def get_telemetry() -> dict:
    """Return server uptime, counters, latency average, and recent request events."""
    with _telemetry_lock:
        finished = _telemetry_state["success_requests"] + _telemetry_state["error_requests"]
        avg_ms = (_telemetry_state["total_latency_ms"] // finished) if finished > 0 else 0
        return {
            "started_at": _telemetry_state["started_at"],
            "uptime_sec": max(0, int(time.time()) - _telemetry_state["started_at"]),
            "total_requests": _telemetry_state["total_requests"],
            "active_requests": _telemetry_state["active_requests"],
            "success_requests": _telemetry_state["success_requests"],
            "error_requests": _telemetry_state["error_requests"],
            "tool_calls_emitted": _telemetry_state["tool_calls_emitted"],
            "avg_latency_ms": avg_ms,
            "last_request": _telemetry_state["last_request"],
            "recent_events": list(_telemetry_state["recent_events"]),
        }


def clear_telemetry() -> dict:
    """Clear recent activity events and counters while preserving server start time."""
    with _telemetry_lock:
        active = _telemetry_state["active_requests"]
        _telemetry_state["total_requests"] = active
        _telemetry_state["success_requests"] = 0
        _telemetry_state["error_requests"] = 0
        _telemetry_state["tool_calls_emitted"] = 0
        _telemetry_state["total_latency_ms"] = 0
        _telemetry_state["last_request"] = None
        _telemetry_state["recent_events"].clear()
    return get_telemetry()


def _get_ssl_ctx():
    global _ssl_ctx
    if _ssl_ctx is None:
        _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


_httpx_lock = threading.Lock()


def _create_httpx_client():
    """Create an isolated httpx.Client configured with proxy and timeout."""
    if not HAS_HTTPX:
        return None
    proxy = CONFIG.get("proxy")
    transport = httpx.HTTPTransport(proxy=proxy) if proxy else None
    return httpx.Client(transport=transport, timeout=CONFIG["request_timeout_sec"], verify=True)


def _get_httpx_client():
    global _httpx_client
    with _httpx_lock:
        if _httpx_client is None and HAS_HTTPX:
            _httpx_client = _create_httpx_client()
        return _httpx_client


def _parse_cookie_pairs(cookie_str: str) -> dict:
    """Parse a raw Cookie header string into an ordered dict of name -> value."""
    pairs = {}
    for part in (cookie_str or "").split(";"):
        if "=" not in part:
            continue
        k, v = part.strip().split("=", 1)
        k = k.strip()
        if k:
            pairs[k] = v.strip()
    return pairs


def _format_cookie_pairs(pairs: dict) -> str:
    """Format cookie pairs into a Cookie header string."""
    return "; ".join(f"{k}={v}" for k, v in pairs.items())


def _default_cookie_file() -> str:
    """Resolve default cookie storage path alongside config.json when unset."""
    cfg_path = get_config_path()
    base_dir = os.path.dirname(cfg_path) if cfg_path else os.getcwd()
    return os.path.join(base_dir, "cookie.txt")


def _write_cookie_store(
    cookie_str: str,
    sapisid: str = None,
    auth_user=None,
    xsrf_token: str = None,
    gemini_bl: str = None,
) -> str:
    """Write cookie store (.txt or .json) and refresh the mtime cache."""
    with _cookie_lock:
        cookie_file = CONFIG.get("cookie_file") or _default_cookie_file()
        CONFIG["cookie_file"] = cookie_file
        os.makedirs(os.path.dirname(os.path.abspath(cookie_file)), exist_ok=True)
        pairs = _parse_cookie_pairs(cookie_str)
        resolved_sapisid = sapisid or pairs.get("SAPISID", "")
        if cookie_file.lower().endswith(".json"):
            payload = {}
            if os.path.exists(cookie_file):
                try:
                    with open(cookie_file, "r", encoding="utf-8") as f:
                        payload = json.load(f)
                except (OSError, json.JSONDecodeError):
                    payload = {}
            payload["cookie"] = cookie_str
            payload["sapisid"] = resolved_sapisid
            if auth_user is not None or "auth_user" not in payload:
                payload["auth_user"] = auth_user
            if xsrf_token:
                payload["xsrf_token"] = xsrf_token
            if gemini_bl:
                payload["gemini_bl"] = gemini_bl
            with open(cookie_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.write("\n")
        else:
            with open(cookie_file, "w", encoding="utf-8") as f:
                f.write(cookie_str)
        mtime = os.path.getmtime(cookie_file)
        _cookie_cache.update({"str": cookie_str, "sapisid": resolved_sapisid or None, "mtime": mtime})
        return cookie_file


def _merge_set_cookies(set_cookie_headers: list) -> bool:
    """Merge Set-Cookie response headers into the configured cookie store when changed."""
    if not set_cookie_headers or not CONFIG.get("cookie_file"):
        return False
    with _cookie_lock:
        cookie_str, _ = load_cookie()
        if not cookie_str:
            return False
        pairs = _parse_cookie_pairs(cookie_str)
        changed = False
        for header in set_cookie_headers:
            first = (header or "").split(";", 1)[0].strip()
            if "=" not in first:
                continue
            name, value = first.split("=", 1)
            name = name.strip()
            value = value.strip()
            if not name or not value or value.lower() == "deleted":
                continue
            if pairs.get(name) != value:
                pairs[name] = value
                changed = True
        if not changed:
            return False
        _write_cookie_store(
            _format_cookie_pairs(pairs),
            sapisid=pairs.get("SAPISID"),
            auth_user=CONFIG.get("auth_user"),
            xsrf_token=CONFIG.get("xsrf_token"),
            gemini_bl=CONFIG.get("gemini_bl"),
        )
        return True


def apply_auth_bundle(
    cookie_str: str,
    sapisid: str = None,
    auth_user=None,
    xsrf_token: str = None,
    gemini_bl: str = None,
    persist_config: bool = True,
) -> dict:
    """Apply a fresh cookie/XSRF bundle to runtime config and disk without restarting."""
    cleaned = (cookie_str or "").strip()
    if cleaned.lower().startswith("cookie:"):
        cleaned = cleaned.split(":", 1)[1].strip()
    pairs = _parse_cookie_pairs(cleaned)
    resolved_sapisid = sapisid or pairs.get("SAPISID", "")
    with _cookie_lock:
        cookie_file = _write_cookie_store(
            cleaned,
            sapisid=resolved_sapisid,
            auth_user=auth_user,
            xsrf_token=xsrf_token,
            gemini_bl=gemini_bl,
        )
        CONFIG["auth_user"] = auth_user if auth_user != "" else None
        if xsrf_token:
            CONFIG["xsrf_token"] = xsrf_token
        if gemini_bl:
            CONFIG["gemini_bl"] = gemini_bl
        if persist_config:
            save_config()
    return {
        "cookie_file": cookie_file,
        "has_sid": "SID" in pairs,
        "has_sapisid": bool(resolved_sapisid),
        "has_xsrf": bool(CONFIG.get("xsrf_token")),
    }


def load_cookie() -> tuple:
    """Load cookie from file with mtime-based caching."""
    cookie_file = CONFIG.get("cookie_file")
    if not cookie_file or not os.path.exists(cookie_file):
        return "", None
    with _cookie_lock:
        try:
            mtime = os.path.getmtime(cookie_file)
            if mtime == _cookie_cache["mtime"] and _cookie_cache["str"]:
                return _cookie_cache["str"], _cookie_cache["sapisid"]
            with open(cookie_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content.startswith("{"):
                data = json.loads(content)
                cookie_str = data.get("cookie", "")
                sapisid = data.get("sapisid", "")
            else:
                cookie_str = content
                pairs = _parse_cookie_pairs(cookie_str)
                sapisid = pairs.get("SAPISID", "")
            _cookie_cache.update({"str": cookie_str, "sapisid": sapisid or None, "mtime": mtime})
            return cookie_str, sapisid if sapisid else None
        except Exception as e:
            log(f"Cookie load error: {e}")
            return _cookie_cache["str"], _cookie_cache["sapisid"]


def make_sapisidhash(sapisid: str) -> str:
    ts = int(time.time())
    h = hashlib.sha1(f"{ts} {sapisid} https://gemini.google.com".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h}"


def _account_prefix() -> str:
    """Return the Gemini account path prefix for non-default Google accounts."""
    auth_user = CONFIG.get("auth_user")
    if auth_user is None or auth_user == "":
        return ""
    return f"/u/{auth_user}"


def _build_headers(model_id: int = None, think_mode: int = 0) -> dict:
    account_prefix = _account_prefix()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://gemini.google.com",
        "Referer": f"https://gemini.google.com{account_prefix}/app",
        "X-Same-Domain": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if account_prefix:
        headers["X-Goog-AuthUser"] = str(CONFIG["auth_user"])
    if model_id is not None:
        headers["x-goog-ext-525001261-bin"] = _build_mode_ext_header(model_id, think_mode)
    cookie_str, sapisid = load_cookie()
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)
    return headers


def _apply_chat_persistence_flags(inner: list) -> None:
    """Apply Gemini Web persistence flags to an outgoing request payload."""
    if CONFIG.get("temporary_chats", False):
        # Match Gemini Web temporary-chat requests.
        inner[41] = [1]
        inner[45] = 1
    else:
        inner[41] = [2]


def _build_payload(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None) -> str:
    inner = [None] * 102
    if file_refs:
        refs = [[None, None, ref] for ref in file_refs]
        inner[0] = [prompt, 0, None, refs, None, None, 0]
    else:
        inner[0] = [prompt, 0, None, None, None, None, 0]
    inner[1] = ["en"]
    inner[2] = ["", "", "", None, None, None, None, None, None, ""]
    inner[6] = [0]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[think_mode]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = list(CLIENT_CAPABILITIES)
    _apply_chat_persistence_flags(inner)
    inner[53] = 0
    inner[59] = str(uuid.uuid4())
    inner[61] = []
    inner[68] = 1
    inner[79] = model_id
    if think_mode:
        inner[80] = think_mode
    if extra_fields:
        for k, v in extra_fields.items():
            inner[k] = v
    outer = [None, json.dumps(inner)]
    params = {"f.req": json.dumps(outer)}
    if CONFIG.get("xsrf_token"):
        params["at"] = CONFIG["xsrf_token"]
    return urllib.parse.urlencode(params)


def _get_url() -> str:
    reqid = int(time.time()) % 1000000
    account_prefix = _account_prefix()
    return (
        f"https://gemini.google.com{account_prefix}/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
    )


def clean_text(text: str, strip: bool = True) -> str:
    text = re.sub(
        r'```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+\n.*?```\n?',
        '', text, flags=re.DOTALL
    )
    text = re.sub(r'http://googleusercontent\.com/card_content/\d+\n?', '', text)
    return text.strip() if strip else text


def _extract_texts_from_line(line: str) -> list:
    """Parse a single wrb.fr line and return list of text strings found."""
    if '"wrb.fr"' not in line or len(line) < 200:
        return []
    try:
        arr = json.loads(line)
        inner_str = arr[0][2]
        if not inner_str or len(inner_str) < 50:
            return []
        inner = json.loads(inner_str)
        if not (isinstance(inner, list) and len(inner) > 4 and inner[4]):
            return []
        texts = []
        for part in inner[4]:
            if isinstance(part, list) and len(part) > 1 and part[1] and isinstance(part[1], list):
                for t in part[1]:
                    if isinstance(t, str) and t:
                        texts.append(t)
        return texts
    except (json.JSONDecodeError, IndexError, TypeError):
        return []


def extract_response_text(raw: str) -> str:
    """Parse full response to get final text."""
    bard_err = re.search(r'BardErrorInfo\s*\[(\d+)\]', raw)
    if bard_err:
        raise RuntimeError(f"Gemini upstream rejected request: BardErrorInfo [{bard_err.group(1)}]")
    last_text = ""
    for line in raw.split("\n"):
        for t in _extract_texts_from_line(line):
            if len(t) > len(last_text):
                last_text = t
    return clean_text(last_text)


def _collect_urllib_set_cookies(resp) -> list:
    headers = getattr(resp, "headers", None)
    if not headers:
        return []
    if hasattr(headers, "get_all"):
        return headers.get_all("Set-Cookie") or []
    val = headers.get("Set-Cookie")
    return [val] if val else []


def _collect_httpx_set_cookies(resp) -> list:
    out = []
    for item in list(getattr(resp, "history", [])) + [resp]:
        headers = getattr(item, "headers", None)
        if headers and hasattr(headers, "get_list"):
            out.extend(headers.get_list("set-cookie"))
    return out


def refresh_page_session(timeout: int = 20) -> dict:
    """Refresh Gemini page tokens (SNlM0e, cfb2h) and persist any Set-Cookie updates."""
    cookie_str, sapisid = load_cookie()
    pairs = _parse_cookie_pairs(cookie_str)
    base_status = {
        "ok": False,
        "authenticated": False,
        "has_snlm0e": False,
        "has_opep7c": False,
        "has_s06grb": False,
        "has_sid": "SID" in pairs,
        "has_sapisid": bool(sapisid),
        "gemini_bl": CONFIG.get("gemini_bl"),
        "cookie_file": CONFIG.get("cookie_file"),
    }
    if not cookie_str:
        _session_state.update({
            "authenticated": False,
            "last_refresh_at": int(time.time()),
            "last_error": "no cookie configured",
        })
        base_status["reason"] = "no_cookie"
        return base_status

    account_prefix = _account_prefix()
    url = f"https://gemini.google.com{account_prefix}/app"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Cookie": cookie_str,
    }
    proxy = CONFIG.get("proxy")
    try:
        set_cookies = []
        if HAS_HTTPX:
            transport = httpx.HTTPTransport(proxy=proxy) if proxy else None
            with httpx.Client(transport=transport, timeout=timeout, verify=True, follow_redirects=True) as client:
                resp = client.get(url, headers=headers)
                html = resp.text
                set_cookies = _collect_httpx_set_cookies(resp)
        else:
            req = urllib.request.Request(url, headers=headers, method="GET")
            ctx = _get_ssl_ctx()
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                    urllib.request.HTTPSHandler(context=ctx),
                )
                resp = opener.open(req, timeout=timeout)
            else:
                resp = urllib.request.urlopen(req, context=ctx, timeout=timeout)
            html = resp.read().decode("utf-8", errors="replace")
            set_cookies = _collect_urllib_set_cookies(resp)

        if set_cookies:
            _merge_set_cookies(set_cookies)

        m_snl = re.search(r'"SNlM0e"\s*:\s*"([^"]+)"', html)
        m_bl = re.search(r'"cfb2h"\s*:\s*"([^"]+)"', html)
        m_pep = re.search(r'"oPEP7c"\s*:\s*"([^"]*)"', html)
        m_s06 = re.search(r'"S06Grb"\s*:\s*"([^"]*)"', html)

        has_snlm0e = bool(m_snl and m_snl.group(1))
        has_opep7c = bool(m_pep and m_pep.group(1))
        has_s06grb = bool(m_s06 and m_s06.group(1))
        authenticated = has_snlm0e or has_opep7c or has_s06grb

        updated_cfg = False
        if authenticated and has_snlm0e and m_snl.group(1) != CONFIG.get("xsrf_token"):
            CONFIG["xsrf_token"] = m_snl.group(1)
            updated_cfg = True
        if m_bl and m_bl.group(1) and m_bl.group(1) != CONFIG.get("gemini_bl"):
            CONFIG["gemini_bl"] = m_bl.group(1)
            updated_cfg = True
        if updated_cfg:
            save_config()

        now = int(time.time())
        _session_state.update({
            "authenticated": authenticated,
            "last_refresh_at": now,
            "last_error": None if authenticated else "session unauthenticated",
        })
        return {
            "ok": True,
            "authenticated": authenticated,
            "has_snlm0e": has_snlm0e,
            "has_opep7c": has_opep7c,
            "has_s06grb": has_s06grb,
            "has_sid": "SID" in pairs,
            "has_sapisid": bool(sapisid),
            "gemini_bl": CONFIG.get("gemini_bl"),
            "cookie_file": CONFIG.get("cookie_file"),
            "last_refresh_at": now,
        }
    except Exception as e:
        now = int(time.time())
        _session_state.update({
            "authenticated": False,
            "last_refresh_at": now,
            "last_error": str(e),
        })
        base_status["error"] = str(e)
        base_status["last_refresh_at"] = now
        return base_status


def get_session_status() -> dict:
    """Return safe, secret-free status of the configured Gemini Web session."""
    cookie_str, sapisid = load_cookie()
    pairs = _parse_cookie_pairs(cookie_str)
    return {
        "cookie_configured": bool(cookie_str),
        "cookie_file": CONFIG.get("cookie_file"),
        "auth_user": CONFIG.get("auth_user"),
        "default_model": CONFIG.get("default_model"),
        "temporary_chats": bool(CONFIG.get("temporary_chats", False)),
        "has_sid": "SID" in pairs,
        "has_sapisid": bool(sapisid),
        "has_xsrf": bool(CONFIG.get("xsrf_token")),
        "gemini_bl": CONFIG.get("gemini_bl"),
        "authenticated": _session_state.get("authenticated"),
        "last_refresh_at": _session_state.get("last_refresh_at"),
        "last_error": _session_state.get("last_error"),
    }


def start_session_refresher():
    """Start background daemon thread that keeps SNlM0e and Set-Cookie values fresh."""
    global _refresher_started
    interval = CONFIG.get("session_refresh_interval_sec", 600)
    if not interval or interval <= 0 or _refresher_started:
        return None
    _refresher_started = True

    def _loop():
        while True:
            if CONFIG.get("cookie_file"):
                status = refresh_page_session()
                if status.get("authenticated"):
                    log("Session refresh OK (authenticated)")
                elif status.get("error"):
                    log(f"Session refresh error: {status['error']}")
                else:
                    log("Session refresh: unauthenticated — click Sync in extension")
            time.sleep(interval)

    thread = threading.Thread(target=_loop, name="gemini-session-refresher", daemon=True)
    thread.start()
    return thread


def generate(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None) -> str:
    """Non-streaming generation with retry."""
    ctx = _get_ssl_ctx()
    proxy = CONFIG.get("proxy")
    url = _get_url()

    last_err = None
    for attempt in range(CONFIG["retry_attempts"]):
        try:
            body = _build_payload(prompt, model_id, think_mode, file_refs, extra_fields).encode()
            headers = _build_headers(model_id, think_mode)
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                    urllib.request.HTTPSHandler(context=ctx)
                )
                resp = opener.open(req, timeout=CONFIG["request_timeout_sec"])
            else:
                resp = urllib.request.urlopen(req, context=ctx, timeout=CONFIG["request_timeout_sec"])
            _merge_set_cookies(_collect_urllib_set_cookies(resp))
            raw = resp.read().decode("utf-8", errors="replace")
            return extract_response_text(raw)
        except Exception as e:
            last_err = e
            if attempt < CONFIG["retry_attempts"] - 1:
                log(f"Retry {attempt+1}/{CONFIG['retry_attempts']}: {e}")
                time.sleep(CONFIG["retry_delay_sec"])
    raise last_err


def generate_stream(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None):
    """Streaming generation via httpx with retry on connection failure."""
    if not HAS_HTTPX:
        text = generate(prompt, model_id, think_mode, file_refs, extra_fields)
        if text:
            yield text
        return

    url = _get_url()
    last_err = None
    emitted_raw_text = ""
    with _create_httpx_client() as client:
        for attempt in range(CONFIG["retry_attempts"]):
            try:
                body = _build_payload(prompt, model_id, think_mode, file_refs, extra_fields)
                headers = _build_headers(model_id, think_mode)
                with client.stream("POST", url, content=body, headers=headers) as resp:
                    resp.raise_for_status()
                    _merge_set_cookies(_collect_httpx_set_cookies(resp))
                    buf = ""
                    for chunk in resp.iter_text():
                        buf += chunk
                        if "BardErrorInfo" in buf:
                            bard_err = re.search(r'BardErrorInfo\s*\[(\d+)\]', buf)
                            if bard_err:
                                raise RuntimeError(
                                    f"Gemini upstream rejected request: BardErrorInfo [{bard_err.group(1)}]"
                                )
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            for t in _extract_texts_from_line(line):
                                if t == emitted_raw_text or emitted_raw_text.startswith(t):
                                    continue
                                if not t.startswith(emitted_raw_text):
                                    raise RuntimeError("Gemini stream content changed during retry")
                                delta = clean_text(t[len(emitted_raw_text):], strip=False)
                                emitted_raw_text = t
                                if delta:
                                    yield delta
                return
            except Exception as e:
                last_err = e
                if attempt < CONFIG["retry_attempts"] - 1:
                    log(f"Stream retry {attempt+1}/{CONFIG['retry_attempts']}: {e}")
                    time.sleep(CONFIG["retry_delay_sec"])
    raise last_err
