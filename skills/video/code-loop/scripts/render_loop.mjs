// Render a looping page to a frame-perfect video.
//
//   node render_loop.mjs <page.html> <out.mp4|out.gif> [options]
//
//   --preview          half size, 30 fps, one sample per frame unless moving fast, JPEG frames (minutes, not an hour)
//   --stills=0,2.5,5   only render full-quality PNG stills at these seconds, into stills/
//   --size=1920x1080   output size: 1920x1080, 1080x1920, 1080x1080, 1080x1350, ...
//   --scale=1          render scale for heavy shaders (0.75 renders smaller, then resizes to --size)
//   --fps=60           frames per second (preview: 30)
//   --from=0 --to=LOOP seconds to render (default: the page's whole loop, window.LOOP)
//   --mink=8           samples per frame when nothing moves: anti-aliasing (preview: 1)
//   --maxk=32          samples per frame on the fastest moves: motion blur (preview: 8)
//   --step=2           target spacing in pixels between motion blur samples
//   --shutter=1        fraction of the frame time the shutter is open (1 = 360 degrees, 0.5 = 180)
//   --loops=1          repeat the loop N times in the mp4
//   --gif              also write a palette-optimised GIF next to the mp4
//   --gif-width=600 --gif-fps=25
//   --crf=14           x264 quality (lower is better; preview: 20)
//   --keep             keep the frames folder
//   --resume           continue an interrupted render: keep finished frames, render the rest
//
// The page must follow the code-loop contract: window.LOOP, READY, render(ms), renderMany([[ms, jx, jy], ...]),
// setJitter(x, y), and optionally probe(ms). It is served from its own folder, so ./node_modules works.
// Set $FFMPEG if ffmpeg is not on the PATH.
import { chromium } from "playwright";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

const [pageArg, outArg, ...rest] = process.argv.slice(2);
if (!pageArg || !outArg) { console.error("usage: node render_loop.mjs <page.html> <out.mp4|out.gif> [--preview] [--size=1920x1080] ..."); process.exit(2); }
const flag = (k) => rest.includes(`--${k}`);
const opt = (k, d) => { const a = rest.find((x) => x.startsWith(`--${k}=`)); return a ? a.slice(k.length + 3) : d; };
const num = (k, d) => { const v = opt(k); return v === undefined ? d : +v; };

const PREVIEW = flag("preview"), FF = process.env.FFMPEG || "ffmpeg";
const [OW, OH] = opt("size", "1920x1080").split("x").map(Number);
const SCALE = PREVIEW ? 0.5 : num("scale", 1);
const FPS = num("fps", PREVIEW ? 30 : 60), MINK = num("mink", PREVIEW ? 1 : 8), MAXK = num("maxk", PREVIEW ? 8 : 32);
const STEP = num("step", PREVIEW ? 6 : 2), SHUT = num("shutter", 1), LOOPS = num("loops", 1), CRF = num("crf", PREVIEW ? 20 : 14);
const STILLS = opt("stills"), OUT = path.resolve(outArg), IS_GIF = OUT.endsWith(".gif");
const BASE = OUT.replace(/\.(mp4|gif|mov)$/i, ""), FRAMES = BASE + ".frames";
const PW = Math.round(OW * SCALE), PH = Math.round(OH * SCALE);
const even = (v) => v - (v % 2);

// ---------- serve the page's folder ----------
const dir = path.dirname(path.resolve(pageArg));
const TYPES = { ".html": "text/html", ".js": "text/javascript", ".mjs": "text/javascript", ".json": "application/json", ".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp", ".glsl": "text/plain", ".hdr": "application/octet-stream", ".glb": "model/gltf-binary", ".woff2": "font/woff2", ".ttf": "font/ttf" };
const server = http.createServer((q, r) => {
  const f = path.join(dir, decodeURIComponent(q.url.split("?")[0]));
  if (!f.startsWith(dir)) { r.writeHead(403); return r.end(); }
  fs.readFile(f, (e, b) => { if (e) { r.writeHead(404); r.end(); } else { r.writeHead(200, { "content-type": TYPES[path.extname(f)] || "application/octet-stream" }); r.end(b); } });
}).listen(0);

