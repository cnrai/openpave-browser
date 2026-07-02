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

// ── Derive LOCATE_ANYTHING_API_URL from PAVE_EPM_URL ───────────────────────
// JWT is NOT loaded here — detector.py reads it fresh on each API call
// (PAVE rotates the token every ~15 min, so caching it would go stale).
// If user hasn't set the API URL explicitly, derive it from PAVE's EPM URL.
// Auto-persist to tokens.yaml so future runs don't need PAVE_EPM_URL in env.
if (!env.LOCATE_ANYTHING_API_URL && process.env.PAVE_EPM_URL) {
  var epmUrl = process.env.PAVE_EPM_URL.replace(/\/$/, "");
  // Don't double-append if PAVE_EPM_URL already ends with /chat/completions
  if (!epmUrl.endsWith("/chat/completions")) {
    epmUrl += "/chat/completions";
  }
  env.LOCATE_ANYTHING_API_URL = epmUrl;

  // Persist to tokens.yaml if not already there
  try {
    var tokensContent = fs.existsSync(tokensPath)
      ? fs.readFileSync(tokensPath, "utf8")
      : "";
    if (!tokensContent.includes("LOCATE_ANYTHING_API_URL")) {
      var line = "LOCATE_ANYTHING_API_URL: \"" + epmUrl + "\"\n";
      fs.appendFileSync(tokensPath, line);
    }
  } catch (e) {
    // Non-fatal — env var is set for this run
  }
}

// ── Strip empty fields recursively ──────────────────────────────────────────
// Removes null, "", [], and {} from nested objects/arrays.
// Preserves meaningful falsy values: 0, false, 0.0.
function stripEmpty(obj) {
  if (Array.isArray(obj)) {
    return obj.map(stripEmpty).filter(
      (item) =>
        item !== null &&
        item !== "" &&
        item !== undefined &&
        !(Array.isArray(item) && item.length === 0) &&
        !(
          item !== null &&
          typeof item === "object" &&
          !Array.isArray(item) &&
          Object.keys(item).length === 0
        )
    );
  }
  if (obj !== null && typeof obj === "object") {
    const result = {};
    for (const [key, value] of Object.entries(obj)) {
      const stripped = stripEmpty(value);
      if (
        stripped !== null &&
        stripped !== "" &&
        stripped !== undefined &&
        !(Array.isArray(stripped) && stripped.length === 0) &&
        !(
          stripped !== null &&
          typeof stripped === "object" &&
          !Array.isArray(stripped) &&
          Object.keys(stripped).length === 0
        )
      ) {
        result[key] = stripped;
      }
    }
    return result;
  }
  return obj;
}

// ── Spawn browser-use.sh ────────────────────────────────────────────────────
const script = path.join(__dirname, "browser-use.sh");
const args = process.argv.slice(2);

const result = spawnSync("bash", [script, ...args], {
  stdio: ["inherit", "pipe", "pipe"],
  env: env,
  maxBuffer: 10 * 1024 * 1024,
});

// Pass stderr through directly
if (result.stderr) process.stderr.write(result.stderr);

// Process stdout: parse JSON, strip empty fields, re-serialize compactly.
// JSON.stringify() defaults to no spaces (", " and ": " are only added
// when a space argument is passed), so this strips whitespace AND empties.
const stdout = (result.stdout || "").toString();
try {
  const parsed = JSON.parse(stdout.trim());
  process.stdout.write(JSON.stringify(stripEmpty(parsed)));
} catch {
  // Not valid JSON — pass through unchanged
  process.stdout.write(stdout);
}

process.exit(result.status || 0);
