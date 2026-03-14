const http = require("http");
const { URL } = require("url");
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const FORCE_LOCAL_PORT = process.env.FORCE_PORT_3000 === "1";
const PORT = FORCE_LOCAL_PORT
  ? 3000
  : Number.parseInt(process.env.PORT || "3000", 10);
const MAX_BODY_BYTES = 1024 * 1024; // 1 MB
const WAIT_TIMEOUT_MS = Number.parseInt(process.env.WAIT_TIMEOUT_MS || "30000", 10);
const LOCAL_PANEL_ENABLED = process.env.LOCAL_PANEL !== "0";
const LOCAL_PENDING_PATH = path.join(__dirname, "local_pending.json");
const LOCAL_CANCEL_PATH = path.join(__dirname, "local_cancel.jsonl");
const LOCAL_CANCEL_TMP = `${LOCAL_CANCEL_PATH}.processing`;
const LOCAL_SERVERS_PATH = path.join(__dirname, "local_servers.json");
const LOCAL_ENDPOINT_STATS_PATH = path.join(__dirname, "local_endpoint_stats.json");
const LOCAL_SERVERS_WINDOW_MS = 5000;
const LOCAL_POLL_MS = 1000;
const IP_LIKE_RE = /^\d+(?:\.\d+)+$/;

const requestsByKey = new Map();
const serverSettingsByIp = new Map();
const serverUsage = new Map();
const endpointStatsByKey = new Map();

function makeKey(ip, endpointPath) {
  const normalized = endpointPath || "";
  return `${ip}|${normalized}`;
}

function getListForKey(ip, endpointPath) {
  const key = makeKey(ip, endpointPath);
  if (!requestsByKey.has(key)) {
    requestsByKey.set(key, []);
  }
  return requestsByKey.get(key);
}

function sendJson(res, statusCode, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
    "Access-Control-Allow-Origin": "*",
  });
  res.end(body);
}

function sendText(res, statusCode, text) {
  res.writeHead(statusCode, {
    "Content-Type": "text/plain; charset=utf-8",
    "Content-Length": Buffer.byteLength(text),
    "Access-Control-Allow-Origin": "*",
  });
  res.end(text);
}

function parseBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];

    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        reject(new Error("Body too large"));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });

    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString("utf8");
      if (!raw) {
        resolve(null);
        return;
      }

      const contentType = (req.headers["content-type"] || "").toLowerCase();
      if (contentType.includes("application/json")) {
        try {
          resolve(JSON.parse(raw));
        } catch (err) {
          reject(new Error("Invalid JSON"));
        }
        return;
      }

      resolve(raw);
    });

    req.on("error", reject);
  });
}

