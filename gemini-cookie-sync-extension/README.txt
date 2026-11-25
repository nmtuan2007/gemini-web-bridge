Gemini Web2API Controller v2.1

Purpose:
- One-click Start / Stop / Restart local server via Windows Native Messaging (`native-host/`).
- Automatically sync Google/Gemini session cookies, SAPISID, XSRF (`SNlM0e`), and `cfb2h` (`gemini_bl`).
- Monitor live API requests, tool_calls emitted, latency, and server logs from the popup Monitor tab.
- Test OpenAI tool_calls compatibility (`Test Tool Call`) and copy Harness Agent configuration.

Installation (Windows):
1. Create `.venv` and copy `config.example.json` to `config.json`.
2. Run `powershell -ExecutionPolicy Bypass -File .\native-host\register-native-host.ps1`.
3. Open `chrome://extensions`, enable Developer mode, and click Load unpacked -> select this folder.
4. Sign in at `https://gemini.google.com/app`, then click the extension icon -> Start.

Security:
Synced cookies and exported `gemini-auth.json` represent your real Google session. Never share or commit them to Git.
