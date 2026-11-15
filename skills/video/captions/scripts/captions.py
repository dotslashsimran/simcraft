"""Word-timed captions: short chunks, the spoken word lit up, chosen words emphasised. Writes .ass (styled, for
burning in) and .srt (plain, for uploading as closed captions).

Usage:
  python captions.py <video> [--words <file>] [--workspace <rough-cut workspace>] [--emphasis "word,two words,..."]
                     [--font "Inter"] [--fontsdir <dir>] [--size 0.075] [--max-words 3] [--max-chars 0]
                     [--color FFFFFF] [--active FFD400] [--emph FF4D4D] [--upper] [--no-active] [--pos 0.72]
                     [--preview] [--stills]
Word timings, first that applies:
  --words <file>      [[start, end, word, ...], ...] on this video's timeline, or rough-cut's {clip: [...]} with one clip
  --workspace <ws>    rough-cut workspace whose render this is: verify's words for that version if present, else
                      audio/words.json mapped through cuts/edl.json (speed included)
  otherwise           the video is transcribed with rough-cut's transcribe.py (needs rough-cut's .venv)
Outputs next to the video: <name>.ass, <name>.srt, <name>_captions.md, <name>_words.json (timings used; fix a
misheard word there and re-run with --words), and with --preview a 480p burned-in check
(<name>_cc_preview.mp4); --stills writes PNG frames at each emphasised word to look at.

Rules it follows (each one is a common caption failure):
- Chunks break at sentence ends and at pauses over 0.3 s first, then at --max-words / --max-chars. A chunk never
  spans a cut or a long pause, so text never sits on screen over silence.
- Every chunk stays up at least 0.5 s (short chunks flash), and comes down 0.25 s after its last word or when the next
  chunk starts, whichever is first. No two chunks overlap.
- The lit word changes exactly when it is spoken (one event per word, so nothing drifts over a long video).
- Position defaults to 72% down the frame for 9:16 (above the TikTok/Reels/Shorts caption and button area) and 88% for
  16:9. Size is a fraction of the frame's short side.
"""
import json, re, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RC = HERE.parents[1] / "rough-cut"
sys.path.insert(0, str(HERE))
from media import ffmpeg, probe, tc  # noqa: E402

arg = lambda k, d=None: sys.argv[sys.argv.index(k) + 1] if k in sys.argv else d
if len(sys.argv) < 2 or sys.argv[1].startswith("--"):
    raise SystemExit(__doc__)
video = Path(sys.argv[1]).expanduser()
info = probe(video); V = info["video"]
if not V:
    raise SystemExit(f"{video.name} has no video stream.")
W, H, FPS = V["w"], V["h"], V["fps"] or 30
vertical = H > W
FF = ffmpeg()
norm = lambda w: re.sub(r"[^\w']", "", w.lower())


# ---------- 1. word timings on this video's timeline ----------
def from_workspace(ws):
    ws = Path(ws).expanduser()
    m = re.match(r"v(\w+?)_\d+$", video.stem)
    if m and (ws / "cuts" / f"verify_v{m.group(1)}_words.json").exists():  # heard on the render itself: most accurate
        return [w[:3] for w in json.load(open(ws / "cuts" / f"verify_v{m.group(1)}_words.json"))], "verify transcript of the render"
    edl = json.load(open(ws / "cuts/edl.json")); src = json.load(open(ws / "audio/words.json"))
    out, t = [], 0.0
    for p in edl["keep"]:
        sp = float(p.get("speed", edl.get("speed", 1.0)))
        L = (p["out"] - p["in"]) / sp
        for w in src.get(p["clip"], []):
            mid = (w[0] + w[1]) / 2
            if p["in"] <= mid <= p["out"]:
                a = t + max(0.0, w[0] - p["in"]) / sp
                b = t + min(p["out"] - p["in"], w[1] - p["in"]) / sp
                out.append([round(a, 3), round(max(b, a + 0.05), 3), w[2]])
        t += L
    return out, "rough-cut transcript mapped through the cut list"


def from_whisper():
    py = RC / ".venv/bin/python"
    if not py.exists():
        raise SystemExit("No word timings given and rough-cut's Whisper env is missing: run "
                         f"sh {RC}/setup.sh, or pass --words / --workspace.")
    out = video.with_name(f"{video.stem}_words.json")
    cmd = [str(py), str(RC / "scripts/transcribe.py"), "--file", str(video), "--out", str(out)]
    if arg("--lang"):
        cmd += ["--lang", arg("--lang")]
    subprocess.run(cmd, check=True)
    return [w[:3] for w in json.load(open(out))], f"Whisper on the video ({out.name})"


