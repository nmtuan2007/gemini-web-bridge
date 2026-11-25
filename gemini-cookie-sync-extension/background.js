const NATIVE_HOST_NAME = "com.gemini_web2api.host";
const KEEPALIVE_ALARM = "gemini-sync-keepalive";
const DEFAULT_SERVER_URL = "http://127.0.0.1:8081";

const CORE_REQUIRED = ["SAPISID"];
const SESSION_ALTERNATIVES = ["__Secure-1PSID", "__Secure-3PSID", "SID"];
const LEGACY_COOKIES = ["SID", "HSID", "SSID", "APISID", "SAPISID"];

const EXPORT_ORDER = [
  "SID",
  "HSID",
  "SSID",
  "APISID",
  "SAPISID",
  "LSID",
  "OSID",
  "SIDCC",
  "AEC",
  "NID",
  "COMPASS",
  "__Secure-1PAPISID",
  "__Secure-1PSID",
  "__Secure-1PSIDTS",
  "__Secure-1PSIDCC",
  "__Secure-1PSIDRTS",
  "__Secure-3PAPISID",
  "__Secure-3PSID",
  "__Secure-3PSIDTS",
  "__Secure-3PSIDCC",
  "__Secure-3PSIDRTS",
  "__Secure-OSID",
  "__Host-1PLSID",
  "__Host-3PLSID"
];

const EXPORT_SET = new Set(EXPORT_ORDER);

const LOOKUP_URLS = [
  "https://gemini.google.com/app",
  "https://accounts.google.com/",
  "https://www.google.com/",
  "https://google.com/",
  "http://www.google.com/",
  "http://google.com/"
];

const CORE_SYNC_COOKIES = new Set(["SID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID"]);
const CORE_AUTO_SYNC_COOLDOWN_MS = 10000;
const MINOR_AUTO_SYNC_COOLDOWN_MS = 45000;

let cookieSyncTimer = null;
let lastAutoSyncAt = 0;
let pendingPrioritySync = false;

function normalizeDomain(domain = "") {
  return domain.replace(/^\./, "").toLowerCase();
}

function isGoogleCookie(cookie) {
  const domain = normalizeDomain(cookie.domain);
  return domain === "google.com" || domain.endsWith(".google.com");
}

function cookieKey(cookie) {
  const partition = cookie.partitionKey ? JSON.stringify(cookie.partitionKey) : "";
  return [
    cookie.storeId || "",
    cookie.name,
    cookie.domain,
    cookie.path,
    partition
  ].join("|");
}

function scoreCookie(cookie) {
  const domain = (cookie.domain || "").toLowerCase();
  let score = 0;

  if (domain === ".google.com") score += 120;
  else if (domain === "google.com") score += 110;
  else if (domain === ".gemini.google.com") score += 100;
  else if (domain === "gemini.google.com") score += 95;
  else if (domain === ".accounts.google.com") score += 80;
  else if (domain === "accounts.google.com") score += 75;
  else if (domain.endsWith(".google.com")) score += 40;

  if (cookie.path === "/") score += 10;
  if (cookie.secure) score += 3;
  if (cookie.httpOnly) score += 2;
  if (!cookie.partitionKey) score += 2;
  if (!cookie.session) score += 1;

  return score;
}

async function readGoogleCookies() {
  const stores = await chrome.cookies.getAllCookieStores();
  const deduped = new Map();

  for (const store of stores) {
    const queries = [
      chrome.cookies.getAll({ storeId: store.id }),
      ...LOOKUP_URLS.map((url) => chrome.cookies.getAll({ storeId: store.id, url }))
    ];
    try {
      queries.push(chrome.cookies.getAll({ storeId: store.id, partitionKey: {} }));
      for (const url of LOOKUP_URLS) {
        queries.push(chrome.cookies.getAll({ storeId: store.id, url, partitionKey: {} }));
      }
    } catch {
      // Ignore on browsers without partitionKey filter support.
    }

    const results = await Promise.allSettled(queries);
    for (const result of results) {
      if (result.status !== "fulfilled") continue;
      for (const cookie of result.value) {
        if (!isGoogleCookie(cookie) || !cookie.value) continue;
        deduped.set(cookieKey(cookie), cookie);
      }
    }
  }

  return [...deduped.values()];
}

