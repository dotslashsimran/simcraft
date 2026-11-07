// node snapshot.mjs <url> <audit_dir> [--links] [--copy] [--full] [--video] [--name slug]
// Reads one docs page the way a new developer sees it and saves the evidence:
//   <audit_dir>/screenshots/<slug>.png        first screen (what the reader sees on arrival)
//   <audit_dir>/screenshots/<slug>_full.png   whole page (with --full)
//   <audit_dir>/snapshots/<slug>.md           the readable text, headings kept
//   <audit_dir>/snapshots/<slug>.json         title, word count, headings, code blocks, links, dead links
//   <audit_dir>/video/<slug>.webm             a scroll through the page (with --video)
// --copy presses every code block's copy button and checks what reaches the clipboard (prompts, empty copies).
// --links checks every link on the page (HEAD, then GET if HEAD is refused) and every #anchor on the same page.
// Run it from a folder where `playwright` resolves (the simcraft repo root has it).
import { chromium } from "playwright";
import fs from "node:fs";
import path from "node:path";

const args = process.argv.slice(2);
const flag = (f) => args.includes(f);
const opt = (f) => { const i = args.indexOf(f); return i >= 0 ? args[i + 1] : undefined; };
const positional = args.filter((a, i) => !a.startsWith("--") && !(i > 0 && args[i - 1] === "--name"));
const [url, auditDir] = positional;
if (!url || !auditDir) {
  console.error("usage: node snapshot.mjs <url> <audit_dir> [--links] [--copy] [--full] [--video] [--name slug]");
  process.exit(2);
}

// Some browser calls (clipboard reads in headless mode) can wait forever, so every risky call gets a deadline.
const within = (ms, promise, fallback = null) => Promise.race([promise.catch(() => fallback), new Promise((r) => setTimeout(() => r(fallback), ms))]);
const log = (...m) => { if (flag("--verbose")) console.error("[snapshot]", ...m); };

const slugify = (u) => {
  const p = new URL(u);
  const s = (p.hostname + p.pathname).replace(/\/+$/, "").replace(/[^a-zA-Z0-9]+/g, "-").replace(/^-|-$/g, "");
  return s.slice(0, 80) || "page";
};
const slug = opt("--name") || slugify(url);
for (const d of ["screenshots", "snapshots"]) fs.mkdirSync(path.join(auditDir, d), { recursive: true });

const browser = await chromium.launch();
const ctxOpts = { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 };
if (flag("--video")) {
  fs.mkdirSync(path.join(auditDir, "video"), { recursive: true });
  ctxOpts.recordVideo = { dir: path.join(auditDir, "video"), size: { width: 1440, height: 900 } };
}
const ctx = await browser.newContext(ctxOpts);
await ctx.grantPermissions(["clipboard-read", "clipboard-write"]).catch(() => {});
const page = await ctx.newPage();
const t0 = Date.now();
let status = null, error = null;
try {
  const resp = await page.goto(url, { waitUntil: "networkidle", timeout: 45000 });
  status = resp ? resp.status() : null;
} catch (e) {
  error = String(e.message || e).split("\n")[0];
}
const loadMs = Date.now() - t0;
await page.waitForTimeout(800);

// Close cookie banners so the screenshot shows the page, not the banner. Record that one was there.
let cookieBanner = false;
for (const label of ["Reject all", "Decline", "Only necessary", "Necessary only", "Accept all", "Accept", "Got it"]) {
  const b = page.getByRole("button", { name: label, exact: false }).first();
  if (await b.isVisible().catch(() => false)) { cookieBanner = true; await b.click().catch(() => {}); await page.waitForTimeout(400); break; }
}

await page.screenshot({ path: path.join(auditDir, "screenshots", `${slug}.png`) });
if (flag("--full")) await page.screenshot({ path: path.join(auditDir, "screenshots", `${slug}_full.png`), fullPage: true }).catch(() => {});

