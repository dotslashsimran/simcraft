"""Render a one-page brand board to check a kit by eye: palette, real fonts, logo, the site itself, and two sample
explainer frames in the brand's theme.

Usage: python board.py <brand_dir>      -> <brand_dir>/board.png
"""
import html, json, os, subprocess, sys, tempfile
from pathlib import Path

d = Path(sys.argv[1]).expanduser().resolve()
b = json.load(open(d / "brand.json"))
C, F, V = b["colors"], b["fonts"], b["vox"]
faces = "".join(f'@font-face{{font-family:"{f["family"]}";src:url("{(d / f["file"]).as_uri()}");font-weight:{f.get("weight") or "100 900"};font-style:{f.get("style") or "normal"}}}'
                for k in ("display", "body", "mono", "serif") for f in F.get(k, {}).get("files", []))
fam = lambda k: f'"{F[k]["family"]}", system-ui' if k in F else "system-ui"
sw = [("surface", C["surface"]), ("paper", C["paper"]), ("ink", C["ink"]), ("muted", C.get("muted")), ("accent", C["accent"])] + \
     [(f"extra {i + 1}", c) for i, c in enumerate(C.get("extras", []))]
swatches = "".join(f'<div class=sw><div class=chip style="background:{c}"></div><b>{n}</b><span>{c}</span></div>' for n, c in sw if c)
logo = f'<img class=logo src="{(d / b["logo"]).as_uri()}">' if b.get("logo") else "<i>no logo found</i>"
disp = F["display"]
page = f"""<!doctype html><meta charset=utf-8><style>{faces}
body{{margin:0;width:1800px;font:16px system-ui;background:#eee;color:#111}} .wrap{{padding:48px;display:grid;grid-template-columns:1fr 1fr;gap:32px}}
h1{{font:600 15px ui-monospace;letter-spacing:.12em;text-transform:uppercase;color:#666;margin:0 0 14px}} .card{{background:#fff;border-radius:14px;padding:28px}}
.sw{{display:inline-block;width:150px;margin:0 12px 14px 0;font:12px ui-monospace}} .chip{{height:84px;border-radius:10px;border:1px solid #0002;margin-bottom:8px}} .sw b{{display:block}}
.logo{{max-height:70px;max-width:340px}} .shot{{width:100%;border-radius:10px;border:1px solid #0002}}
.d{{font-family:{fam("display")};font-weight:{disp.get("weight", 600)};text-transform:{disp.get("transform") or "none"};letter-spacing:{disp.get("letter_spacing_em", 0)}em;font-size:64px;line-height:1;margin:6px 0 18px}}
.bd{{font-family:{fam("body")};font-size:22px;line-height:1.4}} .mn{{font-family:{fam("mono")};font-size:18px;margin-top:14px}} .sf{{font-family:{fam("serif")};font-size:30px;margin-top:14px}}
.meta{{font:13px ui-monospace;color:#555;margin-top:16px;line-height:1.7}} .frames img{{width:100%;border-radius:10px;margin-bottom:12px}}
</style><div class=wrap>
<div class=card><h1>{html.escape(b["name"])} · palette ({b["mode"]})</h1>{swatches}<div style="margin-top:18px">{logo}</div></div>
<div class=card><h1>type</h1><div class=d>The quick agent ships the PR</div>
<div class=bd>Body: {html.escape(F["body"]["family"])}. An agent reads the codebase, writes the fix and opens a pull request.</div>
<div class=mn>mono: {html.escape(F["mono"]["family"])} · const ship = await agent.run()</div>
{f'<div class=sf>serif: {html.escape(F["serif"]["family"])}</div>' if "serif" in F else ""}
<div class=meta>display {html.escape(disp["family"])} {disp.get("weight")} · case {disp.get("transform")} · tracking {disp.get("letter_spacing_em")}em<br>
buttons {b["shape"]["button_radius"]} · cards {b["shape"]["card_radius"]} · texture {b["shape"]["texture"]}<br>
vox: fields {V["field_a"]} / {V["field_b"]} · highlight {V["accent"]} · edges {V["edges"]} · grain {V["grain"]}<br>
image style: {html.escape(b["image_style"])}</div></div>
<div class=card><h1>the site</h1><img class=shot src="{(d / "page_1.png").as_uri()}"></div>
<div class="card frames"><h1>sample explainer frames</h1>FRAMES</div></div>"""

# two sample frames from the vox engine in this brand's theme
vox = Path(__file__).resolve().parents[3] / "video" / "vox-explainer"
tmp = Path(tempfile.mkdtemp())
words = [[0.0, 0.4, "Here's"], [0.4, 0.6, "the"], [0.6, 1.0, "thing"], [3.0, 3.4, "Agents"], [3.4, 3.8, "ship"], [3.8, 4.3, "code"]]
(tmp / "w.json").write_text(json.dumps(words))
subprocess.run([os.environ.get("FFMPEG", "ffmpeg"),
                "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "6", str(tmp / "a.wav")], check=True)
(tmp / "s.json").write_text(json.dumps({"audio": "a.wav", "brand": str(d), "beats": [
    {"at": "Here's", "device": "card", "bg": "a", "label": "Sample card", "head": "Here's the thing about agents", "headmark": "agents", "headmark_at": "thing"},
    {"at": "Agents", "device": "big", "bg": "b", "text": "Agents ship code", "mark": "code", "mark_at": "ship"}]}))
subprocess.run([sys.executable, str(vox / "scripts/build.py"), str(tmp / "s.json"), "--words", str(tmp / "w.json"), "--stills", "2.2,5.2"],
               check=True, capture_output=True)
frames = "".join(f'<img src="{(tmp / f"still_{t}.jpg").as_uri()}">' for t in ("2.2", "5.2"))
(tmp / "board.html").write_text(page.replace("FRAMES", frames))
js = f"""import {{ chromium }} from "playwright"; const b = await chromium.launch(); const p = await b.newPage({{ viewport: {{ width: 1800, height: 1200 }} }});
await p.goto("{(tmp / 'board.html').as_uri()}"); await p.evaluate(() => document.fonts.ready); await p.waitForTimeout(400);
await p.screenshot({{ path: "{d / 'board.png'}", fullPage: true }}); await b.close();"""
(vox.parent.parent / ".board.mjs").write_text(js)
subprocess.run(["node", str(vox.parent.parent / ".board.mjs")], check=True)
(vox.parent.parent / ".board.mjs").unlink()
print(d / "board.png")
