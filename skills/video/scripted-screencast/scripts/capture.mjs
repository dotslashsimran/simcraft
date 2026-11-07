// Pass 1: drive the page on a virtual clock and save one screenshot per frame.
// The page never runs on real time: timers, Date, requestAnimationFrame and CSS/WAAPI
// animations all advance by exactly one frame between screenshots, so every run is identical.
// Writes <work>/frames/*.jpg and <work>/timeline.json (cursor per frame, clicks, zooms, captions).
import { chromium } from "playwright";
import fs from "node:fs";
import path from "node:path";

const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const easeInOut = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
// Human pointer moves accelerate fast and land slowly: warp time so peak speed comes early.
const easeMove = (t) => easeInOut(Math.pow(t, 0.8));

function rng(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) | 0;
    let t = Math.imul(s ^ (s >>> 15), 1 | s);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// Runs inside the page before any app code.
function pageInit({ seed, css }) {
  let s = seed >>> 0;
  Math.random = () => {
    s = (s + 0x6d2b79f5) | 0;
    let t = Math.imul(s ^ (s >>> 15), 1 | s);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  const addCss = () => {
    const st = document.createElement("style");
    st.dataset.screencast = "";
    st.textContent = css;
    (document.head || document.documentElement).appendChild(st);
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", addCss);
  else addCss();

  let dirty = true;
  new MutationObserver(() => (dirty = true)).observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
  for (const ev of ["scroll", "input", "focus", "blur"]) addEventListener(ev, () => (dirty = true), true);
  const ours = new WeakSet();

  window.__sc = {
    scrollEl: null,
    // Advance every animation by dt ms. Returns whether anything on screen may have changed.
    advance(dt, x, y) {
      let busy = dirty;
      dirty = false;
      for (const a of document.getAnimations()) {
        if (a.playState === "finished" || a.playState === "idle") continue;
        if (a.playState === "paused" && !ours.has(a)) continue; // paused by the app itself
        if (!ours.has(a)) { ours.add(a); a.pause(); a.currentTime = 0; }
        else if (a.playState === "running") a.pause(); // the app restarted it
        const end = a.effect ? a.effect.getComputedTiming().endTime : Infinity;
        const ct = (a.currentTime || 0) + dt * (a.playbackRate || 1);
        if (Number.isFinite(end) && (ct >= end || ct <= 0) && a.playbackRate !== 0 && dt > 0) {
          a.finish(); // fires animationend / transitionend like a real run
        } else a.currentTime = ct;
        busy = true;
      }
      const el = document.elementFromPoint(x, y);
      let c = el ? getComputedStyle(el).cursor : "auto";
      if (c === "auto" && el && (el.isContentEditable || el.matches("textarea, input:not([type=checkbox],[type=radio],[type=button],[type=submit],[type=range],[type=color],[type=file])"))) c = "text";
      return { busy, cursor: c === "pointer" ? "pointer" : c === "text" ? "text" : "default" };
    },
    // Find the element that scrolls for `el` (or the page) and remember it for setScroll.
    pickScroller(el) {
      let n = el;
      while (n && n !== document.body && n !== document.documentElement) {
        const st = getComputedStyle(n);
        if (/(auto|scroll|overlay)/.test(st.overflowY) && n.scrollHeight > n.clientHeight + 1) break;
        n = n.parentElement;
      }
      if (!n || n === document.body || n === document.documentElement) n = document.scrollingElement;
      this.scrollEl = n;
      const r = n === document.scrollingElement ? { top: 0, height: innerHeight } : n.getBoundingClientRect();
      return { top: n.scrollTop, max: n.scrollHeight - n.clientHeight, viewTop: r.top, viewHeight: r.height };
    },
    setScroll(v) { this.scrollEl.scrollTop = v; },
  };
}

export async function capture(demo, { fps, dpr, work, quality, log = console.log }) {
  const vw = demo.viewport.width, vh = demo.viewport.height;
  const frameDir = path.join(work, "frames");
  fs.rmSync(work, { recursive: true, force: true });
  fs.mkdirSync(frameDir, { recursive: true });
  const rand = rng(demo.seed);

  const css = [
    "*,*::before,*::after{scroll-behavior:auto!important}",
    demo.hideScrollbars ? "::-webkit-scrollbar{display:none!important}*{scrollbar-width:none!important}" : "",
    demo.freezeAnimations ? "*,*::before,*::after{animation:none!important;transition:none!important}" : "",
    demo.hideCaret ? "*{caret-color:transparent!important}" : "",
    demo.css || "",
  ].join("\n");

  const browser = await chromium.launch({ args: ["--force-color-profile=srgb", "--font-render-hinting=none", "--disable-smooth-scrolling"] });
  const context = await browser.newContext({
    viewport: { width: vw, height: vh }, deviceScaleFactor: dpr,
    colorScheme: demo.colorScheme, locale: demo.locale, timezoneId: demo.timezone, reducedMotion: "no-preference",
  });
  const page = await context.newPage();
  page.setDefaultTimeout(demo.timeout);
  page.on("pageerror", (e) => log(`[page error] ${e.message}`));
  await page.clock.install({ time: new Date(demo.date) });
  await page.addInitScript(pageInit, { seed: demo.seed, css });
  const cdp = await context.newCDPSession(page);

  let clockMs = 0; // virtual ms elapsed since recording began
  const settleTime = async (ms) => { await page.clock.runFor(ms); await page.evaluate((d) => window.__sc.advance(d, 0, 0), ms); };
  const resolveUrl = (u) => (/^[a-z]+:/i.test(u) ? u : "file://" + path.resolve(demo.dir, u));
  const goto = async (u) => {
    await page.goto(resolveUrl(u), { waitUntil: "load" });
    await page.waitForLoadState("networkidle", { timeout: 8000 }).catch(() => {});
    await page.evaluate(() => document.fonts.ready);
    await settleTime(demo.loadSettle);
  };

  const locate = (sel) => {
    if (sel && typeof sel === "object") {
      if (sel.role) return page.getByRole(sel.role, sel.name ? { name: sel.name, exact: !!sel.exact } : {}).first();
      if (sel.text) return page.getByText(sel.text, { exact: !!sel.exact }).first();
      if (sel.label) return page.getByLabel(sel.label).first();
      if (sel.placeholder) return page.getByPlaceholder(sel.placeholder).first();
    }
    return page.locator(sel).first();
  };
  const describe = (sel) => (typeof sel === "string" ? sel : JSON.stringify(sel));

  // ---- setup: staged state, not recorded ----
  if (demo.url) await goto(demo.url);
  for (const st of demo.setup) {
    if (st.goto) await goto(st.goto);
    else if (st.eval) await page.evaluate(st.eval);
    else if (st.localStorage) { await page.evaluate((o) => { for (const [k, v] of Object.entries(o)) localStorage.setItem(k, typeof v === "string" ? v : JSON.stringify(v)); }, st.localStorage); }
    else if (st.reload) await goto(page.url());
    else if (st.click) await locate(st.click).click();
    else if (st.fill) await locate(st.fill).fill(st.text ?? "");
    else if (st.press) await page.keyboard.press(st.press);
    else if (st.wait) await locate(st.wait).waitFor({ state: "visible" });
    else throw new Error(`setup step not understood: ${JSON.stringify(st)}`);
    await settleTime(300);
  }
  await settleTime(demo.loadSettle);

  // ---- recording ----
  const frames = []; // [src, x, y, down, cursorType]
  const events = []; // clicks, actions, zooms, captions
  let frame = 0, shots = 0, lastSrc = -1, force = true;
  let cur = { x: demo.cursorStart[0] * vw, y: demo.cursorStart[1] * vh }, mouseAt = null, down = false;
  const pending = [];
  const now = () => (frame * 1000) / fps;

  async function shoot() {
    const t = Math.round(now());
    if (!mouseAt || mouseAt.x !== cur.x || mouseAt.y !== cur.y) {
      await page.mouse.move(cur.x, cur.y);
      mouseAt = { ...cur };
      force = true;
    }
    const d = t - clockMs;
    if (d > 0) await page.clock.runFor(d);
    clockMs = Math.max(clockMs, t);
    const st = await page.evaluate(([d, x, y]) => window.__sc.advance(d, x, y), [Math.max(d, 0), cur.x, cur.y]);
    if (st.busy || force || lastSrc < 0) {
      const { data } = await cdp.send("Page.captureScreenshot", { format: "jpeg", quality, optimizeForSpeed: true });
      lastSrc = shots++;
      pending.push(fs.promises.writeFile(path.join(frameDir, `${String(lastSrc).padStart(6, "0")}.jpg`), Buffer.from(data, "base64")));
      if (pending.length > 64) await Promise.all(pending.splice(0));
    }
    force = false;
    frames.push([lastSrc, +cur.x.toFixed(2), +cur.y.toFixed(2), down ? 1 : 0, st.cursor]);
    frame++;
    if (frame % fps === 0) process.stdout.write(`\r  captured ${(frame / fps).toFixed(0)}s`);
  }
  const hold = async (ms) => { const end = now() + ms; while (now() < end - 1e-6) await shoot(); };

  async function animateScroll(from, to, ms) {
    if (Math.abs(to - from) < 1) return;
    const n = Math.max(1, Math.round((ms * fps) / 1000));
    for (let k = 1; k <= n; k++) {
      await page.evaluate((v) => window.__sc.setScroll(v), from + (to - from) * easeInOut(k / n));
      force = true; // scroll events arrive a frame late, so never reuse a frame here
      await shoot();
    }
  }
  const scrollMs = (dist, ms) => ms ?? clamp(350 + Math.abs(dist) * 0.55, 500, 1400);

  // Box of the target, scrolled into view smoothly if needed.
  async function boxInView(sel, step) {
    const loc = locate(sel);
    try { await loc.waitFor({ state: "visible", timeout: 3000 }); }
    catch { throw new Error(`can't find a visible element for ${describe(sel)}. If it appears after an action, add {"wait": ${JSON.stringify(sel)}} before this step.`); }
    let b = await loc.boundingBox();
    const margin = 48;
    if (b.y < margin || b.y + b.height > vh - margin) {
      const sc = await loc.evaluate((el) => window.__sc.pickScroller(el));
      const want = b.y + b.height / 2 - (sc.viewTop + sc.viewHeight * 0.45);
      const to = clamp(sc.top + want, 0, sc.max);
      await animateScroll(sc.top, to, scrollMs(to - sc.top, step.scrollMs));
      b = await loc.boundingBox();
    }
    return b;
  }

  // Point inside the box where a person would aim: near the middle, a little off-centre.
  function aim(b, step) {
    if (step.at) return { x: b.x + step.at[0] * b.width, y: b.y + step.at[1] * b.height };
    const jx = (rand() - 0.5) * Math.min(b.width * 0.3, 24), jy = (rand() - 0.5) * Math.min(b.height * 0.3, 8);
    return { x: b.x + b.width / 2 + jx, y: b.y + b.height / 2 + jy };
  }

  async function moveTo(p, ms) {
    const from = { ...cur }, dx = p.x - from.x, dy = p.y - from.y, dist = Math.hypot(dx, dy);
    if (dist < 1.5) return;
    const dur = ms ?? clamp(demo.pace.moveBase + dist * demo.pace.movePerPx, demo.pace.moveMin, demo.pace.moveMax);
    // A gentle arc: control points pushed sideways from the straight line.
    const bend = (0.06 + rand() * 0.1) * dist * (rand() < 0.5 ? -1 : 1);
    const nx = -dy / dist, ny = dx / dist;
    const c1 = { x: from.x + dx * 0.3 + nx * bend, y: from.y + dy * 0.3 + ny * bend };
    const c2 = { x: from.x + dx * 0.75 + nx * bend * 0.6, y: from.y + dy * 0.75 + ny * bend * 0.6 };
    const n = Math.max(1, Math.round((dur * fps) / 1000));
    for (let k = 1; k <= n; k++) {
      const u = easeMove(k / n), v = 1 - u;
      cur = {
        x: clamp(v * v * v * from.x + 3 * v * v * u * c1.x + 3 * v * u * u * c2.x + u * u * u * p.x, 0, vw - 1),
        y: clamp(v * v * v * from.y + 3 * v * v * u * c1.y + 3 * v * u * u * c2.y + u * u * u * p.y, 0, vh - 1),
      };
      await shoot();
    }
    cur = { x: p.x, y: p.y };
  }

  async function press() {
    await page.mouse.down();
    down = true; force = true;
    events.push({ type: "click", t: now(), x: cur.x, y: cur.y });
    await hold(demo.pace.press);
    await page.mouse.up();
    down = false; force = true;
  }

  const small = (b) => (b.width * b.height) / (vw * vh) < demo.autoZoomArea;
  // Remember actions on small targets so the compose pass can zoom in on them.
  // t0: the step began, tAct: the click or first key, t1: the action finished.
  const action = (step, t0, tAct, b) => {
    const zoomWanted = step.zoom ?? (demo.autoZoom && small(b));
    if (zoomWanted) events.push({ type: "action", t0, tAct, t1: now(), rect: [b.x, b.y, b.width, b.height] });
  };

  // ---- steps ----
  await hold(demo.head);
  for (let i = 0; i < demo.steps.length; i++) {
    const st = demo.steps[i];
    const t0 = now();
    try {
      if (st.click !== undefined) {
        const b = await boxInView(st.click, st);
        await moveTo(aim(b, st), st.moveMs);
        const tAct = now();
        await press();
        action(st, t0, tAct, b);
        await hold(st.hold ?? demo.pace.settle);
      } else if (st.type !== undefined) {
        let b = null, tAct = now();
        if (st.into !== undefined) {
          b = await boxInView(st.into, st);
          const focused = await locate(st.into).evaluate((el) => el === document.activeElement || el.contains(document.activeElement));
          if (!focused) {
            await moveTo(aim(b, st), st.moveMs);
            tAct = now();
            await press();
            await hold(180);
          }
          if (st.clear) { await page.keyboard.press(process.platform === "darwin" ? "Meta+A" : "Control+A"); await page.keyboard.press("Backspace"); force = true; }
        }
        // Schedule each key: a base delay with jitter, a little longer after spaces and punctuation.
        const base = 60000 / (demo.pace.typeWpm * 5);
        const times = [];
        let tt = now();
        for (const ch of [...String(st.type)]) {
          tt += base * (0.6 + rand() * 0.8) + (/[ ,.!?]/.test(ch) ? base * 0.5 : 0);
          times.push([tt, ch]);
        }
        let k = 0;
        while (k < times.length) {
          while (k < times.length && times[k][0] <= now()) {
            const ch = times[k][1];
            if (ch === "\n") await page.keyboard.press("Enter"); else await page.keyboard.type(ch);
            k++; force = true;
          }
          await shoot();
        }
        if (b) action(st, t0, tAct, b);
        await hold(st.hold ?? demo.pace.settle);
      } else if (st.press !== undefined) {
        await page.keyboard.press(st.press);
        force = true;
        await hold(st.hold ?? demo.pace.settle);
      } else if (st.hover !== undefined) {
        const b = await boxInView(st.hover, st);
        await moveTo(aim(b, st), st.moveMs);
        if (st.zoom) action(st, t0, now(), b);
        await hold(st.hold ?? demo.pace.hover);
      } else if (st.move !== undefined && Array.isArray(st.move)) {
        await moveTo({ x: st.move[0] * vw, y: st.move[1] * vh }, st.ms);
        if (st.hold) await hold(st.hold);
      } else if (st.scroll !== undefined) {
        if (typeof st.scroll === "number") {
          const sc = await page.evaluate(([x, y]) => window.__sc.pickScroller(document.elementFromPoint(x, y)), [cur.x, cur.y]);
          const to = clamp(sc.top + st.scroll, 0, sc.max);
          await animateScroll(sc.top, to, scrollMs(to - sc.top, st.ms));
        } else {
          const loc = locate(st.scroll);
          await loc.waitFor({ state: "attached", timeout: 3000 });
          const sc = await loc.evaluate((el) => window.__sc.pickScroller(el));
          const b = await loc.boundingBox();
          const to = clamp(sc.top + b.y - sc.viewTop - (st.offset ?? 80), 0, sc.max);
          await animateScroll(sc.top, to, scrollMs(to - sc.top, st.ms));
        }
        await hold(st.hold ?? 300);
      } else if (st.wait !== undefined) {
        if (typeof st.wait === "number") await hold(st.wait);
        else {
          const loc = locate(st.wait), limit = now() + (st.timeout ?? 10000);
          while (!(await loc.isVisible())) {
            if (now() > limit) throw new Error(`waited ${st.timeout ?? 10000} ms of video time and ${describe(st.wait)} never became visible`);
            await shoot();
          }
          await hold(st.hold ?? 0);
        }
      } else if (st.pause !== undefined) {
        await hold(st.pause);
      } else if (st.zoom !== undefined) {
        if (st.zoom === "off" || st.zoom === false || st.zoom === null) events.push({ type: "zoom", t: now(), rect: null });
        else {
          const loc = locate(st.zoom);
          await loc.waitFor({ state: "visible", timeout: 3000 });
          const b = await loc.boundingBox();
          events.push({ type: "zoom", t: now(), rect: [b.x, b.y, b.width, b.height], scale: st.scale });
        }
        if (st.hold) await hold(st.hold);
      } else if (st.caption !== undefined) {
        events.push({ type: "caption", t: now(), text: st.caption || "" });
        if (st.ms) events.push({ type: "caption", t: now() + st.ms, text: "" });
        if (st.hold) await hold(st.hold);
      } else if (st.goto !== undefined) {
        await goto(st.goto);
        force = true;
        await shoot();
      } else if (st.eval !== undefined) {
        await page.evaluate(st.eval);
        force = true;
      } else throw new Error("step not understood");
    } catch (e) {
      const shot = path.join(work, `failed-step-${i + 1}.png`);
      await page.screenshot({ path: shot }).catch(() => {});
      await browser.close();
      throw new Error(`step ${i + 1} ${JSON.stringify(st)}: ${e.message.split("\n")[0]}\n  page at that moment: ${shot}`);
    }
  }
  if (demo.tail) await hold(demo.tail);
  await Promise.all(pending);
  await browser.close();
  process.stdout.write("\r");

  const timeline = { fps, dpr, viewport: { width: vw, height: vh }, duration: now(), shots, frames, events };
  fs.writeFileSync(path.join(work, "timeline.json"), JSON.stringify(timeline));
  log(`  captured ${frames.length} frames (${(now() / 1000).toFixed(1)} s), ${shots} unique screenshots`);
  return timeline;
}
