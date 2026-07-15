#!/usr/bin/env node
/**
 * puppeteer_bridge.js — Node.js daemon for reliable browser control.
 * Protocol: newline-delimited JSON. Send one JSON object per line.
 *
 * Connection modes (auto-detected at startup):
 *   1. ATTACH: If Chrome is running with --remote-debugging-port=9222, attach to it.
 *   2. LAUNCH: Otherwise, launch bundled Chromium (from `puppeteer` package).
 *      Uses a persistent profile at ~/.pave/browser-profile so cookies/logins persist.
 *      Visible (headless: false) by default — user can see and interact with the browser.
 *      Set env PUPPETEER_HEADLESS=1 for headless mode (servers, CI).
 *
 * Stealth mode (default: ON):
 *   Uses puppeteer-extra + puppeteer-extra-plugin-stealth to evade bot detection.
 *   Masks navigator.webdriver, chrome.runtime, CDP evaluation traces, and more.
 *   Set env PUPPETEER_STEALTH=0 to disable (e.g. for debugging or trusted sites).
 */

const STEALTH = process.env.PUPPETEER_STEALTH !== "0" && process.env.PUPPETEER_STEALTH !== "false";

// Use puppeteer-extra with stealth plugin when enabled, plain puppeteer otherwise
let puppeteer;
if (STEALTH) {
  puppeteer = require("puppeteer-extra");
  const StealthPlugin = require("puppeteer-extra-plugin-stealth");
  puppeteer.use(StealthPlugin());
} else {
  puppeteer = require("puppeteer");
}
const net = require("net");
const fs = require("fs");
const os = require("os");
const path = require("path");

const SOCKET_PATH = "/tmp/puppeteer-bridge.sock";
const CDP_URL = "http://127.0.0.1:9222";
const OP_TIMEOUT = 30000; // 30s max per operation (allows for heavy SPAs)
const PROFILE_DIR = path.join(os.homedir(), ".pave", "browser-profile");
const HEADLESS = process.env.PUPPETEER_HEADLESS === "1" || process.env.PUPPETEER_HEADLESS === "true";

// ── Stealth evasion script ───────────────────────────────────────────────────
// Injected on every page navigation when stealth mode is active.
// Masks navigator.webdriver=true, adds chrome.runtime shim, fixes permissions API.
const STEALTH_EVASION = `(function() {
  // navigator.webdriver -> false
  try {
    Object.defineProperty(navigator, 'webdriver', { get: () => false, configurable: true });
  } catch(e) {}

  // chrome.runtime shim (missing in CDP-connected browsers)
  try {
    if (!window.chrome) window.chrome = {};
    if (!window.chrome.runtime) {
      window.chrome.runtime = {
        PlatformOs: { MAC: 'mac', WIN: 'win', ANDROID: 'android', CROS: 'cros', LINUX: 'linux', OPENBSD: 'openbsd' },
        PlatformArch: { ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64' },
        connect: function() {},
        sendMessage: function() {},
      };
    }
  } catch(e) {}

  // Permissions API: Notification.query should return 'denied' not 'prompt'
  try {
    const origQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = function(parameters) {
      if (parameters.name === 'notifications') {
        return Promise.resolve({ state: Notification.permission, onchange: null });
      }
      return origQuery.call(window.navigator.permissions, parameters);
    };
  } catch(e) {}

  // Plugins: ensure length matches real Chrome (5 on desktop)
  try {
    Object.defineProperty(navigator, 'plugins', {
      get: function() {
        var arr = [
          { name: 'Chrome PDF Viewer' },
          { name: 'Chromium PDF Viewer' },
          { name: 'Microsoft Edge PDF Viewer' },
          { name: 'PDF Viewer' },
          { name: 'WebKit built-in PDF' },
        ];
        arr.item = function(i) { return arr[i] || null; };
        arr.namedItem = function(n) { return arr.find(function(p) { return p.name === n; }) || null; };
        arr.refresh = function() {};
        Object.defineProperty(arr, 'length', { value: 5 });
        return arr;
      },
      configurable: true,
    });
  } catch(e) {}

  // Languages: add common secondary locale
  try {
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'], configurable: true });
  } catch(e) {}
})();`;

