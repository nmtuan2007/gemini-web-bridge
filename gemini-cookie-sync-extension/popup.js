const pulseDotEl = document.getElementById("pulseDot");
const subMetaEl = document.getElementById("subMeta");
const badgeEl = document.getElementById("badge");
const startButton = document.getElementById("start");
const stopButton = document.getElementById("stop");
const restartButton = document.getElementById("restart");
const syncButton = document.getElementById("sync");

const baseUrlTextEl = document.getElementById("baseUrlText");
const copyUrlButton = document.getElementById("copyUrl");
const modelSelectEl = document.getElementById("modelSelect");
const tempChatToggleEl = document.getElementById("tempChatToggle");
const accountLabelEl = document.getElementById("accountLabel");

const dotSidEl = document.getElementById("dotSid");
const valSidEl = document.getElementById("valSid");
const dotSapisidEl = document.getElementById("dotSapisid");
const valSapisidEl = document.getElementById("valSapisid");
const dotXsrfEl = document.getElementById("dotXsrf");
const valXsrfEl = document.getElementById("valXsrf");
const dotSyncEl = document.getElementById("dotSync");
const valSyncEl = document.getElementById("valSync");

const statTotalEl = document.getElementById("statTotal");
const statSuccessEl = document.getElementById("statSuccess");
const activeTileEl = document.getElementById("activeTile");
const statActiveEl = document.getElementById("statActive");
const statActiveSubEl = document.getElementById("statActiveSub");
const statToolsEl = document.getElementById("statTools");
const statLatencyEl = document.getElementById("statLatency");
const statLastLatEl = document.getElementById("statLastLat");

const testProbeButton = document.getElementById("testProbe");
const copyHarnessOverviewButton = document.getElementById("copyHarnessOverview");
const statusEl = document.getElementById("status");

const livePillEl = document.getElementById("livePill");
const clearTelemetryButton = document.getElementById("clearTelemetry");
const activityFeedEl = document.getElementById("activityFeed");
const logSourceEl = document.getElementById("logSource");
const refreshLogsButton = document.getElementById("refreshLogs");
const logBoxEl = document.getElementById("logBox");

const serverUrlInput = document.getElementById("serverUrl");
const saveServerUrlButton = document.getElementById("saveServerUrl");
const inspectButton = document.getElementById("inspect");
const openButton = document.getElementById("open");
const exportButton = document.getElementById("export");
const copyHarnessToolsButton = document.getElementById("copyHarnessTools");
const yamlPreviewEl = document.getElementById("yamlPreview");
const toolsStatusEl = document.getElementById("toolsStatus");

const tabButtons = [...document.querySelectorAll(".tab-btn")];
const tabPanels = {
  overview: document.getElementById("tab-overview"),
  monitor: document.getElementById("tab-monitor"),
  tools: document.getElementById("tab-tools")
};

let activeTab = "overview";
let isBusy = false;
let latestState = null;
let overrideBanner = null;
let pollTimer = null;

function buildHarnessYaml(serverUrl) {
  const base = (serverUrl || "http://127.0.0.1:8081").replace(/\/+$/, "");
  return [
    "gemini-web:",
    "  displayName: Gemini Web",
    "  api: openai-completions",
    `  baseURL: ${base}/v1`,
    "  headers:",
    "    Authorization: Bearer local",
    "  compat:",
    "    requiresToolResultName: true",
    "    supportsDeveloperRole: false",
    "  models:",
    "    - id: gemini-4-neon",
    "      name: Gemini 4 Neon",
    "      contextWindow: 1000000",
    "      maxTokens: 65536",
    "      input: [text, image]",
    "    - id: gemini-4-neon-medium",
    "      name: Gemini 4 Neon Medium",
    "      contextWindow: 1000000",
    "      maxTokens: 65536",
    "      input: [text, image]",
    "    - id: gemini-4-hydrogen",
    "      name: Gemini 4 Hydrogen",
    "      contextWindow: 1000000",
    "      maxTokens: 65536",
    "      input: [text, image]"
  ].join("\n");
}

