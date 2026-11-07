#!/usr/bin/env python3
"""Break a reference video down into shots, camera moves, pacing, audio beats and on-screen text.

    python3 analyze.py <video> [-o out_dir] [options]

Writes breakdown.json, breakdown.md, contact_sheet.jpg, shots_detail.jpg and timeline.png.
Runs on python3 + numpy. Uses OpenCV if this Python has it (see setup.sh), a Whisper package for a
transcript if one is installed, and tesseract for on-screen text if it is on the PATH.
"""
import argparse, json, sys, time
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common, cuts, motion, audio, sheets, ocr  # noqa: E402
from common import tc  # noqa: E402


def build_shots(bounds, n, fps):
    starts = [0] + [b["frame"] for b in bounds]
    ends = [b["frame"] for b in bounds] + [n]
    shots = []
    for i, (s, e) in enumerate(zip(starts, ends)):
        b = bounds[i - 1] if i else None
        shots.append({
            "index": i + 1,
            "start_frame": s, "end_frame": e,
            "start": round(s / fps, 3), "end": round(e / fps, 3),
            "duration": round((e - s) / fps, 3),
            "mid_frame": (s + e - 1) // 2,
            "transition_in": b["kind"] if b else "start",
            "transition_frames": b.get("frames", 0) if b else 0,
            "cut_strength": b.get("score") if b else None,
        })
    return shots