let browser = null;
let page = null;
let launchMode = "unknown"; // "attach" or "launch"
let _reconnectTimer = null;
let _reconnectAttempts = 0;
const MAX_RECONNECT_ATTEMPTS = 3;

// ── Reconnection ─────────────────────────────────────────────────────────────

function _scheduleReconnect() {
  /** Schedule a reconnection attempt after losing browser connection. */
  if (_reconnectTimer) return; // Already scheduled
  if (_reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
    console.error("[bridge] Max reconnect attempts (%d) reached, giving up", MAX_RECONNECT_ATTEMPTS);
    return;
  }
  _reconnectAttempts++;
  var delay = 2000 * _reconnectAttempts; // Backoff: 2s, 4s, 6s
  console.error("[bridge] Scheduling reconnect attempt %d/%d in %dms", _reconnectAttempts, MAX_RECONNECT_ATTEMPTS, delay);
  _reconnectTimer = setTimeout(async function() {
    _reconnectTimer = null;
    try {
      await connect();
      _reconnectAttempts = 0; // Reset on success
      console.error("[bridge] Reconnected successfully");
    } catch(e) {
      console.error("[bridge] Reconnect attempt failed: %s", e.message);
      _scheduleReconnect(); // Try again
    }
  }, delay);
}

// ── Stealth helpers ──────────────────────────────────────────────────────────

async function _applyStealthToPage(p) {
  /** Inject evasion script on a page (for attach mode where stealth plugin doesn't auto-apply). */
  if (!STEALTH) return;
  try {
    await p.evaluateOnNewDocument(STEALTH_EVASION);
    // Also run it immediately on current page
    await p.evaluate(STEALTH_EVASION).catch(function() {});
    console.error("[bridge] Stealth evasions applied to page");
  } catch(e) {
    console.error("[bridge] Stealth injection warning: %s", e.message);
  }
}

async function _applyStealthToAllPages(b) {
  /** Apply stealth evasions to all existing pages and set up auto-apply on new pages. */
  if (!STEALTH) return;
  try {
    var pages = await b.pages();
    for (var i = 0; i < pages.length; i++) {
      await _applyStealthToPage(pages[i]);
    }
    // Auto-apply to future pages
    b.on("targetcreated", async function(target) {
      try {
        var newPage = await target.page();
        if (newPage) await _applyStealthToPage(newPage);
      } catch(e) {}
    });
  } catch(e) {
    console.error("[bridge] Stealth bulk apply warning: %s", e.message);
  }
}

// ── Timeout wrapper ───────────────────────────────────────────────────────

function withTimeout(promise, ms, label) {
  return Promise.race([
    promise,
    new Promise(function(_, reject) {
      setTimeout(function() {
        reject(new Error(label + " timed out after " + ms + "ms"));
      }, ms);
    }),
  ]);
}

// ── Connection management ─────────────────────────────────────────────────

async function _probeCDP() {
  /** Check if Chrome is listening on :9222. */
  var http = require("http");
  return new Promise(function(resolve) {
    var req = http.get(CDP_URL + "/json/version", function(res) {
      var data = "";
      res.on("data", function(chunk) { data += chunk; });
      res.on("end", function() {
        try { resolve(JSON.parse(data).Browser || "unknown"); }
        catch (e) { resolve(false); }
      });
    });
    req.on("error", function() { resolve(false); });
    req.setTimeout(2000, function() { req.destroy(); resolve(false); });
  });
}