function getServerBaseUrl() {
  const raw = (serverUrlInput?.value || "http://127.0.0.1:8081").trim();
  return raw.replace(/\/+$/, "");
}

function formatDuration(ms) {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function formatUptime(sec) {
  if (sec === null || sec === undefined) return "";
  const s = Math.max(0, Math.floor(sec));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

function formatRelativeTime(unixSec) {
  if (!unixSec) return "—";
  const diff = Math.max(0, Math.floor(Date.now() / 1000) - unixSec);
  if (diff < 5) return "just now";
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  return new Date(unixSec * 1000).toLocaleTimeString();
}

async function directFallback(message) {
  const baseUrl = (message.serverUrl || getServerBaseUrl()).replace(/\/+$/, "");
  switch (message?.type) {
    case "POLL_STATE":
      return sendRawBg({ type: "GET_STATE" });
    case "SET_SERVER_URL": {
      const nextUrl = (message.serverUrl || "http://127.0.0.1:8081").trim().replace(/\/+$/, "");
      await chrome.storage.local.set({ serverUrl: nextUrl });
      return { ok: true, serverUrl: nextUrl };
    }
    case "RESTART": {
      await sendRawBg({ type: "STOP", serverUrl: baseUrl });
      await new Promise((r) => setTimeout(r, 300));
      return sendRawBg({ type: "START", serverUrl: baseUrl });
    }
    case "GET_LOGS": {
      const resp = await fetch(`${baseUrl}/admin/logs?lines=${encodeURIComponent(String(message.lines || 80))}`);
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data?.error?.message || `HTTP ${resp.status}`);
      return { ok: true, source: "memory", lines: data.lines || [] };
    }
    case "SET_CONFIG": {
      const resp = await fetch(`${baseUrl}/admin/config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(message.patch || {})
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data?.error?.message || `HTTP ${resp.status}`);
      return { ok: true, config: data };
    }
    case "CLEAR_TELEMETRY": {
      const resp = await fetch(`${baseUrl}/admin/telemetry/clear`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}"
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data?.error?.message || `HTTP ${resp.status}`);
      return { ok: true, telemetry: data.telemetry };
    }
    case "TEST_PROBE": {
      const startedAt = Date.now();
      const model = message.model || "gemini-4-neon";
      const resp = await fetch(`${baseUrl}/v1/chat/completions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model,
          stream: false,
          messages: [
            {
              role: "user",
              content: "Use the ping_check tool right now with target='harness'. Do not answer in plain text."
            }
          ],
          tools: [
            {
              type: "function",
              function: {
                name: "ping_check",
                description: "Health probe tool for verifying OpenAI tool_calls compatibility.",
                parameters: {
                  type: "object",
                  properties: {
                    target: { type: "string", description: "Probe target label" }
                  },
                  required: ["target"]
                }
              }
            }
          ],
          tool_choice: "required"
        })
      });
      const durationMs = Date.now() - startedAt;
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data?.error?.message || `Probe failed with HTTP ${resp.status}`);
      const choice = data?.choices?.[0] || {};
      const msg = choice.message || {};
      return {
        ok: true,
        durationMs,
        model: data.model || model,
        finishReason: choice.finish_reason || null,
        toolCalls: Array.isArray(msg.tool_calls) ? msg.tool_calls : [],
        content: msg.content || null
      };
    }
    default:
      throw new Error("Extension service worker is stale. Click Reload on chrome://extensions.");
  }
}

function sendRawBg(message) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(message, (response) => {
      const err = chrome.runtime.lastError;
      if (err) {
        reject(new Error(err.message));
        return;
      }
      if (!response?.ok && response?.error) {
        reject(new Error(response.error));
        return;
      }
      resolve(response);
    });
  });
}

async function sendBg(message) {
  try {
    return await sendRawBg(message);
  } catch (err) {
    if (err?.message === "Unknown command") {
      return await directFallback(message);
    }
    throw err;
  }
}

