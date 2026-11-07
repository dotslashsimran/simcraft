#!/usr/bin/env node
// Render a scripted browser demo into a polished screen recording.
//
//   node render.mjs <demo.json|demo.mjs> [options]
//
//   --preview         fast check: 24 fps, 1x pixels, half-size output, quick encode
//   --vertical        also write a 1080x1920 version that follows the cursor
//   --compose-only    reuse the last capture; only redo zoom, frame, captions and encode
//   --stills 2,5.5    write composed stills at these seconds instead of a video
//   --out <file.mp4>  output path (default: next to the demo file)
//   --keep            keep the captured frames after a final render
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { execFileSync } from "node:child_process";
import { capture } from "./capture.mjs";
import { compose } from "./compose.mjs";

const argv = process.argv.slice(2);
const flag = (n) => argv.includes(n);
const opt = (n) => { const i = argv.indexOf(n); return i >= 0 ? argv[i + 1] : undefined; };
const file = argv.find((a, i) => !a.startsWith("--") && !["--out", "--stills"].includes(argv[i - 1]));
if (!file) { console.error("usage: node render.mjs <demo.json|demo.mjs> [--preview] [--vertical] [--compose-only] [--stills 2,5.5] [--out file.mp4] [--keep]"); process.exit(2); }

const FF = process.env.FFMPEG || "ffmpeg";
try { execFileSync(FF, ["-version"], { stdio: "ignore" }); }
catch { console.error(`ffmpeg not found (tried "${FF}"). Install it or set FFMPEG=/path/to/ffmpeg.`); process.exit(1); }

const demoPath = path.resolve(file);
const raw = demoPath.endsWith(".json") ? JSON.parse(fs.readFileSync(demoPath, "utf8")) : (await import(pathToFileURL(demoPath).href)).default;

const DEFAULTS = {
  viewport: { width: 1280, height: 720 },
  fps: 60,
  seed: 7,
  date: "2026-03-12T09:41:00",
  timezone: "UTC",
  locale: "en-US",
  colorScheme: "light",
  hideScrollbars: true,
  hideCaret: false,
  freezeAnimations: false,
  autoZoom: true,
  autoZoomArea: 0.04,     // a target smaller than 4% of the viewport counts as small
  head: 700,              // still frames before the first step
  tail: 1200,             // still frames after the last step
  loadSettle: 800,
  timeout: 15000,
  cursorStart: [0.62, 0.72],
  setup: [],
  steps: [],
  pace: { moveBase: 380, movePerPx: 0.45, moveMin: 400, moveMax: 900, press: 90, settle: 600, hover: 800, typeWpm: 110 },
  look: {
    size: [1920, 1080],
    background: "aurora",
    backgroundImage: null,
    padding: 0.065,
    radius: 14,
    frame: "mac",
    title: "",
    shadow: true,
    border: true,
    frameColor: null,
    font: '"Inter", -apple-system, "SF Pro Text", "Segoe UI", "Helvetica Neue", Arial, sans-serif',
    cursor: true,
    cursorSize: 1.25,
    clickColor: "40,40,60",
    zoom: 1.8,            // auto zoom amount
    maxZoom: 2.2,
    zoomLead: 280,        // ms the camera starts moving before the action
    zoomSpring: 7,        // higher = snappier camera
    autoZoomGap: 1800,    // actions closer than this share one zoom
    autoZoomLinger: 1100, // ms to stay zoomed after the last action
    autoZoomBridge: 1500, // zoomed-out gaps shorter than this are skipped
    verticalZoom: 1.9,
  },
};
const demo = {
  ...DEFAULTS, ...raw,
  viewport: { ...DEFAULTS.viewport, ...raw.viewport },
  pace: { ...DEFAULTS.pace, ...raw.pace },
  look: { ...DEFAULTS.look, ...raw.look },
  dir: path.dirname(demoPath),
};
if (!demo.url && !demo.steps.some((s) => s.goto) && !demo.setup.some((s) => s.goto)) { console.error("the demo needs a url (or a goto step)"); process.exit(1); }

const preview = flag("--preview");
const name = path.basename(demoPath).replace(/\.(json|mjs|js)$/, "");
const fps = preview ? 24 : demo.fps;
const dpr = preview ? 1 : 2;
const k = preview ? 0.5 : 1;
const work = path.join(demo.dir, ".screencast", `${name}-${preview ? "preview" : "final"}`);
const out = path.resolve(opt("--out") || path.join(demo.dir, `${name}${preview ? ".preview" : ""}.mp4`));
const t0 = Date.now();

let tl;
const tlPath = path.join(work, "timeline.json");
if (flag("--compose-only") || flag("--stills")) {
  if (!fs.existsSync(tlPath)) { console.error(`no capture in ${work}. Run once without --compose-only first.`); process.exit(1); }
  tl = JSON.parse(fs.readFileSync(tlPath, "utf8"));
  if (!fs.existsSync(path.join(work, "frames"))) { console.error(`the captured frames in ${work} were removed after the last final render. Render again without --compose-only, and add --keep to keep them.`); process.exit(1); }
} else {
  console.log(`capturing ${name} at ${fps} fps, ${dpr}x pixels`);
  try { tl = await capture(demo, { fps, dpr, work, quality: preview ? 80 : 92 }); }
  catch (e) { console.error(`\nfailed at ${e.message}`); process.exit(1); }
}

if (flag("--stills")) {
  const ts = opt("--stills").split(",").map(Number);
  const paths = await compose(tl, demo, { work, out, k, preview, stills: ts });
  if (flag("--vertical")) paths.push(...(await compose(tl, demo, { work, out: out.replace(/\.mp4$/, ".vertical.mp4"), k, preview, vertical: true, stills: ts })));
  console.log(paths.join("\n"));
} else {
  await compose(tl, demo, { work, out, k, preview });
  if (flag("--vertical")) await compose(tl, demo, { work, out: out.replace(/\.mp4$/, ".vertical.mp4"), k, preview, vertical: true });
  if (!preview && !flag("--keep")) {
    fs.rmSync(path.join(work, "frames"), { recursive: true, force: true });
    console.log("  removed captured frames (use --keep to keep them for --compose-only)");
  }
}
console.log(`done in ${((Date.now() - t0) / 1000).toFixed(0)} s`);
