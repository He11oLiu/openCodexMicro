import { mkdir, readdir, rm } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";

const home = homedir();
const uid = process.getuid();
const appRoots = [
  join(home, "Library", "Application Support", "openCodexMicro"),
  join(home, "Library", "Application Support", "CodexKeyboard")
];
const bridgeApp = join(home, "Applications", "Codex Bridge.app");
const agentsRoot = join(home, "Library", "LaunchAgents");
await mkdir(agentsRoot, { recursive: true });
const agents = [
  join(agentsRoot, "io.opencodexmicro.d200.plist"),
  join(agentsRoot, "io.opencodexmicro.bridge.plist"),
  ...(await readdir(agentsRoot))
    .filter((name) => name.endsWith(".plist") && name.includes("codexkeyboard"))
    .map((name) => join(agentsRoot, name))
];

for (const agent of agents) {
  try {
    execFileSync("/bin/launchctl", ["bootout", `gui/${uid}`, agent], {
      stdio: "ignore"
    });
  } catch {
    // Already stopped or never installed.
  }
  await rm(agent, { force: true });
}
for (const appRoot of appRoots) {
  await rm(appRoot, { recursive: true, force: true });
}
await rm(bridgeApp, { recursive: true, force: true });
console.log("openCodexMicro removed.");