function setStatus(message, kind = "", target = statusEl) {
  target.textContent = message;
  target.className = `status-box${kind ? ` ${kind}` : ""}`;
  if (target === toolsStatusEl) {
    target.classList.remove("hidden");
  }
}

function setPill(dotEl, valEl, present, labelOk = "OK", labelMiss = "Missing") {
  dotEl.className = `dot-sm ${present ? "dot-ok" : "dot-miss"}`;
  valEl.textContent = present ? labelOk : labelMiss;
}

function setBusy(busy, reason = "") {
  isBusy = busy;
  const running = Boolean(latestState?.running);
  startButton.disabled = busy || running;
  stopButton.disabled = busy || !running;
  restartButton.disabled = busy || !running;
  syncButton.disabled = busy;
  testProbeButton.disabled = busy || !running;
  modelSelectEl.disabled = busy || !running;
  tempChatToggleEl.disabled = busy || !running;
  inspectButton.disabled = busy;
  exportButton.disabled = busy;
  saveServerUrlButton.disabled = busy;
  if (busy && reason) {
    pulseDotEl.className = "pulse-dot busy";
    badgeEl.textContent = "WORKING";
    badgeEl.className = "badge badge-busy";
  }
}

function populateModels(models, activeModel) {
  const list = Array.isArray(models) && models.length > 0
    ? models
    : ["gemini-4-neon", "gemini-4-neon-medium", "gemini-4-hydrogen", "gemini-4-carbon", "gemini-3.7-flash"];

  if (document.activeElement === modelSelectEl) return;
  const currentOptions = [...modelSelectEl.options].map((o) => o.value);
  if (JSON.stringify(currentOptions) !== JSON.stringify(list)) {
    modelSelectEl.textContent = "";
    for (const id of list) {
      const opt = document.createElement("option");
      opt.value = id;
      opt.textContent = id;
      modelSelectEl.appendChild(opt);
    }
  }
  if (activeModel && list.includes(activeModel)) {
    modelSelectEl.value = activeModel;
  }
}

function renderActivityFeed(events) {
  activityFeedEl.textContent = "";
  if (!Array.isArray(events) || events.length === 0) {
    const empty = document.createElement("div");
    empty.className = "feed-empty";
    empty.textContent = "No API requests recorded yet. Requests from Harness Agent or scripts will appear here live.";
    activityFeedEl.appendChild(empty);
    return;
  }

  for (const ev of events) {
    const item = document.createElement("div");
    item.className = "feed-item";

    const top = document.createElement("div");
    top.className = "feed-top";

    const left = document.createElement("div");
    left.className = "feed-left";

    const statusTag = document.createElement("span");
    statusTag.className = `status-tag ${ev.ok ? "tag-ok" : "tag-err"}`;
    statusTag.textContent = String(ev.status || (ev.ok ? 200 : 500));
    left.appendChild(statusTag);

    const modelSpan = document.createElement("span");
    modelSpan.className = "feed-model";
    const shortEp = String(ev.endpoint || "").replace("/v1/", "").replace("/chat/completions", "chat");
    modelSpan.textContent = `${shortEp} · ${ev.model || "?"}`;
    left.appendChild(modelSpan);

    if (ev.stream) {
      const sTag = document.createElement("span");
      sTag.className = "status-tag tag-info";
      sTag.textContent = "SSE";
      left.appendChild(sTag);
    }
    if (ev.tool_calls > 0) {
      const tTag = document.createElement("span");
      tTag.className = "status-tag tag-ok";
      tTag.textContent = `TOOLS (${ev.tool_calls})`;
      left.appendChild(tTag);
    }

    const durSpan = document.createElement("span");
    durSpan.className = "submeta";
    durSpan.textContent = formatDuration(ev.duration_ms);

    top.appendChild(left);
    top.appendChild(durSpan);
    item.appendChild(top);

    const meta = document.createElement("div");
    meta.className = "feed-meta";
    const charsInfo = `in ${(ev.prompt_chars || 0).toLocaleString()} ch · out ${(ev.response_chars || 0).toLocaleString()} ch`;
    const leftMeta = document.createElement("span");
    leftMeta.textContent = charsInfo;
    const rightMeta = document.createElement("span");
    rightMeta.textContent = formatRelativeTime(ev.at);
    meta.appendChild(leftMeta);
    meta.appendChild(rightMeta);
    item.appendChild(meta);

    if (ev.error) {
      const errRow = document.createElement("div");
      errRow.className = "feed-err";
      errRow.textContent = ev.error;
      errRow.title = ev.error;
      item.appendChild(errRow);
    }

    activityFeedEl.appendChild(item);
  }
}

