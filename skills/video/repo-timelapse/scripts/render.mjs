// Render a repo timelapse from history.json (made by extract.py).
//
//   node render.mjs history.json out.mp4 [options]
//
//   --duration 30        seconds (default 30)
//   --fps 60             frames per second (default 60)
//   --vertical           1080x1920 for Reels, TikTok and Shorts (default 1920x1080)
//   --size 2560x1440     any size; the layout scales with it
//   --theme dark|light
//   --highlight src/api  dim everything outside this folder
//   --grow area|fill     area: the map grows with the code (default); fill: always full size
//   --title "My app"  --subtitle "..."  --end-text "follow for part 2"
//   --no-title  --no-end
//   --preview            half size, 30fps, fast encode, to check pacing before the final render
//   --stills 3,12,25     write PNG frames at these seconds instead of a video
//   --list-times         print the video time of every commit
//
// It steps window.render(t) in headless Chromium frame by frame and pipes JPEG frames to ffmpeg.
import { chromium } from "playwright";
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const argv = process.argv.slice(2);
const pos = [], opt = {};
for (let i = 0; i < argv.length; i++) {
  const a = argv[i];
  if (a.startsWith("--")) {
    const key = a.slice(2);
    if (["vertical", "preview", "no-title", "no-end", "list-times"].includes(key)) opt[key] = true;
    else opt[key] = argv[++i];
  } else pos.push(a);
}
const [dataPath, out] = pos;
if (!dataPath || !out) {
  console.error("usage: node render.mjs history.json out.mp4 [--duration 30] [--fps 60] [--vertical] [--theme light] [--highlight dir] [--preview] [--stills 3,12]");
  process.exit(1);
}
const FF = process.env.FFMPEG || "ffmpeg";
const preview = !!opt.preview;
let [width, height] = opt.size ? opt.size.split("x").map(Number) : opt.vertical ? [1080, 1920] : [1920, 1080];
if (preview) { width = Math.round(width / 2 / 2) * 2; height = Math.round(height / 2 / 2) * 2; }
const fps = Number(opt.fps) || (preview ? 30 : 60);
const duration = Number(opt.duration) || 30;

const data = fs.readFileSync(dataPath, "utf8");
const OPTS = {
  width, height, duration, theme: opt.theme || "dark", highlight: opt.highlight || null, grow: opt.grow || "area",
  title: opt.title || null, subtitle: opt.subtitle || null, endText: opt["end-text"] || null,
  titleCard: !opt["no-title"], endCard: !opt["no-end"],
};

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 1 });
page.setDefaultTimeout(300000);
let failed = false;
page.on("pageerror", (e) => { console.error("[page error]", e.message); failed = true; });
await page.addInitScript(`window.DATA = ${data}; window.OPTS = ${JSON.stringify(OPTS)};`);
await page.goto("file://" + path.join(here, "../engine/timelapse.html"));
await page.evaluate(async () => { await window.READY; });
if (failed) { await browser.close(); process.exit(1); }
const info = await page.evaluate(() => window.INFO);
if (!info.highlightFound) console.warn(`warning: no files under --highlight ${OPTS.highlight}`);
if (opt["list-times"]) console.log("event times:", info.times.join(" "));
console.log(`${info.events} events, ${info.captions} captions, ${width}x${height} @ ${fps}fps, ${duration}s`);

const decode = (url) => Buffer.from(url.slice(url.indexOf(",") + 1), "base64");

if (opt.stills) {
  fs.mkdirSync(out, { recursive: true });
  for (const t of opt.stills.split(",").map(Number)) {
    await page.evaluate((v) => window.render(v), t);
    const p = path.join(out, `still_${String(t).replace(".", "_")}s.png`);
    await page.locator("canvas").screenshot({ path: p });
    console.log(p);
  }
} else {
  const n = Math.round(duration * fps);
  const ff = spawn(FF, ["-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", String(fps), "-c:v", "mjpeg", "-i", "-",
    "-c:v", "libx264", "-preset", preview ? "veryfast" : "slow", "-crf", preview ? "23" : "16", "-vf", "scale=in_range=pc:out_range=tv,format=yuv420p", "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
    "-movflags", "+faststart", "-frames:v", String(n), out], { stdio: ["pipe", "inherit", "inherit"] });
  const done = new Promise((r) => ff.on("close", r));
  const BATCH = 6, started = Date.now();
  for (let i = 0; i < n; i += BATCH) {
    const times = [];
    for (let j = i; j < Math.min(n, i + BATCH); j++) times.push(j / fps);
    const urls = await page.evaluate((ts) => ts.map((t) => window.frame(t)), times);
    for (const u of urls) { const buf = decode(u); if (!ff.stdin.write(buf)) await new Promise((r) => ff.stdin.once("drain", r)); }
    if (i % (fps * 2) < BATCH) process.stdout.write(`\r${Math.round((100 * i) / n)}%  `);
  }
  ff.stdin.end();
  const code = await done;  // wait for ffmpeg to exit, a half-written mp4 has no index
  if (code !== 0) { console.error("ffmpeg failed"); process.exit(1); }
  console.log(`\r${n} frames in ${Math.round((Date.now() - started) / 1000)}s -> ${out}`);
}
await browser.close();
if (failed) process.exit(1);
