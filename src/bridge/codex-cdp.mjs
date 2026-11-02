import { execFile } from "node:child_process";
import { promisify } from "node:util";
import WebSocket from "ws";
import { localThreadKey } from "./thread-key.mjs";
import { selectRateLimitQuery } from "./rate-limit-query.mjs";
import {
  collectRendererScriptUrls,
  readSignalValue,
  rendererResourceUrls,
  resolveMicroBus
} from "./renderer-resources.mjs";

export { resolveMicroBus } from "./renderer-resources.mjs";

const execFileAsync = promisify(execFile);
const USAGE_REFRESH_MS = Math.max(
  15000,
  Number(process.env.CODEX_KEYBOARD_USAGE_REFRESH_SECONDS || 600) * 1000
);
export const SLOT_SOURCE_REFRESH_MS = 2000;

export function isSlotSourceFresh(
  source,
  root,
  now = Date.now(),
  refreshMs = 2000
) {
  return Boolean(
    source && source.root === root && source.node?.store &&
    Number.isFinite(source.discoveredAt) &&
    now - source.discoveredAt >= 0 &&
    now - source.discoveredAt < refreshMs
  );
}

export function readCachedSlotSource(
  source,
  root,
  now = Date.now(),
  refreshMs = 2000
) {
  if (!isSlotSourceFresh(source, root, now, refreshMs)) return null;
  try {
    const slots = readSignalValue(source.node, source.resolver, source.contextMap);
    if (!(
      Array.isArray(slots) && slots.length === 6 &&
      slots.every((slot, index) => slot?.id === index)
    )) return null;
    return slots;
  } catch {
    return null;
  }
}

export async function resolveSlotSnapshot({
  source,
  root,
  refreshMs = 2000,
  discover,
  invalidate = () => {},
  clock = Date.now
}) {
  const slots = readCachedSlotSource(source, root, clock(), refreshMs);
  if (slots) return { source, slots, cacheHit: true };
  if (source) invalidate(source);
  const discovered = await discover();
  if (!discovered?.source || !(
    Array.isArray(discovered.slots) && discovered.slots.length === 6 &&
    discovered.slots.every((slot, index) => slot?.id === index)
  )) {
    throw new Error("Codex Micro slot store was not found");
  }
  return {
    source: { ...discovered.source, discoveredAt: clock() },
    slots: discovered.slots,
    cacheHit: false
  };
}

export function findRateLimitPayload(value, depth = 0, seen = new Set()) {
  if (!value || typeof value !== "object" || depth > 4 || seen.has(value)) return null;
  seen.add(value);
  for (const key of ["rate_limit", "rateLimit", "rate_limits", "rateLimits"]) {
    const candidate = value[key];
    if (candidate && typeof candidate === "object") return candidate;
  }
  if (
    value.primary_window || value.secondary_window ||
    value.primaryWindow || value.secondaryWindow ||
    value.primary || value.secondary
  ) return value;
  for (const nested of Object.values(value)) {
    const found = findRateLimitPayload(nested, depth + 1, seen);
    if (found) return found;
  }
  return null;
}

export function normalizeUsageWindow(window, role) {
  if (!window || typeof window !== "object") return null;
  const rawUsedPercent = window.used_percent ?? window.usedPercent;
  if (
    !["number", "string"].includes(typeof rawUsedPercent) ||
    (typeof rawUsedPercent === "string" && !rawUsedPercent.trim())
  ) return null;
  const usedPercent = Number(rawUsedPercent);
  if (!Number.isFinite(usedPercent) || usedPercent < 0 || usedPercent > 100) {
    return null;
  }
  const seconds = Number(window.limit_window_seconds ?? window.limitWindowSeconds);
  const durationMinutes = Number(window.windowDurationMins ?? window.window_duration_mins);
  const minutes = Number.isFinite(seconds) && seconds > 0 ? seconds / 60
    : Number.isFinite(durationMinutes) && durationMinutes > 0 ? durationMinutes
      : null;
  const kind = minutes != null && Math.abs(minutes - 300) <= 1 ? "five-hour"
    : minutes != null && Math.abs(minutes - 10080) <= 1 ? "weekly"
      : "other";
  const used = Math.min(100, Math.max(0, usedPercent));
  return {
    id: kind === "other" ? role : kind,
    kind,
    usedPercent: used,
    remainingPercent: 100 - used,
    resetsAt: Number(window.reset_at ?? window.resetsAt) || null
  };
}

