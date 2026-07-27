import { createServer } from "node:http";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { CodexCdpClient } from "./codex-cdp.mjs";

const execFileAsync = promisify(execFile);
const HOST = "127.0.0.1";
const PORT = Number(process.env.CODEX_KEYBOARD_PORT || 17373);
const client = new CodexCdpClient();
let cached = {
  connected: false,
  slots: Array.from({ length: 6 }, (_, id) => ({
    id, threadKey: null, title: null, status: "off", selected: false
  })),
  error: "Waiting for Codex",
  updatedAt: Date.now()
};
let refreshPromise = null;

async function focusCodex() {
  await execFileAsync("/usr/bin/open", ["-b", "com.openai.codex"], {
    timeout: 3000
  });
}

async function refresh() {
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    try {
      const snapshot = await client.snapshot();
      cached = { connected: true, ...snapshot, error: null, updatedAt: Date.now() };
    } catch (error) {
      cached = { ...cached, connected: false, error: error.message, updatedAt: Date.now() };
    }
  })();
  try {
    await refreshPromise;
  } finally {
    refreshPromise = null;
  }
}

function json(response, status, body) {
  response.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "Access-Control-Allow-Origin": "http://127.0.0.1"
  });
  response.end(`${JSON.stringify(body)}\n`);
}

const server = createServer(async (request, response) => {
  const url = new URL(request.url || "/", `http://${HOST}:${PORT}`);
  if (request.method === "GET" && url.pathname === "/health") {
    await refresh();
    return json(response, 200, { ok: true, codexConnected: cached.connected, updatedAt: cached.updatedAt });
  }
  if (request.method === "GET" && url.pathname === "/state") {
    if (url.searchParams.get("refresh") === "1" || Date.now() - cached.updatedAt > 29000) {
      await refresh();
    }
    return json(response, 200, cached);
  }
  if (request.method === "POST" && url.pathname === "/focus") {
    try {
      await focusCodex();
      return json(response, 200, { ok: true });
    } catch (error) {
      return json(response, 503, { ok: false, error: error.message });
    }
  }
  const match = request.method === "POST" && url.pathname.match(/^\/agent\/([0-5])\/click$/);
  if (match) {
    try {
      await Promise.all([
        client.clickAgent(Number(match[1])),
        focusCodex()
      ]);
      return json(response, 200, { ok: true });
    } catch (error) {
      return json(response, 503, { ok: false, error: error.message });
    }
  }
  const threadMatch = request.method === "POST" && url.pathname.match(
    /^\/thread\/([0-9a-f-]{36})\/click$/i
  );
  if (threadMatch) {
    try {
      const slot = Number(url.searchParams.get("slot") || 0);
      await Promise.all([
        client.clickThread(threadMatch[1], slot),
        focusCodex()
      ]);
      return json(response, 200, { ok: true, bridge: true });
    } catch (error) {
      return json(response, 503, {
        ok: false,
        bridge: false,
        error: error.message
      });
    }
  }
  const action = request.method === "POST" && url.pathname.match(
    /^\/action\/(fast|approve|reject|fork|mic|steer|submit)\/(down|up)$/
  );
  if (action) {
    const keys = {
      fast: "ACT06",
      approve: "ACT07",
      reject: "ACT08",
      fork: "ACT09",
      mic: "ACT10",
      submit: "ACT12"
    };
    try {
      if (action[1] === "steer") {
        if (action[2] === "down") {
          await focusCodex();
          await client.dispatchComposerSteer();
        }
        return json(response, 200, { ok: true });
      }
      await client.dispatchAction(keys[action[1]], action[2] === "down" ? 1 : 0);
      return json(response, 200, { ok: true });
    } catch (error) {
      return json(response, 503, { ok: false, error: error.message });
    }
  }
  const joystick = request.method === "POST" && url.pathname.match(
    /^\/joystick\/(up|right|down|left)\/(down|up)$/
  );
  if (joystick) {
    try {
      await client.dispatchJoystick(joystick[1], joystick[2] === "down" ? 1 : 0);
      return json(response, 200, { ok: true });
    } catch (error) {
      return json(response, 503, { ok: false, error: error.message });
    }
  }
  return json(response, 404, { ok: false, error: "Not found" });
});

server.listen(PORT, HOST, () => {
  console.log(`Codex Keyboard bridge listening on http://${HOST}:${PORT}`);
  void refresh();
});

const timer = setInterval(() => void refresh(), 30000);
timer.unref();

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    clearInterval(timer);
    client.disconnect();
    server.close(() => process.exit(0));
  });
}
