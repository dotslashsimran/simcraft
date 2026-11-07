"""Generate images with Gemini (Nano Banana) or OpenAI (gpt-image). Standard library only.

Usage:
  python gen_image.py "<prompt>" [--out DIR] [--name NAME] [--n 4] [--aspect 16:9|9:16|1:1|4:3|3:4]
                      [--provider gemini|openai] [--model M] [--ref img.png ...] [--cutout] [--quality high] [--brand devin]
  python gen_image.py --models            list the image models your keys can use
  python gen_image.py --check             say which keys are set (never prints them)

Keys come from the environment or ~/.config/simcraft/keys.env (chmod 600), never from the command line:
  GEMINI_API_KEY=...      OPENAI_API_KEY=...
They are sent only in request headers, never in a URL, and never printed or logged.

Every image gets a sidecar <name>.json with the exact prompt, model and settings, so it can be remade or tweaked.
--n makes variations in parallel and a contact sheet (<name>_sheet.png) to pick from.
--ref passes reference images: for editing, for style, or to keep one character/object the same across images.
--cutout generates on flat chroma green and removes it, for a transparent PNG you can drop into a collage.
"""
import base64, json, os, struct, sys, time, urllib.error, urllib.request, zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

KEYFILE = Path.home() / ".config/simcraft/keys.env"
ALIASES = {"GEMINI_API_KEY": ["SIMCRAFT_GEMINI_KEY", "SKILL_IMAGE_GEN_GEMINI_KEY", "GOOGLE_API_KEY"],
           "OPENAI_API_KEY": ["SIMCRAFT_OPENAI_KEY", "SKILL_IMAGE_GEN_OPENAI_KEY"]}


def key(name):
    for n in [name] + ALIASES[name]:
        if os.environ.get(n):
            return os.environ[n].strip()
    if KEYFILE.exists():
        for line in KEYFILE.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() in [name] + ALIASES[name] and v.strip():
                return v.strip().strip("'\"")
    return None


def post(url, body, headers, timeout=300):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={**headers, "Content-Type": "application/json"})
    for attempt in range(4):
        try:
            return json.load(urllib.request.urlopen(req, timeout=timeout))
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="ignore")[:400]
            if e.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(4 * (attempt + 1)); continue
            raise SystemExit(f"API error {e.code}: {msg}")


def get(url, headers):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60))


# ---------- Gemini ----------
def gemini_models(k):
    d = get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=300", {"x-goog-api-key": k})
    return [m["name"].split("/")[-1] for m in d.get("models", [])
            if "image" in m["name"] and "generateContent" in m.get("supportedGenerationMethods", [])]


def gemini_pick(k, want):
    if want: return want
    ms = gemini_models(k)
    # prefer the newest "pro" image model, then the newest flash image model
    rank = lambda m: (("pro" in m) * 2 + ("flash" in m), m)
    ms = sorted((m for m in ms if "preview" not in m or "pro" in m), key=rank, reverse=True) or ms
    if not ms: raise SystemExit("no Gemini image model available for this key")
    return ms[0]


def gemini(prompt, model, aspect, refs, k, quality):
    parts = [{"text": prompt}]
    for r in refs:
        mime = "image/png" if r.suffix.lower() == ".png" else "image/jpeg"
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(r.read_bytes()).decode()}})
    cfg = {"responseModalities": ["IMAGE", "TEXT"]}
    if aspect: cfg["imageConfig"] = {"aspectRatio": aspect}
    d = post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
             {"contents": [{"parts": parts}], "generationConfig": cfg}, {"x-goog-api-key": k})
    if d.get("promptFeedback", {}).get("blockReason"):
        raise SystemExit(f"blocked by safety filter: {d['promptFeedback']['blockReason']}")
    for c in d.get("candidates", []):
        for p in c.get("content", {}).get("parts", []):
            data = p.get("inlineData") or p.get("inline_data")
            if data: return base64.b64decode(data["data"])
        if c.get("finishReason") not in (None, "STOP"):
            raise SystemExit(f"no image: finishReason {c.get('finishReason')}")
    raise SystemExit("no image in response: " + json.dumps(d)[:300])


# ---------- OpenAI ----------
OPENAI_SIZE = {"1:1": "1024x1024", "16:9": "1536x1024", "4:3": "1536x1024", "9:16": "1024x1536", "3:4": "1024x1536", None: "auto"}


