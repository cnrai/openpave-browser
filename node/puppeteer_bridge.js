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
 */

const puppeteer = require("puppeteer");
const net = require("net");
const fs = require("fs");
const os = require("os");
const path = require("path");

const SOCKET_PATH = "/tmp/puppeteer-bridge.sock";
const CDP_URL = "http://127.0.0.1:9222";
const OP_TIMEOUT = 20000; // 20s max per operation
const PROFILE_DIR = path.join(os.homedir(), ".pave", "browser-profile");
const HEADLESS = process.env.PUPPETEER_HEADLESS === "1" || process.env.PUPPETEER_HEADLESS === "true";

let browser = null;
let page = null;
let launchMode = "unknown"; // "attach" or "launch"

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

async function connect() {
  if (browser && (browser.connected || browser.isConnected())) return;

  // Try attaching to existing Chrome first
  var chromeVersion = await _probeCDP();
  if (chromeVersion) {
    console.error("[bridge] Attaching to Chrome %s at %s", chromeVersion, CDP_URL);
    browser = await puppeteer.connect({ browserURL: CDP_URL, defaultViewport: null });
    launchMode = "attach";
    var pages = await browser.pages();
    page = pages[pages.length - 1] || (await browser.newPage());
    browser.on("disconnected", function() {
      console.error("[bridge] Disconnected from browser");
      browser = null;
      page = null;
    });
    console.error("[bridge] Connected (attach mode). Tab: %s", await page.title());
    return;
  }

  // Launch bundled Chromium
  console.error("[bridge] No Chrome on :9222, launching bundled Chromium (headless: %s)", HEADLESS);
  if (!fs.existsSync(PROFILE_DIR)) fs.mkdirSync(PROFILE_DIR, { recursive: true });
  browser = await puppeteer.launch({
    headless: HEADLESS,
    userDataDir: PROFILE_DIR,
    defaultViewport: null,
    args: [
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-background-timer-throttling",
      "--disable-renderer-backgrounding",
      "--window-size=1280,900",
    ],
  });
  launchMode = "launch";
  var lp = await browser.pages();
  page = lp[0] || (await browser.newPage());
  browser.on("disconnected", function() {
    console.error("[bridge] Browser closed");
    browser = null;
    page = null;
  });
  console.error("[bridge] Launched bundled Chromium (launch mode). Profile: %s", PROFILE_DIR);
}

async function getPage() {
  if (!browser || (!browser.connected && !browser.isConnected())) {
    await connect();
    return page;
  }
  // Health check: verify the connection is actually alive
  try {
    await withTimeout(page.evaluate("1"), 3000, "health check");
  } catch (e) {
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
      await withTimeout(page.evaluate("1"), 2000, "ping");
      return { ok: true, pong: true, connected: true, mode: launchMode };
    } catch (e) {
      return { ok: false, connected: false, error: e.message };
    }
  },

  async url(cmd) {
    const p = await getPage();
    return { ok: true, url: p.url(), title: await withTimeout(p.title(), OP_TIMEOUT, "title") };
  },

  async navigate(cmd) {
    const p = await getPage();
    await withTimeout(
      p.goto(cmd.url, { waitUntil: "domcontentloaded", timeout: cmd.timeout || 15000 }),
      OP_TIMEOUT, "navigate"
    );
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
    process.exit(1);
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
    if (fs.existsSync(SOCKET_PATH)) fs.unlinkSync(SOCKET_PATH);
    if (fs.existsSync("/tmp/puppeteer-bridge-ready")) fs.unlinkSync("/tmp/puppeteer-bridge-ready");
    if (browser && launchMode === "launch") browser.close();
    process.exit(0);
  });
}

main();