function renderState(state) {
  latestState = state;
  const baseUrl = (state.serverUrl || "http://127.0.0.1:8081").replace(/\/+$/, "");
  if (document.activeElement !== serverUrlInput && state.serverUrl) {
    serverUrlInput.value = state.serverUrl;
  }
  baseUrlTextEl.textContent = `${baseUrl}/v1`;
  yamlPreviewEl.textContent = buildHarnessYaml(baseUrl);

  const auth = state.auth || {};
  const telemetry = auth.telemetry || {};
  const running = Boolean(state.running);
  const authenticated = Boolean(auth.authenticated ?? state.lastSync?.authenticated);

  const hasSid = Boolean(auth.has_sid ?? state.lastSync?.hasSid);
  const hasSapisid = Boolean(auth.has_sapisid ?? state.lastSync?.hasSapisid);
  const hasXsrf = Boolean(auth.has_xsrf ?? state.lastSync?.hasSnlm0e);

  setPill(dotSidEl, valSidEl, hasSid);
  setPill(dotSapisidEl, valSapisidEl, hasSapisid);
  setPill(dotXsrfEl, valXsrfEl, hasXsrf);

  if (state.lastSync?.at) {
    dotSyncEl.className = `dot-sm ${state.lastSync.authenticated ? "dot-ok" : "dot-warn"}`;
    valSyncEl.textContent = new Date(state.lastSync.at).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit"
    });
  } else {
    dotSyncEl.className = "dot-sm";
    valSyncEl.textContent = "Never";
  }

  accountLabelEl.textContent = auth.auth_user !== undefined && auth.auth_user !== null && auth.auth_user !== ""
    ? `account /u/${auth.auth_user}`
    : "default account";

  populateModels(auth.models, auth.default_model);
  if (typeof auth.temporary_chats === "boolean") {
    tempChatToggleEl.checked = auth.temporary_chats;
  }

  // Telemetry tiles
  const activeCount = Number(telemetry.active_requests || 0);
  const totalCount = Number(telemetry.total_requests || 0);
  const okCount = Number(telemetry.success_requests || 0);
  const errCount = Number(telemetry.error_requests || 0);
  const toolsCount = Number(telemetry.tool_calls_emitted || 0);

  statTotalEl.textContent = String(totalCount);
  statSuccessEl.textContent = `${okCount} ok · ${errCount} err`;
  statActiveEl.textContent = String(activeCount);
  statActiveSubEl.textContent = activeCount > 0 ? "generating…" : "idle";
  activeTileEl.classList.toggle("active-now", activeCount > 0);
  statToolsEl.textContent = String(toolsCount);
  statLatencyEl.textContent = totalCount > 0 && telemetry.avg_latency_ms
    ? formatDuration(telemetry.avg_latency_ms)
    : "—";
  statLastLatEl.textContent = telemetry.last_request
    ? `last: ${formatDuration(telemetry.last_request.duration_ms)}`
    : "last: —";

  renderActivityFeed(telemetry.recent_events || []);

  if (!isBusy) {
    startButton.disabled = running;
    stopButton.disabled = !running;
    restartButton.disabled = !running;
    testProbeButton.disabled = !running;
    modelSelectEl.disabled = !running;
    tempChatToggleEl.disabled = !running;
  }

  if (running) {
    livePillEl.textContent = "LIVE · 2s";
    livePillEl.className = "live-pill";
    const metaParts = [];
    if (auth.version) metaParts.push(`v${auth.version}`);
    if (state.pid) metaParts.push(`PID ${state.pid}`);
    if (telemetry.uptime_sec !== undefined) metaParts.push(`Up ${formatUptime(telemetry.uptime_sec)}`);
    subMetaEl.textContent = metaParts.join(" · ") || "Local OpenAI gateway running";

    if (!isBusy) {
      if (activeCount > 0) {
        pulseDotEl.className = "pulse-dot busy";
      } else {
        pulseDotEl.className = `pulse-dot ${authenticated ? "on" : "warn"}`;
      }
      if (authenticated) {
        badgeEl.textContent = `READY · ${(auth.default_model || "4 NEON").replace("gemini-", "").toUpperCase()}`;
        badgeEl.className = "badge badge-on";
      } else {
        badgeEl.textContent = "NO AUTH";
        badgeEl.className = "badge badge-warn";
      }
    }

    if (!overrideBanner) {
      if (authenticated) {
        const lastEv = telemetry.last_request;
        const lastSummary = lastEv
          ? `\nLast call: ${lastEv.model} · ${lastEv.status} in ${formatDuration(lastEv.duration_ms)}` +
            (lastEv.tool_calls ? ` · ${lastEv.tool_calls} tool_call(s)` : "")
          : "";
        setStatus(
          `Gateway online at ${baseUrl}/v1 (auto-sync active).${lastSummary}`,
          "ok"
        );
      } else {
        setStatus(
          "Server is running, but Google session is not authenticated.\n" +
          "Sign in at gemini.google.com, then click ⚡ Sync.",
          "warn"
        );
      }
    }
  } else {
    livePillEl.textContent = "OFFLINE";
    livePillEl.className = "live-pill off";
    subMetaEl.textContent = state.nativeAvailable
      ? "Native host ready · Click Start"
      : "Native host missing";

    if (!isBusy) {
      pulseDotEl.className = "pulse-dot";
      badgeEl.textContent = "STOPPED";
      badgeEl.className = "badge badge-off";
    }

    if (!overrideBanner) {
      if (!state.nativeAvailable) {
        setStatus(
          "Server is stopped and Chrome Native Host is not registered.\nRun native-host\\register-native-host.ps1 first.",
          "warn"
        );
      } else {
        setStatus("Server is stopped. Click ▶ Start to launch local gateway and auto-sync cookies.");
      }
    }
  }

  if (overrideBanner) {
    setStatus(overrideBanner.message, overrideBanner.kind);
  }
}