async function _cleanupStaleChrome() {
  /** Kill Chromium processes using our profile directory and remove lock files. */
  var lockFiles = ["SingletonLock", "SingletonSocket", "SingletonCookie"];
  var hasLock = lockFiles.some(function(f) {
    return fs.existsSync(path.join(PROFILE_DIR, f));
  });

  if (!hasLock) return; // No stale Chrome detected

  console.error("[bridge] Stale Chromium detected (lock files found), cleaning up...");

  // Kill Chromium processes that reference our profile directory
  try {
    var cp = require("child_process");
    // Find PIDs using our profile dir
    var psOut = cp.execSync("ps aux 2>/dev/null || ps -ef 2>/dev/null", { encoding: "utf8", timeout: 5000 });
    var lines = psOut.split("\n");
    var pids = [];
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      if ((line.includes("chromium") || line.includes("chrome") || line.includes("Google Chrome")) &&
          line.includes(PROFILE_DIR)) {
        var match = line.match(/\b(\d+)\b/);
        if (match && match[1]) {
          var pid = parseInt(match[1], 10);
          if (pid !== process.pid && pid > 1) pids.push(pid);
        }
      }
    }
    // Kill found processes (SIGTERM first, then SIGKILL after 2s)
    for (var j = 0; j < pids.length; j++) {
      try { process.kill(pids[j], "SIGTERM"); } catch(e) {}
    }
    if (pids.length > 0) {
      await new Promise(function(r) { setTimeout(r, 2000); });
      for (var k = 0; k < pids.length; k++) {
        try { process.kill(pids[k], "SIGKILL"); } catch(e) {}
      }
      await new Promise(function(r) { setTimeout(r, 500); });
    }
  } catch(e) {
    console.error("[bridge] Cleanup: ps scan failed (%s), trying pkill", e.message);
    try {
      var cp2 = require("child_process");
      var profileEscaped = PROFILE_DIR.replace(/\//g, '\\/');
      // Match all Chrome variants: chromium, Chrome, "Google Chrome for Testing"
      cp2.execSync('pkill -f "chromium.*' + profileEscaped + '" 2>/dev/null || true', { timeout: 5000 });
      cp2.execSync('pkill -f "chrome.*' + profileEscaped + '" 2>/dev/null || true', { timeout: 5000 });
      cp2.execSync('pkill -f "Google Chrome for Testing.*' + profileEscaped + '" 2>/dev/null || true', { timeout: 5000 });
      await new Promise(function(r) { setTimeout(r, 2000); });
    } catch(e2) {}
  }

  // Remove lock files
  for (var l = 0; l < lockFiles.length; l++) {
    var lockPath = path.join(PROFILE_DIR, lockFiles[l]);
    try { if (fs.existsSync(lockPath)) fs.unlinkSync(lockPath); } catch(e) {}
  }
  console.error("[bridge] Stale Chromium cleanup complete");
}

