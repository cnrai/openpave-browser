#!/usr/bin/env node
/**
 * puppeteer_bridge.js — Node.js daemon for reliable browser control.
 * Protocol: newline-delimited JSON. Send one JSON object per line.
 */

const puppeteer = require("puppeteer-core");
const net = require("net");
const fs = require("fs");

const SOCKET_PATH = "/tmp/puppeteer-bridge.sock";
const CDP_URL = "http://127.0.0.1:9222";

let browser = null;
let page = null;

async function connect() {
  if (browser && browser.connected) return;
  console.error("[bridge] Connecting to %s...", CDP_URL);
  browser = await puppeteer.connect({ browserURL: CDP_URL, defaultViewport: null });
  const pages = await browser.pages();
  page = pages[pages.length - 1] || (await browser.newPage());
  browser.on("disconnected", () => { browser = null; page = null; });
  console.error("[bridge] Connected. Tab: %s", await page.title());
}

async function getPage() {
  if (!browser || !browser.connected) await connect();
  if (!page) {
    const pages = await browser.pages();
    page = pages[pages.length - 1];
  }
  return page;
}

// ── Handlers ──────────────────────────────────────────────────────────────

const handlers = {
  async ping(cmd) {
    return { ok: true, pong: true, connected: !!(browser && browser.connected) };
  },

  async url(cmd) {
    const p = await getPage();
    return { ok: true, url: p.url(), title: await p.title() };
  },

  async navigate(cmd) {
    const p = await getPage();
    await p.goto(cmd.url, { waitUntil: "domcontentloaded", timeout: cmd.timeout || 15000 });
    return { ok: true, url: p.url(), title: await p.title() };
  },

  async type(cmd) {
    const p = await getPage();
    await p.click(cmd.selector, { clickCount: 3 });
    await p.keyboard.press("Backspace");
    await p.type(cmd.selector, cmd.text, { delay: 10 });
    return { ok: true, selector: cmd.selector, text: cmd.text };
  },

  async type_text(cmd) {
    const p = await getPage();
    await p.keyboard.type(cmd.text, { delay: 10 });
    return { ok: true, text: cmd.text };
  },

  async click(cmd) {
    const p = await getPage();
    if (cmd.selector) {
      await p.click(cmd.selector);
      return { ok: true, selector: cmd.selector };
    } else if (cmd.x !== undefined) {
      await p.mouse.click(cmd.x, cmd.y);
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
    await p.mouse.wheel({ deltaY: dy });
    return { ok: true, direction: cmd.direction, amount: cmd.amount };
  },

  async screenshot(cmd) {
    const p = await getPage();
    var outPath = cmd.output || "/tmp/browser-use-screenshot.png";
    await p.screenshot({ path: outPath, fullPage: cmd.fullPage || false });
    return { ok: true, path: outPath };
  },

  async dom(cmd) {
    const p = await getPage();
    var data = await p.evaluate(function() {
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
    });
    return Object.assign({ ok: true }, data);
  },

  async eval(cmd) {
    const p = await getPage();
    var result = await p.evaluate(cmd.code);
    return { ok: true, result: JSON.stringify(result) };
  },

  async new_tab(cmd) {
    if (!browser || !browser.connected) await connect();
    page = await browser.newPage();
    if (cmd.url) await page.goto(cmd.url, { waitUntil: "domcontentloaded", timeout: 15000 });
    return { ok: true, url: page.url() };
  },
};

async function handleRequest(data) {
  try {
    var cmd = JSON.parse(data.toString().trim());
    var handler = handlers[cmd.action];
    if (!handler) return { ok: false, error: "unknown action: " + cmd.action };
    return await handler(cmd);
  } catch (err) {
    console.error("[bridge] Error: %s", err.message);
    return { ok: false, error: err.message };
  }
}

// ── Socket server (newline-delimited JSON) ────────────────────────────────

async function main() {
  try { await connect(); } catch (err) {
    console.error("[bridge] Connect failed: %s", err.message);
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
  console.error("[bridge] Listening on %s", SOCKET_PATH);

  process.on("SIGINT", function() {
    if (fs.existsSync(SOCKET_PATH)) fs.unlinkSync(SOCKET_PATH);
    if (fs.existsSync("/tmp/puppeteer-bridge-ready")) fs.unlinkSync("/tmp/puppeteer-bridge-ready");
    process.exit(0);
  });
}

main();