async function refreshState(lightweight = false) {
  try {
    const state = await sendBg({ type: lightweight ? "POLL_STATE" : "GET_STATE" });
    renderState(state);
  } catch (error) {
    setStatus(error?.message || String(error), "err");
  }
}

async function refreshLogs() {
  try {
    const res = await sendBg({ type: "GET_LOGS", serverUrl: getServerBaseUrl(), lines: 80 });
    const lines = Array.isArray(res.lines) ? res.lines : [];
    logSourceEl.textContent = res.source === "memory" ? "live buffer" : (res.source || "file");
    logBoxEl.textContent = lines.length > 0 ? lines.join("\n") : "(no logs yet)";
    logBoxEl.scrollTop = logBoxEl.scrollHeight;
  } catch (error) {
    logSourceEl.textContent = "error";
    logBoxEl.textContent = error?.message || String(error);
  }
}

function switchTab(nextTab) {
  activeTab = nextTab;
  for (const btn of tabButtons) {
    btn.classList.toggle("active", btn.dataset.tab === nextTab);
  }
  for (const [name, panel] of Object.entries(tabPanels)) {
    panel.classList.toggle("active", name === nextTab);
  }
  if (nextTab === "monitor") {
    refreshLogs().catch(() => {});
  }
}

for (const btn of tabButtons) {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
}

