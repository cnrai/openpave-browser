#!/usr/bin/env node
// Thin Node.js wrapper so PAVE's skill runner can invoke the bash entrypoint.
// PAVE executes `entrypoint` as `node <file>`, so we need a JS entry point
// that delegates to browser-use.sh.
//
// This wrapper also loads BROWSER_USE_* env vars from ~/.pave/tokens.yaml,
// since PAVE doesn't automatically pass tokens.yaml entries as env vars.

const { spawnSync } = require("child_process");
const path = require("path");
const fs = require("fs");

// ── Load env vars from ~/.pave/tokens.yaml ──────────────────────────────────
const tokensPath = path.join(process.env.HOME || "", ".pave", "tokens.yaml");
const env = { ...process.env };

try {
  if (fs.existsSync(tokensPath)) {
    const content = fs.readFileSync(tokensPath, "utf8");
    // Simple parser for flat KEY: "value" lines (handles comments and blanks)
    for (const line of content.split("\n")) {
      const m = line.match(/^\s*(BROWSER_USE_\w+)\s*:\s*"?([^"\n#]+)"?\s*(?:#.*)?$/);
      if (m) {
        env[m[1]] = m[2].trim();
      }
    }
  }
} catch (e) {
  // Non-fatal — fall through with whatever env we have
}

// ── Spawn browser-use.sh ────────────────────────────────────────────────────
const script = path.join(__dirname, "browser-use.sh");
const args = process.argv.slice(2);

const result = spawnSync("bash", [script, ...args], {
  stdio: "inherit",
  env: env,
});

process.exit(result.status || 0);
