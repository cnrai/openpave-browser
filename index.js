#!/usr/bin/env node
// Thin Node.js wrapper so PAVE's skill runner can invoke the bash entrypoint.
// PAVE executes `entrypoint` as `node <file>`, so we need a JS entry point
// that delegates to browser-use.sh.
//
// This wrapper also:
//   1. Loads BROWSER_USE_* env vars from ~/.pave/tokens.yaml
//   2. Loads PAVE JWT from ~/.pave/membership-credentials.json → LOCATE_ANYTHING_API_KEY
//   3. Sets default LOCATE_ANYTHING_API_URL if EPM URL is configured

const { spawnSync } = require("child_process");
const path = require("path");
const fs = require("fs");

const env = { ...process.env };

// ── Load env vars from ~/.pave/tokens.yaml ──────────────────────────────────
const tokensPath = path.join(process.env.HOME || "", ".pave", "tokens.yaml");
try {
  if (fs.existsSync(tokensPath)) {
    const content = fs.readFileSync(tokensPath, "utf8");
    for (const line of content.split("\n")) {
      const m = line.match(/^\s*(BROWSER_USE_\w+|LOCATE_ANYTHING_\w+)\s*:\s*"?([^"\n#]+)"?\s*(?:#.*)?$/);
      if (m) {
        env[m[1]] = m[2].trim();
      }
    }
  }
} catch (e) {
  // Non-fatal
}

// ── Load PAVE JWT → LOCATE_ANYTHING_API_KEY ─────────────────────────────────
// If user hasn't explicitly set API key, auto-load from PAVE membership creds
if (!env.LOCATE_ANYTHING_API_KEY) {
  for (const credPath of [
    path.join(process.env.HOME || "", ".pave", "membership-credentials.json"),
    path.join(process.env.HOME || "", ".pave", "epm-token.json"),
  ]) {
    try {
      if (fs.existsSync(credPath)) {
        const creds = JSON.parse(fs.readFileSync(credPath, "utf8"));
        if (creds.access_token) {
          env.LOCATE_ANYTHING_API_KEY = creds.access_token;
          break;
        }
      }
    } catch (e) {
      // Non-fatal
    }
  }
}

// ── Derive LOCATE_ANYTHING_API_URL from PAVE_EPM_URL ───────────────────────
// If user hasn't set the API URL explicitly, derive it from PAVE's EPM URL
if (!env.LOCATE_ANYTHING_API_URL && process.env.PAVE_EPM_URL) {
  // PAVE_EPM_URL is like "https://epm.openpave.ai/pave/v1"
  // API URL should be "https://epm.openpave.ai/pave/v1/chat/completions"
  env.LOCATE_ANYTHING_API_URL = process.env.PAVE_EPM_URL.replace(/\/$/, "") + "/chat/completions";
}

// ── Spawn browser-use.sh ────────────────────────────────────────────────────
const script = path.join(__dirname, "browser-use.sh");
const args = process.argv.slice(2);

const result = spawnSync("bash", [script, ...args], {
  stdio: "inherit",
  env: env,
});

process.exit(result.status || 0);
