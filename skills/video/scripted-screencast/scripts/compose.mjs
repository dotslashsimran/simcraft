// Pass 2: turn captured frames + timeline into the finished video.
// Plans the camera (zoom and pan) over the whole timeline, then draws each output frame in a
// canvas page (background, window, page frame, cursor, ripples, caption) and pipes it to ffmpeg.
import { chromium } from "playwright";
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const FF = process.env.FFMPEG || "ffmpeg";

export function layout(tl, demo, vertical) {
  const look = demo.look;
  const [W, H] = vertical ? [1080, 1920] : look.size;
  const vw = tl.viewport.width, vh = tl.viewport.height;
  const hasCaptions = tl.events.some((e) => e.type === "caption" && e.text);
  const pad = look.padding * Math.min(W, H);
  const capSpace = hasCaptions && !vertical ? H * 0.075 : 0;
  const tbRatio = look.frame === "mac" ? 0.024 : 0; // title bar height relative to window width
  // fit: (vw*s) wide, (vh*s + tb) tall, tb = tbRatio * vw * s
  const s = Math.min((W - 2 * pad) / vw, (H - 2 * pad - capSpace) / (vh + tbRatio * vw));
  const ww = vw * s, tb = Math.round(tbRatio * ww), wh = vh * s + tb;
  const wx = (W - ww) / 2, wy = (H - capSpace - wh) / 2;
  return {
    W, H, s, wx, wy, ww, wh, tb, vertical,
    radius: look.radius, shadow: look.shadow, border: look.border, title: look.title,
    frameColor: look.frameColor ?? (demo.colorScheme === "dark" ? "#24252b" : "#f2f2f5"),
    dark: demo.colorScheme === "dark",
    background: look.background, font: look.font,
    unit: look.cursorSize * s, clickRgb: look.clickColor,
    captionSize: vertical ? 50 : Math.round(H * 0.031),
    captionY: vertical ? H * 0.87 : H - capSpace / 2 - pad * 0.45,
  };
}