const browser = await chromium.launch({ args: ["--use-angle=metal", "--enable-gpu", "--ignore-gpu-blocklist"] });
const page = await browser.newPage({ viewport: { width: PW, height: PH }, deviceScaleFactor: 1 });
page.on("pageerror", (e) => { console.error("[page error]", e.message); process.exitCode = 1; });
page.on("console", (m) => { if (m.type() === "error") console.error("[page console]", m.text()); });
await page.goto(`http://localhost:${server.address().port}/${path.basename(pageArg)}?w=${OW}&h=${OH}&scale=${SCALE}`);
await page.waitForFunction(() => window.READY && window.renderMany && window.setJitter, null, { timeout: 120000 });
await page.evaluate(() => window.READY);
const LOOP = await page.evaluate(() => window.LOOP);
const HAS_PROBE = await page.evaluate(() => typeof window.probe === "function");
if (!LOOP) throw new Error("the page must set window.LOOP (loop length in ms)");
const FROM = num("from", 0), TO = num("to", LOOP / 1000);
if (Math.abs(LOOP / 1000 * FPS - Math.round(LOOP / 1000 * FPS)) > 1e-6) console.warn(`warning: LOOP ${LOOP} ms is not a whole number of frames at ${FPS} fps, so the seam will slip`);

// ---------- sample pattern ----------
// Sub-pixel offsets come from the Halton (2, 3) sequence, reshaped into a tent filter one pixel wide on each side:
// smoother edges than a box, still sharp. The same offsets are used on every frame, so leftover aliasing holds still
// instead of shimmering. Time samples are stratified across the open shutter and paired with a fixed shuffle of the
// offsets, so the streak of a moving edge is not skewed to one side.
const halton = (i, b) => { let f = 1, r = 0; for (; i > 0; i = Math.floor(i / b)) { f /= b; r += f * (i % b); } return r; };
const tent = (u) => (u < 0.5 ? Math.sqrt(2 * u) - 1 : 1 - Math.sqrt(2 - 2 * u));
const shuffle = (k) => { const a = [...Array(k).keys()]; let s = 99991; for (let i = k - 1; i > 0; i--) { s = (s * 16807) % 2147483647; const j = s % (i + 1); [a[i], a[j]] = [a[j], a[i]]; } return a; };
function samples(mid, dt, k) {
  if (k === 1) return [[mid, 0, 0]];
  const a = mid - (dt * SHUT) / 2, span = dt * SHUT, perm = shuffle(k), moving = span > 0;
  return perm.map((pj, j) => [moving ? a + ((j + 0.5) / k) * span : mid, tent(halton(pj + 1, 2)), tent(halton(pj + 1, 3))]);
}

// how far (output pixels) the fastest probe point moves while the shutter is open
async function motion(a, c) {
  if (!HAS_PROBE) return 0;
  const [P, R] = await page.evaluate(([a, c]) => [window.probe(a), window.probe(c)], [a, c]);
  let m = 0;
  P.forEach((u, i) => { const v = R[i];
    const onScreen = (w) => Math.abs(w[2]) <= 1 && Math.abs(w[0]) < OW && Math.abs(w[1]) < OH;
    if (onScreen(u) || onScreen(v)) m = Math.max(m, Math.hypot(u[0] - v[0], u[1] - v[1])); });
  return m;
}
async function sampleCount(mid, dt) {
  if (SHUT === 0) return MINK;
  const px = await motion(mid - (dt * SHUT) / 2, mid + (dt * SHUT) / 2);
  let k = MINK; while (k < MAXK && px / k > STEP) k *= 2;
  return Math.min(Math.max(k, 1), MAXK);
}
async function shot(ms, dt, type) {
  const k = await sampleCount(ms, dt);
  await page.evaluate((l) => window.renderMany(l), samples(ms, dt, k));
  // a long GPU stall (or the machine sleeping) can time out one screenshot: redraw and retry instead of losing the render
  for (let tries = 1; ; tries++) {
    try { return [await page.screenshot({ type, timeout: 120000, ...(type === "jpeg" ? { quality: 94 } : {}), clip: { x: 0, y: 0, width: PW, height: PH } }), k]; }
    catch (e) { if (tries >= 3) throw e; console.warn(`screenshot at ${ms.toFixed(0)} ms timed out, retrying`); await page.evaluate((l) => window.renderMany(l), samples(ms, dt, k)); }
  }
}

const run = (args) => { const r = spawnSync(FF, ["-y", "-loglevel", "error", ...args], { stdio: "inherit" }); if (r.status !== 0) throw new Error("ffmpeg failed: " + args.join(" ")); };
const T0 = Date.now(), dt = 1000 / FPS;