def openai(prompt, model, aspect, refs, k, quality, cutout):
    h = {"Authorization": f"Bearer {k}"}
    model = model or "gpt-image-2"
    if refs:  # edits endpoint takes multipart form data
        bnd = "----simcraft" + str(time.time_ns())
        fields = [("model", model), ("prompt", prompt), ("size", OPENAI_SIZE.get(aspect, "auto")), ("quality", quality)]
        body = b"".join(f"--{bnd}\r\nContent-Disposition: form-data; name=\"{n}\"\r\n\r\n{v}\r\n".encode() for n, v in fields)
        for r in refs:
            body += (f"--{bnd}\r\nContent-Disposition: form-data; name=\"image[]\"; filename=\"{r.name}\"\r\n"
                     f"Content-Type: image/png\r\n\r\n").encode() + r.read_bytes() + b"\r\n"
        body += f"--{bnd}--\r\n".encode()
        req = urllib.request.Request("https://api.openai.com/v1/images/edits", data=body,
                                     headers={**h, "Content-Type": f"multipart/form-data; boundary={bnd}"})
        try:
            d = json.load(urllib.request.urlopen(req, timeout=300))
        except urllib.error.HTTPError as e:
            raise SystemExit(f"API error {e.code}: {e.read().decode(errors='ignore')[:400]}")
    else:
        body = {"model": model, "prompt": prompt, "n": 1, "size": OPENAI_SIZE.get(aspect, "auto"), "quality": quality}
        if cutout: body["background"] = "transparent"  # gpt-image does real transparency, no chroma key needed
        d = post("https://api.openai.com/v1/images/generations", body, h)
    item = d["data"][0]
    if item.get("b64_json"): return base64.b64decode(item["b64_json"])
    return urllib.request.urlopen(item["url"], timeout=120).read()


# ---------- PNG helpers (no Pillow needed) ----------
def png_read(data):
    """Decode an 8-bit RGB/RGBA PNG to (w, h, rows of RGBA bytes). Returns None for other formats."""
    if data[:8] != b"\x89PNG\r\n\x1a\n": return None
    i, w = 8, None; idat = b""
    while i < len(data):
        n = struct.unpack(">I", data[i:i + 4])[0]; t = data[i + 4:i + 8]; c = data[i + 8:i + 8 + n]
        if t == b"IHDR":
            w, h, bd, ct = struct.unpack(">IIBB", c[:10])
            if bd != 8 or ct not in (2, 6): return None
            ch = 3 if ct == 2 else 4
        elif t == b"IDAT": idat += c
        i += 12 + n
    raw = zlib.decompress(idat); stride = w * ch; rows = []; prev = bytearray(stride); p = 0
    for _ in range(h):
        f = raw[p]; line = bytearray(raw[p + 1:p + 1 + stride]); p += 1 + stride
        for x in range(stride):
            a = line[x - ch] if x >= ch else 0; b = prev[x]; cc = prev[x - ch] if x >= ch else 0
            if f == 1: line[x] = (line[x] + a) & 255
            elif f == 2: line[x] = (line[x] + b) & 255
            elif f == 3: line[x] = (line[x] + ((a + b) >> 1)) & 255
            elif f == 4:
                pa, pb, pc = abs(b - cc), abs(a - cc), abs(a + b - 2 * cc)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else cc)) & 255
        prev = line
        rows.append(line if ch == 4 else bytearray(b for px in range(w) for b in (*line[px * 3:px * 3 + 3], 255)))
    return w, h, rows


