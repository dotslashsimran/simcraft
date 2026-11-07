// node extract.mjs <url> <out_dir> [more urls...]
// Reads a brand's real design from its live website: CSS variables, computed colours and fonts of the key elements,
// button shapes, font files, logo, favicon, theme colour, and the colours that actually cover the screen.
// Writes <out_dir>/raw.json plus screenshots, fonts and logo files. propose.py turns raw.json into brand.json.
import { chromium } from "playwright";
import fs from "node:fs";
import path from "node:path";

const [url, outDir, ...more] = process.argv.slice(2);
fs.mkdirSync(path.join(outDir, "fonts"), { recursive: true });
const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1,
  userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0 Safari/537.36" });
const fontUrls = new Set();
const pages = [];

for (const [i, u] of [url, ...more].entries()) {
  const p = await ctx.newPage();
  p.on("response", (r) => { const t = r.headers()["content-type"] || ""; if (/font|woff/.test(t) || /\.(woff2?|otf|ttf)(\?|$)/.test(r.url())) fontUrls.add(r.url()); });
  await p.goto(u, { waitUntil: "networkidle", timeout: 60000 }).catch(() => {});
  await p.waitForTimeout(2000);
  for (const label of ["Reject all", "Decline", "Reject", "Only necessary", "Necessary only"]) {
    const b = p.getByRole("button", { name: label, exact: false }).first();
    if (await b.isVisible().catch(() => false)) { await b.click().catch(() => {}); await p.waitForTimeout(500); break; }
  }
  await p.screenshot({ path: path.join(outDir, `page_${i + 1}.png`) });
  await p.screenshot({ path: path.join(outDir, `page_${i + 1}_full.png`), fullPage: true }).catch(() => {});
  const data = await p.evaluate(() => {
    const cs = (e) => getComputedStyle(e);
    const vis = (e) => { const r = e.getBoundingClientRect(); const s = cs(e); return r.width > 4 && r.height > 4 && s.visibility !== "hidden" && s.display !== "none" && +s.opacity > 0.1; };
    const pick = (sel, n = 6) => [...document.querySelectorAll(sel)].filter(vis).slice(0, n);
    const style = (e) => { const s = cs(e); const r = e.getBoundingClientRect();
      return { tag: e.tagName.toLowerCase(), text: (e.innerText || "").trim().slice(0, 40), color: s.color, bg: s.backgroundColor, bgImage: s.backgroundImage.slice(0, 160),
        font: s.fontFamily, weight: s.fontWeight, size: s.fontSize, lh: s.lineHeight, ls: s.letterSpacing, transform: s.textTransform,
        radius: s.borderRadius, border: s.border, shadow: s.boxShadow.slice(0, 120), w: Math.round(r.width), h: Math.round(r.height) }; };
    // CSS custom properties on :root / body: often the design system tokens themselves
    const vars = {};
    for (const sheet of [...document.styleSheets]) { let rules; try { rules = sheet.cssRules; } catch { continue; }
      for (const r of rules || []) if (r.selectorText && /^(:root|html|body|\[data-theme)/.test(r.selectorText))
        for (const k of r.style) if (k.startsWith("--")) { const v = r.style.getPropertyValue(k).trim(); if (/#|rgb|hsl|oklch|font|px|rem/.test(v) && Object.keys(vars).length < 400) vars[k] = v; } }
    // @font-face rules give family -> file url, so the real font can be loaded by name later
    // resolve every token to its final value (tokens often point at other tokens: rgb(var(--text-accent)))
    const root = getComputedStyle(document.documentElement), resolved = {};
    for (const k of Object.keys(vars)) { const probe = document.createElement("div"); probe.style.color = `var(${k})`; document.body.appendChild(probe);
      const c = getComputedStyle(probe).color; probe.remove(); const raw = root.getPropertyValue(k).trim();
      resolved[k] = /^(rgb|#|hsl|oklch|color)/.test(raw) || /rgb\(var/.test(vars[k]) ? c : raw; }
    const fontFaces = [];
    for (const sheet of [...document.styleSheets]) { let rules; try { rules = sheet.cssRules; } catch { continue; }
      for (const r of rules || []) if (r.constructor.name === "CSSFontFaceRule") {
        const src = r.style.getPropertyValue("src"); const m = src.match(/url\(["']?([^"')]+)["']?\)/);
        if (m) fontFaces.push({ family: r.style.getPropertyValue("font-family").replace(/["']/g, "").trim(), weight: r.style.getPropertyValue("font-weight") || "400",
          style: r.style.getPropertyValue("font-style") || "normal", url: new URL(m[1], sheet.href || location.href).href }); } }
    const logo = [...document.querySelectorAll("header a[href='/'] svg, header a[href='/'] img, a[aria-label*=home i] svg, a[aria-label*=home i] img, header svg, header img, [class*=logo i] svg, [class*=logo i] img")]
      .filter(vis).slice(0, 3).map((e) => e.tagName === "IMG" ? { img: e.currentSrc || e.src } : { svg: e.outerHTML.slice(0, 20000), color: cs(e).color });
    const links = [...document.querySelectorAll("link[rel*=icon], link[rel=apple-touch-icon]")].map((l) => l.href);
    return {
      url: location.href, title: document.title,
      theme_color: document.querySelector("meta[name=theme-color]")?.content || null,
      og_image: document.querySelector("meta[property='og:image']")?.content || null,
      body: style(document.body), html_bg: cs(document.documentElement).backgroundColor,
      h1: pick("h1", 3).map(style), h2: pick("h2", 4).map(style), p: pick("main p, p", 6).map(style),
      buttons: pick("a[class*=button i], button, a[class*=btn i], [role=button]", 10).map(style),
      nav: pick("nav a, header a", 6).map(style), code: pick("code, pre", 3).map(style),
      cards: pick("[class*=card i]", 4).map(style),
      vars: resolved, fontFaces, logo, icons: links,
    };
  });
  pages.push(data);
  await p.close();
}

// colours that actually cover the first screen, from the screenshot, counted by area
const shot = await ctx.newPage();
await shot.setContent(`<canvas id=c></canvas>`);
const png = fs.readFileSync(path.join(outDir, "page_1.png")).toString("base64");
const area = await shot.evaluate(async (b64) => {
  const img = new Image(); img.src = "data:image/png;base64," + b64; await img.decode();
  const c = document.getElementById("c"); c.width = 360; c.height = Math.round(360 * img.height / img.width);
  const g = c.getContext("2d"); g.drawImage(img, 0, 0, c.width, c.height);
  const d = g.getImageData(0, 0, c.width, c.height).data, count = {};
  for (let i = 0; i < d.length; i += 4) { const k = [d[i], d[i + 1], d[i + 2]].map((v) => Math.round(v / 12) * 12).join(","); count[k] = (count[k] || 0) + 1; }
  const n = d.length / 4;
  return Object.entries(count).sort((a, b) => b[1] - a[1]).slice(0, 14).map(([k, v]) => ({ rgb: k.split(",").map(Number), share: +(v / n).toFixed(3) }));
}, png);

// download the font files the site loads (for local rendering only; never commit or redistribute them)
const fonts = [];
for (const ff of pages.flatMap((p) => p.fontFaces)) fontUrls.add(ff.url);
for (const u of [...fontUrls].slice(0, 40)) {
  try { const r = await ctx.request.get(u); if (!r.ok()) continue;
    const name = decodeURIComponent(u.split("/").pop().split("?")[0]).replace(/[^\w.-]/g, "_").slice(-80);
    fs.writeFileSync(path.join(outDir, "fonts", name), await r.body()); fonts.push({ url: u, file: "fonts/" + name });
  } catch {}
}
// logo: inline SVG saved as a file, or the image downloaded
const lg = pages[0].logo[0];
if (lg?.svg) fs.writeFileSync(path.join(outDir, "logo.svg"), lg.svg.includes("xmlns") ? lg.svg : lg.svg.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"'));
else if (lg?.img) { try { const r = await ctx.request.get(lg.img); fs.writeFileSync(path.join(outDir, "logo" + (lg.img.includes(".svg") ? ".svg" : ".png")), await r.body()); } catch {} }
const icon = pages[0].icons.find((i) => /apple-touch|180|192|512/.test(i)) || pages[0].icons[0];
if (icon) { try { const r = await ctx.request.get(icon); fs.writeFileSync(path.join(outDir, "icon" + path.extname(icon.split("?")[0]) || ".png"), await r.body()); } catch {} }

fs.writeFileSync(path.join(outDir, "raw.json"), JSON.stringify({ pages, area, fonts }, null, 1));
await browser.close();
console.log(`read ${pages.length} page(s), ${fonts.length} font files, logo: ${lg ? (lg.svg ? "svg" : "img") : "none"} -> ${outDir}/raw.json`);