function selectBestCookies(cookies) {
  const selected = new Map();
  for (const name of EXPORT_ORDER) {
    const candidates = cookies
      .filter((cookie) => cookie.name === name && cookie.value)
      .sort((a, b) => scoreCookie(b) - scoreCookie(a));
    if (candidates.length > 0) selected.set(name, candidates[0]);
  }
  return selected;
}

function validateSelection(selected) {
  const missingCore = CORE_REQUIRED.filter((name) => !selected.has(name));
  const sessionCookie = SESSION_ALTERNATIVES.find((name) => selected.has(name));
  return {
    missingCore,
    sessionCookie,
    valid: missingCore.length === 0 && Boolean(sessionCookie)
  };
}

function listPresent(selected, names) {
  return names.filter((name) => selected.has(name));
}

function summarizeAvailable(cookies) {
  return [...new Set(cookies
    .map((cookie) => cookie.name)
    .filter((name) => /SID|APISID|LSID|OSID|COMPASS/.test(name)))]
    .sort();
}

function chooseGeminiTab(tabs) {
  return tabs.find((tab) => tab.active) || tabs[0] || null;
}

function getActiveGeminiAccount(tabs) {
  const ordered = [];
  const chosen = chooseGeminiTab(tabs);
  if (chosen) ordered.push(chosen);
  for (const tab of tabs) {
    if (tab !== chosen) ordered.push(tab);
  }
  for (const tab of ordered) {
    try {
      const url = new URL(tab.url || "");
      if (url.hostname !== "gemini.google.com") continue;
      const match = url.pathname.match(/^\/u\/(\d+)(?:\/|$)/);
      return match ? match[1] : null;
    } catch {
      // Ignore malformed URLs.
    }
  }
  return null;
}

function decodeJsEscaped(value) {
  if (!value) return null;
  try {
    return JSON.parse(`"${value.replace(/"/g, '\\"')}"`);
  } catch {
    return value
      .replace(/\\u003d/gi, "=")
      .replace(/\\u0026/gi, "&")
      .replace(/\\u003c/gi, "<")
      .replace(/\\u003e/gi, ">");
  }
}

function extractWizFieldFromHtml(html, name) {
  const patterns = [
    new RegExp(`"${name}"\\s*:\\s*"([^"\\n]+)"`),
    new RegExp(`\\\\"${name}\\\\"\\s*:\\s*\\\\"([^"\\n]+)\\\\"`)
  ];
  for (const pattern of patterns) {
    const match = html.match(pattern);
    if (match?.[1]) return decodeJsEscaped(match[1]);
  }
  return null;
}

async function fetchGeminiMetadataDirect(authUser) {
  const prefix = authUser ? `/u/${authUser}` : "";
  const url = `https://gemini.google.com${prefix}/app`;
  try {
    const resp = await fetch(url, { credentials: "include", redirect: "follow" });
    const html = await resp.text();
    const xsrfToken = extractWizFieldFromHtml(html, "SNlM0e");
    const geminiBl = extractWizFieldFromHtml(html, "cfb2h");
    if (!xsrfToken) {
      return {
        xsrfToken: null,
        geminiBl: geminiBl || null,
        source: null,
        error: "Not signed in to Gemini in Chrome. Open gemini.google.com and sign in."
      };
    }
    return {
      xsrfToken,
      geminiBl: geminiBl || null,
      source: "background-fetch",
      url: resp.url
    };
  } catch (error) {
    return {
      xsrfToken: null,
      geminiBl: null,
      source: null,
      error: error?.message || String(error)
    };
  }
}

