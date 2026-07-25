import {
  chmod,
  cp,
  mkdir,
  readFile,
  readdir,
  rm,
  writeFile
} from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { homedir } from "node:os";
import { join, resolve } from "node:path";

const home = homedir();
const uid = process.getuid();
const startDaemon = !process.argv.includes("--no-start");
const appRoot = join(home, "Library", "Application Support", "openCodexMicro");
const legacyAppRoot = join(
  home,
  "Library",
  "Application Support",
  "CodexKeyboard"
);
const venv = join(appRoot, "venv");
const agentsRoot = join(home, "Library", "LaunchAgents");
const d200Agent = join(
  agentsRoot,
  "io.opencodexmicro.d200.plist"
);
const python = execFileSync("/usr/bin/which", ["python3"], {
  encoding: "utf8"
}).trim();
const xml = (value) => String(value)
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&apos;");

try {
  execFileSync(python, [
    "-c",
    "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
  ]);
} catch {
  throw new Error(
    `openCodexMicro requires Python 3.11 or newer; found ${python}.`
  );
}

await mkdir(appRoot, { recursive: true, mode: 0o700 });
await chmod(appRoot, 0o700);
await mkdir(agentsRoot, { recursive: true });
const legacyAgents = (await readdir(agentsRoot))
  .filter((name) => name.endsWith(".plist") && name.includes("codexkeyboard"))
  .map((name) => join(agentsRoot, name));

const userTheme = join(appRoot, "icon-theme.json");
const legacyTheme = join(legacyAppRoot, "icon-theme.json");
const userThemeTemplate = `${JSON.stringify({
  overridesOnly: true,
  surfaces: {},
  tasks: {},
  usage: {}
}, null, 2)}\n`;

async function validJsonObject(file) {
  try {
    const value = JSON.parse(await readFile(file, "utf8"));
    return value && typeof value === "object" && !Array.isArray(value);
  } catch {
    return false;
  }
}

let hasUserTheme = await validJsonObject(userTheme);
if (!hasUserTheme) {
  try {
    await readFile(userTheme);
    const backup = `${userTheme}.invalid-${Date.now()}.bak`;
    await cp(userTheme, backup);
    console.warn(`Invalid theme preserved at: ${backup}`);
  } catch {
    // No current theme exists.
  }
  if (await validJsonObject(legacyTheme)) {
    await cp(legacyTheme, userTheme);
    hasUserTheme = true;
    console.log("Migrated the CodexKeyboard theme to openCodexMicro.");
  }
}
if (!hasUserTheme) {
  await writeFile(userTheme, userThemeTemplate, { mode: 0o600 });
}

try {
  await readFile(legacyTheme);
  await cp(legacyTheme, join(appRoot, "icon-theme.legacy-backup.json"));
} catch {
  // No legacy theme needs to be retained.
}

await cp(resolve("standalone/d200.py"), join(appRoot, "d200.py"));
await cp(resolve("standalone/native_codex.py"), join(appRoot, "native_codex.py"));
await rm(join(appRoot, "assets"), { recursive: true, force: true });
await cp(resolve("standalone/assets"), join(appRoot, "assets"), {
  recursive: true
});
await cp(
  resolve("standalone/icon-theme.default.json"),
  join(appRoot, "icon-theme.default.json")
);
await cp(
  resolve("standalone/requirements.txt"),
  join(appRoot, "requirements.txt")
);

execFileSync(python, ["-m", "venv", venv], { stdio: "inherit" });
execFileSync(join(venv, "bin", "pip"), [
  "install",
  "--disable-pip-version-check",
  "-r",
  join(appRoot, "requirements.txt")
], { stdio: "inherit" });

const d200Plist = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>io.opencodexmicro.d200</string>
  <key>ProgramArguments</key><array>
    <string>${xml(join(venv, "bin", "python"))}</string>
    <string>${xml(join(appRoot, "d200.py"))}</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>OPEN_CODEX_MICRO_OUTPUT_WRITES</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>ThrottleInterval</key><integer>2</integer>
  <key>StandardOutPath</key><string>${xml(join(appRoot, "d200.log"))}</string>
  <key>StandardErrorPath</key><string>${xml(join(appRoot, "d200-error.log"))}</string>
</dict></plist>
`;
await writeFile(d200Agent, d200Plist, { mode: 0o644 });

for (const agent of [d200Agent, ...legacyAgents]) {
  try {
    execFileSync("/bin/launchctl", ["bootout", `gui/${uid}`, agent], {
      stdio: "ignore"
    });
  } catch {
    // The service may not be installed.
  }
}
for (const agent of legacyAgents) {
  await rm(agent, { force: true });
}
await rm(legacyAppRoot, { recursive: true, force: true });

if (startDaemon) {
  execFileSync("/bin/launchctl", ["bootstrap", `gui/${uid}`, d200Agent]);
  console.log("openCodexMicro installed and started.");
} else {
  console.log("openCodexMicro installed; daemon start skipped.");
}