// Camera per output frame: { z, vx, vy } where (vx, vy) is the top-left of the visible stage area.
export function planCamera(tl, demo, L) {
  const look = demo.look, vw = tl.viewport.width, vh = tl.viewport.height, dur = tl.duration;
  const toStage = (x, y) => [L.wx + x * L.s, L.wy + L.tb + y * L.s];
  const cursorAt = (t) => {
    const f = tl.frames[clamp(Math.round((t * tl.fps) / 1000), 0, tl.frames.length - 1)];
    return toStage(f[1], f[2]);
  };
  const baseZ = L.vertical ? look.verticalZoom : 1;

  // Auto zoom: group nearby small-target actions into segments.
  const segs = [];
  if (demo.autoZoom) {
    for (const a of tl.events.filter((e) => e.type === "action").sort((p, q) => p.t0 - q.t0)) {
      const r = a.rect, last = segs[segs.length - 1];
      if (last && a.t0 - last.t1 < look.autoZoomGap) {
        const x0 = Math.min(last.rect[0], r[0]), y0 = Math.min(last.rect[1], r[1]);
        const x1 = Math.max(last.rect[0] + last.rect[2], r[0] + r[2]), y1 = Math.max(last.rect[1] + last.rect[3], r[1] + r[3]);
        if (x1 - x0 < vw / 1.4 && y1 - y0 < vh / 1.4) {
          last.rect = [x0, y0, x1 - x0, y1 - y0];
          last.t1 = a.t1;
          continue;
        }
      }
      segs.push({ t0: a.t0, tAct: a.tAct ?? a.t0, t1: a.t1, rect: [...r] });
    }
    for (const g of segs) {
      const [x, y, w, h] = g.rect, cx = x + w / 2, cy = y + h / 2;
      const nw = Math.max(w + 80, vw * 0.4), nh = Math.max(h + 80, vh * 0.4);
      g.rect = [cx - nw / 2, cy - nh / 2, nw, nh];
      g.start = Math.max(g.t0, g.tAct - 450);
      g.end = Math.min(g.t1 + look.autoZoomLinger, dur - 900);
    }
    // A short zoomed-out gap between two zooms looks like pumping: stay in and pan instead.
    for (let i = 1; i < segs.length; i++) if (segs[i].start - segs[i - 1].end < look.autoZoomBridge) segs[i - 1].end = segs[i].start;
  }
  const zooms = tl.events.filter((e) => e.type === "zoom").sort((p, q) => p.t - q.t);

  // Keep the view on the window when zoomed in; at full view it shows the whole stage.
  const clampAxis = (c, v, lo, hi, S) => {
    const m = 30;
    if (v >= S) return S / 2;
    const inner = v <= hi - lo + 2 * m ? clamp(c, lo - m + v / 2, hi + m - v / 2) : (lo + hi) / 2;
    return clamp(inner, v / 2, S - v / 2);
  };
  const clampCenter = (x, y, z) => [
    clampAxis(x, L.W / z, L.wx, L.wx + L.ww, L.W),
    clampAxis(y, L.H / z, L.wy, L.wy + L.wh, L.H),
  ];

  function target(t) {
    const te = t + look.zoomLead;
    let rect = null, zmax = look.zoom;
    const ez = zooms.filter((e) => e.t <= te).pop();
    if (ez && ez.rect) { rect = ez.rect; zmax = ez.scale ?? look.maxZoom; }
    else { const g = segs.find((s) => te >= s.start && te <= s.end); if (g) rect = g.rect; }
    let z = baseZ, fx = L.W / 2, fy = L.H / 2;
    const [cx, cy] = cursorAt(te);
    if (L.vertical) { fx = cx; fy = cy; }
    if (rect) {
      const [x0, y0] = toStage(rect[0], rect[1]), rw = rect[2] * L.s, rh = rect[3] * L.s;
      const fit = Math.min((L.W * 0.9) / (rw + 40), (L.H * 0.9) / (rh + 40));
      // the vertical view is already zoomed, so it only pushes in a little further
      z = L.vertical ? Math.max(baseZ, Math.min(baseZ * 1.3, fit)) : Math.max(baseZ, Math.min(zmax, look.maxZoom, fit));
      fx = x0 + rw / 2; fy = y0 + rh / 2;
    }
    if (z > 1.01) {
      // follow the cursor: keep it inside the middle part of the view
      const visW = L.W / z, visH = L.H / z, mx = visW * 0.2, my = visH * 0.2;
      if (cx < fx - visW / 2 + mx) fx = cx + visW / 2 - mx;
      if (cx > fx + visW / 2 - mx) fx = cx - visW / 2 + mx;
      if (cy < fy - visH / 2 + my) fy = cy + visH / 2 - my;
      if (cy > fy + visH / 2 - my) fy = cy - visH / 2 + my;
    }
    [fx, fy] = clampCenter(fx, fy, z);
    return [fx, fy, Math.log(z)];
  }

  // Critically damped spring toward the target, stepped in 1 ms slices so it is the same at any fps.
  const w = look.zoomSpring;
  let x = target(0), v = [0, 0, 0];
  const out = [];
  let t = 0;
  for (let i = 0; i < tl.frames.length; i++) {
    const ti = (i * 1000) / tl.fps;
    while (t < ti) {
      const tg = target(t), dt = 0.001;
      for (let k = 0; k < 3; k++) {
        v[k] += (-2 * w * v[k] - w * w * (x[k] - tg[k])) * dt;
        x[k] += v[k] * dt;
      }
      t += 1;
    }
    const z = Math.max(Math.exp(x[2]), 1);
    const [cx, cy] = clampCenter(x[0], x[1], z);
    out.push({ z, vx: cx - L.W / (2 * z), vy: cy - L.H / (2 * z) });
  }
  return out;
}