async function readGeminiPageMetadata(tabs, savedAuthUser) {
  const tab = chooseGeminiTab(tabs);
  if (tab?.id) {
    try {
      const result = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        world: "MAIN",
        func: () => {
          const wiz = globalThis.WIZ_global_data || {};
          const html = document.documentElement?.innerHTML || "";
          const decode = (value) => {
            if (!value) return null;
            try {
              return JSON.parse(`"${value.replace(/"/g, '\\"')}"`);
            } catch {
              return value
                .replace(/\\u003d/gi, "=")
                .replace(/\\u0026/gi, "&")
                .replace(/\\u003c/gi, "<")
                .replace(/\\u003e/gi, ">");
            }
          };
          const regexValue = (name) => {
            const patterns = [
              new RegExp(`"${name}"\\s*:\\s*"([^"\\n]+)"`),
              new RegExp(`\\\\"${name}\\\\"\\s*:\\s*\\\\"([^"\\n]+)\\\\"`)
            ];
            for (const pattern of patterns) {
              const match = html.match(pattern);
              if (match?.[1]) return decode(match[1]);
            }
            return null;
          };
          let resourceBl = null;
          try {
            for (const entry of performance.getEntriesByType("resource")) {
              if (!entry?.name?.includes("gemini.google.com")) continue;
              const bl = new URL(entry.name).searchParams.get("bl");
              if (bl) {
                resourceBl = bl;
                break;
              }
            }
          } catch {
            // Ignore performance API errors.
          }
          const xsrfToken = wiz.SNlM0e || regexValue("SNlM0e");
          const geminiBl = wiz.cfb2h || resourceBl || regexValue("cfb2h");
          let source = null;
          if (wiz.SNlM0e || wiz.cfb2h) source = "WIZ_global_data";
          else if (resourceBl) source = "performance";
          else if (xsrfToken || geminiBl) source = "page-html";
          return {
            xsrfToken: xsrfToken || null,
            geminiBl: geminiBl || null,
            source,
            url: location.href
          };
        }
      });
      const tabData = result?.[0]?.result;
      if (tabData?.xsrfToken) return tabData;
    } catch {
      // Fall back to direct background fetch below.
    }
  }
  return fetchGeminiMetadataDirect(savedAuthUser);
}

async function buildInspection() {
  const stored = await chrome.storage.local.get(["authUser"]);
  const [cookies, tabs] = await Promise.all([
    readGoogleCookies(),
    chrome.tabs.query({ url: "https://gemini.google.com/*" })
  ]);

  const selected = selectBestCookies(cookies);
  const validation = validateSelection(selected);
  const legacyPresent = listPresent(selected, LEGACY_COOKIES);
  const legacyMissing = LEGACY_COOKIES.filter((name) => !selected.has(name));
  const available = summarizeAvailable(cookies);
  const tabAuthUser = getActiveGeminiAccount(tabs);
  const authUser = tabs.length > 0 ? tabAuthUser : (stored.authUser ?? null);
  const pageMetadata = await readGeminiPageMetadata(tabs, authUser);

  if (tabs.length > 0) {
    await chrome.storage.local.set({ authUser: tabAuthUser });
  }

  return {
    cookies,
    selected,
    validation,
    legacyPresent,
    legacyMissing,
    available,
    authUser,
    pageMetadata
  };
}

function inspectionMessage(info) {
  const {
    cookies,
    selected,
    validation,
    legacyPresent,
    legacyMissing,
    available,
    authUser,
    pageMetadata
  } = info;

  const lines = [
    `Found ${cookies.length} cookie(s) across Google domains.`,
    "",
    `SAPISID: ${selected.has("SAPISID") ? "present" : "missing"}`,
    `Session cookie: ${validation.sessionCookie || "missing"}`,
    `XSRF / SNlM0e: ${pageMetadata.xsrfToken ? "present" : "missing"}`,
    `gemini_bl / cfb2h: ${pageMetadata.geminiBl ? "present" : "missing"}`,
    `auth_user: ${authUser ?? "default account"}`,
    `Page data source: ${pageMetadata.source || "unavailable"}`,
    "",
    `Legacy present: ${legacyPresent.join(", ") || "none"}`,
    `Legacy missing: ${legacyMissing.join(", ") || "none"}`,
    "",
    `Available for export: ${available.join(", ") || "none"}`
  ];

  if (pageMetadata.error) {
    lines.push("", `Note: ${pageMetadata.error}`);
  }
  if (validation.valid && pageMetadata.xsrfToken) {
    lines.push("", "Session and XSRF are ready.");
  } else if (validation.valid) {
    lines.push("", "Cookies found, but XSRF is missing. Sign in to gemini.google.com first.");
  } else {
    lines.push("", "Session incomplete: SAPISID and one session cookie are required.");
  }
  return lines.join("\n");
}

function buildCookieString(info) {
  return EXPORT_ORDER
    .filter((name) => info.selected.has(name))
    .map((name) => `${name}=${info.selected.get(name).value}`)
    .join("; ");
}

function buildAuthPayload(info) {
  return {
    cookie: buildCookieString(info),
    sapisid: info.selected.get("SAPISID").value,
    auth_user: info.authUser,
    xsrf_token: info.pageMetadata.xsrfToken,
    gemini_bl: info.pageMetadata.geminiBl
  };
}

