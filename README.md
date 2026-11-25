# gemini-web-bridge

<p align="center">
  <img src="logo.png" width="200" alt="gemini-web-bridge logo">
</p>

> **Credits & Attribution:** This repository is built upon and inspired by the original **[Sophomoresty/gemini-web2api](https://github.com/Sophomoresty/gemini-web2api)** project (MIT License). All core Gemini Web reverse-engineering, `StreamGenerate` protocol translation, and multi-endpoint foundations belong to the original author and contributors.

Turn Google Gemini's web interface (`gemini.google.com`, including **Gemini 4 Neon / Hydrogen / Carbon**) into a local **OpenAI-compatible & Coding-Agent-ready API**, controlled in one click through an all-in-one **Chrome Extension** with auto cookie/XSRF sync and live telemetry monitoring.

---

## What's Added on Top of `gemini-web2api`

- **Gemini 4 Mode Routing**: Supports `gemini-4-hydrogen`, `gemini-4-neon` (High thinking), `gemini-4-neon-medium`, and `gemini-4-carbon`, encoding `x-goog-ext-525001261-bin` mode headers.
- **All-in-One Chrome Extension Controller (`v2.1`)**:
  - **Start / Stop / Restart / Sync** the local server directly from the browser toolbar via Windows Native Messaging (`native-host/`).
  - **Automatic Session Maintenance**: Debounced cookie change listeners, periodic `SNlM0e` (XSRF) / `cfb2h` refresh, and `Set-Cookie` rotation merging.
  - **Live Monitor & Diagnostics**: Real-time request feed, tool-call counters, latency metrics, live server logs, 1-click **Test Tool Call** probe, runtime default model & `Temporary Chat` toggles, and 1-click Harness YAML copy.
- **Coding Agent & Tool-Calling Hardening** (tested end-to-end with **DeepSeek Harness**, OpenAI SDK, and Codex CLI):
  - Places `[External OpenAI Tool Harness Protocol]` with compact JSON schemas after conversation history so models reliably emit tool calls even with 20+ declared tools.
  - Supports `role: "developer"`, recovers missing tool names via `tool_call_id` on `role: "tool"`, and gracefully handles multi-call / declared-tool JSON fallbacks.
  - Sends early SSE headers + `: keepalive` comments during extended thinking so clients don't hit read/header timeouts, includes `delta.tool_calls[].index`, and reports trailing streaming `usage` chunks.

---

## Core Features (Inherited & Extended)

- **OpenAI Compatible**: Drop-in replacement for `/v1/chat/completions`, `/v1/responses`, and `/v1/models` (with `context_window` and `max_tokens` discovery metadata)
- **Google Gemini CLI Compatible**: Native endpoints (`/v1beta/models`, `:generateContent`, `:streamGenerateContent`)
- **Tool Calling**: OpenAI `tools` / `tool_choice` & Responses API function calling
- **Multimodal Input**: Image URLs and `data:...;base64,...` uploads
- **Optional API Keys**: No auth required locally when `api_keys` is `[]`, Bearer/`x-api-key` auth when configured
- **Thinking Depth Override**: Append `@think=N` to any model name (`0` deepest – `4` shallowest)

---

## Quick Start (Windows + Chrome Extension — Recommended)

### 1. Clone & Install Python Environment

```powershell
git clone git@github.com:nmtuan2007/gemini-web-bridge.git
cd gemini-web-bridge

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy config.example.json config.json
```

Edit `config.json` as needed (for purely local loopback use without an API key, set `"host": "127.0.0.1"` and `"api_keys": []`).

### 2. Register Native Messaging Host

```powershell
powershell -ExecutionPolicy Bypass -File .\native-host\register-native-host.ps1
```

This generates `native-host/com.gemini_web2api.host.json` for your local path and registers it for Chrome and Edge (`HKCU`).

### 3. Load the Chrome Extension

1. Open `chrome://extensions` and enable **Developer mode**.
2. Click **Load unpacked** and select `gemini-cookie-sync-extension/`.
3. Sign in at [gemini.google.com/app](https://gemini.google.com/app).
4. Click the **Gemini Web2API Controller** icon and press **Start**.

The extension launches the local server at `http://127.0.0.1:8081/v1` and keeps cookies + XSRF synced automatically.

---

## Quick Start (CLI / Cross-Platform)

```bash
pip install httpx
cp config.example.json config.json
python -m gemini_web2api --config config.json
```

Or using a cookie file directly:

```bash
python -m gemini_web2api --cookie-file cookie.txt
```

Server starts at `http://localhost:8081/v1`.

---

## Available Models

| Model | Description |
|-------|-------------|
| `gemini-4-neon` | Deeper analysis, extended thinking (`4 Neon High`) |
| `gemini-4-neon-medium` | Deeper analysis, medium thinking |
| `gemini-4-hydrogen` | Everyday fast model (`4 Hydrogen`) |
| `gemini-4-carbon` | Complex coding work (`4 Carbon / Ultra`) |
| `gemini-3.7-flash` | All-around model (`3.7 Flash`) |
| `gemini-3.6-flash` | All-around model (`3.6 Flash`) |
| `gemini-3.5-flash-thinking` | Deep thinking mode, long output |
| `gemini-3.5-flash-thinking-lite` | Dynamic adaptive thinking |
| `gemini-3.1-pro` | Pro routing (requires Gemini Advanced cookie) |
| `gemini-auto` | Auto model selection |
| `gemini-flash-lite` | Lightweight fast model |

---

## DeepSeek Harness Integration (`~/.dsh/settings.yaml`)

You can copy this snippet directly from the extension (`Copy Harness YAML`) into `~/.dsh/settings.yaml`:

```yaml
llm-pi-ai:
  providers:
    gemini-web:
      displayName: Gemini Web
      api: openai-completions
      baseURL: http://127.0.0.1:8081/v1
      headers:
        Authorization: Bearer local
      compat:
        requiresToolResultName: true
        supportsDeveloperRole: false
      models:
        - id: gemini-4-neon
          name: Gemini 4 Neon
          contextWindow: 1000000
          maxTokens: 65536
          input: [text, image]
        - id: gemini-4-neon-medium
          name: Gemini 4 Neon Medium
          contextWindow: 1000000
          maxTokens: 65536
          input: [text, image]
        - id: gemini-4-hydrogen
          name: Gemini 4 Hydrogen
          contextWindow: 1000000
          maxTokens: 65536
          input: [text, image]

agent-default-model:
  provider: gemini-web
  model: gemini-4-neon

agent-presets:
  default: standard
```

---

## Client Examples

### OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8081/v1", api_key="local")
resp = client.chat.completions.create(
    model="gemini-4-neon",
    messages=[{"role": "user", "content": "Explain how async generators work"}],
)
print(resp.choices[0].message.content)
```

### Gemini CLI

```bash
export GEMINI_API_KEY=none
export GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:8081
gemini
```

---

## Docker & Proxy

```bash
cp config.example.json config.json
docker build -t gemini-web-bridge .
docker run -d --name gemini-web-bridge -p 8081:8081 -v ./config.json:/app/config.json gemini-web-bridge
```

Or configure `"proxy": "http://127.0.0.1:7890"` in `config.json` or pass `--proxy`.

---

## Running Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

---

## Security Notes

- Treat `config.json`, `cookie.txt`, `gemini-auth.json`, and `native-host/server.log` as local-only secrets — all are excluded by `.gitignore`.
- Admin endpoints (`/admin/*`) restrict callers to loopback addresses (`127.0.0.1`, `::1`) and trusted local / extension origins.

---

## Acknowledgments & Original Author

- Core project & protocol reverse-engineering by **[Sophomoresty/gemini-web2api](https://github.com/Sophomoresty/gemini-web2api)** (MIT License)
- Original upstream development acknowledgments: [GenericAgent](https://github.com/lsdefine/GenericAgent) & [LinuxDo](https://linux.do/)

## License

[MIT](LICENSE)