def pacing(shots, duration, win=2.0):
    d = np.array([s["duration"] for s in shots])
    cut_t = np.array([s["start"] for s in shots[1:]])
    bins = np.arange(0, duration + win, win)
    counts = np.histogram(cut_t, bins=bins)[0] if len(cut_t) else np.zeros(len(bins) - 1, int)
    third = duration / 3
    def asl(a, b):
        sel = [s["duration"] for s in shots if a <= s["start"] < b]
        return round(float(np.mean(sel)), 2) if sel else None
    return {
        "shots": len(shots),
        "cuts": len(cut_t),
        "avg_shot_s": round(float(d.mean()), 2),
        "median_shot_s": round(float(np.median(d)), 2),
        "shortest_s": round(float(d.min()), 2),
        "longest_s": round(float(d.max()), 2),
        "cuts_per_s": round(len(cut_t) / duration, 2) if duration else 0,
        "window_s": win,
        "cuts_per_window": counts.tolist(),
        "avg_shot_by_third_s": [asl(0, third), asl(third, 2 * third), asl(2 * third, duration + 1)],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("-o", "--out", help="output folder (default: ./<name>-breakdown)")
    ap.add_argument("--sensitivity", type=float, default=1.0, help="cut detection: >1 finds more cuts, <1 fewer")
    ap.add_argument("--min-shot", type=float, default=0.1, help="shortest real shot in seconds; closer cuts are merged or called flashes")
    ap.add_argument("--tol-ms", type=float, default=0, help="a cut counts as on the beat within this many ms (default: 1.5 frames, at least 50)")
    ap.add_argument("--motion", choices=["auto", "numpy", "opencv"], default="auto")
    ap.add_argument("--max-fps", type=float, default=30, help="analyse at most this frame rate")
    ap.add_argument("--no-transcript", action="store_true")
    ap.add_argument("--no-ocr", action="store_true")
    a = ap.parse_args()

    src = Path(a.video).expanduser().resolve()
    if not src.exists():
        sys.exit(f"not found: {src}")
    out = Path(a.out).expanduser().resolve() if a.out else Path.cwd() / f"{src.stem}-breakdown"
    if out == src.parent:
        sys.exit("pick an output folder that is not the video's own folder")
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    log = lambda m: print(f"[{time.time() - t0:5.1f}s] {m}", flush=True)

    inf = common.info(src)
    fps = min(inf["fps"], a.max_fps)
    aw, ah = common.analysis_size(inf["width"], inf["height"], 180)
    log(f"{src.name}: {inf['width']}x{inf['height']} {inf['fps']:.3g} fps {inf['duration']:.2f}s; analysing at {aw}x{ah} {fps:g} fps")
    frames = common.decode_frames(src, aw, ah, fps=fps)
    n = len(frames)
    duration = n / fps

    # shots
    bounds, flashes, sig = cuts.detect(frames, fps, a.sensitivity, a.min_shot)
    shots = build_shots(bounds, n, fps)
    log(f"{len(shots)} shots ({sum(b['kind'] == 'cut' for b in bounds)} hard cuts, "
        f"{sum(b['kind'] != 'cut' for b in bounds)} gradual), {len(flashes)} flash frames")

    # camera motion per shot, skipping frames inside a dissolve
    gray = sig["luma"]
    est = motion.estimator(ah, aw, a.motion)
    for i, s in enumerate(shots):
        lo = s["start_frame"] + (s["transition_frames"] + 1) // 2
        nxt = shots[i + 1] if i + 1 < len(shots) else None
        hi = s["end_frame"] - ((nxt["transition_frames"] + 1) // 2 if nxt else 0)
        if hi - lo < 3:
            lo, hi = s["start_frame"], s["end_frame"]
        s["clean_start"], s["clean_end"] = lo, hi  # the part of the shot outside any dissolve
        rows, rate, k = motion.track_shot(gray, lo, hi, est, fps)
        cam = motion.describe(rows, aw, ah, fps, sad=sig["sad"][lo + 1:hi], k=k)
        # long shots often hold several moves in a row: describe each phase too
        phases = motion.segments(rows, aw, ah, fps, k) if (hi - lo) / fps >= 1.2 else [(0, len(rows))]
        if len(phases) > 1:
            cam["phases"] = []
            for p0, p1 in phases:
                ph = motion.describe(rows[p0:p1], aw, ah, fps, k=k)
                cam["phases"].append({"start": round((lo + p0 * k) / fps, 2), "end": round((lo + p1 * k) / fps, 2),
                                      **{x: ph[x] for x in ("label", "pan_pct_width_per_s", "tilt_pct_height_per_s",
                                                            "zoom_total", "roll_total_deg", "speed_profile")}})
            labels = [ph["label"] for ph in cam["phases"]]
            if len(set(labels)) > 1:
                cam["summary"] = cam["label"]
                cam["label"] = " > ".join(labels)
        # a compact camera path (cumulative, at the sampling rate) for keyframing a remake
        if len(rows):
            cam["path"] = {
                "t": [round(s["start"] + (lo - s["start_frame"] + j * k) / fps, 3) for j in range(1, len(rows) + 1)],
                "pan_pct_width": np.round(-np.cumsum(rows[:, 0] * k) / aw * 100, 2).tolist(),
                "tilt_pct_height": np.round(-np.cumsum(rows[:, 1] * k) / ah * 100, 2).tolist(),
                "zoom": np.round(np.exp(np.cumsum(np.log1p(rows[:, 2]) * k)), 4).tolist(),
            }
        s["camera"] = cam
        s["motion_energy"] = round(float(np.median(sig["sad"][lo + 1:hi])) if hi - lo > 1 else 0.0, 2)
        s["brightness"] = round(float(gray[s["mid_frame"]].mean() / 2.55), 1)
        s["colour_rgb"] = [int(v) for v in frames[s["mid_frame"]].reshape(-1, 3).mean(axis=0)]
    log(f"camera motion: {est.name}")

    # audio
    tol = (a.tol_ms / 1000) if a.tol_ms else max(0.05, 1.5 / fps)
    aud = {"present": False}
    cut_times = [s["start"] for s in shots[1:]]
    if inf["audio"]:
        y = common.decode_audio(src)
        if y is not None and len(y) > audio.SR * 0.5 and np.abs(y).max() > 1e-4:
            env = audio.onset_envelope(y)
            on_t = audio.refine(y, audio.frame_time(audio.pick_peaks(env)))
            bpm, clarity = audio.tempo(env)
            bt = audio.refine(y, audio.frame_time(audio.beats(env, bpm))) if clarity > 0.1 else np.array([])
            loud = audio.loudness(src)
            hop = int(audio.SR * 0.02)
            rms = np.sqrt(np.convolve(y.astype(np.float64) ** 2, np.ones(hop) / hop, mode="valid")[::hop])
            loud_start = np.flatnonzero(rms > 10 ** (-40 / 20))
            sound_start = round(float(loud_start[0] * hop / audio.SR), 2) if len(loud_start) else None
            aud = {
                "present": True,
                "sound_starts_s": sound_start,
                "bpm": round(bpm, 1) if clarity > 0.1 else None,
                "pulse_clarity": round(clarity, 2),
                "onsets": [round(float(t), 3) for t in on_t],
                "beats": [round(float(t), 3) for t in bt],
                "loudness": {k: (np.round(v, 1).tolist() if isinstance(v, list) else v) for k, v in loud.items()},
                "loudness_hits": audio.hits(loud["t"], loud["momentary"]),
                "tolerance_ms": round(tol * 1000),
                "cuts_vs_onsets": audio.align(cut_times, on_t, tol, duration),
                "cuts_vs_beats": audio.align(cut_times, bt, tol, duration) if len(bt) else None,
            }
            log(f"audio: {len(on_t)} onsets, tempo {aud['bpm']} BPM (clarity {clarity:.2f}), "
                f"{aud['cuts_vs_onsets']['hit_pct']}% of cuts within {tol * 1000:.0f} ms of an onset "
                f"(chance {aud['cuts_vs_onsets']['chance_pct']}%)")
        if not a.no_transcript:
            try:
                tr = audio.transcript(src)
            except Exception as e:  # a broken model download should not sink the breakdown
                tr = None
                log(f"transcript failed: {e}")
            aud["transcript"] = tr
            log("transcript: " + (tr["backend"] if tr else "skipped (no Whisper package in this Python)"))

    # on-screen text
    text = None
    if not a.no_ocr:
        text = ocr.text_spans(src)
        log("on-screen text: " + ("skipped (tesseract not installed)" if text is None else f"{len(text)} spans"))

    # per-shot audio notes
    onsets = np.array(aud.get("onsets", []))
    beats = np.array(aud.get("beats", []))
    ref = beats if len(beats) else onsets
    on_beat = []
    for s in shots:
        note = {}
        if aud.get("present"):
            if s["index"] > 1 and len(ref):
                d = s["start"] - ref[np.argmin(np.abs(ref - s["start"]))]
                note["cut_offset_ms"] = round(d * 1000)
                note["cut_on_beat"] = bool(abs(d) <= tol)
                if len(onsets):
                    note["cut_on_onset"] = bool(np.min(np.abs(onsets - s["start"])) <= tol)
                on_beat.append(note["cut_on_beat"] or note.get("cut_on_onset", False))
            note["onsets_in_shot"] = int(((onsets >= s["start"]) & (onsets < s["end"])).sum())
            lt, lm = np.array(aud["loudness"]["t"]), np.array(aud["loudness"]["momentary"])
            sel = (lt >= s["start"]) & (lt < s["end"] + 0.05)
            note["loudness_lufs"] = round(float(lm[sel].mean()), 1) if sel.any() else None
            note["hits"] = [h for h in aud["loudness_hits"] if s["start"] <= h < s["end"]]
            tr = aud.get("transcript")
            if tr:
                note["words"] = " ".join(w["word"] for w in tr["words"] if s["start"] <= w["start"] < s["end"])
        elif s["index"] > 1:
            on_beat.append(False)
        if text:
            note["text_on_screen"] = [t["text"] for t in text if t["start"] < s["end"] and t["end"] > s["start"]]
        s["audio"] = note

    pace = pacing(shots, duration)

    # pictures
    sheet = sheets.contact_sheet(src, fps, shots, inf["width"], inf["height"], str(out / "contact_sheet"))
    detail = sheets.detail_sheet(src, fps, shots, inf["width"], inf["height"], str(out / "shots_detail"))
    tl = sheets.timeline(frames, fps, duration, shots, bounds, aud.get("loudness", {}), aud.get("onsets", []),
                         aud.get("beats", []), on_beat, str(out / "timeline.png"))
    log("pictures: " + ", ".join(Path(p).name for p in sheet + detail + [tl]))

    data = {
        "source": {"file": src.name, **inf, "analysis_fps": fps, "analysis_size": [aw, ah]},
        "motion_estimator": est.name,
        "pacing": pace,
        "shots": shots,
        "flash_frames": [{"time": round(f["frame"] / fps, 3), "frames": f["frames"]} for f in flashes],
        "audio": aud,
        "on_screen_text": text,
        "hook": hook(shots, aud, text, duration),
        "images": {"contact_sheet": [Path(p).name for p in sheet], "shots_detail": [Path(p).name for p in detail],
                   "timeline": Path(tl).name},
    }
    (out / "breakdown.json").write_text(json.dumps(data, indent=1))
    (out / "breakdown.md").write_text(markdown(data))
    log(f"done: {out}")


def hook(shots, aud, text, duration, span=2.0):
    """Measured facts about the first two seconds. The judgement is left to the reader."""
    first = [s for s in shots if s["start"] < span]
    h = {
        "first_cut_s": shots[1]["start"] if len(shots) > 1 else None,
        "cuts_in_first_2s": max(0, len(first) - 1),
        "shot_1_camera": shots[0]["camera"]["label"],
        "motion_first_2s_vs_rest": None,
    }
    e_first = [s["motion_energy"] for s in first]
    e_rest = [s["motion_energy"] for s in shots if s["start"] >= span]
    if e_first and e_rest:
        h["motion_first_2s_vs_rest"] = round(float(np.mean(e_first)) / (float(np.median(e_rest)) + 0.1), 2)
    if aud.get("present"):
        lt, lm = np.array(aud["loudness"]["t"]), np.array(aud["loudness"]["momentary"])
        h["sound_starts_s"] = aud.get("sound_starts_s")
        h["first_onset_s"] = aud["onsets"][0] if aud["onsets"] else None
        h["loudness_first_2s_lufs"] = round(float(lm[lt <= span].mean()), 1) if (lt <= span).any() else None
        h["loudness_rest_lufs"] = round(float(lm[lt > span].mean()), 1) if (lt > span).any() else None
        tr = aud.get("transcript")
        if tr:
            h["first_words"] = " ".join(w["word"] for w in tr["words"] if w["start"] < span)
            h["first_word_s"] = round(tr["words"][0]["start"], 2) if tr["words"] else None
    if text:
        h["text_first_2s"] = [t["text"] for t in text if t["start"] < span]
    return h


def cam_short(c):
    if c.get("phases"):
        return "; ".join(f"{tc(p['start'])} {p['label']}" + (f" (x{p['zoom_total']})" if p["zoom_total"] != 1 and "zoom" in p["label"] or "push" in p["label"] or "pull" in p["label"] else "")
                         for p in c["phases"])
    lab = c["label"]
    bits = []
    if any(m.startswith("pan") for m in c.get("moves", [])):
        bits.append(f"{abs(c['pan_pct_width_per_s'])}%W/s")
    if any(m.startswith("tilt") for m in c.get("moves", [])):
        bits.append(f"{abs(c['tilt_pct_height_per_s'])}%H/s")
    if any(m.split()[0] in ("zoom", "push", "pull") for m in c.get("moves", [])):
        bits.append(f"x{c['zoom_total']}")
    if c.get("speed_profile") and c.get("moves"):
        bits.append(c["speed_profile"])
    if c.get("subject_motion"):
        bits.append(c["subject_motion"])
    return lab + (f" ({', '.join(bits)})" if bits else "")


def markdown(d):
    src, p, aud, h = d["source"], d["pacing"], d["audio"], d["hook"]
    L = []
    w = L.append
    w(f"# Breakdown: {src['file']}\n")
    w(f"{src['width']}x{src['height']}, {src['fps']:g} fps, {src['duration']:.2f} s. "
      f"Camera motion measured with {d['motion_estimator']}.\n")
    w("Pictures: " + ", ".join(f"[{x}]({x})" for x in d["images"]["contact_sheet"] + d["images"]["shots_detail"]
                               + [d["images"]["timeline"]]) + "\n")
    w("## At a glance\n")
    w(f"- **{p['shots']} shots**, average {p['avg_shot_s']} s (median {p['median_shot_s']} s, "
      f"shortest {p['shortest_s']} s, longest {p['longest_s']} s), {p['cuts_per_s']} cuts per second.")
    thirds = p["avg_shot_by_third_s"]
    w(f"- Average shot length by thirds of the video: {' / '.join(str(x) if x is not None else '-' for x in thirds)} s.")
    w(f"- Cuts per {p['window_s']:g} s window: {' '.join(map(str, p['cuts_per_window']))}")
    if d["flash_frames"]:
        w(f"- Flash frames at: {', '.join(tc(f['time']) for f in d['flash_frames'])}")
    if aud.get("present"):
        co, cb = aud["cuts_vs_onsets"], aud.get("cuts_vs_beats")
        w(f"- Music: {str(aud['bpm']) + ' BPM' if aud['bpm'] else 'no steady beat'} (pulse clarity {aud['pulse_clarity']}), "
          f"{len(aud['onsets'])} onsets, integrated loudness {aud['loudness']['integrated_lufs']} LUFS, "
          f"range {aud['loudness']['range_lu']} LU.")
        if co["hit_pct"] is not None:
            w(f"- **Cuts on sound:** {co['hit_pct']}% of cuts land within {aud['tolerance_ms']} ms of an onset; random cuts would "
              f"score about {co['chance_pct']}%." + (f" Typical offset {co['median_offset_ms']} ms (negative = cut comes before the sound)." if co["median_offset_ms"] is not None else ""))
        if cb and cb["hit_pct"] is not None:
            w(f"- **Cuts on the beat grid:** {cb['hit_pct']}% (chance {cb['chance_pct']}%).")
    else:
        w("- No usable audio.")
    w("")
    w("## Hook: the first 2 seconds (measured)\n")
    for k, v in h.items():
        if v is not None and not (isinstance(v, (list, str)) and len(v) == 0):
            w(f"- {k.replace('_', ' ')}: {v}")
    w("\n**Read (fill in after looking at the contact sheet):** what is the promise or question in frame one, "
      "what changes before 0.5 s, and what makes someone stay to 2 s?\n")
    w("## Beat map\n")
    w("| # | time | length | in | camera | what happens | audio |")
    w("|---|------|--------|----|--------|--------------|-------|")
    for s in d["shots"]:
        a = s.get("audio", {})
        bits = []
        if "cut_offset_ms" in a:
            bits.append(("on beat" if a["cut_on_beat"] else "off beat") + f" ({a['cut_offset_ms']:+d} ms)")
        if a.get("hits"):
            bits.append("loudness jump " + ", ".join(tc(x) for x in a["hits"]))
        if a.get("loudness_lufs") is not None:
            bits.append(f"{a['loudness_lufs']} LUFS")
        if a.get("words"):
            bits.append(f"\"{a['words']}\"")
        if a.get("text_on_screen"):
            bits.append("text: " + " / ".join(a["text_on_screen"]))
        tin = s["transition_in"] + (f" {s['transition_frames']}f" if s["transition_frames"] else "")
        w(f"| {s['index']} | {tc(s['start'])} | {s['duration']:.2f}s | {tin} | {cam_short(s['camera'])} | _ | {'; '.join(bits)} |")
    w("")
    if aud.get("transcript"):
        w("## Transcript\n")
        w(aud["transcript"]["text"] + "\n")
    if d["on_screen_text"]:
        w("## On-screen text\n")
        for t in d["on_screen_text"]:
            w(f"- {tc(t['start'])} to {tc(t['end'])}: {t['text']}")
        w("")
    elif d["on_screen_text"] is None:
        w("On-screen text was not checked (tesseract is not installed).\n")
    w("## Remake plan\n")
    w("_Fill this in after looking at the contact sheet, the detail sheet and the timeline. Use the measured numbers above; "
      "do not guess timings._\n")
    w(f"1. **Format:** {src['width']}x{src['height']} at {src['fps']:g} fps, {src['duration']:.2f} s.")
    w("2. **What makes it work:** the two or three things a viewer would miss if they were gone.")
    w("3. **Shot list to build:** one line per shot: what is in frame, lens and camera height, start and end camera "
      "position (use the camera path in breakdown.json for keyframes), easing.")
    w("4. **Timing:** cut times to copy exactly, and which ones sit on the beat.")
    w("5. **Audio:** music tempo, where the loudness jumps are, voice or sound effects to match.")
    w("6. **Text and graphics:** what appears, when, and for how long.")
    w("7. **Check:** render, then compare side by side with `compare.py` at the same timestamps and fix the biggest difference first.")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main()