function sendNative(action, extra = {}) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendNativeMessage(NATIVE_HOST_NAME, { action, ...extra }, (response) => {
      const err = chrome.runtime.lastError;
      if (err) {
        reject(new Error(err.message));
        return;
      }
      resolve(response || { ok: false, error: "Empty response from native host" });
    });
  });
}

async function getStoredConfig() {
  const data = await chrome.storage.local.get(["enabled", "serverUrl", "lastSync"]);
  return {
    enabled: Boolean(data.enabled),
    serverUrl: (data.serverUrl || DEFAULT_SERVER_URL).replace(/\/+$/, ""),
    lastSync: data.lastSync || null
  };
}

async function updateBadge(running, authenticated) {
  try {
    if (!running) {
      await chrome.action.setBadgeText({ text: "OFF" });
      await chrome.action.setBadgeBackgroundColor({ color: "#6c757d" });
    } else if (authenticated) {
      await chrome.action.setBadgeText({ text: "ON" });
      await chrome.action.setBadgeBackgroundColor({ color: "#17823b" });
    } else {
      await chrome.action.setBadgeText({ text: "!" });
      await chrome.action.setBadgeBackgroundColor({ color: "#b34800" });
    }
  } catch {
    // Ignore badge errors.
  }
}

async function syncSession(serverUrlOverride = null) {
  const cfg = await getStoredConfig();
  const baseUrl = (serverUrlOverride || cfg.serverUrl).replace(/\/+$/, "");
  const info = await buildInspection();

  if (!info.validation.valid || !info.pageMetadata.xsrfToken) {
    throw new Error(inspectionMessage(info));
  }

  const payload = buildAuthPayload(info);
  const resp = await fetch(`${baseUrl}/admin/auth`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  const result = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(result?.error?.message || `Server returned HTTP ${resp.status}`);
  }

  lastAutoSyncAt = Date.now();
  const summary = {
    at: Date.now(),
    baseUrl,
    authenticated: Boolean(result.authenticated),
    hasSid: Boolean(result.has_sid),
    hasSapisid: Boolean(result.has_sapisid),
    hasSnlm0e: Boolean(result.has_snlm0e),
    cookieFile: result.cookie_file || null
  };
  await chrome.storage.local.set({ lastSync: summary, serverUrl: baseUrl });
  await updateBadge(true, summary.authenticated);
  return summary;
}

async function startAll(serverUrlOverride = null) {
  const cfg = await getStoredConfig();
  let baseUrl = (serverUrlOverride || cfg.serverUrl).replace(/\/+$/, "");

  const nativeResp = await sendNative("start");
  if (!nativeResp.ok || !nativeResp.running) {
    throw new Error(nativeResp.error || "Failed to start local server");
  }
  if (!serverUrlOverride && nativeResp.baseUrl) {
    baseUrl = nativeResp.baseUrl.replace(/\/+$/, "");
  }

  await chrome.storage.local.set({ enabled: true, serverUrl: baseUrl });
  await chrome.alarms.create(KEEPALIVE_ALARM, { periodInMinutes: 5 });

  let syncResult = null;
  let syncError = null;
  try {
    syncResult = await syncSession(baseUrl);
  } catch (err) {
    syncError = err?.message || String(err);
    await updateBadge(true, false);
  }

  return {
    ok: true,
    running: true,
    baseUrl,
    pid: nativeResp.pid || null,
    sync: syncResult,
    syncError
  };
}

async function stopAll(serverUrlOverride = null) {
  const cfg = await getStoredConfig();
  const baseUrl = (serverUrlOverride || cfg.serverUrl).replace(/\/+$/, "");

  await chrome.storage.local.set({ enabled: false });
  await chrome.alarms.clear(KEEPALIVE_ALARM);
  if (cookieSyncTimer) {
    clearTimeout(cookieSyncTimer);
    cookieSyncTimer = null;
  }

  let stopped = false;
  try {
    const nativeResp = await sendNative("stop");
    stopped = !nativeResp.running;
  } catch {
    try {
      const resp = await fetch(`${baseUrl}/admin/shutdown`, { method: "POST" });
      stopped = resp.ok;
    } catch {
      stopped = true;
    }
  }

  await updateBadge(false, false);
  return { ok: true, running: !stopped, baseUrl };
}

