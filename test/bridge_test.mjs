import assert from "node:assert/strict";
import test from "node:test";

import {
  decodeThreadPathSegment,
  localThreadKey,
  validateThreadId
} from "../src/bridge/thread-key.mjs";
import {
  CodexCdpClient,
  SLOT_SOURCE_REFRESH_MS,
  isSlotSourceFresh,
  readCachedSlotSource,
  resolveMicroBus,
  resolveSlotSnapshot,
  selectRateLimitUsage
} from "../src/bridge/codex-cdp.mjs";
import { selectLaunchAgentNode } from "../scripts/node-path.mjs";

const UUID = "f6805b8a-332a-43a0-a118-52d3e59542f6";

test("Micro bus discovery reuses the bus cached during enablement", async () => {
  const cachedBus = {
    handlers: new Map(),
    dispatchHostMessage() {}
  };
  const importedUrls = [];

  const resolved = await resolveMicroBus({
    cachedBus,
    urls: [
      "app://codex/assets/app-initial-a.js",
      "app://codex/assets/unrelated-lazy-b.js"
    ],
    importNamespace: async (url) => {
      importedUrls.push(url);
      return {};
    }
  });

  assert.equal(resolved, cachedBus);
  assert.deepEqual(importedUrls, []);
});

test("Micro bus discovery only imports targeted assets and stops when found", async () => {
  const bus = {
    handlers: new Map(),
    dispatchMessage() {}
  };
  const importedUrls = [];
  const urls = [
    "app://codex/assets/unrelated-lazy-a.js",
    "app://codex/assets/app-initial-b.js",
    "app://codex/assets/codex-micro-runtime-c.js",
    "app://codex/assets/vscode-api-d.js"
  ];

  const resolved = await resolveMicroBus({
    cachedBus: null,
    urls,
    importNamespace: async (url) => {
      importedUrls.push(url);
      return url.includes("codex-micro-runtime") ? { bus } : {};
    }
  });

  assert.equal(resolved, bus);
  assert.deepEqual(importedUrls, [urls[1], urls[2]]);
});

test("LaunchAgent Node selection prefers overrides and stable paths", () => {
  const executable = new Set([
    "/custom/node",
    "/opt/homebrew/bin/node",
    "/opt/homebrew/Cellar/node/99.0.0/bin/node"
  ]);
  const isExecutable = (path) => executable.has(path);
  const isCompatible = () => true;
  assert.equal(selectLaunchAgentNode({
    environment: { CODEX_KEYBOARD_NODE: "/custom/node" },
    stablePaths: ["/opt/homebrew/bin/node", "/usr/local/bin/node"],
    fallback: "/opt/homebrew/Cellar/node/99.0.0/bin/node",
    isExecutable,
    isCompatible
  }), "/custom/node");
  assert.equal(selectLaunchAgentNode({
    environment: {},
    stablePaths: ["/opt/homebrew/bin/node", "/usr/local/bin/node"],
    fallback: "/opt/homebrew/Cellar/node/99.0.0/bin/node",
    isExecutable,
    isCompatible
  }), "/opt/homebrew/bin/node");
  assert.equal(selectLaunchAgentNode({
    environment: {},
    stablePaths: ["/opt/homebrew/bin/node", "/usr/local/bin/node"],
    fallback: "/opt/homebrew/Cellar/node/99.0.0/bin/node",
    isExecutable: (path) => path === "/usr/local/bin/node",
    isCompatible
  }), "/usr/local/bin/node");
  assert.equal(selectLaunchAgentNode({
    environment: {},
    stablePaths: ["/opt/homebrew/bin/node", "/usr/local/bin/node"],
    fallback: "/opt/homebrew/Cellar/node/99.0.0/bin/node",
    isExecutable: (path) => path.includes("/Cellar/"),
    isCompatible
  }), "/opt/homebrew/Cellar/node/99.0.0/bin/node");
  assert.throws(() => selectLaunchAgentNode({
    environment: { CODEX_KEYBOARD_NODE: "/missing/node" },
    stablePaths: [],
    fallback: null,
    isExecutable: () => false,
    isCompatible
  }), /CODEX_KEYBOARD_NODE is not executable/);
  assert.throws(() => selectLaunchAgentNode({
    environment: {},
    stablePaths: [],
    fallback: null,
    isExecutable: () => false,
    isCompatible
  }), /Node was not found/);
});

