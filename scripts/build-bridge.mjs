import { build } from "esbuild";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

const root = resolve(".");
const output = resolve(root, "dist", "bridge.mjs");
await mkdir(resolve(root, "dist"), { recursive: true });
await build({
  entryPoints: [resolve(root, "src", "bridge", "server.mjs")],
  outfile: output,
  bundle: true,
  platform: "node",
  format: "esm",
  target: "node20",
  minify: false,
  banner: {
    js: "import { createRequire as __createRequire } from 'node:module'; const require = __createRequire(import.meta.url);"
  }
});
console.log(`Built bridge: ${output}`);
