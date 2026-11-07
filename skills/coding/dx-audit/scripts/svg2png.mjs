// node svg2png.mjs <in.svg> <out.png> [scale]
// Renders an SVG file to PNG with the Playwright Chromium, so the chart works without any Python plotting library.
import { chromium } from "playwright";
import fs from "node:fs";

const [inFile, outFile, scale = "2"] = process.argv.slice(2);
if (!inFile || !outFile) { console.error("usage: node svg2png.mjs <in.svg> <out.png> [scale]"); process.exit(2); }
const svg = fs.readFileSync(inFile, "utf8");
const browser = await chromium.launch();
const page = await browser.newPage({ deviceScaleFactor: Number(scale) });
await page.setContent(`<html><body style="margin:0;background:#fff">${svg}</body></html>`);
await page.locator("svg").first().screenshot({ path: outFile });
await browser.close();
console.log(outFile);
