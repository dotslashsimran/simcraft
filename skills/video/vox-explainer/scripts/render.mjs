// node render.mjs <spec.json> <out.mp4> [--stills=1.5,4,9.2] [--scale 0.5]
// Loads engine/vox.html with the spec, steps window.render(ms) frame by frame, pipes frames to ffmpeg.
// --stills writes JPEGs at those seconds instead (for checking a layout without a full render).
// --scale 0.5 renders half size (fast preview); the layout scales with it.
import { chromium } from "playwright";
import { spawn, execSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const [specPath, out, ...rest] = process.argv.slice(2);
const stills = rest.find((a) => a.startsWith("--stills="));
const scale = Number(rest[rest.indexOf("--scale") + 1]) || 1;
const spec = JSON.parse(fs.readFileSync(specPath, "utf8"));
const FF = process.env.FFMPEG || (() => { try { return execSync("command -v ffmpeg").toString().trim(); } catch { return "ffmpeg"; } })();
const fps = spec.fps || 30;

// images in the spec are resolved relative to the spec file
const base = path.dirname(path.resolve(specPath));
for (const b of spec.beats) if (b.img && !b.img.startsWith("file:") && !b.img.startsWith("http")) b.img = "file://" + path.resolve(base, b.img);

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: spec.w, height: spec.h }, deviceScaleFactor: scale });
page.setDefaultTimeout(180000);
page.on("pageerror", (e) => { console.error("[page error]", e.message); process.exitCode = 1; });
await page.addInitScript((s) => { window.SPEC = s; }, spec);
await page.goto("file://" + path.join(here, "../engine/vox.html"));
await page.evaluate(async () => { await window.READY; });

if (stills) {
  const dir = path.dirname(out);
  for (const t of stills.slice(9).split(",").map(Number)) {
    await page.evaluate((ms) => window.render(ms), t * 1000);
    const p = path.join(dir, `still_${t}.jpg`);
    await page.screenshot({ path: p, type: "jpeg", quality: 85 });
    console.log(p);
  }
} else {
  const n = Math.round((spec.dur / 1000) * fps);
  const ff = spawn(FF, ["-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", String(fps), "-c:v", "mjpeg", "-i", "-",
    "-c:v", "libx264", "-preset", scale < 1 ? "veryfast" : "medium", "-crf", scale < 1 ? "20" : "17", "-pix_fmt", "yuv420p",
    "-color_range", "tv", "-frames:v", String(n), out], { stdio: ["pipe", "inherit", "inherit"] });
  const done = new Promise((r) => ff.on("close", r));
  for (let i = 0; i < n; i++) {
    await page.evaluate((ms) => window.render(ms), (i * 1000) / fps);
    const buf = await page.screenshot({ type: "jpeg", quality: 92 });
    if (!ff.stdin.write(buf)) await new Promise((r) => ff.stdin.once("drain", r));
    if (i % fps === 0) process.stdout.write(`\r${Math.round((100 * i) / n)}%`);
  }
  ff.stdin.end();
  await done;  // wait for ffmpeg to exit, not for the file to exist (a half-written mp4 has no moov atom)
  console.log(`\rframes ${n} -> ${out}`);
}
await browser.close();