export function selectRateLimitUsage(queries, now = Date.now()) {
  const query = selectRateLimitQuery(queries);
  const rateLimit = findRateLimitPayload(query?.state?.data);
  const windows = rateLimit ? [
    normalizeUsageWindow(
      rateLimit.primary_window ?? rateLimit.primaryWindow ?? rateLimit.primary,
      "primary"
    ),
    normalizeUsageWindow(
      rateLimit.secondary_window ?? rateLimit.secondaryWindow ?? rateLimit.secondary,
      "secondary"
    )
  ].filter((window) => window && window.kind !== "other") : [];
  const usage = windows.some((window) => window.kind === "weekly") ? {
    windows,
    observedAt: Number(query?.state?.dataUpdatedAt) || now
  } : null;
  return { query: usage ? query : null, refreshQuery: query, usage };
}
const DEVICE_STATE = {
  type: "codex-micro-device-state-changed",
  state: { status: "connected", error: null, battery: { percentage: 100, isCharging: true } }
};
const MICRO_ACTION_KEYS = Object.freeze({
  fast: "ACT06",
  approve: "ACT07",
  reject: "ACT08",
  fork: "ACT09",
  mic: "ACT10",
  submit: "ACT12"
});
const RENDERER_ACTIONS = new Set(["pin", "new"]);

const ENABLE_EXPRESSION = `(async () => {
  const gateName = "3207467860";
  const statsig = globalThis.__STATSIG__;
  const clients = [...new Set([statsig?.firstInstance, ...Object.values(statsig?.instances ?? {})].filter(Boolean))];
  for (const client of clients) {
    if (client.overrideAdapter?.__codexKeyboardGate !== gateName) {
      const original = client.overrideAdapter ?? {};
      client.overrideAdapter = new Proxy(original, {
        get(target, property) {
          if (property === "__codexKeyboardGate") return gateName;
          if (property === "getGateOverride") return (gate, user, options) => {
            if (gate?.name === gateName) return { ...gate, value: true };
            const fallback = Reflect.get(target, property, target);
            return typeof fallback === "function" ? fallback.call(target, gate, user, options) : gate;
          };
          const value = Reflect.get(target, property, target);
          return typeof value === "function" ? value.bind(target) : value;
        }
      });
    }
    client._memoCache = {};
    client.$emt?.({ name: "values_updated", status: client.loadingStatus });
  }
  const urls = (${rendererResourceUrls.toString()})();
  const resolveMicroBus = ${resolveMicroBus.toString()};
  const bus = await resolveMicroBus({
    cachedBus: globalThis[Symbol.for("codex-keyboard-micro-bus")],
    urls,
    importNamespace: (url) => import(url)
  });
  if (bus) {
    globalThis[Symbol.for("codex-keyboard-micro-bus")] = bus;
    const dispatch = bus.dispatchHostMessage ?? bus.dispatchMessage;
    dispatch.call(bus, ${JSON.stringify(DEVICE_STATE)});
    return { ready: true, clients: clients.length };
  }
  return { ready: clients.length > 0, clients: clients.length };
})()`;

