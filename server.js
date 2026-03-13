const http = require("http");
const { URL } = require("url");
const crypto = require("crypto");

const FORCE_LOCAL_PORT = process.env.FORCE_PORT_3000 === "1";
const PORT = FORCE_LOCAL_PORT
  ? 3000
  : Number.parseInt(process.env.PORT || "3000", 10);
const MAX_BODY_BYTES = 1024 * 1024; // 1 MB
const WAIT_TIMEOUT_MS = Number.parseInt(process.env.WAIT_TIMEOUT_MS || "60000", 10);
const IP_LIKE_RE = /^\d+(?:\.\d+)+$/;

const requestsByKey = new Map();

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

function respondToWaiter(entry, waiter) {
  if (waiter.responded) {
    return;
  }
  waiter.responded = true;
  clearTimeout(waiter.timeoutId);
  sendJson(waiter.res, 200, {
    ok: true,
    id: entry.id,
    response: entry.response,
    respondedAt: entry.respondedAt,
  });
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
    method: entry.method,
    data: entry.data,
    query: entry.query,
    receivedAt: entry.receivedAt,
    responded: entry.responded,
    response: entry.response,
    respondedAt: entry.respondedAt,
  };
}

function waitForResponse(entry, res) {
  if (entry.responded) {
    sendJson(res, 200, {
      ok: true,
      id: entry.id,
      response: entry.response,
      respondedAt: entry.respondedAt,
    });
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
    sendJson(res, 504, {
      ok: false,
      id: entry.id,
      error: "Response timeout",
    });
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
      },
    });
    return;
  }

  if (
    pathParts[0] === "send" &&
    pathParts.length >= 3 &&
    pathParts[pathParts.length - 1] === "list"
  ) {
    const ip = decodeURIComponent(pathParts[1]);
    if (!isIpLike(ip)) {
      sendJson(res, 400, { ok: false, error: "Invalid ip-like value." });
      return;
    }

    if (req.method !== "GET") {
      sendText(res, 405, "Method Not Allowed");
      return;
    }

    const endpointPath = pathParts.slice(2, -1).join("/");
    const list = getListForKey(ip, endpointPath);
    sendJson(res, 200, {
      ok: true,
      ip,
      endpoint: endpointPath || null,
      requests: list.map((entry) => toPublicEntry(entry)),
    });
    return;
  }

  if (pathParts[0] === "send" && pathParts.length >= 2) {
    const ip = decodeURIComponent(pathParts[1]);
    if (!isIpLike(ip)) {
      sendJson(res, 400, { ok: false, error: "Invalid ip-like value." });
      return;
    }

    const endpointPath = pathParts.slice(2).join("/");
    const list = getListForKey(ip, endpointPath);
    const query = searchParamsToObject(url.searchParams);
    if (req.method === "POST") {
      try {
        const data = await parseBody(req);
        const entry = {
          id: newId(),
          method: "POST",
          data,
          query,
          endpoint: endpointPath || null,
          receivedAt: new Date().toISOString(),
          responded: false,
          response: null,
          respondedAt: null,
          waiters: [],
        };
        list.push(entry);
        waitForResponse(entry, res);
      } catch (err) {
        sendJson(res, 400, { ok: false, error: err.message });
      }
      return;
    }

    if (req.method === "GET") {
      const entry = {
        id: newId(),
        method: "GET",
        data: null,
        query,
        endpoint: endpointPath || null,
        receivedAt: new Date().toISOString(),
        responded: false,
        response: null,
        respondedAt: null,
        waiters: [],
      };
      list.push(entry);
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

        entry.responded = true;
        entry.response = response;
        entry.respondedAt = new Date().toISOString();
        notifyWaiters(entry);
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