function newId() {
  if (crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return crypto.randomBytes(16).toString("hex");
}

function isIpLike(value) {
  return IP_LIKE_RE.test(value);
}

function normalizeHost(value) {
  return value.trim().toLowerCase().replace(/\.$/, "");
}

function normalizeSettingKey(key) {
  return String(key).toLowerCase().replace(/[^a-z0-9]/g, "");
}

function extractSettingsFromObject(obj) {
  if (!obj || typeof obj !== "object") {
    return { serverName: null, description: null };
  }
  let serverName = null;
  let description = null;
  for (const [key, value] of Object.entries(obj)) {
    const normalized = normalizeSettingKey(key);
    if (normalized === "servername" && typeof value === "string") {
      serverName = value;
    } else if (normalized === "description" && typeof value === "string") {
      description = value;
    }
  }
  return { serverName, description };
}

function setServerSettings(ip, updates) {
  const current = serverSettingsByIp.get(ip) || {};
  const next = { ...current };
  if (typeof updates.ServerName === "string" && updates.ServerName.trim()) {
    next.ServerName = normalizeHost(updates.ServerName);
  }
  if (typeof updates.Description === "string") {
    next.Description = updates.Description.trim();
  }
  serverSettingsByIp.set(ip, next);
}

function getServerName(ip) {
  const settings = serverSettingsByIp.get(ip);
  return settings && typeof settings.ServerName === "string" ? settings.ServerName : null;
}

function findIpByServerName(name) {
  const target = normalizeHost(name);
  for (const [ip, settings] of serverSettingsByIp.entries()) {
    if (settings && settings.ServerName === target) {
      return ip;
    }
  }
  return null;
}

function makeEndpointStatsKey(ip, endpointPath) {
  return `${ip}|${endpointPath || ""}`;
}

function stableQueryString(queryObj) {
  const keys = Object.keys(queryObj || {}).sort();
  if (keys.length === 0) {
    return "(no query)";
  }
  const parts = [];
  keys.forEach((key) => {
    const value = queryObj[key];
    if (Array.isArray(value)) {
      value.forEach((v) => parts.push(`${key}=${String(v)}`));
    } else {
      parts.push(`${key}=${String(value)}`);
    }
  });
  return parts.join("&");
}

function recordEndpointRequest(ip, endpointPath, serverName, method, queryObj) {
  const key = makeEndpointStatsKey(ip, endpointPath);
  const existing = endpointStatsByKey.get(key) || {
    ip,
    endpoint: endpointPath || "",
    serverName: serverName || null,
    totalRequests: 0,
    methodCounts: { GET: 0, POST: 0 },
    lastRequestAt: null,
    totalResponses: 0,
    sumResponseDelayMs: 0,
    lastResponseDelayMs: null,
    lastResponseAt: null,
    queries: new Map(),
  };

  existing.serverName = serverName || existing.serverName;
  existing.totalRequests += 1;
  if (method === "GET" || method === "POST") {
    existing.methodCounts[method] += 1;
  }
  existing.lastRequestAt = new Date().toISOString();

  const queryKey = stableQueryString(queryObj);
  const queryEntry = existing.queries.get(queryKey) || { count: 0 };
  queryEntry.count += 1;
  existing.queries.set(queryKey, queryEntry);

  endpointStatsByKey.set(key, existing);
}

function recordEndpointResponse(ip, endpointPath, delayMs) {
  const key = makeEndpointStatsKey(ip, endpointPath);
  const existing = endpointStatsByKey.get(key);
  if (!existing) {
    return;
  }
  existing.totalResponses += 1;
  existing.sumResponseDelayMs += Math.max(0, delayMs);
  existing.lastResponseDelayMs = Math.max(0, delayMs);
  existing.lastResponseAt = new Date().toISOString();
}

function writeLocalEndpointStats() {
  if (!LOCAL_PANEL_ENABLED) {
    return;
  }
  const endpoints = [];
  for (const stats of endpointStatsByKey.values()) {
    const queries = [];
    for (const [query, info] of stats.queries.entries()) {
      queries.push({ query, count: info.count });
    }
    endpoints.push({
      ip: stats.ip,
      serverName: stats.serverName || null,
      endpoint: stats.endpoint,
      totalRequests: stats.totalRequests,
      methodCounts: stats.methodCounts,
      lastRequestAt: stats.lastRequestAt,
      totalResponses: stats.totalResponses,
      avgResponseDelayMs:
        stats.totalResponses > 0
          ? Math.round(stats.sumResponseDelayMs / stats.totalResponses)
          : null,
      lastResponseDelayMs: stats.lastResponseDelayMs,
      lastResponseAt: stats.lastResponseAt,
      queries,
    });
  }
  const payload = {
    updatedAt: new Date().toISOString(),
    endpoints,
  };
  const tmpPath = `${LOCAL_ENDPOINT_STATS_PATH}.tmp`;
  fs.writeFile(tmpPath, JSON.stringify(payload), (err) => {
    if (err) {
      return;
    }
    fs.rename(tmpPath, LOCAL_ENDPOINT_STATS_PATH, () => {});
  });
}

function searchParamsToObject(params) {
  const obj = {};
  for (const [key, value] of params) {
    if (Object.prototype.hasOwnProperty.call(obj, key)) {
      if (Array.isArray(obj[key])) {
        obj[key].push(value);
      } else {
        obj[key] = [obj[key], value];
      }
    } else {
      obj[key] = value;
    }
  }
  return obj;
}

function parsePlainTextPayload(text) {
  const trimmed = text.trim();
  if (!trimmed) {
    return {};
  }

  if (
    (trimmed.startsWith("{") && trimmed.endsWith("}")) ||
    (trimmed.startsWith("[") && trimmed.endsWith("]"))
  ) {
    try {
      return JSON.parse(trimmed);
    } catch (err) {
      // Fall through to other parsing.
    }
  }

  if (trimmed.includes("=")) {
    const params = new URLSearchParams(trimmed);
    return searchParamsToObject(params);
  }

  return { response: trimmed };
}

function removeWaiter(entry, waiter) {
  entry.waiters = entry.waiters.filter((item) => item !== waiter);
}

function buildWaiterPayload(entry) {
  if (entry.cancelled) {
    return {
      statusCode: 200,
      payload: {
        ok: false,
        id: entry.id,
        error: entry.cancelReason || "Cancelled",
        cancelled: true,
        respondedAt: entry.respondedAt,
      },
    };
  }

  return {
    statusCode: 200,
    payload: {
      ok: true,
      id: entry.id,
      response: entry.response,
      respondedAt: entry.respondedAt,
    },
  };
}

function respondToWaiter(entry, waiter) {
  if (waiter.responded) {
    return;
  }
  waiter.responded = true;
  clearTimeout(waiter.timeoutId);
  const { statusCode, payload } = buildWaiterPayload(entry);
  sendJson(waiter.res, statusCode, payload);
}

function notifyWaiters(entry) {
  if (!entry.waiters || entry.waiters.length === 0) {
    return;
  }
  entry.waiters.forEach((waiter) => respondToWaiter(entry, waiter));
  entry.waiters = [];
}

function toPublicEntry(entry) {
  return {
    id: entry.id,
    ip: entry.ip,
    method: entry.method,
    data: entry.data,
    query: entry.query,
    endpoint: entry.endpoint || null,
    source: entry.source || null,
    receivedAt: entry.receivedAt,
    responded: entry.responded,
    cancelled: entry.cancelled || false,
    cancelReason: entry.cancelReason || null,
    response: entry.response,
    respondedAt: entry.respondedAt,
  };
}

function getAllPending() {
  const pending = [];
  for (const list of requestsByKey.values()) {
    for (const entry of list) {
      if (!entry.responded) {
        pending.push(entry);
      }
    }
  }
  return pending;
}

function updateServerUsage(ip, endpointPath, source) {
  const now = Date.now();
  const endpointKey = endpointPath || "";
  if (!serverUsage.has(ip)) {
    serverUsage.set(ip, new Map());
  }
  const endpoints = serverUsage.get(ip);
  const entry = endpoints.get(endpointKey) || {
    lastServer: 0,
    lastSend: 0,
    lastDelayMs: null,
  };
  if (source === "server") {
    entry.lastServer = now;
  }
  if (source === "send") {
    entry.lastSend = now;
  }
  endpoints.set(endpointKey, entry);
}

function setEndpointDelay(ip, endpointPath, delayMs) {
  const endpointKey = endpointPath || "";
  if (!serverUsage.has(ip)) {
    serverUsage.set(ip, new Map());
  }
  const endpoints = serverUsage.get(ip);
  const entry = endpoints.get(endpointKey) || {
    lastServer: 0,
    lastSend: 0,
    lastDelayMs: null,
  };
  entry.lastDelayMs = typeof delayMs === "number" ? Math.max(0, delayMs) : null;
  endpoints.set(endpointKey, entry);
}

function getServerLastSeen(ip) {
  const endpoints = serverUsage.get(ip);
  if (!endpoints) {
    return 0;
  }
  let lastSeen = 0;
  for (const entry of endpoints.values()) {
    if (entry.lastServer && entry.lastServer > lastSeen) {
      lastSeen = entry.lastServer;
    }
  }
  return lastSeen;
}

function isServerActive(ip) {
  const lastSeen = getServerLastSeen(ip);
  return lastSeen && Date.now() - lastSeen <= LOCAL_SERVERS_WINDOW_MS;
}

function writeLocalServers() {
  if (!LOCAL_PANEL_ENABLED) {
    return;
  }

  const now = Date.now();
  const servers = [];
  const allIps = new Set([
    ...serverUsage.keys(),
    ...serverSettingsByIp.keys(),
  ]);

  for (const ip of allIps) {
    const endpoints = serverUsage.get(ip) || new Map();
    const endpointList = [];
    for (const [endpoint, entry] of endpoints.entries()) {
      endpointList.push({
        endpoint: endpoint || null,
        lastSeen: entry.lastServer || 0,
        lastServer: entry.lastServer || 0,
        lastSend: entry.lastSend || null,
        lastDelayMs: entry.lastDelayMs ?? null,
      });
    }
    const lastServerAt = getServerLastSeen(ip);
    const active = lastServerAt && now - lastServerAt <= LOCAL_SERVERS_WINDOW_MS;
    const serverName = getServerName(ip);
    const settings = serverSettingsByIp.get(ip) || {};
    servers.push({
      ip,
      serverName: serverName || null,
      description: typeof settings.Description === "string" ? settings.Description : null,
      active,
      lastServerAt: lastServerAt || null,
      endpoints: endpointList,
    });
  }

  const payload = {
    updatedAt: new Date().toISOString(),
    windowMs: LOCAL_SERVERS_WINDOW_MS,
    servers,
  };

  const tmpPath = `${LOCAL_SERVERS_PATH}.tmp`;
  fs.writeFile(tmpPath, JSON.stringify(payload), (err) => {
    if (err) {
      return;
    }
    fs.rename(tmpPath, LOCAL_SERVERS_PATH, () => {});
  });
}

function writeLocalPending() {
  if (!LOCAL_PANEL_ENABLED) {
    return;
  }

  const pending = getAllPending().map((entry) => toPublicEntry(entry));
  const payload = {
    updatedAt: new Date().toISOString(),
    count: pending.length,
    pending,
  };
  const tmpPath = `${LOCAL_PENDING_PATH}.tmp`;
  fs.writeFile(tmpPath, JSON.stringify(payload), (err) => {
    if (err) {
      return;
    }
    fs.rename(tmpPath, LOCAL_PENDING_PATH, () => {});
  });
}

function cancelEntryById(requestId, reason) {
  for (const list of requestsByKey.values()) {
    const entry = list.find((item) => item.id === requestId);
    if (entry) {
      if (entry.responded) {
        return { ok: false, statusCode: 409, error: "Already responded." };
      }
      entry.responded = true;
      entry.cancelled = true;
      entry.cancelReason = reason || "Cancelled";
      entry.respondedAt = new Date().toISOString();
      notifyWaiters(entry);
      writeLocalPending();
      return { ok: true, statusCode: 200 };
    }
  }
  return { ok: false, statusCode: 404, error: "Request id not found." };
}

function waitForResponse(entry, res) {
  if (entry.responded) {
    const { statusCode, payload } = buildWaiterPayload(entry);
    sendJson(res, statusCode, payload);
    return;
  }

  const waiter = {
    res,
    responded: false,
    timeoutId: null,
  };

  waiter.timeoutId = setTimeout(() => {
    if (waiter.responded) {
      return;
    }
    waiter.responded = true;
    removeWaiter(entry, waiter);
    entry.responded = true;
    entry.cancelled = true;
    entry.cancelReason = "Response timeout";
    entry.respondedAt = new Date().toISOString();
    notifyWaiters(entry);
    writeLocalPending();
    const { statusCode, payload } = buildWaiterPayload(entry);
    sendJson(res, statusCode, payload);
  }, WAIT_TIMEOUT_MS);

  entry.waiters.push(waiter);

  res.on("close", () => {
    if (waiter.responded) {
      return;
    }
    waiter.responded = true;
    clearTimeout(waiter.timeoutId);
    removeWaiter(entry, waiter);
  });
}


const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || "localhost"}`);
  const path = url.pathname;
  const pathParts = path.split("/").filter(Boolean);
  const remoteAddress = req.socket && req.socket.remoteAddress ? req.socket.remoteAddress : "unknown";
  const now = new Date().toISOString();
  // eslint-disable-next-line no-console
  console.log(`[${now}] ${remoteAddress} ${req.method} ${url.pathname}${url.search}`);

  if (req.method === "OPTIONS") {
    res.writeHead(204, {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
    });
    res.end();
    return;
  }

  if (path === "/" && req.method === "GET") {
    sendJson(res, 200, {
      ok: true,
      endpoints: {
        send: "/send/{ip-like}",
        server: "/server/{ip-like}",
        serverSettings: "/serversettings/{ip-like}",
      },
    });
    return;
  }

  const serverSettingsMatch = path.match(/^\/serversettings\/([^/]+)$/);
  if (serverSettingsMatch) {
    const ip = decodeURIComponent(serverSettingsMatch[1]);
    if (!isIpLike(ip)) {
      sendJson(res, 400, { ok: false, error: "Invalid ip-like value." });
      return;
    }

    if (req.method === "GET") {
      const settings = serverSettingsByIp.get(ip) || null;
      sendJson(res, 200, { ok: true, ip, settings });
      return;
    }

    if (req.method === "POST") {
      try {
        const payload = await parseBody(req);
        const query = searchParamsToObject(url.searchParams);
        let serverName = null;
        let description = null;

        if (payload && typeof payload === "object") {
          const extracted = extractSettingsFromObject(payload);
          serverName = extracted.serverName;
          description = extracted.description;
        } else if (typeof payload === "string" && payload.trim()) {
          const parsed = parsePlainTextPayload(payload);
          const extracted = extractSettingsFromObject(parsed);
          if (extracted.serverName || extracted.description) {
            serverName = extracted.serverName;
            description = extracted.description;
          } else if (parsed && typeof parsed === "object" && Object.keys(parsed).length > 0) {
            sendJson(res, 400, { ok: false, error: "Missing ServerName or Description in payload." });
            return;
          } else {
            serverName = payload.trim();
          }
        }

        if (query && typeof query === "object") {
          const extracted = extractSettingsFromObject(query);
          if (extracted.serverName) {
            serverName = extracted.serverName;
          }
          if (extracted.description) {
            description = extracted.description;
          }
        }

        if (!serverName && typeof description !== "string") {
          sendJson(res, 400, { ok: false, error: "Missing ServerName or Description in payload." });
          return;
        }

        setServerSettings(ip, { ServerName: serverName, Description: description });
        sendJson(res, 200, { ok: true, ip, settings: serverSettingsByIp.get(ip) });
      } catch (err) {
        sendJson(res, 400, { ok: false, error: err.message });
      }
      return;
    }

    sendText(res, 405, "Method Not Allowed");
    return;
  }

  if (pathParts[0] === "send" && pathParts.length >= 2) {
    const host = decodeURIComponent(pathParts[1]);
    const targetIp = findIpByServerName(host);
    if (!targetIp) {
      const hint = isIpLike(host)
        ? "IP values are not accepted on /send. Use a ServerName instead."
        : "Unknown server name. Set it via /serversettings/{ip}.";
      sendJson(res, 404, { ok: false, error: hint });
      return;
    }
    if (!isServerActive(targetIp)) {
      sendJson(res, 503, { ok: false, error: "server is real but is not working" });
      return;
    }

  const endpointPath = pathParts.slice(2).join("/");
  const list = getListForKey(targetIp, endpointPath);
  const query = searchParamsToObject(url.searchParams);
  updateServerUsage(targetIp, endpointPath, "send");
  writeLocalServers();
  if (req.method === "POST") {
    try {
      const data = await parseBody(req);
      recordEndpointRequest(
        targetIp,
        endpointPath,
        getServerName(targetIp),
        req.method,
        query
      );
      writeLocalEndpointStats();
      const entry = {
          id: newId(),
          ip: targetIp,
          host: normalizeHost(host),
          method: "POST",
          data,
          query,
          endpoint: endpointPath || null,
          source: "send",
          receivedAt: new Date().toISOString(),
          sentAtMs: Date.now(),
          responded: false,
          cancelled: false,
          cancelReason: null,
          response: null,
          respondedAt: null,
          waiters: [],
        };
        list.push(entry);
        writeLocalPending();
        waitForResponse(entry, res);
      } catch (err) {
        sendJson(res, 400, { ok: false, error: err.message });
      }
      return;
    }

    if (req.method === "GET") {
      recordEndpointRequest(
        targetIp,
        endpointPath,
        getServerName(targetIp),
        req.method,
        query
      );
      writeLocalEndpointStats();
      const entry = {
        id: newId(),
        ip: targetIp,
        host: normalizeHost(host),
        method: "GET",
        data: null,
        query,
        endpoint: endpointPath || null,
        source: "send",
        receivedAt: new Date().toISOString(),
        sentAtMs: Date.now(),
        responded: false,
        cancelled: false,
        cancelReason: null,
        response: null,
        respondedAt: null,
        waiters: [],
      };
      list.push(entry);
      writeLocalPending();
      waitForResponse(entry, res);
      return;
    }

    sendText(res, 405, "Method Not Allowed");
    return;
  }

  if (pathParts[0] === "server" && pathParts.length >= 2) {
    const ip = decodeURIComponent(pathParts[1]);
    if (!isIpLike(ip)) {
      sendJson(res, 400, { ok: false, error: "Invalid ip-like value." });
      return;
    }

    const endpointPath = pathParts.slice(2).join("/");
    const list = getListForKey(ip, endpointPath);
    updateServerUsage(ip, endpointPath, "server");
    writeLocalServers();

    if (req.method === "GET") {
      const pending = list
        .filter((entry) => !entry.responded)
        .map((entry) => toPublicEntry(entry));
      sendJson(res, 200, { ok: true, ip, pending });
      return;
    }

    if (req.method === "POST") {
      try {
        const payload = await parseBody(req);
        const query = searchParamsToObject(url.searchParams);
        let payloadObj = null;

        if (payload && typeof payload === "object") {
          payloadObj = payload;
        } else if (typeof payload === "string") {
          payloadObj = parsePlainTextPayload(payload);
        }

        const id =
          (payloadObj && payloadObj.id) ||
          (typeof query.id === "string" ? query.id : null);

        let response = null;
        if (payloadObj && Object.prototype.hasOwnProperty.call(payloadObj, "response")) {
          response = payloadObj.response;
        } else if (typeof payload === "string" && payload.trim().length > 0) {
          response = payload;
        } else if (payloadObj && typeof payloadObj === "object") {
          const clone = { ...payloadObj };
          if (Object.prototype.hasOwnProperty.call(clone, "id")) {
            delete clone.id;
          }
          if (Object.keys(clone).length > 0) {
            response = clone;
          }
        }

        if (!id) {
          sendJson(res, 400, { ok: false, error: "Missing id in payload." });
          return;
        }

        const entry = list.find((item) => item.id === id);
        if (!entry) {
          sendJson(res, 404, { ok: false, error: "Request id not found." });
          return;
        }
        if (entry.responded) {
          sendJson(res, 409, { ok: false, error: "Request already resolved." });
          return;
        }

        entry.responded = true;
        entry.response = response;
        entry.respondedAt = new Date().toISOString();
      if (entry.sentAtMs) {
        const delayMs = Date.now() - entry.sentAtMs;
        setEndpointDelay(entry.ip, entry.endpoint, delayMs);
        recordEndpointResponse(entry.ip, entry.endpoint, delayMs);
      }
      notifyWaiters(entry);
      writeLocalPending();
      writeLocalServers();
      writeLocalEndpointStats();
      sendJson(res, 200, { ok: true });
      } catch (err) {
        sendJson(res, 400, { ok: false, error: err.message });
      }
      return;
    }

    sendText(res, 405, "Method Not Allowed");
    return;
  }

  sendJson(res, 404, { ok: false, error: "Not Found" });
});

server.listen(PORT, () => {
  // eslint-disable-next-line no-console
  console.log(`Listening on port ${PORT}`);
});

if (LOCAL_PANEL_ENABLED) {
  writeLocalPending();
  writeLocalServers();
  writeLocalEndpointStats();
  setInterval(() => {
    writeLocalServers();
    fs.rename(LOCAL_CANCEL_PATH, LOCAL_CANCEL_TMP, (err) => {
      if (err) {
        return;
      }
      fs.readFile(LOCAL_CANCEL_TMP, "utf8", (readErr, data) => {
        fs.unlink(LOCAL_CANCEL_TMP, () => {});
        if (readErr) {
          return;
        }
        const lines = data.split(/\r?\n/).filter((line) => line.trim().length > 0);
        lines.forEach((line) => {
          let id = null;
          let reason = "Local cancel";
          try {
            const parsed = JSON.parse(line);
            if (typeof parsed === "string") {
              id = parsed;
            } else if (parsed && typeof parsed === "object") {
              id = parsed.id || parsed.requestId || null;
              if (parsed.reason) {
                reason = parsed.reason;
              }
            }
          } catch (parseErr) {
            id = line.trim();
          }
          if (id) {
            cancelEntryById(id, reason);
          }
        });
      });
    });
  }, LOCAL_POLL_MS);
}