def png_write(w, h, rows):
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    chunk = lambda t, c: struct.pack(">I", len(c)) + t + c + struct.pack(">I", zlib.crc32(t + c) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def chroma_cut(data, key=(0, 255, 0)):
    """Remove a flat green background with clean edges.

    Edge pixels are a blend of subject and green. Muting the green leaves an olive fringe, so instead the
    blend is undone: alpha comes from how green the pixel is, then the subject colour is recovered as
    (pixel - (1 - alpha) * green) / alpha.
    """
    img = png_read(data)
    if not img: return None
    w, h, rows = img
    kr, kg, kb = key
    for r in rows:
        for x in range(w):
            i = 4 * x
            R, G, B = r[i], r[i + 1], r[i + 2]
            g = G - max(R, B)  # how much greener than anything else
            if g >= 150:
                r[i + 3] = 0; continue
            if g <= 10:
                continue
            al = 1 - (g - 10) / 140
            r[i] = max(0, min(255, int((R - (1 - al) * kr) / al)))
            r[i + 1] = max(0, min(255, int((G - (1 - al) * kg) / al)))
            r[i + 2] = max(0, min(255, int((B - (1 - al) * kb) / al)))
            r[i + 1] = min(r[i + 1], max(r[i], r[i + 2]) + 20)  # last bit of spill
            r[i + 3] = int(255 * al)
    # choke: the outer ring of "solid" pixels is still a blend with green (a red edge pixel reads as solid because
    # red is its strongest channel), so each takes the colour from 2 px further inside the subject
    solid = [[rows[y][4 * x + 3] >= 250 for x in range(w)] for y in range(h)]
    for y in range(h):
        for x in range(w):
            if not solid[y][x]: continue
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                xx, yy = x + dx, y + dy
                if 0 <= xx < w and 0 <= yy < h and not solid[yy][xx]:
                    sx, sy = x - 2 * dx, y - 2 * dy
                    if 0 <= sx < w and 0 <= sy < h and solid[sy][sx]:
                        rows[y][4 * x:4 * x + 3] = rows[sy][4 * sx:4 * sx + 3]
                        rows[y][4 * x + 3] = 235
                    break
    # edge decontamination: a soft edge pixel takes its colour from the nearest solid pixel of the subject
    # (it keeps its own transparency), so no green or olive line survives against any background
    for y in range(h):
        r = rows[y]
        for x in range(w):
            a_ = r[4 * x + 3]
            if 0 < a_ < 250:
                best = None
                for d in (1, 2, 3):
                    for yy, xx in ((y, x - d), (y, x + d), (y - d, x), (y + d, x), (y - d, x - d), (y + d, x + d), (y - d, x + d), (y + d, x - d)):
                        if 0 <= yy < h and 0 <= xx < w and rows[yy][4 * xx + 3] >= 250:
                            best = rows[yy][4 * xx:4 * xx + 3]; break
                    if best: break
                if best: r[4 * x:4 * x + 3] = best
    return png_write(w, h, rows)


def sheet(paths, out):
    """Contact sheet via ffmpeg if available (it's only a viewing aid)."""
    import shutil, subprocess
    ff = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
    if not Path(ff).exists() or len(paths) < 2: return None
    ins = [x for p in paths for x in ("-i", str(p))]
    fc = "".join(f"[{i}:v]scale=512:-2,drawtext=text='{i + 1}':x=16:y=12:fontsize=44:fontcolor=white:box=1:boxcolor=black@0.6[v{i}];"
                 for i in range(len(paths))) + "".join(f"[v{i}]" for i in range(len(paths))) + f"hstack=inputs={len(paths)}"
    r = subprocess.run([ff, "-v", "error", "-y", *ins, "-filter_complex", fc, "-frames:v", "1", str(out)])
    return out if r.returncode == 0 else None


# ---------- main ----------
a = sys.argv[1:]
opt = lambda k, d=None: a[a.index(k) + 1] if k in a else d
if "--check" in a:
    for n in ALIASES: print(n, "set" if key(n) else "not set")
    print("key file:", KEYFILE, "(exists)" if KEYFILE.exists() else "(missing)")
    sys.exit()
if "--models" in a:
    if key("GEMINI_API_KEY"): print("gemini:", ", ".join(gemini_models(key("GEMINI_API_KEY"))))
    if key("OPENAI_API_KEY"): print("openai: gpt-image-2, gpt-image-1")
    sys.exit()

prompt = a[0]
refs = []
if "--ref" in a:
    i = a.index("--ref") + 1
    while i < len(a) and not a[i].startswith("--"): refs.append(Path(a[i]).expanduser()); i += 1
n, aspect, quality, cutout = int(opt("--n", 1)), opt("--aspect"), opt("--quality", "high"), "--cutout" in a
provider = opt("--provider") or ("gemini" if key("GEMINI_API_KEY") else "openai" if key("OPENAI_API_KEY") else None)
if not provider or not key("GEMINI_API_KEY" if provider == "gemini" else "OPENAI_API_KEY"):
    raise SystemExit(f"no API key. Add GEMINI_API_KEY=... or OPENAI_API_KEY=... to {KEYFILE} (then chmod 600 it).")
k = key("GEMINI_API_KEY" if provider == "gemini" else "OPENAI_API_KEY")
model = gemini_pick(k, opt("--model")) if provider == "gemini" else (opt("--model") or "gpt-image-2")

full = prompt
if opt("--brand"):  # a brand kit from the brand-kit skill: its image style and palette go into every prompt
    bname = opt("--brand")
    bdir = Path(bname).expanduser() if "/" in bname else Path.home() / ".local/share/simcraft/brands" / bname
    if not (bdir / "brand.json").exists():
        raise SystemExit(f'no brand kit for "{bname}". Run the brand-kit skill first.')
    full += " Style: " + json.load(open(bdir / "brand.json"))["image_style"] + "."
if cutout and provider == "gemini":
    full += (" Isolated on a perfectly flat, uniform pure chroma-key green background (#00FF00), no shadow on the "
             "background, no gradient, no floor, subject fully inside the frame with margin around it.")
out = Path(opt("--out", ".")).expanduser(); out.mkdir(parents=True, exist_ok=True)
name = opt("--name") or "img_" + time.strftime("%Y%m%d_%H%M%S")


def one(i):
    data = gemini(full, model, aspect, refs, k, quality) if provider == "gemini" else openai(full, model, aspect, refs, k, quality, cutout)
    if cutout and provider == "gemini":
        cut = chroma_cut(data)
        if cut: data = cut
    ext = ".png" if data[:4] == b"\x89PNG" else ".jpg"
    p = out / (f"{name}_{i + 1}{ext}" if n > 1 else f"{name}{ext}")
    p.write_bytes(data)
    return p


with ThreadPoolExecutor(min(n, 4)) as ex:
    paths = list(ex.map(one, range(n)))
meta = {"prompt": prompt, "brand": opt("--brand"), "sent_prompt": full, "provider": provider, "model": model, "aspect": aspect, "quality": quality,
        "cutout": cutout, "refs": [str(r) for r in refs], "files": [str(p) for p in paths], "made": time.strftime("%Y-%m-%d %H:%M")}
(out / f"{name}.json").write_text(json.dumps(meta, indent=1))
s = sheet(paths, out / f"{name}_sheet.png") if n > 1 else None
print(f"{provider} {model}: " + " ".join(str(p) for p in paths) + (f"\nsheet: {s}" if s else ""))