if arg("--words"):
    d = json.load(open(Path(arg("--words")).expanduser()))
    if isinstance(d, dict):
        if len(d) != 1:
            raise SystemExit("--words has several clips: use --workspace so they are mapped through the cut list.")
        d = next(iter(d.values()))
    words, source = [w[:3] for w in d], arg("--words")
elif arg("--workspace"):
    words, source = from_workspace(arg("--workspace"))
else:
    words, source = from_whisper()
FILLERS = {"um", "umm", "uh", "uhh", "uhm", "erm", "er", "hmm", "hm", "mm", "mmm", "ah"}
words = sorted([w for w in words if norm(w[2]) and norm(w[2]) not in FILLERS], key=lambda w: w[0])
if not words:
    raise SystemExit("No words to caption.")
# overlapping word times (whisper does this at chunk joins) would light two words at once: clamp each to the next start
for a, b in zip(words, words[1:]):
    if a[1] > b[0]:
        a[1] = max(a[0] + 0.02, b[0])

wj = video.with_name(f"{video.stem}_words.json")
if not (arg("--words") and Path(arg("--words")).expanduser().resolve() == wj.resolve()):
    json.dump(words, open(wj, "w"))  # the exact timings captioned, reusable with --words after hand fixes

# ---------- 2. chunks ----------
MAXW = int(arg("--max-words", 3 if vertical else 5))
MAXC = int(arg("--max-chars", 0)) or (18 if vertical else 32)
PAUSE, HOLD, MIN_ON = 0.3, 0.25, 0.5
upper = "--upper" in sys.argv
chunks, hard, cur = [], [], []  # hard[k]: chunk k ended at a pause or sentence end (not just because it was full)
for i, w in enumerate(words):
    text = w[2].strip()
    if cur:
        gap = w[0] - cur[-1][1]
        chars = len(" ".join(x[2] for x in cur + [[0, 0, text]]))
        h = gap > PAUSE or bool(re.search(r"[.?!]$", cur[-1][2]))
        if h or len(cur) >= MAXW or chars > MAXC:
            chunks.append(cur); hard.append(h); cur = []
    cur.append([w[0], w[1], text])
if cur:
    chunks.append(cur); hard.append(True)
# no one-word orphan at the end of a sentence ("...this pipeline" / "works."): move a word over from the full chunk
for k in range(len(chunks) - 1):
    if not hard[k] and hard[k + 1] and len(chunks[k + 1]) == 1 and len(chunks[k]) >= 3:
        moved = chunks[k].pop()
        if len(" ".join(x[2] for x in [moved] + chunks[k + 1])) <= MAXC:
            chunks[k + 1].insert(0, moved)
        else:
            chunks[k].append(moved)

times = []
for k, c in enumerate(chunks):
    a = c[0][0]
    nxt = chunks[k + 1][0][0] if k + 1 < len(chunks) else c[-1][1] + 10
    b = min(max(c[-1][1] + HOLD, a + MIN_ON), nxt)
    times.append((a, b))

# ---------- 3. emphasis ----------
emph_raw = [e.strip() for e in (arg("--emphasis") or "").split(",") if e.strip()]
emph_seq = [[norm(x) for x in e.split()] for e in emph_raw]
flat = [(ci, wi, norm(w[2])) for ci, c in enumerate(chunks) for wi, w in enumerate(c)]
emph = set(); found = {e: 0 for e in emph_raw}
for e, seq in zip(emph_raw, emph_seq):
    for i in range(len(flat) - len(seq) + 1):
        if [f[2] for f in flat[i:i + len(seq)]] == seq:
            emph.update((f[0], f[1]) for f in flat[i:i + len(seq)]); found[e] += 1

# ---------- 4. ASS ----------
def bgr(hexrgb):
    h = hexrgb.lstrip("#"); return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}".upper()


font = arg("--font", "Inter")
size = round(min(W, H) * float(arg("--size", 0.075 if vertical else 0.06)))
pos = float(arg("--pos", 0.72 if vertical else 0.88))
col, act, emc = arg("--color", "FFFFFF"), arg("--active", "FFD400"), arg("--emph", "FF4D4D")
active_on = "--no-active" not in sys.argv
outline = max(2, round(size * 0.09))
marginv = round(H * (1 - pos))
esc = lambda s: s.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def ass_t(t):
    t = max(0.0, t); cs = int(round(t * 100)); h, cs = divmod(cs, 360000); m, cs = divmod(cs, 6000); s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def line(ci, lit):
    parts = []
    for wi, w in enumerate(chunks[ci]):
        t = esc(w[2].upper() if upper else w[2])
        e = (ci, wi) in emph
        if wi == lit and e:  # emphasised word being spoken: emphasis colour and a quick pop
            parts.append(f"{{\\c{bgr(emc)}\\fscx118\\fscy118\\t(0,90,\\fscx100\\fscy100)}}{t}{{\\c{bgr(col)}\\fscx100\\fscy100}}")
        elif e:
            parts.append(f"{{\\c{bgr(emc)}}}{t}{{\\c{bgr(col)}}}")
        elif wi == lit and active_on:
            parts.append(f"{{\\c{bgr(act)}}}{t}{{\\c{bgr(col)}}}")
        else:
            parts.append(t)
    return " ".join(parts)