async function connect() {
  if (browser && (browser.connected || browser.isConnected())) return;

  // Try attaching to existing Chrome first
  var chromeVersion = await _probeCDP();
  if (chromeVersion) {
    console.error("[bridge] Attaching to Chrome %s at %s", chromeVersion, CDP_URL);
    try {
      browser = await puppeteer.connect({ browserURL: CDP_URL, defaultViewport: null });
      launchMode = "attach";
      // Apply stealth evasions to all pages (attach mode needs manual injection)
      await _applyStealthToAllPages(browser);
      var pages = await browser.pages();
      page = pages[pages.length - 1] || (await browser.newPage());
      browser.on("disconnected", function() {
        console.error("[bridge] Disconnected from browser");
        browser = null;
        page = null;
        _scheduleReconnect();
      });
      console.error("[bridge] Connected (attach mode). Tab: %s", await page.title());
      return;
    } catch(e) {
      console.error("[bridge] Attach failed: %s, falling back to launch", e.message);
      browser = null;
      page = null;
    }
  }

  // Clean up stale Chrome before launching
  await _cleanupStaleChrome();

  // Launch bundled Chromium
  console.error("[bridge] No Chrome on :9222, launching bundled Chromium (headless: %s, stealth: %s)", HEADLESS, STEALTH);
  if (!fs.existsSync(PROFILE_DIR)) fs.mkdirSync(PROFILE_DIR, { recursive: true });

  // Clear ALL session/tab restore data so Chrome starts with no restored tabs
  try {
    var defDir = path.join(PROFILE_DIR, "Default");
    // Delete Sessions directory contents
    var sessDir = path.join(defDir, "Sessions");
    if (fs.existsSync(sessDir)) {
      for (var f of fs.readdirSync(sessDir)) fs.unlinkSync(path.join(sessDir, f));
    }
    // Delete individual session/tab state files
    var staleFiles = ["Current Session", "Current Tabs", "Last Session", "Last Tabs"];
    for (var sf of staleFiles) {
      var fp = path.join(defDir, sf);
      if (fs.existsSync(fp)) fs.unlinkSync(fp);
    }
    console.error("[bridge] Cleared all session/tab restore data");
  } catch (e) {
    console.error("[bridge] Could not clear session data: %s", e.message);
  }

  browser = await puppeteer.launch({
    headless: HEADLESS,
    userDataDir: PROFILE_DIR,
    defaultViewport: null,
    args: [
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-background-timer-throttling",
      "--disable-renderer-backgrounding",
      "--disable-session-crashed-bubble",
      "--hide-crash-restore-bubble",
      "--window-size=1280,900",
      "--disable-blink-features=AutomationControlled",
    ],
  });
  launchMode = "launch";
  // Wait for Chrome to finish restoring tabs, then close extras
  await new Promise(function(r) { setTimeout(r, 1500); });
  var lp = await browser.pages();
  page = lp[0] || (await browser.newPage());
  for (var i = 1; i < lp.length; i++) {
    try { await lp[i].close(); } catch (e) {}
  }
  try { await page.goto("about:blank", { waitUntil: "domcontentloaded" }).catch(function(){}); } catch (e) {}
  browser.on("disconnected", function() {
    console.error("[bridge] Browser closed");
    browser = null;
    page = null;
    _scheduleReconnect();
  });
  console.error("[bridge] Launched bundled Chromium (launch mode). Profile: %s", PROFILE_DIR);
}

async function getPage() {
  if (!browser || (!browser.connected && !browser.isConnected())) {
    await connect();
    return page;
  }
  // Health check: verify the connection is actually alive.
  // Use a longer timeout and retry for heavy SPAs that block the main thread.
  try {
    await withTimeout(page.evaluate("1"), 8000, "health check");
  } catch (e) {
    // The page might be busy loading a heavy JS bundle (e.g. CampBrain 2.5MB).
    // Check if the browser process is still alive before deciding to reconnect.
    // If browser is still connected, the page is just busy — return it as-is
    // and let the caller's operation timeout handle it.
    if (browser && (browser.connected || browser.isConnected())) {
      console.error("[bridge] Page busy (health check failed), but browser alive — keeping page: %s", e.message);
      return page;
    }
    console.error("[bridge] Connection stale, reconnecting: %s", e.message);
    browser = null;
    page = null;
    await connect();
  }
  return page;
}

// ── Handlers ──────────────────────────────────────────────────────────────

