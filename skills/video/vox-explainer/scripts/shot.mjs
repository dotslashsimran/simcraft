// node shot.mjs <url> <out.png> [width] [height] [--full]
// Screenshot a real web page to pin into the video as evidence (the photo device). Cookie banners are a common
// eyesore, so common consent buttons are dismissed by declining when possible, and fixed overlays are hidden.
import { chromium } from "playwright";
const [url, out, w = "1440", h = "900", full] = process.argv.slice(2);
const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: +w, height: +h }, deviceScaleFactor: 2 });
await p.goto(url, { waitUntil: "networkidle", timeout: 60000 }).catch(() => {});
await p.waitForTimeout(1500);
for (const label of ["Reject all", "Decline", "Reject", "Only necessary", "Necessary only", "Close"]) {
  const btn = p.getByRole("button", { name: label, exact: false }).first();
  if (await btn.isVisible().catch(() => false)) { await btn.click().catch(() => {}); await p.waitForTimeout(500); break; }
}
await p.addStyleTag({ content: "[class*=cookie i],[id*=cookie i],[class*=consent i],[id*=consent i]{display:none!important}" });
await p.screenshot({ path: out, fullPage: full === "--full" });
await b.close();
console.log(out);