async function restartAll(serverUrlOverride = null) {
  const cfg = await getStoredConfig();
  let baseUrl = (serverUrlOverride || cfg.serverUrl).replace(/\/+$/, "");

  const nativeResp = await sendNative("restart");
  if (!nativeResp.ok || !nativeResp.running) {
    throw new Error(nativeResp.error || "Failed to restart local server");
  }
  if (!serverUrlOverride && nativeResp.baseUrl) {
    baseUrl = nativeResp.baseUrl.replace(/\/+$/, "");
  }

  await chrome.storage.local.set({ enabled: true, serverUrl: baseUrl });
  await chrome.alarms.create(KEEPALIVE_ALARM, { periodInMinutes: 5 });

  let syncResult = null;
  let syncError = null;
  try {
    syncResult = await syncSession(baseUrl);
  } catch (err) {
    syncError = err?.message || String(err);
    await updateBadge(true, false);
  }

  return {
    ok: true,
    running: true,
    baseUrl,
    pid: nativeResp.pid || null,
    sync: syncResult,
    syncError
  };
}

async function getLogs(serverUrlOverride = null, lines = 80) {
  const cfg = await getStoredConfig();
  const baseUrl = (serverUrlOverride || cfg.serverUrl).replace(/\/+$/, "");
  try {
    const resp = await fetch(`${baseUrl}/admin/logs?lines=${encodeURIComponent(String(lines))}`);
    if (resp.ok) {
      const data = await resp.json();
      return { ok: true, source: "memory", lines: data.lines || [] };
    }
  } catch {
    // Fall back to reading server.log via native host when server is stopped.
  }
  try {
    const nativeResp = await sendNative("logs", { lines });
    return {
      ok: Boolean(nativeResp.ok),
      source: "file",
      lines: nativeResp.lines || [],
      logFile: nativeResp.logFile || null,
      error: nativeResp.error || null
    };
  } catch (err) {
    return { ok: false, source: "unavailable", lines: [], error: err?.message || String(err) };
  }
}

async function updateRuntimeConfig(serverUrlOverride = null, patch = {}) {
  const cfg = await getStoredConfig();
  const baseUrl = (serverUrlOverride || cfg.serverUrl).replace(/\/+$/, "");
  const resp = await fetch(`${baseUrl}/admin/config`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch)
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data?.error?.message || `HTTP ${resp.status}`);
  }
  return { ok: true, config: data };
}

async function clearServerTelemetry(serverUrlOverride = null) {
  const cfg = await getStoredConfig();
  const baseUrl = (serverUrlOverride || cfg.serverUrl).replace(/\/+$/, "");
  const resp = await fetch(`${baseUrl}/admin/telemetry/clear`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}"
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data?.error?.message || `HTTP ${resp.status}`);
  }
  return { ok: true, telemetry: data.telemetry };
}