export async function compose(tl, demo, { work, out, vertical = false, k = 1, preview = false, stills = null, log = console.log }) {
  const L = layout(tl, demo, vertical);
  const cams = planCamera(tl, demo, L);
  const toStage = (x, y) => [L.wx + x * L.s, L.wy + L.tb + y * L.s];
  const clicks = tl.events.filter((e) => e.type === "click");
  const caps = tl.events.filter((e) => e.type === "caption").sort((a, b) => a.t - b.t);
  const press = demo.pace.press;

  function frameData(i) {
    const t = (i * 1000) / tl.fps;
    const [src, x, y, , type] = tl.frames[i];
    const [sx, sy] = toStage(x, y);
    let pr = 0;
    for (const c of clicks) {
      const d = t - c.t;
      if (d >= -10 && d < press + 160) pr = Math.max(pr, d < 50 ? clamp(d / 50, 0, 1) : d < press ? 1 : 1 - (d - press) / 160);
    }
    const ripples = clicks.filter((c) => t >= c.t && t - c.t < 520).map((c) => [...toStage(c.x, c.y), (t - c.t) / 520]);
    let cap = null;
    const ci = caps.findLastIndex((c) => c.t <= t);
    if (ci >= 0 && caps[ci].text) {
      const next = caps[ci + 1];
      const aIn = clamp((t - caps[ci].t) / 220, 0, 1), aOut = next ? clamp((next.t - t) / 220, 0, 1) : clamp((tl.duration - t) / 300, 0, 1);
      cap = { text: caps[ci].text, alpha: Math.min(aIn, aOut) };
    }
    const nextSrc = tl.frames.slice(i + 1).find((f) => f[0] !== src);
    const url = (n) => pathToFileURL(path.join(work, "frames", `${String(n).padStart(6, "0")}.jpg`)).href;
    return {
      src: url(src), next: nextSrc ? url(nextSrc[0]) : null, ...cams[i],
      cur: { x: sx, y: sy, type, press: 1 - 0.16 * pr, show: demo.look.cursor !== false },
      ripples, cap,
    };
  }

  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: Math.round(L.W * k), height: Math.round(L.H * k) }, deviceScaleFactor: 1 });
  page.on("pageerror", (e) => log("[compositor error] " + e.message));
  await page.goto(pathToFileURL(path.join(here, "compositor.html")).href);
  const bgImg = demo.look.backgroundImage ? pathToFileURL(path.resolve(demo.dir, demo.look.backgroundImage)).href : null;
  await page.evaluate((cfg) => window.setup(cfg), { ...L, k, backgroundImage: bgImg });
  const cdp = await page.context().newCDPSession(page);
  const grab = async (q) => Buffer.from((await cdp.send("Page.captureScreenshot", { format: "jpeg", quality: q, optimizeForSpeed: true })).data, "base64");

  if (stills) {
    const dir = path.join(path.dirname(out), "stills");
    fs.mkdirSync(dir, { recursive: true });
    const paths = [];
    for (const s of stills) {
      const i = clamp(Math.round(s * tl.fps), 0, tl.frames.length - 1);
      await page.evaluate((f) => window.draw(f), frameData(i));
      const p = path.join(dir, `${path.basename(out, ".mp4")}_${s}s.jpg`);
      fs.writeFileSync(p, await grab(92));
      paths.push(p);
    }
    await browser.close();
    return paths;
  }

  const n = tl.frames.length;
  const ff = spawn(FF, [
    "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", String(tl.fps), "-c:v", "mjpeg", "-i", "-",
    "-vf", "scale=in_range=pc:out_range=tv:in_color_matrix=bt601:out_color_matrix=bt709,format=yuv420p",
    "-c:v", "libx264", "-preset", preview ? "veryfast" : "slow", "-crf", preview ? "23" : "16",
    "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709", "-color_range", "tv",
    "-movflags", "+faststart", "-frames:v", String(n), out,
  ], { stdio: ["pipe", "inherit", "inherit"] });
  const done = new Promise((res, rej) => { ff.on("close", (c) => (c === 0 ? res() : rej(new Error("ffmpeg failed")))); ff.on("error", rej); });
  for (let i = 0; i < n; i++) {
    await page.evaluate((f) => window.draw(f), frameData(i));
    const buf = await grab(preview ? 85 : 95);
    if (!ff.stdin.write(buf)) await new Promise((r) => ff.stdin.once("drain", r));
    if (i % tl.fps === 0) process.stdout.write(`\r  composing ${Math.round((100 * i) / n)}%`);
  }
  ff.stdin.end();
  await done;
  await browser.close();
  process.stdout.write("\r");
  log(`  wrote ${out} (${L.W * k}x${L.H * k}, ${tl.fps} fps, ${(tl.duration / 1000).toFixed(1)} s)`);
  return out;
}