test("LaunchAgent Node selection rejects incompatible executables", () => {
  const stablePaths = ["/opt/homebrew/bin/node", "/usr/local/bin/node"];
  const fallback = "/nvm/node-20/bin/node";
  const isExecutable = () => true;
  const compatible = new Set(["/usr/local/bin/node", fallback]);
  const isCompatible = (path) => compatible.has(path);

  assert.equal(selectLaunchAgentNode({
    environment: {},
    stablePaths,
    fallback,
    isExecutable,
    isCompatible
  }), "/usr/local/bin/node");
  assert.equal(selectLaunchAgentNode({
    environment: {},
    stablePaths: ["/opt/homebrew/bin/node"],
    fallback,
    isExecutable,
    isCompatible
  }), fallback);
  assert.throws(() => selectLaunchAgentNode({
    environment: { CODEX_KEYBOARD_NODE: "/custom/node-18" },
    stablePaths,
    fallback,
    isExecutable,
    isCompatible
  }), /CODEX_KEYBOARD_NODE requires Node 20 or newer/);
});

test("expired Micro slot sources are invalidated and rediscovered", async () => {
  const root = {};
  const oldSlots = Array.from({ length: 6 }, (_, id) => ({
    id,
    status: id === 0 ? "working" : "idle"
  }));
  const latestSlots = Array.from({ length: 6 }, (_, id) => ({
    id,
    status: id < 3 ? "working" : "idle"
  }));
  const source = {
    root,
    node: { store: { get: () => oldSlots } },
    resolver: { resolve: () => "slots" },
    contextMap: new Map(),
    discoveredAt: 1000
  };
  assert.equal(isSlotSourceFresh(source, root, 2999), true);
  assert.equal(isSlotSourceFresh(source, root, 3000), false);
  let invalidated = false;
  let discoveries = 0;
  const resolved = await resolveSlotSnapshot({
    source,
    root,
    refreshMs: SLOT_SOURCE_REFRESH_MS,
    clock: () => 3000,
    invalidate: (expired) => { invalidated = expired === source; },
    discover: async () => {
      discoveries += 1;
      return {
        slots: latestSlots,
        source: {
          root,
          node: { store: { get: () => latestSlots } },
          resolver: { resolve: () => "slots" },
          contextMap: new Map(),
          queryClients: [{ id: "latest-query-client" }]
        }
      };
    }
  });
  assert.equal(invalidated, true);
  assert.equal(discoveries, 1);
  assert.equal(resolved.cacheHit, false);
  assert.equal(resolved.source.discoveredAt, 3000);
  assert.equal(resolved.source.queryClients[0].id, "latest-query-client");
  assert.equal(resolved.slots.filter((slot) => slot.status === "working").length, 3);
  assert.equal(
    readCachedSlotSource(resolved.source, root, 3001),
    latestSlots
  );
});

test("fresh Micro slot sources still require six ordered slot ids", async () => {
  const root = {};
  const invalidSlots = Array.from({ length: 6 }, (_, id) => ({
    id: id === 5 ? 4 : id,
    status: "idle"
  }));
  const latestSlots = Array.from({ length: 6 }, (_, id) => ({
    id,
    status: id < 2 ? "working" : "idle"
  }));
  const source = {
    root,
    node: { store: { get: () => invalidSlots } },
    resolver: { resolve: () => "slots" },
    contextMap: new Map(),
    discoveredAt: 1000
  };
  let invalidated = false;
  const resolved = await resolveSlotSnapshot({
    source,
    root,
    clock: () => 1500,
    invalidate: (rejected) => { invalidated = rejected === source; },
    discover: async () => ({
      slots: latestSlots,
      source: {
        root,
        node: { store: { get: () => latestSlots } },
        resolver: { resolve: () => "slots" },
        contextMap: new Map()
      }
    })
  });

  assert.equal(invalidated, true);
  assert.equal(resolved.cacheHit, false);
  assert.equal(resolved.slots.filter((slot) => slot.status === "working").length, 2);
});

test("usage probing skips stale legacy and false-positive queries", () => {
  const staleLegacy = {
    queryKey: ["rate-limit-status"],
    state: { data: null, dataUpdatedAt: 10 }
  };
  const falsePositive = {
    queryKey: ["settings"],
    state: { data: { primary: { color: "blue" } }, dataUpdatedAt: 20 }
  };
  const compatible = {
    queryKey: ["account", "rateLimits"],
    state: {
      data: {
        response: {
          rateLimits: {
            primary: { usedPercent: 37, windowDurationMins: 300, resetsAt: 123 },
            secondary: { usedPercent: 12, windowDurationMins: 10080, resetsAt: 456 }
          }
        }
      },
      dataUpdatedAt: 789
    }
  };
  const selected = selectRateLimitUsage(
    [staleLegacy, falsePositive, compatible],
    999
  );
  assert.equal(selected.query, compatible);
  assert.equal(selected.refreshQuery, compatible);
  assert.deepEqual(
    selected.usage.windows.map((window) => [window.kind, window.remainingPercent]),
    [["five-hour", 63], ["weekly", 88]]
  );
  assert.equal(selected.usage.observedAt, 789);
});

