import { accessSync, constants } from "node:fs";
import { execFileSync } from "node:child_process";

export const MIN_NODE_MAJOR = 20;

export const STABLE_NODE_PATHS = Object.freeze([
  "/opt/homebrew/bin/node",
  "/usr/local/bin/node"
]);

export function compatibleNode(path, minimumMajor = MIN_NODE_MAJOR) {
  try {
    const version = execFileSync(path, ["-p", "process.versions.node"], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
      timeout: 2000
    }).trim();
    const major = Number(version.split(".", 1)[0]);
    return Number.isInteger(major) && major >= minimumMajor;
  } catch {
    return false;
  }
}

export function selectLaunchAgentNode({
  environment = process.env,
  stablePaths = STABLE_NODE_PATHS,
  fallback = process.execPath,
  isExecutable = (path) => {
    try {
      accessSync(path, constants.X_OK);
      return true;
    } catch {
      return false;
    }
  },
  isCompatible = compatibleNode
} = {}) {
  const configured = String(environment.CODEX_KEYBOARD_NODE || "").trim();
  if (configured) {
    if (!isExecutable(configured)) {
      throw new Error(`CODEX_KEYBOARD_NODE is not executable: ${configured}`);
    }
    if (!isCompatible(configured)) {
      throw new Error(
        `CODEX_KEYBOARD_NODE requires Node ${MIN_NODE_MAJOR} or newer: ${configured}`
      );
    }
    return configured;
  }
  for (const candidate of stablePaths) {
    if (isExecutable(candidate) && isCompatible(candidate)) return candidate;
  }
  if (fallback && isExecutable(fallback) && isCompatible(fallback)) return fallback;
  throw new Error(
    `Node was not found. Install Node ${MIN_NODE_MAJOR} or newer at ` +
    "/opt/homebrew/bin/node or " +
    "/usr/local/bin/node, or set CODEX_KEYBOARD_NODE."
  );
}