ev = []
for ci, (a, b) in enumerate(times):
    c = chunks[ci]
    if not active_on and not any((ci, wi) in emph for wi in range(len(c))):
        ev.append((a, b, line(ci, -1))); continue
    for wi, w in enumerate(c):
        s = a if wi == 0 else w[0]
        e = b if wi == len(c) - 1 else c[wi + 1][0]
        if e - s >= 0.5 / FPS:
            ev.append((s, e, line(ci, wi)))
# centisecond rounding must never make neighbours overlap
ass_ev = []
for s, e, txt in ev:
    s2, e2 = ass_t(s), ass_t(e)
    if s2 != e2:
        ass_ev.append(f"Dialogue: 0,{s2},{e2},Cap,,0,0,0,,{txt}")

ass = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: {"TV.709" if not V["hdr"] else "None"}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{size},{bgr(col)},{bgr(col)},&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{outline},0,2,{round(W * 0.06)},{round(W * 0.06)},{marginv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""" + "\n".join(ass_ev) + "\n"
ass_p, srt_p = video.with_suffix(".ass"), video.with_suffix(".srt")
ass_p.write_text(ass)


def srt_t(t):
    ms = int(round(max(0.0, t) * 1000)); h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


srt_p.write_text("\n".join(f"{k + 1}\n{srt_t(a)} --> {srt_t(b)}\n{' '.join(w[2] for w in chunks[k])}\n"
                           for k, (a, b) in enumerate(times)))

# ---------- 5. checks ----------
issues = []
cover = sum(1 for c in chunks for _ in c)
if cover != len(words):
    issues.append(f"{len(words) - cover} words not in any caption")
over = [(k, times[k][1], times[k + 1][0]) for k in range(len(times) - 1) if times[k][1] > times[k + 1][0] + 1e-6]
if over:
    issues.append(f"{len(over)} overlapping captions")
fast = [(k, len(" ".join(w[2] for w in chunks[k])) / (times[k][1] - times[k][0])) for k in range(len(chunks))]
fast = [(k, r) for k, r in fast if r > 25]
lines = [f"# Captions: {video.name}", "",
         f"- words from {source}: {len(words)} words in {len(chunks)} captions (max {MAXW} words / {MAXC} chars)",
         f"- {font} {size}px, {'upper case, ' if upper else ''}at {pos:.0%} down the frame; spoken word "
         f"{'#' + act if active_on else 'not'} lit; emphasis #{emc}",
         f"- {len(ass_ev)} subtitle events, {ass_p.name} + {srt_p.name}"]
for e, n in found.items():
    lines.append(f"- emphasis \"{e}\": {'found ' + str(n) + 'x' if n else '**not found in the transcript** (check the spelling Whisper used)'}")
if fast:
    lines.append(f"- {len(fast)} caption(s) faster than 25 chars/s, hard to read: " +
                 ", ".join(f"{tc(times[k][0])}" for k, _ in fast[:8]))
lines += [f"- **{i}**" for i in issues] or ["- checks: every word captioned once, no overlaps"]
fontsdir = arg("--fontsdir")
assf = "ass=" + str(ass_p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'") + \
       (f":fontsdir={Path(fontsdir).expanduser()}" if fontsdir else "")

if "--stills" in sys.argv:
    pts = sorted({chunks[ci][wi][0] + 0.06 for ci, wi in emph}) or [times[len(times) // 2][0] + 0.1]
    for k, t in enumerate(pts[:12]):
        png = video.with_name(f"{video.stem}_cc_{k:02d}.png")
        subprocess.run([FF, "-v", "error", "-y", "-ss", f"{t:.3f}", "-copyts", "-i", str(video), "-vf",
                        f"{assf},setpts=PTS-STARTPTS", "-frames:v", "1", str(png)], check=True)
        lines.append(f"- still {tc(t)}: {png.name}")
if "--preview" in sys.argv:
    pv = video.with_name(f"{video.stem}_cc_preview.mp4")
    sw = 854 if not vertical else 480
    subprocess.run([FF, "-v", "error", "-y", "-i", str(video), "-vf", f"{assf},scale={sw}:-2", "-c:v", "libx264",
                    "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", str(pv)], check=True)
    lines.append(f"- preview: {pv.name}")
lines.append(f"\nBurn in at the final encode (one encode, not two): export.py ... --ass {ass_p.name}")
video.with_name(f"{video.stem}_captions.md").write_text("\n".join(lines) + "\n")
print("\n".join(lines))
