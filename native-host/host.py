"""Chrome Native Messaging host for starting/stopping local gemini-web2api server."""
import json
import msvcrt
import os
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NATIVE_DIR = os.path.dirname(os.path.abspath(__file__))
PID_FILE = os.path.join(NATIVE_DIR, "server.pid")
LOG_FILE = os.path.join(NATIVE_DIR, "server.log")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")


def read_message(stream) -> dict | None:
    """Read a single length-prefixed JSON Native Messaging frame from stdin."""
    raw_len = stream.read(4)
    if not raw_len or len(raw_len) < 4:
        return None
    msg_len = struct.unpack("<I", raw_len)[0]
    if msg_len == 0:
        return {}
    raw_body = stream.read(msg_len)
    if not raw_body or len(raw_body) < msg_len:
        return None
    return json.loads(raw_body.decode("utf-8"))


def write_message(stream, payload: dict) -> None:
    """Write a single length-prefixed JSON Native Messaging frame to stdout."""
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    stream.write(struct.pack("<I", len(encoded)))
    stream.write(encoded)
    stream.flush()


def load_local_config() -> dict:
    """Load port/host/api_keys from config.json when present."""
    cfg = {"port": 8081, "host": "127.0.0.1", "api_keys": []}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (OSError, json.JSONDecodeError):
            pass
    return cfg


def get_base_url(cfg: dict) -> str:
    host = cfg.get("host") or "127.0.0.1"
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    return f"http://{host}:{int(cfg.get('port') or 8081)}"


def _auth_headers(cfg: dict) -> dict:
    keys = cfg.get("api_keys") or []
    headers = {"Content-Type": "application/json"}
    if keys:
        headers["Authorization"] = f"Bearer {keys[0]}"
    return headers


def _http_json(url: str, method: str = "GET", headers: dict = None, timeout: float = 1.5):
    req = urllib.request.Request(
        url,
        data=(b"{}" if method == "POST" else None),
        headers=headers or {},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return resp.status, json.loads(raw) if raw.strip() else {}


def is_server_running(base_url: str) -> bool:
    try:
        status, _ = _http_json(f"{base_url}/", timeout=1.2)
        return status == 200
    except Exception:
        return False


def _read_pid() -> int | None:
    if not os.path.exists(PID_FILE):
        return None
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _remove_pid() -> None:
    if os.path.exists(PID_FILE):
        try:
            os.remove(PID_FILE)
        except OSError:
            pass


def _kill_pid(pid: int) -> None:
    if os.name == "nt":
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


def _python_executable() -> str:
    venv_dir = os.path.join(BASE_DIR, ".venv", "Scripts")
    for name in ("pythonw.exe", "python.exe"):
        candidate = os.path.join(venv_dir, name)
        if os.path.exists(candidate):
            return candidate
    return sys.executable


def start_server(cfg: dict) -> dict:
    base_url = get_base_url(cfg)
    if is_server_running(base_url):
        return {"ok": True, "running": True, "alreadyRunning": True, "baseUrl": base_url, "pid": _read_pid()}

    py_exe = _python_executable()
    cmd = [py_exe, "-m", "gemini_web2api"]
    if os.path.exists(CONFIG_FILE):
        cmd.extend(["--config", CONFIG_FILE])

    log_mode = "w" if (os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 1_048_576) else "a"
    log_handle = open(LOG_FILE, log_mode, encoding="utf-8")
    creation_flags = 0
    if os.name == "nt":
        creation_flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )

    proc = subprocess.Popen(
        cmd,
        cwd=BASE_DIR,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=log_handle,
        close_fds=True,
        creationflags=creation_flags,
    )
    log_handle.close()

    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(proc.pid))

    deadline = time.time() + 6.0
    while time.time() < deadline:
        if is_server_running(base_url):
            return {"ok": True, "running": True, "alreadyRunning": False, "baseUrl": base_url, "pid": proc.pid}
        if proc.poll() is not None:
            _remove_pid()
            return {"ok": False, "running": False, "baseUrl": base_url, "error": f"Server exited with code {proc.returncode}"}
        time.sleep(0.15)

    return {"ok": False, "running": False, "baseUrl": base_url, "error": "Timed out waiting for server startup"}


def stop_server(cfg: dict) -> dict:
    base_url = get_base_url(cfg)
    if is_server_running(base_url):
        try:
            _http_json(f"{base_url}/admin/shutdown", method="POST", headers=_auth_headers(cfg), timeout=2.0)
        except Exception:
            pass
        deadline = time.time() + 2.5
        while time.time() < deadline and is_server_running(base_url):
            time.sleep(0.1)

    pid = _read_pid()
    if is_server_running(base_url) and pid:
        _kill_pid(pid)
        time.sleep(0.3)
    _remove_pid()
    still_up = is_server_running(base_url)
    return {"ok": not still_up, "running": still_up, "baseUrl": base_url}


def read_logs(lines: int = 80) -> dict:
    """Read recent tail lines from the local server.log file."""
    bounded = max(1, min(int(lines or 80), 300))
    if not os.path.exists(LOG_FILE):
        return {"ok": True, "lines": [], "logFile": LOG_FILE}
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.read().splitlines()
        return {"ok": True, "lines": all_lines[-bounded:], "logFile": LOG_FILE}
    except OSError as exc:
        return {"ok": False, "error": str(exc), "lines": [], "logFile": LOG_FILE}


def restart_server(cfg: dict) -> dict:
    """Stop the running server if up, then launch a fresh instance."""
    stop_res = stop_server(cfg)
    if stop_res.get("running"):
        return {**stop_res, "ok": False, "restarted": False, "error": "Failed to stop existing server"}
    start_res = start_server(cfg)
    start_res["restarted"] = True
    return start_res


def get_status(cfg: dict) -> dict:
    base_url = get_base_url(cfg)
    if not is_server_running(base_url):
        return {"ok": True, "running": False, "baseUrl": base_url, "pid": None}
    try:
        _, auth_status = _http_json(f"{base_url}/admin/auth/status", headers=_auth_headers(cfg), timeout=1.5)
    except Exception:
        auth_status = None
    return {"ok": True, "running": True, "baseUrl": base_url, "pid": _read_pid(), "auth": auth_status}


def handle_request(request: dict, cfg: dict = None) -> dict:
    """Dispatch a Native Messaging request payload."""
    cfg = cfg if cfg is not None else load_local_config()
    action = (request or {}).get("action", "status")
    if action == "start":
        return start_server(cfg)
    if action == "stop":
        return stop_server(cfg)
    if action == "restart":
        return restart_server(cfg)
    if action == "logs":
        return read_logs((request or {}).get("lines", 80))
    if action == "status":
        return get_status(cfg)
    return {"ok": False, "error": f"Unknown action: {action}"}


def main():
    if os.name == "nt":
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    request = read_message(sys.stdin.buffer)
    if request is None:
        return
    try:
        response = handle_request(request)
    except Exception as e:
        response = {"ok": False, "error": str(e)}
    write_message(sys.stdout.buffer, response)


if __name__ == "__main__":
    main()