startButton.addEventListener("click", async () => {
  overrideBanner = null;
  setBusy(true, "starting");
  setStatus("Starting local server and syncing Google session…");
  try {
    const res = await sendBg({ type: "START", serverUrl: getServerBaseUrl() });
    if (res.syncError) {
      overrideBanner = {
        message: `Server started at ${res.baseUrl}/v1, but cookie sync failed:\n\n${res.syncError}`,
        kind: "warn"
      };
    }
  } catch (error) {
    overrideBanner = { message: error?.message || String(error), kind: "err" };
  } finally {
    setBusy(false);
    await refreshState();
  }
});

stopButton.addEventListener("click", async () => {
  overrideBanner = null;
  setBusy(true, "stopping");
  setStatus("Stopping local server…");
  try {
    await sendBg({ type: "STOP", serverUrl: getServerBaseUrl() });
  } catch (error) {
    overrideBanner = { message: error?.message || String(error), kind: "err" };
  } finally {
    setBusy(false);
    await refreshState();
  }
});

restartButton.addEventListener("click", async () => {
  overrideBanner = null;
  setBusy(true, "restarting");
  setStatus("Restarting local server cleanly…");
  try {
    const res = await sendBg({ type: "RESTART", serverUrl: getServerBaseUrl() });
    if (res.syncError) {
      overrideBanner = {
        message: `Server restarted, but cookie sync failed:\n\n${res.syncError}`,
        kind: "warn"
      };
    }
  } catch (error) {
    overrideBanner = { message: error?.message || String(error), kind: "err" };
  } finally {
    setBusy(false);
    await refreshState();
    if (activeTab === "monitor") {
      await refreshLogs();
    }
  }
});

syncButton.addEventListener("click", async () => {
  overrideBanner = null;
  setBusy(true, "syncing");
  setStatus("Syncing Google cookies and XSRF…");
  try {
    await sendBg({ type: "SYNC_NOW", serverUrl: getServerBaseUrl() });
  } catch (error) {
    overrideBanner = { message: error?.message || String(error), kind: "warn" };
  } finally {
    setBusy(false);
    await refreshState();
  }
});

modelSelectEl.addEventListener("change", async () => {
  const chosen = modelSelectEl.value;
  overrideBanner = null;
  try {
    await sendBg({
      type: "SET_CONFIG",
      serverUrl: getServerBaseUrl(),
      patch: { default_model: chosen }
    });
    overrideBanner = { message: `Default model updated to ${chosen}.`, kind: "ok" };
  } catch (error) {
    overrideBanner = { message: error?.message || String(error), kind: "err" };
  } finally {
    await refreshState();
  }
});

tempChatToggleEl.addEventListener("change", async () => {
  const enabled = tempChatToggleEl.checked;
  overrideBanner = null;
  try {
    await sendBg({
      type: "SET_CONFIG",
      serverUrl: getServerBaseUrl(),
      patch: { temporary_chats: enabled }
    });
    overrideBanner = {
      message: `Temporary Chats ${enabled ? "enabled" : "disabled"}.`,
      kind: "ok"
    };
  } catch (error) {
    tempChatToggleEl.checked = !enabled;
    overrideBanner = { message: error?.message || String(error), kind: "err" };
  } finally {
    await refreshState();
  }
});

testProbeButton.addEventListener("click", async () => {
  const model = modelSelectEl.value || "gemini-4-neon";
  overrideBanner = null;
  setBusy(true, "probing");
  let elapsed = 0;
  setStatus(`Testing tool_calls with ${model} (0s)…`);
  const ticker = setInterval(() => {
    elapsed += 1;
    setStatus(`Testing tool_calls with ${model} (${elapsed}s)…`);
  }, 1000);
  try {
    const res = await sendBg({
      type: "TEST_PROBE",
      serverUrl: getServerBaseUrl(),
      model
    });
    const tc = res.toolCalls?.[0];
    if (tc) {
      overrideBanner = {
        message:
          `PASS (${formatDuration(res.durationMs)}) · finish_reason=${res.finishReason}\n` +
          `${tc.function?.name}(${tc.function?.arguments})`,
        kind: "ok"
      };
    } else {
      overrideBanner = {
        message:
          `WARN (${formatDuration(res.durationMs)}) · finish_reason=${res.finishReason}\n` +
          `Model replied without tool_calls: ${String(res.content || "").slice(0, 140)}`,
        kind: "warn"
      };
    }
  } catch (error) {
    overrideBanner = { message: error?.message || String(error), kind: "err" };
  } finally {
    clearInterval(ticker);
    setBusy(false);
    await refreshState();
  }
});

