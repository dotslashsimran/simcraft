"""First-pass cut list: drops dead air, fillers, repeats, retakes and ghost words.

Usage: python find_cuts.py <workspace> [--gap 0.35] [--keep-fillers]
Reads audio/words.json and audio/<clip>.wav.
Writes cuts/edl.json (pieces to keep, in order) and cuts/cut_sheet.md (every cut and why, plus things to check).

This is the mechanical pass. Claude then reads the cut sheet and the transcript and makes the calls a script
can't: which take is better, a filler that is part of a joke, a pause that is a beat.
"""
import json, re, sys
from difflib import SequenceMatcher
from pathlib import Path
from common import Voice, load_wav, tc

ws = Path(sys.argv[1]).expanduser()
arg = lambda k, d: sys.argv[sys.argv.index(k) + 1] if k in sys.argv else d
GAP = float(arg("--gap", 0.35))   # longest pause kept inside a piece
PRE, POST = 0.06, 0.10            # room before the voice starts / after it stops (added to the snapped edges)
PHRASE_GAP = 0.6                  # a pause this long, or . ? !, ends a phrase
MIN_PIECE = 0.20
FILLERS = {"um", "umm", "ummm", "uh", "uhh", "uhm", "erm", "er", "hmm", "hm", "mm", "mmm", "mhm", "ah", "ahh"}
keep_fillers = "--keep-fillers" in sys.argv
norm = lambda w: re.sub(r"[^\w']", "", w.lower())

data = json.load(open(ws / "audio/words.json"))
edl, sheet, check = [], [], []
tot_in = tot_out = n_words_kept = 0.0