const SNAPSHOT_EXPRESSION = `(async () => {
  const startedAt = performance.now();
  const root = document.getElementById("root");
  const reactKey = root && Object.getOwnPropertyNames(root).find((key) => key.startsWith("__reactContainer$"));
  if (!root || !reactKey) throw new Error("Codex React root was not found");
  const sourceKey = Symbol.for("codex-keyboard-micro-snapshot-source");
  const validSlots = (slots) =>
    Array.isArray(slots) && slots.length === 6 &&
    slots.every((slot, index) => slot?.id === index);
  const readSignalValue = ${readSignalValue.toString()};
  const isSlotSourceFresh = ${isSlotSourceFresh.toString()};
  const readCachedSlotSource = ${readCachedSlotSource.toString()};
  const resolveSlotSnapshot = ${resolveSlotSnapshot.toString()};
  const resolveMicroBus = ${resolveMicroBus.toString()};
  const discoverSource = async () => {
    const urls = (${rendererResourceUrls.toString()})();
    const slotSignalsUrl = urls.find((url) => url.includes("/assets/codex-micro-slot-signals-"));
    if (!slotSignalsUrl) throw new Error("Codex Micro slot signals are not loaded");

    const busKey = Symbol.for("codex-keyboard-micro-bus");
    const bus = await resolveMicroBus({
      cachedBus: globalThis[busKey],
      urls,
      importNamespace: (url) => import(url)
    });
    if (bus) {
      globalThis[busKey] = bus;
      if ((bus.handlers.get("codex-micro-hid-event")?.size ?? 0) === 0) {
        (bus.dispatchHostMessage ?? bus.dispatchMessage).call(bus, ${JSON.stringify(DEVICE_STATE)});
      }
    }

    const signals = await import(slotSignalsUrl);
    const resolvers = Object.values(signals).filter((candidate) =>
      candidate && typeof candidate === "object" &&
      typeof candidate.resolve === "function" && typeof candidate.createSubscriberAtom === "function"
    );
    let rootFiber = root[reactKey];
    while (rootFiber?.return) rootFiber = rootFiber.return;
    const queue = [rootFiber?.stateNode?.current ?? rootFiber];
    const seen = new Set();
    const queryClients = new Set();
    let found = null;
    let sourceParts = null;
    while (queue.length && seen.size < 30000) {
      const fiber = queue.pop();
      if (!fiber || seen.has(fiber)) continue;
      seen.add(fiber);
      const values = [fiber.memoizedProps?.value];
      let dependency = fiber.dependencies?.firstContext;
      while (dependency) { values.push(dependency.memoizedValue); dependency = dependency.next; }
      for (const value of values) {
        if (
          value && typeof value.getQueryCache === "function" &&
          typeof value.getQueryData === "function"
        ) queryClients.add(value);
        if (sourceParts || !(value instanceof Map)) continue;
        for (const node of value.values()) {
          if (!node?.store || typeof node.store.get !== "function") continue;
          for (const resolver of resolvers) {
            try {
              const slots = readSignalValue(node, resolver, value);
              if (validSlots(slots)) {
                found = slots;
                sourceParts = { node, resolver, contextMap: value };
                break;
              }
            } catch {}
          }
          if (sourceParts) break;
        }
      }
      queue.push(fiber.child, fiber.sibling);
    }
    if (!found || !sourceParts) throw new Error("Codex Micro slot store was not found");
    return {
      slots: found,
      source: {
        root,
        ...sourceParts,
        queryClients: [...queryClients]
      }
    };
  };
  const resolvedSource = await resolveSlotSnapshot({
    source: globalThis[sourceKey],
    root,
    refreshMs: ${SLOT_SOURCE_REFRESH_MS},
    discover: discoverSource,
    invalidate: () => { delete globalThis[sourceKey]; }
  });
  const source = resolvedSource.source;
  const found = resolvedSource.slots;
  const queryClients = new Set(source.queryClients ?? []);
  const cacheHit = resolvedSource.cacheHit;
  if (!cacheHit) globalThis[sourceKey] = source;
  let usage = null;
  const selectRateLimitQuery = ${selectRateLimitQuery.toString()};
  const findRateLimitPayload = ${findRateLimitPayload.toString()};
  const normalizeUsageWindow = ${normalizeUsageWindow.toString()};
  const selectRateLimitUsage = ${selectRateLimitUsage.toString()};
  const queries = [...queryClients].flatMap((queryClient) => {
    try { return queryClient.getQueryCache().getAll(); } catch { return []; }
  });
  try {
    const selected = selectRateLimitUsage(queries, Date.now());
    const query = selected.refreshQuery;
    const now = Date.now();
    const updatedAt = Number(query?.state?.dataUpdatedAt) || 0;
    const refreshKey = Symbol.for("codex-keyboard-rate-limit-refresh-at");
    const lastAttempt = Number(globalThis[refreshKey]) || 0;
    if (
      query && typeof query.fetch === "function" &&
      now - updatedAt >= ${USAGE_REFRESH_MS} && now - lastAttempt >= ${USAGE_REFRESH_MS}
    ) {
      globalThis[refreshKey] = now;
      try { Promise.resolve(query.fetch()).catch(() => {}); } catch {}
    }
    usage = selected.usage;
  } catch {}
  const active = document.querySelector("[data-app-action-sidebar-thread-id][data-app-action-sidebar-thread-active=true]")
    ?? document.querySelector("[data-app-action-sidebar-thread-id][aria-current=page]");
  const activeThreadKey = document.querySelector("[data-above-composer-conversation-id]")
    ?.getAttribute("data-above-composer-conversation-id")
    ?? active?.getAttribute("data-app-action-sidebar-thread-id")
    ?? null;
  const normalizeThreadKey = (value) => String(value ?? "").replace(/^local:/, "");
  return {
    activeThreadKey,
    slots: found.map((slot) => ({
      id: slot.id,
      threadKey: slot.threadKey ?? null,
      title: slot.title ?? slot.thread?.title ?? slot.task?.title ?? null,
      status: slot.status ?? "idle",
      selected: Boolean(slot.selected) || Boolean(
        activeThreadKey && normalizeThreadKey(slot.threadKey) === normalizeThreadKey(activeThreadKey)
      )
    })),
    usage,
    usageSource: "cdp",
    usageAvailable: Boolean(usage),
    bridgeSnapshot: {
      source: cacheHit ? "cache" : "discovery",
      durationMs: performance.now() - startedAt
    }
  };
})()`;