// ---------- stills only ----------
if (STILLS) {
  fs.mkdirSync("stills", { recursive: true });
  for (const s of STILLS.split(",").map(Number)) {
    const [buf, k] = await shot(s * 1000, dt, "png");
    const f = path.join("stills", `${path.basename(BASE)}_${s.toFixed(2)}s.png`); fs.writeFileSync(f, buf); console.log(f, `(${k} samples)`);
  }
  await browser.close(); server.close(); process.exit();
}

// ---------- frames ----------
// --resume keeps frames already on disk from an interrupted run (same options!) and renders only the missing ones
if (!flag("resume")) fs.rmSync(FRAMES, { recursive: true, force: true });
fs.mkdirSync(FRAMES, { recursive: true });
const n = Math.round((TO - FROM) * FPS), ext = PREVIEW ? "jpg" : "png";
let total = 0;
for (let i = 0; i < n; i++) {
  if (flag("resume") && fs.existsSync(path.join(FRAMES, `${String(i).padStart(5, "0")}.${ext}`))) continue;
  const [buf, k] = await shot(FROM * 1000 + i * dt, dt, PREVIEW ? "jpeg" : "png"); total += k;
  fs.writeFileSync(path.join(FRAMES, `${String(i).padStart(5, "0")}.${ext}`), buf);
  if (i % FPS === 0) console.log(`frame ${i}/${n}  ${k} samples  ${((Date.now() - T0) / 1000).toFixed(0)}s`);
}

// ---------- seam frames: the frame at t = 0 and the frame one step after the last (t = LOOP). They must match. ----------
if (FROM === 0 && Math.abs(TO * 1000 - LOOP) < 1e-6) {
  const [first] = await shot(0, dt, "png"), [next] = await shot(LOOP, dt, "png");
  fs.writeFileSync(BASE + ".seam-first.png", first); fs.writeFileSync(BASE + ".seam-next.png", next);
  console.log(first.equals(next) ? "seam: frame at LOOP is byte-identical to frame 0" : "seam: frame at LOOP differs from frame 0, run check_loop.py to see by how much");
}
await browser.close(); server.close();

// ---------- encode ----------
const input = ["-framerate", String(FPS), "-i", path.join(FRAMES, `%05d.${ext}`)];
const W = even(PREVIEW ? PW : OW), H = even(PREVIEW ? PH : OH);   // a --scale render is resized to the full output size
if (!IS_GIF) {
  // RGB to YUV with the BT.709 matrix and tag it as such. ffmpeg defaults to BT.601, which players then decode
  // as 709, shifting reds and greens.
  run([...(LOOPS > 1 ? ["-stream_loop", String(LOOPS - 1)] : []), ...input,
    "-vf", `scale=${W}:${H}:flags=lanczos:out_color_matrix=bt709:out_range=tv,format=yuv420p`,
    "-c:v", "libx264", "-preset", PREVIEW ? "veryfast" : "slow", "-crf", String(CRF), "-profile:v", "high",
    "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
    "-r", String(FPS), "-movflags", "+faststart", OUT]);
  console.log("wrote", OUT);
}
if (IS_GIF || flag("gif")) {
  const gw = num("gif-width", 600), gfps = num("gif-fps", 25), gif = IS_GIF ? OUT : BASE + ".gif";
  if (Math.abs((LOOP / 1000) * gfps - Math.round((LOOP / 1000) * gfps)) > 1e-6) console.warn(`warning: the loop is not a whole number of frames at ${gfps} fps, so the GIF seam will slip`);
  // Ordered (bayer) dither stays put from frame to frame; error diffusion dither crawls and bloats the file.
  // diff_mode=rectangle only re-encodes the part of each frame that changed.
  run([...input, "-vf", `fps=${gfps},scale=${gw}:-2:flags=lanczos,split[a][b];[a]palettegen=max_colors=256:stats_mode=full[p];[b][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle`, "-loop", "0", gif]);
  console.log("wrote", gif, `${(fs.statSync(gif).size / 1e6).toFixed(1)} MB`);
}
if (!flag("keep")) fs.rmSync(FRAMES, { recursive: true, force: true });
console.log(`done: ${n} frames, ${total} renders this run (${(total / n).toFixed(1)} per frame), ${((Date.now() - T0) / 60000).toFixed(1)} min`);