const handlers = {
  async ping(cmd) {
    if (!browser) return { ok: false, connected: false };
    var connected = browser.connected || browser.isConnected();
    if (!connected) return { ok: false, connected: false };
    try {
      await withTimeout(page.evaluate("1"), 10000, "ping");
      return { ok: true, pong: true, connected: true, mode: launchMode };
    } catch (e) {
      // Bridge is alive and browser is connected, but the page may be busy
      // (e.g. heavy SPA loading a large JS bundle). Report as alive-but-busy
      // so the caller doesn't restart us unnecessarily.
      return { ok: true, connected: false, busy: true, error: e.message };
    }
  },

  async url(cmd) {
    const p = await getPage();
    return { ok: true, url: p.url(), title: await withTimeout(p.title(), OP_TIMEOUT, "title") };
  },

  async navigate(cmd) {
    const p = await getPage();
    var navTimeout = cmd.timeout || 15000;
    await withTimeout(
      p.goto(cmd.url, { waitUntil: "domcontentloaded", timeout: navTimeout }),
      OP_TIMEOUT, "navigate"
    );
    // For heavy SPAs (e.g. CampBrain/Vue), wait briefly for JS to bootstrap.
    // This catches the case where domcontentloaded fires on a thin shell
    // but the app hasn't rendered yet. We don't use networkidle0 because
    // some sites keep long-poll connections open indefinitely.
    try {
      await p.waitForFunction("document.readyState === 'complete'", { timeout: 5000 });
    } catch(e) {}
    return { ok: true, url: p.url(), title: await p.title() };
  },

  async type(cmd) {
    const p = await getPage();
    // Focus + select all existing text via JS (avoids Puppeteer's click() which can hang)
    var exists = await withTimeout(p.evaluate(function(sel) {
      var el = document.querySelector(sel);
      if (el) {
        el.focus();
        if (el.select) el.select();          // textarea/input: select all
        else if (el.value !== undefined) el.value = ""; // input: clear
        return true;
      }
      return false;
    }, cmd.selector), OP_TIMEOUT, "type:focus");
    if (!exists) return { ok: false, error: "element not found: " + cmd.selector };
    // Delete any selected text, then type
    await withTimeout(p.keyboard.press("Backspace"), 3000, "type:clear");
    await withTimeout(p.keyboard.type(cmd.text, { delay: 10 }), OP_TIMEOUT, "type:input");
    return { ok: true, selector: cmd.selector, text: cmd.text };
  },

  async type_text(cmd) {
    const p = await getPage();
    await withTimeout(p.keyboard.type(cmd.text, { delay: 10 }), OP_TIMEOUT, "type_text");
    return { ok: true, text: cmd.text };
  },

  async click(cmd) {
    const p = await getPage();
    if (cmd.selector) {
      // Use evaluate-based click (avoids Puppeteer page.click() hang
      // on elements with overlays or complex event handling)
      var ok = await withTimeout(p.evaluate(function(sel) {
        var el = document.querySelector(sel);
        if (!el) return false;
        el.scrollIntoView({ block: "center" });
        el.click();
        return true;
      }, cmd.selector), OP_TIMEOUT, "click:selector");
      if (!ok) return { ok: false, error: "element not found: " + cmd.selector };
      return { ok: true, selector: cmd.selector };
    } else if (cmd.x !== undefined) {
      await withTimeout(p.mouse.click(cmd.x, cmd.y), OP_TIMEOUT, "click:coords");
      return { ok: true, x: cmd.x, y: cmd.y };
    }
    return { ok: false, error: "need selector or x,y" };
  },

  async press(cmd) {
    const p = await getPage();
    var keyMap = {
      return: "Enter", enter: "Enter", escape: "Escape", esc: "Escape",
      tab: "Tab", "delete": "Delete", backspace: "Backspace", space: "Space",
      up: "ArrowUp", down: "ArrowDown", left: "ArrowLeft", right: "ArrowRight",
    };
    var key = cmd.key;
    if (key.includes("+")) {
      var parts = key.split("+");
      var modMap = { cmd: "Meta", ctrl: "Control", alt: "Alt", shift: "Shift" };
      var mapped = parts.map(function(k) {
        var lower = k.trim().toLowerCase();
        return modMap[lower] || keyMap[lower] || k.trim();
      });
      for (var i = 0; i < mapped.length - 1; i++) await p.keyboard.down(mapped[i]);
      await p.keyboard.press(mapped[mapped.length - 1]);
      for (var i = mapped.length - 2; i >= 0; i--) await p.keyboard.up(mapped[i]);
    } else {
      await p.keyboard.press(keyMap[key.toLowerCase()] || key);
    }
    return { ok: true, key: key };
  },

  async scroll(cmd) {
    const p = await getPage();
    var dy = cmd.direction === "up" ? -(cmd.amount || 500) : (cmd.amount || 500);
    await withTimeout(p.mouse.wheel({ deltaY: dy }), OP_TIMEOUT, "scroll");
    // Small delay for scroll to settle
    await new Promise(function(r) { setTimeout(r, 300); });
    return { ok: true, direction: cmd.direction, amount: cmd.amount };
  },

  async screenshot(cmd) {
    const p = await getPage();
    var outPath = cmd.output || "/tmp/browser-use-screenshot.png";
    await withTimeout(p.screenshot({ path: outPath, fullPage: cmd.fullPage || false }), OP_TIMEOUT, "screenshot");
    return { ok: true, path: outPath };
  },

  async dom(cmd) {
    const p = await getPage();
    var data = await withTimeout(p.evaluate(function() {
      function buildSelector(el) {
        if (el.id) return "#" + el.id;
        var name = el.getAttribute("name");
        if (name) return el.tagName.toLowerCase() + '[name="' + name + '"]';
        var aria = el.getAttribute("aria-label");
        if (aria) return el.tagName.toLowerCase() + '[aria-label="' + aria + '"]';
        var parts = [];
        var node = el;
        while (node && node.nodeType === 1 && parts.length < 4) {
          var part = node.tagName.toLowerCase();
          if (node.className && typeof node.className === "string") {
            var cls = node.className.trim().split(/\s+/)[0];
            if (cls) part += "." + cls;
          }
          var parent = node.parentElement;
          if (parent) {
            var sibs = Array.from(parent.children).filter(function(c) { return c.tagName === node.tagName; });
            if (sibs.length > 1) part += ":nth-of-type(" + (sibs.indexOf(node) + 1) + ")";
          }
          parts.unshift(part);
          node = node.parentElement;
        }
        return parts.join(" > ");
      }
      var results = [];
      var sel = 'a, button, input, textarea, select, [role="button"], [role="link"], [role="textbox"], [onclick], [tabindex]';
      var els = document.querySelectorAll(sel);
      els.forEach(function(el, i) {
        var r = el.getBoundingClientRect();
        if (r.width > 2 && r.height > 2) {
          results.push({
            id: i + 1,
            tag: el.tagName.toLowerCase(),
            type: el.getAttribute("type") || "",
            role: el.getAttribute("role") || "",
            name: el.getAttribute("name") || "",
            placeholder: el.getAttribute("placeholder") || "",
            text: (el.innerText || el.textContent || "").trim().substring(0, 100),
            href: (el.href || "").substring(0, 120),
            value: (el.value || "").substring(0, 50),
            ariaLabel: el.getAttribute("aria-label") || "",
            selector: buildSelector(el),
            x: Math.round(r.x), y: Math.round(r.y),
            w: Math.round(r.width), h: Math.round(r.height),
            cx: Math.round(r.x + r.width / 2), cy: Math.round(r.y + r.height / 2),
          });
        }
      });
      return {
        title: document.title, url: location.href,
        scrollX: window.scrollX, scrollY: window.scrollY,
        viewportW: window.innerWidth, viewportH: window.innerHeight,
        elements: results.slice(0, 80),
      };
    }), OP_TIMEOUT, "dom");
    return Object.assign({ ok: true }, data);
  },

  async eval(cmd) {
    const p = await getPage();
    var result = await withTimeout(p.evaluate(cmd.code), OP_TIMEOUT, "eval");
    return { ok: true, result: JSON.stringify(result) };
  },

  async new_tab(cmd) {
    if (!browser) await connect();
    page = await browser.newPage();
    if (cmd.url) await page.goto(cmd.url, { waitUntil: "domcontentloaded", timeout: 15000 });
    return { ok: true, url: page.url() };
  },

  async focus(cmd) {
    // In launch mode, bring the browser window to front
    if (launchMode === "launch" && browser) {
      var tp = await getPage();
      await withTimeout(tp.bringToFront(), 3000, "focus");
      return { ok: true, mode: launchMode };
    }
    // In attach mode, we can't control the Chrome window directly
    return { ok: true, mode: launchMode, note: "attach mode — window focus not controlled" };
  },

  async force_cleanup(cmd) {
    /** Kill browser, clean up lock files, reset state. Caller should restart bridge after. */
    console.error("[bridge] force_cleanup requested");
    try {
      if (browser) {
        try { await browser.close(); } catch(e) {
          console.error("[bridge] browser.close() failed: %s", e.message);
        }
      }
    } catch(e) {}
    browser = null;
    page = null;

    // Kill Chrome processes using our profile (all naming variants)
    try {
      var cp = require("child_process");
      cp.execSync('pkill -9 -f "chromium.*browser-profile" 2>/dev/null || true', { timeout: 5000 });
      cp.execSync('pkill -9 -f "chrome.*browser-profile" 2>/dev/null || true', { timeout: 5000 });
      cp.execSync('pkill -9 -f "Google Chrome for Testing.*browser-profile" 2>/dev/null || true', { timeout: 5000 });
      await new Promise(function(r) { setTimeout(r, 1000); });
    } catch(e) {}

    // Remove lock files
    var lockFiles = ["SingletonLock", "SingletonSocket", "SingletonCookie"];
    for (var i = 0; i < lockFiles.length; i++) {
      try {
        var lp = path.join(PROFILE_DIR, lockFiles[i]);
        if (fs.existsSync(lp)) fs.unlinkSync(lp);
      } catch(e) {}
    }

    // Remove bridge socket
    try { if (fs.existsSync(SOCKET_PATH)) fs.unlinkSync(SOCKET_PATH); } catch(e) {}
    try { if (fs.existsSync("/tmp/puppeteer-bridge-ready")) fs.unlinkSync("/tmp/puppeteer-bridge-ready"); } catch(e) {}

    _reconnectAttempts = 0;
    if (_reconnectTimer) { clearTimeout(_reconnectTimer); _reconnectTimer = null; }

    return { ok: true, message: "cleanup complete, browser state reset" };
  },
};