test("usage probing rejects windows without a supported duration", () => {
  const incomplete = {
    queryKey: ["account", "rateLimits"],
    state: {
      data: {
        rateLimits: {
          primary: { usedPercent: 42 }
        }
      },
      dataUpdatedAt: 123
    }
  };

  const selected = selectRateLimitUsage([incomplete], 999);

  assert.equal(selected.query, null);
  assert.equal(selected.refreshQuery, incomplete);
  assert.equal(selected.usage, null);
});

test("usage probing rejects invalid percentages instead of coercing them", () => {
  for (const usedPercent of [null, false, "", "   ", -1, 101]) {
    const query = {
      queryKey: ["account", "rateLimits"],
      state: {
        data: {
          rateLimits: {
            secondary: { usedPercent, windowDurationMins: 10080 }
          }
        },
        dataUpdatedAt: 123
      }
    };

    const selected = selectRateLimitUsage([query], 999);

    assert.equal(selected.query, null, `usedPercent=${JSON.stringify(usedPercent)}`);
    assert.equal(selected.usage, null, `usedPercent=${JSON.stringify(usedPercent)}`);
  }
});

test("usage probing requires a weekly window for the D200 display", () => {
  const fiveHourOnly = {
    queryKey: ["account", "rateLimits"],
    state: {
      data: {
        rateLimits: {
          primary: { usedPercent: 42, windowDurationMins: 300 }
        }
      },
      dataUpdatedAt: 123
    }
  };

  const selected = selectRateLimitUsage([fiveHourOnly], 999);

  assert.equal(selected.query, null);
  assert.equal(selected.refreshQuery, fiveHourOnly);
  assert.equal(selected.usage, null);
});

test("usage probing accepts the legacy snake_case weekly payload", () => {
  const legacy = {
    queryKey: ["rate-limit-status"],
    state: {
      data: {
        rate_limit: {
          primary_window: {
            used_percent: 23,
            limit_window_seconds: 604800,
            reset_at: 456
          }
        }
      },
      dataUpdatedAt: 123
    }
  };

  const selected = selectRateLimitUsage([legacy], 999);

  assert.equal(selected.query, legacy);
  assert.deepEqual(selected.usage.windows, [{
    id: "weekly",
    kind: "weekly",
    usedPercent: 23,
    remainingPercent: 77,
    resetsAt: 456
  }]);
});

test("generated CDP snapshot expression remains valid JavaScript", async () => {
  const client = new CodexCdpClient();
  client.connect = async () => {};
  client.evaluate = async (expression) => {
    Function(`return ${expression}`);
    return { slots: [], usage: null };
  };
  assert.deepEqual(await client.snapshot(), { slots: [], usage: null });
});

test("accepts formal and explicit temporary Codex thread ids", () => {
  assert.equal(validateThreadId(UUID), UUID);
  assert.equal(
    validateThreadId(`client-new-thread:${UUID}`),
    `client-new-thread:${UUID}`
  );
  assert.equal(localThreadKey(UUID), `local:${UUID}`);
  assert.equal(
    localThreadKey(`client-new-thread:${UUID}`),
    `local:client-new-thread:${UUID}`
  );
});

test("decodes one URL path segment and rejects arbitrary ids", () => {
  assert.equal(
    decodeThreadPathSegment(`client-new-thread%3A${UUID}`),
    `client-new-thread:${UUID}`
  );
  assert.throws(() => validateThreadId("arbitrary-thread"), /Invalid/);
  assert.throws(() => decodeThreadPathSegment("%not-encoded"), /encoded/);
});

test("named Micro actions preserve press and release phases", async () => {
  const client = new CodexCdpClient();
  const calls = [];
  client.dispatchAction = async (...args) => calls.push(args);

  for (const [action, key] of [
    ["fast", "ACT06"],
    ["fork", "ACT09"],
    ["submit", "ACT12"]
  ]) {
    await client.dispatchNamedAction(action, true);
    await client.dispatchNamedAction(action, false);
    assert.deepEqual(calls.splice(0), [[key, 1], [key, 0]]);
  }
});

test("renderer actions execute once on key down", async () => {
  const client = new CodexCdpClient();
  const calls = [];
  client.dispatchRendererAction = async (action) => calls.push(action);

  for (const action of ["pin", "new"]) {
    await client.dispatchNamedAction(action, true);
    await client.dispatchNamedAction(action, false);
  }
  assert.deepEqual(calls, ["pin", "new"]);
});

test("unknown bridge actions are rejected", async () => {
  const client = new CodexCdpClient();
  await assert.rejects(
    client.dispatchNamedAction("unknown", true),
    /Unsupported Codex bridge action/
  );
});
