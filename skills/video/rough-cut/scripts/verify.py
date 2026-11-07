"""Check a render: transcribe it again and compare with what the cut list says should be there.

Usage: python verify.py <workspace> [--v 1] [--w 854]
Writes cuts/verify_v<N>.md and prints a summary.

Catches:
- words missing from the render (usually clipped at a cut: the edge landed inside the word)
- words in the render that shouldn't be there (a filler or a bit of a retake leaking through a cut)
Mishearings (Northwind / North wind) are ignored: two words that look alike by letters count as the same word.
"""
import json, re, subprocess, sys
from difflib import SequenceMatcher
from pathlib import Path
from common import tc, Voice, load_wav

ws = Path(sys.argv[1]).expanduser()
arg = lambda k, d: sys.argv[sys.argv.index(k) + 1] if k in sys.argv else d
V, Wd = arg("--v", "1"), arg("--w", "854")
render = ws / "renders" / f"v{V}_{Wd}.mp4"
edl = json.load(open(ws / "cuts/edl.json"))
src_words = json.load(open(ws / "audio/words.json"))
NUM = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five", "6": "six", "7": "seven",
       "8": "eight", "9": "nine", "10": "ten"}
norm = lambda w: (lambda t: NUM.get(t, t))(re.sub(r"[^\w']", "", w.lower()))
SKIP = {"dollar", "dollars", "percent"}  # "$300" vs "300 dollar", "20%" vs "20 percent": same speech, different spelling

# expected words, with where they sit in the render timeline
expected, t, cuts = [], 0.0, []
for p in edl["keep"]:
    sp = float(p.get("speed", edl.get("speed", 1.0)))
    for w in src_words.get(p["clip"], []):
        mid = (w[0] + w[1]) / 2
        if w[1] > p["in"] + 0.03 and w[0] < p["out"] - 0.03 and norm(w[2]) and norm(w[2]) not in SKIP:  # any word the piece reaches into
            expected.append((norm(w[2]), t + (mid - p["in"]) / sp, p["clip"], w[0], w[2]))
    t += (p["out"] - p["in"]) / sp
    cuts.append(t)

# hear the render piece by piece: one long run of fast speech with no pauses makes whisper skip whole sentences
starts = [0.0] + cuts[:-1]
win = [[round(a, 3), round(b, 3)] for a, b in zip(starts, cuts) if b - a > 0.05]
win_json = ws / "cuts" / f".verify_v{V}_windows.json"
json.dump(win, open(win_json, "w"))
out_json = ws / "cuts" / f".verify_v{V}.json"
subprocess.run([sys.executable, str(Path(__file__).with_name("transcribe.py")), "--file", str(render),
                "--out", str(out_json), "--windows", str(win_json)] + (["--lang", arg("--lang", "")] if "--lang" in sys.argv else []), check=True)
heard = [(norm(w[2]), (w[0] + w[1]) / 2, w[2]) for w in json.load(open(out_json)) if norm(w[2]) and norm(w[2]) not in SKIP]
out_json.replace(ws / "cuts" / f"verify_v{V}_words.json"); win_json.unlink()  # words kept for debugging

DIG = {v: k for k, v in NUM.items()}
canon = lambda ws_: "".join(DIG.get(w, w) for w in ws_)  # "sop two" vs "soc2" -> "sop2" vs "soc2"
alike = lambda a, b: a == b or SequenceMatcher(None, a, b).ratio() >= 0.6
ea, ha = [e[0] for e in expected], [h[0] for h in heard]
missing, extra, unclear = [], [], []
for op, i1, i2, j1, j2 in SequenceMatcher(None, ea, ha, autojunk=False).get_opcodes():
    if op == "equal": continue
    if op == "replace" and i2 - i1 == j2 - j1 and all(alike(ea[i], ha[j]) for i, j in zip(range(i1, i2), range(j1, j2))):
        continue
    if op == "replace" and SequenceMatcher(None, canon(ea[i1:i2]), canon(ha[j1:j2])).ratio() >= 0.45:
        unclear.append((expected[i1], " ".join(e[4] for e in expected[i1:i2]), " ".join(h[2] for h in heard[j1:j2])))
        continue
    missing += expected[i1:i2]
    extra += heard[j1:j2]

# the sound test: does any cut land while the voice is still going? (independent of what whisper hears)
voices, inside = {}, []
for k, p in enumerate(edl["keep"]):
    v = voices.setdefault(p["clip"], Voice(load_wav(ws / "audio" / f"{Path(p['clip']).stem}.wav")))
    prev = edl["keep"][k - 1] if k else None
    nxt = edl["keep"][k + 1] if k + 1 < len(edl["keep"]) else None
    joined_before = prev and prev["clip"] == p["clip"] and abs(prev["out"] - p["in"]) < 0.02
    joined_after = nxt and nxt["clip"] == p["clip"] and abs(nxt["in"] - p["out"]) < 0.02
    if not joined_before and v.is_on(p["in"] - 0.03) and v.is_on(p["in"] + 0.03):
        inside.append((p["clip"], p["in"], "start", p["text"].split()[0] if p["text"] else ""))
    if not joined_after and v.is_on(p["out"] - 0.03) and v.is_on(p["out"] + 0.03):
        inside.append((p["clip"], p["out"], "end", p["text"].split()[-1] if p["text"] else ""))
near_cut = lambda rt: min((abs(rt - c) for c in cuts), default=9) < 0.4
lines = [f"# Verify v{V}", "", f"Expected {len(expected)} words, heard {len(heard)}. "
         f"**{100 * (1 - len(missing) / max(1, len(expected))):.1f}% of the kept words are in the render**, "
         f"{len(inside)} cut(s) land inside sound, {len(extra)} unexpected word(s), {len(unclear)} mishearing(s).", ""]
if inside:
    lines.append("## Cuts that land while the voice is still going (real risk of a clipped word or a pop)")
    for clip, t_, side, w in inside:
        lines.append(f"- {clip} {tc(t_)}, {side} of the piece near \"{w}\"")
    lines.append("")
if missing:
    lines.append("## Missing (expected but not heard)")
    for _, rt, clip, st, w in missing:
        lines.append(f"- render {tc(rt)} (source {clip} {tc(st)}): \"{w}\"" + ("  <- right at a cut" if near_cut(rt) else ""))
    lines.append("")
if extra:
    lines.append("## Heard but not in the cut list (leaked filler or retake?)")
    for _, rt, w in extra:
        lines.append(f"- render {tc(rt)}: \"{w}\"" + ("  <- right at a cut" if near_cut(rt) else ""))
    lines.append("")
if unclear:
    lines.append("## Mishearings (the two transcripts disagree on wording; usually mumbling or noise, not a cut problem)")
    for e, a_, b_ in unclear:
        lines.append(f"- render {tc(e[1])}: cut list \"{a_}\" / render \"{b_}\"")
    lines.append("")
if not (missing or extra or inside):
    lines.append("Clean: every kept word is there, nothing extra got in, and no cut lands inside the voice.")
(ws / "cuts" / f"verify_v{V}.md").write_text("\n".join(lines) + "\n")
print("\n".join(lines[:3]))
print(f"See cuts/verify_v{V}.md")
