"""Turn raw.json (from extract.mjs) into brand.json: design roles, fonts, shapes, and the vox/image-gen theme.

Usage: python propose.py <brand_dir> [--name "Devin"]
Writes <brand_dir>/brand.json. Everything in it is a proposal from measurements; Claude reviews the board
(scripts/board.py) against the screenshots and fixes anything that's off before the kit is used.
"""
import colorsys, json, re, sys
from pathlib import Path

d = Path(sys.argv[1]).expanduser()
raw = json.load(open(d / "raw.json"))
P = raw["pages"][0]
name = sys.argv[sys.argv.index("--name") + 1] if "--name" in sys.argv else re.split(r"[:|\-–]", P["title"])[0].strip()


def rgb(s):
    if not s: return None
    m = re.match(r"rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?", s)
    if m:
        if m.group(4) is not None and float(m.group(4)) < 0.6: return None  # see-through: not a real fill
        return tuple(int(float(m.group(i))) for i in (1, 2, 3))
    m = re.match(r"#([0-9a-f]{6})", s.strip(), re.I)
    return tuple(int(m.group(1)[i:i + 2], 16) for i in (0, 2, 4)) if m else None


hexc = lambda c: "#%02X%02X%02X" % c
lum = lambda c: (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255
sat = lambda c: colorsys.rgb_to_hsv(*(x / 255 for x in c))[1] * (max(c) / 255)
dist = lambda a, b: sum((x - y) ** 2 for x, y in zip(a, b)) ** .5
first_family = lambda f: re.split(r",", f)[0].strip().strip('"\'')

# ---- colours ----
area = [tuple(a["rgb"]) for a in raw["area"]]
surface = rgb(P["body"]["bg"]) or rgb(P["html_bg"]) or area[0]
h1 = (P["h1"] or P["h2"] or [P["body"]])[0]
ink = rgb(h1["color"]) or rgb(P["body"]["color"]) or (20, 20, 20)
def blended(s, over):
    """A see-through text colour (rgba(25,25,25,.56)) as the solid colour it shows on the page."""
    m = re.match(r"rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?", s or "")
    if not m: return None
    a = float(m.group(4)) if m.group(4) else 1.0
    return tuple(int(float(m.group(i + 1)) * a + over[i] * (1 - a)) for i in range(3))


muted = next((blended(e["color"], surface) for e in P["p"] if blended(e["color"], surface) and dist(blended(e["color"], surface), ink) > 30), None)
named = {k: rgb(v) for k, v in P["vars"].items() if rgb(v)}
cand = [c for k, c in named.items() if re.search(r"accent|primary|brand", k) and sat(c) > .35]
cand += [rgb(b["bg"]) for b in P["buttons"] if rgb(b["bg"]) and sat(rgb(b["bg"])) > .35]
cand += [rgb(P["theme_color"])] if rgb(P["theme_color"]) and sat(rgb(P["theme_color"])) > .3 else []
cand += sorted((c for c in area if sat(c) > .35), key=sat, reverse=True)
accent = cand[0] if cand else ink
extras = []
for c in sorted(named.values(), key=sat, reverse=True):
    if sat(c) > .35 and all(dist(c, x) > 60 for x in [accent] + extras): extras.append(c)
extras = extras[:4]
dark = lum(surface) < .35
paper = next((rgb(c["bg"]) for c in P["cards"] if rgb(c["bg"]) and dist(rgb(c["bg"]), surface) > 6), None) or \
    ((255, 255, 255) if not dark else tuple(min(255, x + 18) for x in surface))

# ---- type ----
faces = {}
local = {f["url"]: f["file"] for f in raw["fonts"]}
for f in sum((p["fontFaces"] for p in raw["pages"]), []):
    if f["url"] in local: faces.setdefault(f["family"].lower(), []).append({"family": f["family"], "weight": f["weight"], "style": f["style"], "file": local[f["url"]]})


def font(el, fallback):
    if not el: return {"family": fallback}
    fam = first_family(el["font"])
    size = float(re.sub(r"px", "", el["size"]) or 16)
    ls = el["ls"]; ls_em = 0 if ls in ("normal", "") else round(float(ls.replace("px", "")) / size, 3)
    return {"family": fam, "weight": el["weight"], "transform": el["transform"], "letter_spacing_em": ls_em,
            "files": faces.get(fam.lower(), [])}


def best(kind):
    """The first element of this kind, on any page read, whose font the site actually ships (so it can be used);
    otherwise the first one at all. System fonts (-apple-system, ui-sans-serif) map to system-ui."""
    els = [e for pg in raw["pages"] for e in pg.get(kind, [])]
    own = [e for e in els if faces.get(first_family(e["font"]).lower())]
    return (own or els or [None])[0]


display = font(best("h1") or best("h2"), "system-ui")
body = font(best("p") or P["body"], "system-ui")
mono = font(best("code"), "ui-monospace")
for f in (display, body, mono):
    if f["family"].startswith("-apple") or f["family"].startswith("ui-") or f["family"] in ("system-ui", "BlinkMacSystemFont"):
        f["family"] = "system-ui" if f is not mono else "ui-monospace"
if not mono.get("files"):  # code font not self-hosted: use any mono face the site loads
    m = next((k for k in faces if "mono" in k), None)
    if m: mono = {"family": faces[m][0]["family"], "weight": "400", "files": faces[m]}
serif = next((faces[k] for k in faces if "serif" in k), None)

# ---- shape ----
radii = [b["radius"] for b in P["buttons"] if b["radius"] not in ("0px", "")]
r0 = radii[0] if radii else "0px"
rpx = float(re.match(r"[\d.e+]+", r0).group()) if re.match(r"[\d.e+]+", r0) else 0
button = "pill" if rpx > 100 else f"{rpx:g}px" if rpx else "square"
has_texture = any("url(" in (e.get("bgImage") or "") and "gradient" not in e.get("bgImage", "") for e in P["cards"] + P["buttons"])

on = lambda c: (255, 255, 255) if lum(c) < .55 else (20, 20, 20)
brand = {
    "name": name, "source": [p["url"] for p in raw["pages"]],
    "mode": "dark" if dark else "light",
    "colors": {"surface": hexc(surface), "paper": hexc(paper), "ink": hexc(ink), "muted": hexc(muted) if muted else None,
               "accent": hexc(accent), "on_accent": hexc(on(accent)), "extras": [hexc(c) for c in extras]},
    "fonts": {"display": display, "body": body, "mono": mono,
              **({"serif": {"family": serif[0]["family"], "files": serif}} if serif else {})},
    "shape": {"button_radius": button, "card_radius": "12px" if rpx else "0px", "texture": "paper" if has_texture else "none"},
    "logo": next((f for f in ("logo.svg", "logo.png") if (d / f).exists()), None) or next((f.name for f in d.glob("icon.*")), None),
    "icon": next((f.name for f in d.glob("icon.*")), None),
    # how the brand shows up in a Vox-style explainer. Two colour fields, a card surface, a highlighter.
    # Clean product brands get clean edges and no grain; the torn-paper look stays only for brands that use texture.
    "vox": {"field_a": hexc(surface), "field_b": hexc(ink) if sat(accent) < .35 or dark else hexc(accent),
            "paper": hexc(paper), "ink": hexc(ink), "accent": hexc(accent), "on_accent": hexc(on(accent)),
            "edges": "torn" if has_texture else "clean", "grain": 0.06 if has_texture else 0.0,
            "display_case": display.get("transform") or "none"},
    "image_style": (f"clean, modern, flat editorial illustration in {name}'s brand style; "
                    f"{'dark' if dark else 'light'} {hexc(surface)} background, {hexc(ink)} linework, "
                    f"{hexc(accent)} as the only accent colour" + (f", secondary {', '.join(hexc(c) for c in extras[:2])}" if extras else "")
                    + "; generous empty space, no gradients, no 3D, no text"),
    "notes": [],
}
(d / "brand.json").write_text(json.dumps(brand, indent=1))
print(json.dumps({k: brand[k] for k in ("name", "mode", "colors", "shape", "vox")}, indent=1))
print("fonts:", {k: (v["family"], len(v.get("files", []))) for k, v in brand["fonts"].items()})