async function handleRequest(data) {
  try {
    var cmd = JSON.parse(data.toString().trim());
    var handler = handlers[cmd.action];
    if (!handler) return { ok: false, error: "unknown action: " + cmd.action };
    // Global timeout on the entire handler
    return await withTimeout(handler(cmd), OP_TIMEOUT + 5000, cmd.action);
  } catch (err) {
    console.error("[bridge] Error: %s", err.message);
    return { ok: false, error: err.message };
  }
}

// ── Socket server (newline-delimited JSON) ────────────────────────────────

async function main() {
  try { await connect(); } catch (err) {
    console.error("[bridge] Connect/launch failed: %s", err.message);
    // Don't exit — start the server anyway. Commands will trigger reconnect via getPage().
    // But clean up stale state so getPage() can try fresh.
    browser = null;
    page = null;
    _reconnectAttempts = 0;
  }

  if (fs.existsSync(SOCKET_PATH)) fs.unlinkSync(SOCKET_PATH);

  var server = net.createServer(function(socket) {
    var buffer = "";
    socket.on("data", async function(chunk) {
      buffer += chunk.toString();
      var idx;
      while ((idx = buffer.indexOf("\n")) >= 0) {
        var line = buffer.substring(0, idx);
        buffer = buffer.substring(idx + 1);
        if (line.trim()) {
          var result = await handleRequest(line);
          try { socket.write(JSON.stringify(result) + "\n"); } catch(e) {}
        }
      }
    });
    socket.on("error", function() {});
  });

  server.listen(SOCKET_PATH);
  fs.chmodSync(SOCKET_PATH, 0o666);
  fs.writeFileSync("/tmp/puppeteer-bridge-ready", "ready\n");
  console.error("[bridge] Listening on %s (mode: %s)", SOCKET_PATH, launchMode);

  process.on("SIGINT", function() {
    _shutdown("SIGINT");
  });
  process.on("SIGTERM", function() {
    _shutdown("SIGTERM");
  });

  async function _shutdown(signal) {
    console.error("[bridge] Received %s, shutting down gracefully", signal);
    if (browser) {
      try {
        if (launchMode === "launch") {
          await withTimeout(browser.close(), 5000, "shutdown:close");
        } else {
          browser.disconnect();
        }
      } catch(e) {
        console.error("[bridge] Error closing browser: %s", e.message);
      }
    }
    browser = null;
    page = null;
    if (fs.existsSync(SOCKET_PATH)) fs.unlinkSync(SOCKET_PATH);
    if (fs.existsSync("/tmp/puppeteer-bridge-ready")) fs.unlinkSync("/tmp/puppeteer-bridge-ready");
    process.exit(0);
  }
}

main();