function selectMainTarget(targets) {
  const pages = targets.filter((target) =>
    target.type === "page" && target.webSocketDebuggerUrl && target.url?.startsWith("app://")
  );
  return pages.find((target) => {
    try { return new URL(target.url).pathname === "/index.html" && !new URL(target.url).search; }
    catch { return false; }
  }) ?? pages.find((target) => !/avatar-overlay|composition-surface/i.test(target.url || ""));
}

async function fetchJson(url, timeout = 1200) {
  const response = await fetch(url, { signal: AbortSignal.timeout(timeout) });
  if (!response.ok) throw new Error(`${url} returned HTTP ${response.status}`);
  return response.json();
}

async function discoverDebugPort() {
  const { stdout } = await execFileAsync("/bin/ps", ["-axo", "command="], { timeout: 4000 });
  for (const line of stdout.split("\n")) {
    if (!line.includes("--remote-debugging-address=127.0.0.1")) continue;
    const port = Number(line.match(/--remote-debugging-port(?:=|\s+)(\d+)/)?.[1]);
    if (!Number.isInteger(port)) continue;
    try {
      await fetchJson(`http://127.0.0.1:${port}/json/version`, 500);
      return port;
    } catch {}
  }
  throw new Error("Codex is not running with the local debug bridge");
}

export class CodexCdpClient {
  socket = null;
  nextId = 0;
  pending = new Map();
  lastSnapshot = null;