const data = await page.evaluate(() => {
  const main = document.querySelector("main, article, [role=main]") || document.body;
  const vis = (e) => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e); return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none"; };
  const headings = [...main.querySelectorAll("h1,h2,h3")].filter(vis).map((h) => ({ level: +h.tagName[1], text: h.innerText.trim(), id: h.id || null }));
  const code = [...main.querySelectorAll("pre")].filter(vis).map((pre) => {
    const text = pre.innerText.replace(/\n+$/, "");
    // Look for a copy button next to the block, and for prompts that would be copied along with the command.
    const box = pre.closest("div") || pre.parentElement;
    const copyButton = !!(box && box.querySelector("button[class*=copy i], button[aria-label*=copy i], button[title*=copy i], clipboard-copy, .md-clipboard"));
    let heading = null, el = pre;
    while (el && !heading) { el = el.previousElementSibling || el.parentElement; if (el && /^H[1-3]$/.test(el.tagName)) heading = el.innerText.trim(); if (el === main) break; }
    const lang = ((pre.querySelector("code") || pre).className.match(/language-([\w-]+)/) || [])[1] || null;
    return { text, lang, heading, copyButton, lines: text.split("\n").length };
  });
  const links = [...document.querySelectorAll("a[href]")].map((a) => ({ href: a.href, text: (a.innerText || a.getAttribute("aria-label") || "").trim().slice(0, 80), inMain: main.contains(a) }));
  const ids = [...document.querySelectorAll("[id], a[name]")].map((e) => e.id || e.getAttribute("name"));
  // Markdown-ish text of the main content: headings, paragraphs, list items, code.
  const lines = [];
  const walk = (node) => {
    for (const c of node.children) {
      if (!vis(c)) continue;
      const t = c.tagName;
      if (/^H[1-6]$/.test(t)) lines.push("\n" + "#".repeat(+t[1]) + " " + c.innerText.trim());
      else if (t === "PRE") lines.push("\n```\n" + c.innerText.replace(/\n+$/, "") + "\n```");
      else if (t === "P" || t === "LI" || t === "BLOCKQUOTE" || t === "TD" || t === "TH") lines.push((t === "LI" ? "- " : "") + c.innerText.trim());
      else if (t === "NAV" || t === "FOOTER" || t === "SCRIPT" || t === "STYLE") continue;
      else walk(c);
    }
  };
  walk(main);
  const text = lines.join("\n").replace(/\n{3,}/g, "\n\n").trim();
  return { title: document.title, headings, code, links, ids, text, words: (main.innerText.match(/\S+/g) || []).length,
    lang: document.documentElement.lang || null, canonical: (document.querySelector("link[rel=canonical]") || {}).href || null };
});

const finalUrl = page.url();
const pageNoHash = finalUrl.split("#")[0];

// Same-page and cross-page anchors: a link to #section that has no matching id is a silent dead link.
const ids = new Set(data.ids);
const uniq = new Map();
for (const l of data.links) {
  if (!/^https?:/.test(l.href)) continue;
  if (!uniq.has(l.href)) uniq.set(l.href, l);
}
const linkList = [...uniq.values()].map((l) => ({ ...l, internal: new URL(l.href).hostname === new URL(finalUrl).hostname }));

let dead = [];
if (flag("--links")) {
  const checkOne = async (l) => {
    const [base, hash] = l.href.split("#");
    if (base === pageNoHash && hash) {
      return ids.has(decodeURIComponent(hash)) ? null : { href: l.href, text: l.text, status: "missing-anchor" };
    }
    const tryFetch = async (method) => {
      const ac = new AbortController(); const timer = setTimeout(() => ac.abort(), 15000);
      try { const r = await fetch(base, { method, redirect: "follow", signal: ac.signal, headers: { "user-agent": "Mozilla/5.0 dx-audit link check" } }); return r.status; }
      finally { clearTimeout(timer); }
    };
    try {
      let s = await tryFetch("HEAD");
      if (s === 405 || s === 403 || s === 400 || s === 501) s = await tryFetch("GET");
      if (s >= 400 && s !== 429) return { href: l.href, text: l.text, status: s };
      return null;
    } catch (e) {
      return { href: l.href, text: l.text, status: "error", error: String(e.cause?.code || e.name || e.message) };
    }
  };
  const queue = [...linkList]; const workers = [];
  for (let i = 0; i < 8; i++) workers.push((async () => { while (queue.length) { const l = queue.shift(); const r = await checkOne(l); if (r) dead.push(r); } })());
  await Promise.all(workers);
}

// A fetch can be refused by bot protection, so re-check every failure in the real browser before calling it dead.
if (dead.length) {
  const confirmed = [];
  const p2 = await ctx.newPage();
  for (const d of dead) {
    if (d.status === "missing-anchor") { confirmed.push(d); continue; }
    try {
      log("re-check", d.href);
      const r = await within(25000, p2.goto(d.href, { waitUntil: "domcontentloaded", timeout: 20000 }));
      const s = r ? r.status() : null;
      if (s === null || s >= 400) confirmed.push({ ...d, status: s ?? d.status });
    } catch (e) { confirmed.push({ ...d, error: String(e.message || e).split("\n")[0] }); }
  }
  await p2.close();
  dead = confirmed;
}

