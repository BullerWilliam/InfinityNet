const http = require("http");
const { URL } = require("url");
const crypto = require("crypto");

const PORT = process.env.PORT || 3000;
const MAX_BODY_BYTES = 1024 * 1024; // 1 MB
const WAIT_TIMEOUT_MS = Number.parseInt(process.env.WAIT_TIMEOUT_MS || "60000", 10);
const IP_LIKE_RE = /^\d+(?:\.\d+)+$/;

const requestsByIp = new Map();

function getListForIp(ip) {
  if (!requestsByIp.has(ip)) {
    requestsByIp.set(ip, []);
  }
  return requestsByIp.get(ip);
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

  const sendMatch = path.match(/^\/send\/([^/]+)$/);
  if (sendMatch) {
    const ip = decodeURIComponent(sendMatch[1]);
    if (!isIpLike(ip)) {
      sendJson(res, 400, { ok: false, error: "Invalid ip-like value." });
      return;
    }

    const list = getListForIp(ip);
    const query = searchParamsToObject(url.searchParams);
    const hasQuery = Object.keys(query).length > 0;

    if (req.method === "POST") {
      try {
        const data = await parseBody(req);
        const entry = {
          id: newId(),
          method: "POST",
          data,
          query,
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
      if (hasQuery) {
        const entry = {
          id: newId(),
          method: "GET",
          data: null,
          query,
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

      sendJson(res, 200, {
        ok: true,
        ip,
        requests: list,
      });
      return;
    }

    sendText(res, 405, "Method Not Allowed");
    return;
  }

  const serverMatch = path.match(/^\/server\/([^/]+)$/);
  if (serverMatch) {
    const ip = decodeURIComponent(serverMatch[1]);
    if (!isIpLike(ip)) {
      sendJson(res, 400, { ok: false, error: "Invalid ip-like value." });
      return;
    }

    const list = getListForIp(ip);

    if (req.method === "GET") {
      const pending = list.filter((entry) => !entry.responded);
      sendJson(res, 200, { ok: true, ip, pending });
      return;
    }

    if (req.method === "POST") {
      try {
        const payload = await parseBody(req);
        const id = payload && typeof payload === "object" ? payload.id : null;
        const response = payload && typeof payload === "object" ? payload.response : payload;

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