  async connect() {
    if (this.socket?.readyState === WebSocket.OPEN) return;
    const port = await discoverDebugPort();
    const target = selectMainTarget(await fetchJson(`http://127.0.0.1:${port}/json/list`));
    if (!target?.webSocketDebuggerUrl) throw new Error("Codex main renderer was not found");
    const socket = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("Timed out connecting to Codex")), 3000);
      socket.once("open", () => { clearTimeout(timer); resolve(); });
      socket.once("error", reject);
    });
    socket.on("message", (raw) => this.handleMessage(String(raw)));
    socket.on("close", () => this.disconnect());
    socket.on("error", () => this.disconnect());
    this.socket = socket;
    await this.refreshRendererResources();
    await this.evaluate(ENABLE_EXPRESSION);
  }

  async refreshRendererResources() {
    // Resource Timing can be cleared while imported modules remain loaded.
    const { frameTree } = await this.sendCommand("Page.getResourceTree", {});
    const urls = collectRendererScriptUrls(frameTree);
    await this.evaluate(`(() => {
      globalThis[Symbol.for("codex-keyboard-renderer-resource-urls")] = ${JSON.stringify(urls)};
      return true;
    })()`);
  }

  async snapshot() {
    await this.connect();
    try {
      try {
        this.lastSnapshot = await this.evaluate(SNAPSHOT_EXPRESSION);
      } catch (error) {
        if (!/Codex Micro slot signals are not loaded/.test(error.message)) throw error;
        await this.refreshRendererResources();
        this.lastSnapshot = await this.evaluate(SNAPSHOT_EXPRESSION);
      }
      return this.lastSnapshot;
    } catch (error) {
      this.disconnect();
      throw error;
    }
  }

  async clickAgent(slot) {
    await this.connect();
    const snapshot = this.lastSnapshot ?? await this.snapshot();
    const agent = snapshot.slots[slot];
    if (!agent?.threadKey) throw new Error(`Agent slot ${slot + 1} is empty`);
    return this.clickThreadKey(agent.threadKey, slot);
  }

  async clickThread(threadId, slot = 0) {
    await this.connect();
    return this.clickThreadKey(localThreadKey(threadId), slot);
  }

  async clickThreadKey(threadKey, slot) {
    // Use the same native HID path as Codex Micro. The DOM click introduced
    // perceptible navigation scheduling; it is now only a non-blocking fallback.
    try {
      await this.dispatchAgent(slot, threadKey, 1);
      void (async () => {
        await new Promise((resolve) => setTimeout(resolve, 35));
        await this.dispatchAgent(slot, threadKey, 0);
        await this.activateThread(threadKey);
      })().catch(() => {});
    } catch {
      await this.activateThread(threadKey);
    }
  }

  async dispatchAgent(slot, threadKey, act) {
    return this.dispatchMicroMessage({
      type: "codex-micro-hid-event",
      event: { key: `AG0${slot}`, act, slot, threadKey }
    }, "codex-micro-hid-event");
  }

  async dispatchAction(key, act) {
    return this.dispatchMicroMessage({
      type: "codex-micro-hid-event",
      event: { key, act, slot: null, threadKey: null }
    }, "codex-micro-hid-event");
  }

  async dispatchNamedAction(action, pressed) {
    const key = MICRO_ACTION_KEYS[action];
    if (key) return this.dispatchAction(key, pressed ? 1 : 0);
    if (!RENDERER_ACTIONS.has(action)) {
      throw new Error(`Unsupported Codex bridge action: ${action}`);
    }
    if (!pressed) return true;
    return this.dispatchRendererAction(action);
  }

  async dispatchRendererAction(action) {
    await this.connect();
    const invoked = await this.evaluate(`(() => {
      const action = ${JSON.stringify(action)};
      const visible = (element) => element && element.offsetParent !== null;
      let target = null;
      if (action === "pin") {
        const active = document.querySelector(
          "[data-app-action-sidebar-thread-active=true]"
        ) ?? document.querySelector(
          "[data-app-action-sidebar-thread-id][aria-current=page]"
        );
        target = active && [...active.querySelectorAll("button")].find(
          (button) => visible(button) && ["Pin chat", "Unpin chat"].includes(
            button.getAttribute("aria-label")
          )
        );
      } else if (action === "new") {
        const buttons = [...document.querySelectorAll("button")].filter(visible);
        target = buttons.find(
          (button) => button.getAttribute("aria-label") === "New chat"
        ) ?? buttons.find(
          (button) => (button.innerText || "").trim() === "New chat"
        );
      }
      if (!target) return false;
      target.click();
      return true;
    })()`);
    if (!invoked) throw new Error(`Codex ${action} action is not available`);
    return true;
  }

  async dispatchComposerSteer() {
    await this.connect();
    const clicked = await this.evaluate(`(() => {
      const editor = [...document.querySelectorAll('[contenteditable="true"][role="textbox"]')]
        .find((element) => element.offsetParent !== null);
      if (!editor) throw new Error("Codex composer is not available");
      editor.focus();
      const steer = [...document.querySelectorAll('button')]
        .find((element) =>
          element.offsetParent !== null &&
          element.getAttribute("aria-label") === "Steer"
        );
      if (!steer) return false;
      steer.click();
      return true;
    })()`);
    if (!clicked) throw new Error("Codex Steer action is not available");
  }

  async dispatchJoystick(direction, distance) {
    const angle = { up: 0.75, right: 0, down: 0.25, left: 0.5 }[direction];
    if (angle === undefined) throw new Error(`Unknown joystick direction: ${direction}`);
    return this.dispatchMicroMessage({
      type: "codex-micro-joystick-event",
      event: { angle, distance }
    }, "codex-micro-joystick-event");
  }

  async dispatchMicroMessage(message, requiredHandler) {
    return this.evaluate(`(async () => {
      const cacheKey = Symbol.for("codex-keyboard-micro-bus");
      const isMicroBus = (candidate) =>
        candidate && candidate.handlers instanceof Map &&
        (
          candidate.handlers.has(${JSON.stringify(requiredHandler)}) ||
          [...candidate.handlers.keys()].some((key) => String(key).startsWith("codex-micro-"))
        ) &&
        (
          typeof candidate.dispatchHostMessage === "function" ||
          typeof candidate.dispatchMessage === "function"
        );
      let bus = globalThis[cacheKey];
      if (!isMicroBus(bus)) {
        const urls = (${rendererResourceUrls.toString()})();
        const resolveMicroBus = ${resolveMicroBus.toString()};
        bus = await resolveMicroBus({
          cachedBus: null,
          urls,
          importNamespace: (url) => import(url)
        });
        if (isMicroBus(bus)) {
          globalThis[cacheKey] = bus;
        } else {
          bus = null;
        }
      }
      if (!bus) throw new Error("Codex Micro event bus was not found");
      const dispatch = bus.dispatchHostMessage ?? bus.dispatchMessage;
      if ((bus.handlers.get(${JSON.stringify(requiredHandler)})?.size ?? 0) === 0) {
        dispatch.call(bus, ${JSON.stringify(DEVICE_STATE)});
      }
      // Never put Micro handler discovery on the physical key hot path. The
      // native event is dispatched immediately; clickAgent keeps a DOM
      // activation fallback in the background in case Codex has not installed
      // its handler yet.
      dispatch.call(bus, ${JSON.stringify(message)});
      return true;
    })()`);
  }

  async activateThread(threadKey) {
    return this.evaluate(`(async () => {
      const key = ${JSON.stringify(threadKey)};
      const normalize = (value) => String(value ?? "").replace(/^local:/, "");
      const current = () => document.querySelector("[data-above-composer-conversation-id]")
        ?.getAttribute("data-above-composer-conversation-id")
        ?? document.querySelector("[data-app-action-sidebar-thread-id][data-app-action-sidebar-thread-active=true]")
          ?.getAttribute("data-app-action-sidebar-thread-id");
      if (normalize(current()) === normalize(key)) return true;
      const item = [...document.querySelectorAll("[data-app-action-sidebar-thread-id]")]
        .find((el) => normalize(el.getAttribute("data-app-action-sidebar-thread-id")) === normalize(key));
      if (!item) throw new Error("Task is not loaded in the Codex sidebar");
      (item.querySelector("button,a,[role=button],[role=link]") ?? item).click();
      return true;
    })()`);
  }

  evaluate(expression) {
    return this.sendCommand("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true
    });
  }

  sendCommand(method, params) {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      return Promise.reject(new Error("Codex bridge is disconnected"));
    }
    const id = ++this.nextId;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error("Codex runtime response timed out"));
      }, 7000);
      this.pending.set(id, { resolve, reject, timer, method });
      this.socket.send(JSON.stringify({
        id,
        method,
        params
      }));
    });
  }

  handleMessage(raw) {
    let message;
    try { message = JSON.parse(raw); } catch { return; }
    const pending = this.pending.get(message.id);
    if (!pending) return;
    this.pending.delete(message.id);
    clearTimeout(pending.timer);
    if (message.error) return pending.reject(new Error(message.error.message));
    if (message.result?.exceptionDetails) {
      return pending.reject(new Error(
        message.result.exceptionDetails.exception?.description
        ?? message.result.exceptionDetails.text
        ?? "Codex evaluation failed"
      ));
    }
    pending.resolve(pending.method === "Runtime.evaluate"
      ? message.result?.result?.value
      : message.result);
  }

  disconnect() {
    const socket = this.socket;
    this.socket = null;
    if (socket?.readyState === WebSocket.OPEN) socket.close();
    for (const { reject, timer } of this.pending.values()) {
      clearTimeout(timer);
      reject(new Error("Codex bridge disconnected"));
    }
    this.pending.clear();
  }
}
