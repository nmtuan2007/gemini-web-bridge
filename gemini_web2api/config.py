"""Configuration management."""
import json
import os
import threading

DEFAULT_CONFIG = {
    "port": 8081,
    "host": "0.0.0.0",
    "retry_attempts": 3,
    "retry_delay_sec": 2,
    "request_timeout_sec": 180,
    "session_refresh_interval_sec": 600,
    "gemini_bl": "boq_assistant-bard-web-server_20260716.08_p0",
    "auth_user": None,
    "xsrf_token": None,
    "default_model": "gemini-3.6-flash",
    "log_requests": True,
    "cookie_file": None,
    "proxy": None,
    "api_keys": [],
    "temporary_chats": False,
}

CONFIG = dict(DEFAULT_CONFIG)
_config_path = None
_config_lock = threading.RLock()


def get_config_path():
    """Return the active config.json path if loaded."""
    return _config_path


def set_config_path(path: str = None):
    """Set the active config.json path explicitly."""
    global _config_path
    _config_path = os.path.abspath(path) if path else None


def load_config(path: str = None):
    """Load config from JSON file."""
    global _config_path
    if path and os.path.exists(path):
        with _config_lock:
            _config_path = os.path.abspath(path)
            with open(_config_path, "r", encoding="utf-8") as f:
                CONFIG.update(json.load(f))
    return CONFIG


def save_config(path: str = None) -> bool:
    """Persist runtime auth/config updates to disk when a config path is known."""
    target = path or _config_path
    if not target:
        return False
    with _config_lock:
        data = {}
        if os.path.exists(target):
            try:
                with open(target, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                data = {}
        for key in (
            "cookie_file",
            "auth_user",
            "xsrf_token",
            "gemini_bl",
            "default_model",
            "temporary_chats",
        ):
            data[key] = CONFIG.get(key)
        with open(target, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
    return True


def find_config():
    """Search for config file in standard locations."""
    for p in ["./config.json", os.path.expanduser("~/.config/gemini-web2api/config.json")]:
        if os.path.exists(p):
            return p
    return None