async function runToolCallProbe(serverUrlOverride = null, model = "gemini-4-neon") {
  const cfg = await getStoredConfig();
  const baseUrl = (serverUrlOverride || cfg.serverUrl).replace(/\/+$/, "");
  const startedAt = Date.now();
  const resp = await fetch(`${baseUrl}/v1/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model: model || "gemini-4-neon",
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
  if (!resp.ok) {
    throw new Error(data?.error?.message || `Probe failed with HTTP ${resp.status}`);
  }
  const choice = data?.choices?.[0] || {};
  const msg = choice.message || {};
  const toolCalls = Array.isArray(msg.tool_calls) ? msg.tool_calls : [];
  return {
    ok: true,
    durationMs,
    model: data.model || model,
    finishReason: choice.finish_reason || null,
    toolCalls,
    content: msg.content || null
  };
}

const SW_VERSION = "2.1.1";
let cachedPid = null;
let cachedNativeAvailable = true;

async function getFullState(options = {}) {
  const cfg = await getStoredConfig();
  const lightweight = Boolean(options?.lightweight);
  let running = false;
  let auth = null;
  let pid = cachedPid;
  let nativeAvailable = cachedNativeAvailable;

  if (lightweight) {
    try {
      const resp = await fetch(`${cfg.serverUrl}/admin/auth/status`);
      if (resp.ok) {
        running = true;
        auth = await resp.json();
      }
    } catch {
      running = false;
      pid = null;
    }
  } else {
    try {
      const nativeResp = await sendNative("status");
      nativeAvailable = true;
      running = Boolean(nativeResp.running);
      auth = nativeResp.auth || null;
      pid = nativeResp.pid || null;
      cachedPid = pid;
      cachedNativeAvailable = true;
    } catch {
      nativeAvailable = false;
      cachedNativeAvailable = false;
      try {
        const resp = await fetch(`${cfg.serverUrl}/admin/auth/status`);
        if (resp.ok) {
          running = true;
          auth = await resp.json();
        }
      } catch {
        running = false;
      }
    }
  }

  const authenticated = Boolean(auth?.authenticated ?? cfg.lastSync?.authenticated);
  await updateBadge(running, authenticated);
  return {
    swVersion: SW_VERSION,
    enabled: cfg.enabled,
    running,
    pid,
    nativeAvailable,
    serverUrl: cfg.serverUrl,
    auth,
    lastSync: cfg.lastSync
  };
}

chrome.cookies.onChanged.addListener((changeInfo) => {
  if (changeInfo.removed) return;
  const cookie = changeInfo.cookie;
  if (!cookie || !EXPORT_SET.has(cookie.name) || !isGoogleCookie(cookie)) return;

  if (CORE_SYNC_COOKIES.has(cookie.name)) {
    pendingPrioritySync = true;
  }
  if (cookieSyncTimer) clearTimeout(cookieSyncTimer);
  cookieSyncTimer = setTimeout(async () => {
    cookieSyncTimer = null;
    const isPriority = pendingPrioritySync;
    pendingPrioritySync = false;
    const cfg = await getStoredConfig();
    if (!cfg.enabled) return;
    const cooldown = isPriority ? CORE_AUTO_SYNC_COOLDOWN_MS : MINOR_AUTO_SYNC_COOLDOWN_MS;
    if (Date.now() - lastAutoSyncAt < cooldown) return;
    try {
      await syncSession(cfg.serverUrl);
    } catch {
      // Ignore transient errors during background cookie rotation.
    }
  }, 1500);
});

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== KEEPALIVE_ALARM) return;
  const cfg = await getStoredConfig();
  if (!cfg.enabled) return;
  try {
    const st = await sendNative("status");
    if (!st.running) {
      await sendNative("start");
    }
    await syncSession(cfg.serverUrl);
  } catch {
    await updateBadge(false, false);
  }
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  (async () => {
    switch (message?.type) {
      case "GET_STATE":
        return await getFullState();
      case "POLL_STATE":
        return await getFullState({ lightweight: true });
      case "START":
        return await startAll(message.serverUrl);
      case "STOP":
        return await stopAll(message.serverUrl);
      case "RESTART":
        return await restartAll(message.serverUrl);
      case "SYNC_NOW":
        return { ok: true, sync: await syncSession(message.serverUrl) };
      case "SET_SERVER_URL": {
        const nextUrl = (message.serverUrl || DEFAULT_SERVER_URL).trim().replace(/\/+$/, "");
        await chrome.storage.local.set({ serverUrl: nextUrl });
        return { ok: true, serverUrl: nextUrl };
      }
      case "GET_LOGS":
        return await getLogs(message.serverUrl, message.lines || 80);
      case "SET_CONFIG":
        return await updateRuntimeConfig(message.serverUrl, message.patch || {});
      case "CLEAR_TELEMETRY":
        return await clearServerTelemetry(message.serverUrl);
      case "TEST_PROBE":
        return await runToolCallProbe(message.serverUrl, message.model);
      case "INSPECT": {
        const info = await buildInspection();
        return {
          ok: true,
          ready: info.validation.valid && Boolean(info.pageMetadata.xsrfToken),
          message: inspectionMessage(info)
        };
      }
      case "EXPORT": {
        const info = await buildInspection();
        if (!info.validation.valid || !info.pageMetadata.xsrfToken) {
          throw new Error(inspectionMessage(info));
        }
        const exportedCount = EXPORT_ORDER.filter((name) => info.selected.has(name)).length;
        return {
          ok: true,
          payload: buildAuthPayload(info),
          summary:
            `Created gemini-auth.json with ${exportedCount} cookie(s) and XSRF.\n` +
            `Session cookie: ${info.validation.sessionCookie}\n` +
            `auth_user: ${info.authUser ?? "null"}`
        };
      }
      default:
        return { ok: false, error: "Unknown command" };
    }
  })()
    .then((result) => sendResponse(result))
    .catch((err) => sendResponse({ ok: false, error: err?.message || String(err) }));
  return true;
});