// --copy: press each code block's copy button and compare what lands on the clipboard with what the reader sees.
// Catches prompts ($, >>>) copied along with the command, and copied text that differs from the shown text.
const copyChecks = [];
if (flag("--copy")) {
  const blocks = await page.$$("main pre, article pre, [role=main] pre");
  for (const [i, pre] of blocks.entries()) {
    const btn = await pre.evaluateHandle((el) => { const box = el.closest("div") || el.parentElement;
      return box && box.querySelector("button[class*=copy i], button[aria-label*=copy i], button[title*=copy i], clipboard-copy, .md-clipboard"); });
    const el = btn.asElement();
    // Blocks inside closed tabs (other OS, other shell) are not what this reader sees, so skip them.
    if (!el || !(await pre.isVisible())) continue;
    const shown = (await pre.innerText()).replace(/\n+$/, "");
    log("copy block", i);
    await within(2000, page.evaluate(() => navigator.clipboard.writeText("")));
    await within(2000, pre.hover());
    await within(3000, el.click({ force: true, timeout: 2500 }));
    await page.waitForTimeout(150);
    const copied = await within(2000, page.evaluate(() => navigator.clipboard.readText()));
    if (copied === null) { copyChecks.push({ block: i, error: "clipboard not readable" }); continue; }
    const hasPrompt = /^\s*(\$|>>>|%|PS>|C:\\>) /m.test(copied);
    copyChecks.push({ block: i, firstLine: shown.split("\n")[0].slice(0, 80), copiedPrompt: hasPrompt, empty: copied.trim() === "",
      same: copied.trim() === shown.trim(), copied: copied.slice(0, 300) });
  }
}

if (flag("--video")) {
  // Scroll slowly through the page so the recording shows what a reader scrolls past.
  const h = await page.evaluate(() => document.body.scrollHeight);
  for (let y = 0; y < Math.min(h, 12000); y += 300) { await page.mouse.wheel(0, 300); await page.waitForTimeout(120); }
}
const video = page.video();
await ctx.close();
if (video) { const p = await video.path(); const dest = path.join(auditDir, "video", `${slug}.webm`); fs.renameSync(p, dest); }
await browser.close();

const out = {
  url, finalUrl, slug, status, error, loadMs, title: data.title, lang: data.lang, canonical: data.canonical,
  words: data.words, readMinutes: +(data.words / 238).toFixed(1), cookieBanner,
  headings: data.headings, codeBlocks: data.code, links: linkList, linksChecked: flag("--links"), deadLinks: dead, copyChecks,
  takenAt: new Date().toISOString(),
};
fs.writeFileSync(path.join(auditDir, "snapshots", `${slug}.json`), JSON.stringify(out, null, 2));
fs.writeFileSync(path.join(auditDir, "snapshots", `${slug}.md`), `<!-- ${finalUrl} -->\n# ${data.title}\n\n${data.text}\n`);

// Short summary for the terminal. The JSON has everything.
const promptHazards = data.code.filter((c) => /^\s*[$>%] /m.test(c.text) && !c.copyButton).length;
const withPrompt = data.code.filter((c) => /^\s*(\$|>>>|%) /m.test(c.text)).length;
console.log(`${slug}: HTTP ${status ?? "-"}${error ? " ERROR " + error : ""}, ${loadMs} ms, ${data.words} words (~${out.readMinutes} min read)`);
console.log(`  ${data.headings.length} headings, ${data.code.length} code blocks (${data.code.filter((c) => c.copyButton).length} with copy button${promptHazards ? `, ${promptHazards} with shell prompts and no copy button` : ""})`);
if (withPrompt) console.log(`  ${withPrompt} blocks show a shell prompt${flag("--copy") ? "" : " (run with --copy to see if the copy button strips it)"}`);
if (flag("--copy")) {
  const bad = copyChecks.filter((c) => c.copiedPrompt || c.empty || c.error);
  console.log(`  copy buttons tested: ${copyChecks.length}, problems: ${bad.length}`);
  for (const c of bad) console.log(`  COPY block ${c.block}: ${c.error || (c.empty ? "copied nothing" : "prompt copied with command")}  ${c.firstLine || ""}`);
}
console.log(`  ${linkList.length} links${flag("--links") ? `, ${dead.length} dead` : " (run with --links to check them)"}`);
for (const d of dead) console.log(`  DEAD ${d.status}${d.error ? " " + d.error : ""}  ${d.href}  "${d.text}"`);
console.log(`  -> snapshots/${slug}.json, snapshots/${slug}.md, screenshots/${slug}.png`);