async function copyTextWithFeedback(buttonEl, text, copiedLabel = "Copied!") {
  try {
    await navigator.clipboard.writeText(text);
    const old = buttonEl.textContent;
    buttonEl.textContent = copiedLabel;
    setTimeout(() => { buttonEl.textContent = old; }, 1300);
  } catch {
    // Ignore clipboard permission errors.
  }
}

copyUrlButton.addEventListener("click", () => {
  copyTextWithFeedback(copyUrlButton, baseUrlTextEl.textContent);
});

copyHarnessOverviewButton.addEventListener("click", () => {
  copyTextWithFeedback(copyHarnessOverviewButton, buildHarnessYaml(getServerBaseUrl()), "✓ YAML Copied");
});

copyHarnessToolsButton.addEventListener("click", () => {
  copyTextWithFeedback(copyHarnessToolsButton, buildHarnessYaml(getServerBaseUrl()), "✓ YAML Copied");
});

clearTelemetryButton.addEventListener("click", async () => {
  try {
    await sendBg({ type: "CLEAR_TELEMETRY", serverUrl: getServerBaseUrl() });
    await refreshState();
  } catch (error) {
    setStatus(error?.message || String(error), "warn");
  }
});

refreshLogsButton.addEventListener("click", () => {
  refreshLogs().catch(() => {});
});

saveServerUrlButton.addEventListener("click", async () => {
  try {
    const next = getServerBaseUrl();
    await sendBg({ type: "SET_SERVER_URL", serverUrl: next });
    setStatus(`Saved server URL: ${next}`, "ok", toolsStatusEl);
    await refreshState();
  } catch (error) {
    setStatus(error?.message || String(error), "err", toolsStatusEl);
  }
});

inspectButton.addEventListener("click", async () => {
  setBusy(true);
  setStatus("Inspecting Google cookies and XSRF…", "", toolsStatusEl);
  try {
    const res = await sendBg({ type: "INSPECT" });
    setStatus(res.message, res.ready ? "ok" : "warn", toolsStatusEl);
  } catch (error) {
    setStatus(error?.message || String(error), "err", toolsStatusEl);
  } finally {
    setBusy(false);
    await refreshState();
  }
});

openButton.addEventListener("click", async () => {
  await chrome.tabs.create({ url: "https://gemini.google.com/app" });
});

exportButton.addEventListener("click", async () => {
  setBusy(true);
  setStatus("Preparing gemini-auth.json…", "", toolsStatusEl);
  try {
    const res = await sendBg({ type: "EXPORT" });
    const blob = new Blob([JSON.stringify(res.payload, null, 2) + "\n"], {
      type: "application/json;charset=utf-8"
    });
    const url = URL.createObjectURL(blob);
    await chrome.downloads.download({
      url,
      filename: "gemini-auth.json",
      saveAs: true,
      conflictAction: "uniquify"
    });
    setTimeout(() => URL.revokeObjectURL(url), 30000);
    setStatus(res.summary, "ok", toolsStatusEl);
  } catch (error) {
    setStatus(error?.message || String(error), "err", toolsStatusEl);
  } finally {
    setBusy(false);
    await refreshState();
  }
});

refreshState(false);
pollTimer = setInterval(() => {
  if (isBusy) return;
  refreshState(true).catch(() => {});
  if (activeTab === "monitor") {
    refreshLogs().catch(() => {});
  }
}, 2000);
window.addEventListener("unload", () => {
  if (pollTimer) clearInterval(pollTimer);
});
