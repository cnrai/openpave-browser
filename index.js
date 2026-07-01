#!/usr/bin/env node
// Thin Node.js wrapper so PAVE's skill runner can invoke the bash entrypoint.
// PAVE executes `entrypoint` as `node <file>`, so we need a JS entry point
// that delegates to browser-use.sh.

const { spawnSync } = require("child_process");
const path = require("path");

const script = path.join(__dirname, "browser-use.sh");
const args = process.argv.slice(2);

const result = spawnSync("bash", [script, ...args], {
  stdio: "inherit",
  env: process.env,
});

process.exit(result.status || 0);