for clip, raw_words in data.items():
    W = [(w[0], w[1], w[2], w[3] if len(w) > 3 else 1.0) for w in raw_words]
    if not W:
        sheet += [f"## {clip}", "- no speech, left out. Add it by hand if it's a visual moment.", ""]
        continue
    V = Voice(load_wav(ws / "audio" / f"{Path(clip).stem}.wav"))
    tok = [norm(w[2]) for w in W]
    drop = {}

    # ghost words: Whisper sometimes writes words into silence ("Thank you." at the end is the classic)
    # Only words on their own count: a quiet "of" inside a sentence is real, its timing is just off.
    for i, (s, e, w, p) in enumerate(W):
        alone = (i == 0 or s - W[i - 1][1] > 0.3) and (i + 1 == len(W) or W[i + 1][0] - e > 0.3)
        if alone and V.fraction(s - 0.1, e + 0.1) < 0.1:
            drop[i] = "ghost (no voice under it, transcription made it up)"
    # fillers
    if not keep_fillers:
        for i, t in enumerate(tok):
            if t in FILLERS:
                drop.setdefault(i, "filler")
    # repeated words or chunks said twice in a row: "I I", "so the so the". Keep the second.
    live = [i for i in range(len(W)) if i not in drop]
    for n in (4, 3, 2, 1):
        k = 0
        while k + 2 * n <= len(live):
            a, b = live[k:k + n], live[k + n:k + 2 * n]
            if all(tok[x] for x in a) and [tok[x] for x in a] == [tok[x] for x in b] \
                    and W[b[0]][0] - W[a[-1]][1] < 1.0 and not any(x in drop for x in a):
                for x in a: drop[x] = "repeat"
                k += n
            else:
                k += 1
        live = [i for i in range(len(W)) if i not in drop]

    # phrases (split on pauses and sentence ends), then retakes
    phrases, cur = [], []
    for i in live:
        if cur and W[i][0] - W[cur[-1]][1] >= PHRASE_GAP:
            phrases.append(cur); cur = []
        cur.append(i)
        if re.search(r"[.?!]$", W[i][2]):
            phrases.append(cur); cur = []
    if cur: phrases.append(cur)
    NUM = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five", "6": "six", "7": "seven",
           "8": "eight", "9": "nine", "10": "ten"}
    words_of = lambda ph: [NUM.get(tok[i], tok[i]) for i in ph if tok[i]]
    sim = lambda x, y: SequenceMatcher(None, " ".join(x), " ".join(y)).ratio()  # by letters: survives mishearings
    for a, A in enumerate(phrases):
        ta = words_of(A)
        if not ta: continue
        for B in phrases[a + 1:a + 8]:
            tb = words_of(B)
            gap = W[B[0]][0] - W[A[-1]][1]
            if gap > 20: break
            if not tb: continue
            why = None
            if len(ta) >= 3 and len(tb) >= 3 and sim(ta, tb) >= 0.75:
                why = "retake (same line said again, kept the later take)"
            elif len(ta) < len(tb) and (len(ta) >= 2 or gap < 4) and \
                    (sim(ta, tb[:len(ta)]) >= 0.8 or (gap < 3 and len(ta) >= 3 and sim(ta, tb[:len(ta)]) >= 0.7)):
                why = "false start (stopped, then said it fully)"
            elif len(ta) >= 3 and len(tb) >= 3 and len(ta) <= len(tb) and sim(ta[-3:], tb[-3:]) >= 0.85 \
                    and gap < 15 and sim(ta, tb) >= 0.45:
                why = "retake (same ending said again, kept the later take)"
            if why:
                for i in A: drop.setdefault(i, why)
                break

    # pieces: runs of kept words, edges snapped to the real voice, never reaching into a cut word
    kept = [i for i in range(len(W)) if i not in drop]
    runs = []
    for i in kept:
        if runs and W[i][0] - W[runs[-1][-1]][1] < GAP and all(j not in drop for j in range(runs[-1][-1] + 1, i)):
            runs[-1].append(i)
        else:
            runs.append([i])
    pieces = []
    for r in runs:
        f, l = r[0], r[-1]
        lb = W[f - 1][1] if f > 0 else 0.0
        ub = W[l + 1][0] if l + 1 < len(W) else W[l][1] + 1.0
        s = max(V.onset(W[f][0]) - PRE, lb, 0.0)
        e = min(V.offset(W[l][1]) + POST, ub)
        # no real gap at an edge (an "uh" glued onto a word): cut at the quietest point instead of mid-sound
        if V.is_on(s) and f > 0:
            s = V.quietest(max(lb, s - 0.08), min(W[f][0] + 0.02, s + 0.06))
        if V.is_on(e) and l + 1 < len(W):
            e = V.quietest(max(W[l][1] - 0.02, e - 0.06), min(ub, e + 0.08)) + 0.01
        if e <= s: s, e = W[f][0], W[l][1]
        # a pause hiding inside one long whisper "word" still gets cut
        segs, a = [], s
        for x, y in V.silences(s + PRE, e - POST, GAP + 0.15):
            segs.append((a, x + POST)); a = y - PRE
        segs.append((a, e))
        for a, b in segs:
            if b - a < MIN_PIECE: continue
            text = " ".join(W[i][2] for i in r if a <= (W[i][0] + W[i][1]) / 2 <= b)
            if pieces and a <= pieces[-1]["out"] + 0.02:
                pieces[-1]["out"] = round(b, 3)
                pieces[-1]["text"] = (pieces[-1]["text"] + " " + text).strip()
            else:
                pieces.append({"clip": clip, "in": round(a, 3), "out": round(b, 3), "text": text})
    edl += pieces

    span_in = W[-1][1] - W[0][0]
    span_out = sum(p["out"] - p["in"] for p in pieces)
    kw = sum(1 for i in kept if tok[i])
    tot_in += span_in; tot_out += span_out; n_words_kept += kw
    rows = []
    i = 0
    while i < len(W):
        if i in drop:
            j = i
            while j + 1 < len(W) and drop.get(j + 1) == drop[i]: j += 1
            rows.append((W[i][0], f"- {tc(W[i][0])}  cut {drop[i]}: \"{' '.join(w[2] for w in W[i:j + 1])}\""))
            i = j + 1
        else:
            i += 1
    for p, q in zip(pieces, pieces[1:]):
        if q["in"] - p["out"] >= 1.0:
            rows.append((p["out"], f"- {tc(p['out'])}  cut {q['in'] - p['out']:.1f}s pause"))
    for i in kept:
        if W[i][3] < 0.4 and tok[i]:
            check.append(f"- {clip} {tc(W[i][0])}: \"{W[i][2]}\" (unsure what was said)")
    sheet.append(f"## {clip}\n{tc(span_in)} of talking -> {tc(span_out)}, {len(pieces)} pieces, "
                 f"{kw / span_out if span_out else 0:.1f} words/sec after cuts\n")
    sheet += [r for _, r in sorted(rows)] + [""]

(ws / "cuts").mkdir(exist_ok=True)
json.dump({"gap": GAP, "speed": 1.0, "keep": edl}, open(ws / "cuts/edl.json", "w"), indent=1)
wps = n_words_kept / tot_out if tot_out else 0
head = ["# Cut sheet", "",
        f"**{tc(tot_in)} -> {tc(tot_out)}** ({100 * (1 - tot_out / tot_in) if tot_in else 0:.0f}% cut), "
        f"{len(edl)} pieces, pace {wps:.1f} words/sec.", ""]
if wps and wps < 3.2:
    head += [f"Pace is on the slow side for snappy content (3.3-3.8 is typical). A speed of "
             f"{min(1.2, 3.5 / wps):.2f} would bring it to about 3.5. Ask before applying.", ""]
if check:
    head += ["## Check these words (low confidence, may be misheard or mumbled)"] + check + [""]
(ws / "cuts/cut_sheet.md").write_text("\n".join(head + sheet))
print(f"{len(edl)} pieces, {tc(tot_in)} -> {tc(tot_out)}, {wps:.1f} w/s. See cuts/cut_sheet.md")
