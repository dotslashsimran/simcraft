#!/usr/bin/env python3
"""Compare a remake with its reference at matching timestamps.

Video (always re-encoded, both clips put on the same frame rate so they cannot drift):
    python3 compare.py ref.mp4 remake.mp4 -o compare.mp4 [--mode side|stack|blend|diff]
Stills (frame pairs you can look at as one image, at each reference shot's middle by default):
    python3 compare.py ref.mp4 remake.mp4 --stills compare.jpg [--breakdown breakdown.json | --at 0.5 1.2 3]

Modes: side = left/right, stack = top/bottom (good for vertical posts), blend = 50/50 overlay
(lines up horizons and edges), diff = difference (black where the two match).
--ref-start / --remake-start line the clips up if one has a lead-in. A shorter remake holds its
last frame, or repeats with --loop-remake.
"""
import argparse, json, sys
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
from common import font_file, esc, run, tc  # noqa: E402
from sheets import _save  # noqa: E402


def label(name, font, size):
    if not font:
        return ""
    ff = esc(font)
    return (f",drawtext=fontfile='{ff}':text='{esc(name)}':x=12:y=12:fontsize={size}:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=6:expansion=none"
            f",drawtext=fontfile='{ff}':text='%{{pts\\:hms}}':x=w-tw-12:y=12:fontsize={size}:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=6")


def video(a):
    ri, mi = common.info(a.ref), common.info(a.remake)
    fps = a.fps or ri["fps"]
    dur = a.duration or max(ri["duration"] - a.ref_start, 0.1)
    font = font_file() if not a.no_labels else None
    rw, rh = ri["width"], ri["height"]
    if a.mode == "side":
        H = a.size or min(rh, 1080)
        H -= H % 2
        norm = f"scale=-2:{H}:flags=lanczos,setsar=1"
    elif a.mode == "stack":
        W = a.size or min(rw, 1080)
        W -= W % 2
        norm = f"scale={W}:-2:flags=lanczos,setsar=1"
    else:
        W, H = rw - rw % 2, rh - rh % 2
        norm = f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    size = 28 if a.mode in ("side", "stack") else 32
    hold = f",tpad=stop_mode=clone:stop_duration={dur:.3f}"
    r_chain = f"[0:v]fps={fps},setpts=PTS-STARTPTS,{norm}{hold}"
    m_chain = f"[1:v]fps={fps},setpts=PTS-STARTPTS,{norm}{hold}"
    if a.mode in ("side", "stack"):
        g = (f"{r_chain}{label(a.ref_label, font, size)}[r];{m_chain}{label(a.remake_label, font, size)}[m];"
             f"[r][m]{'hstack' if a.mode == 'side' else 'vstack'}=inputs=2,format=yuv420p[v]")
    else:
        # blend averages in YUV; diff compares brightness only (gray), which is what alignment needs
        fmt, bm = ("yuv420p", "average") if a.mode == "blend" else ("gray", "difference")
        g = (f"{r_chain},format={fmt}[r];{m_chain},format={fmt}[m];[r][m]blend=all_mode={bm},format=yuv420p"
             f"{label(a.mode + ': ' + a.ref_label + ' + ' + a.remake_label, font, size)}[v]")
    args = ["-y", "-ss", str(a.ref_start), "-t", f"{dur:.3f}", "-i", str(a.ref)]
    if a.loop_remake:
        args += ["-stream_loop", "-1"]  # fine here: the output is re-encoded, never stream-copied
    args += ["-ss", str(a.remake_start), "-i", str(a.remake), "-filter_complex", g, "-map", "[v]"]
    src_audio = {"ref": (0, ri), "remake": (1, mi)}.get(a.audio)
    if src_audio and src_audio[1]["audio"]:
        args += ["-map", f"{src_audio[0]}:a:0", "-c:a", "aac", "-b:a", "192k"]
    args += ["-t", f"{dur:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
             "-r", f"{fps}", "-movflags", "+faststart", str(a.out)]
    run(args)
    print(f"wrote {a.out} ({a.mode}, {dur:.2f} s at {fps:g} fps)")


def stills(a):
    ri, mi = common.info(a.ref), common.info(a.remake)
    if a.at:
        times = a.at
    elif a.breakdown:
        d = json.loads(Path(a.breakdown).read_text())
        times = [round((s["start"] + s["end"]) / 2, 3) for s in d["shots"]]
    else:
        times = list(np.round(np.linspace(0.1, ri["duration"] - 0.1, 8), 2))
    tw = 480 if ri["width"] >= ri["height"] else 270
    th = int(round(tw * ri["height"] / ri["width"] / 2) * 2)
    fps = a.fps or ri["fps"]
    rf = [int(round((t + a.ref_start) * fps)) for t in times]
    mf = [int(round((t + a.remake_start) * fps)) for t in times]
    R, rgot = common.grab(a.ref, fps, rf, tw, th)
    M, mgot = common.grab(a.remake, fps, mf, tw, th, fit=True)
    rmap, mmap = dict(zip(rgot, R)), dict(zip(mgot, M))
    pad, lab = 6, 24
    W = pad + 2 * (tw + pad)
    H = pad + len(times) * (th + lab + pad)
    img = np.full((H, W, 3), 18, np.uint8)
    texts = []
    for i, t in enumerate(times):
        y = pad + i * (th + lab + pad)
        for c, (fmap, f, name) in enumerate([(rmap, rf[i], a.ref_label), (mmap, mf[i], a.remake_label)]):
            x = pad + c * (tw + pad)
            if f in fmap:
                img[y + lab:y + lab + th, x:x + tw] = fmap[f]
            texts.append((x + 2, y + 4, 15, "white", f"{name}  {tc(t)}" + ("" if f in fmap else "  (no frame: clip has ended)")))
    _save(img, a.stills, texts)
    print(f"wrote {a.stills} ({len(times)} pairs)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ref")
    ap.add_argument("remake")
    ap.add_argument("-o", "--out", help="comparison video path")
    ap.add_argument("--mode", choices=["side", "stack", "blend", "diff"], default="side")
    ap.add_argument("--stills", help="write a still-pairs image (jpg/png) instead of, or as well as, a video")
    ap.add_argument("--breakdown", help="breakdown.json: take still times from the middle of each shot")
    ap.add_argument("--at", type=float, nargs="+", help="still times in seconds (reference clock)")
    ap.add_argument("--ref-start", type=float, default=0.0)
    ap.add_argument("--remake-start", type=float, default=0.0)
    ap.add_argument("--duration", type=float, help="default: the rest of the reference")
    ap.add_argument("--fps", type=float, help="default: the reference frame rate")
    ap.add_argument("--size", type=int, help="panel height (side) or width (stack)")
    ap.add_argument("--audio", choices=["ref", "remake", "none"], default="ref")
    ap.add_argument("--loop-remake", action="store_true", help="repeat a shorter remake instead of holding its last frame")
    ap.add_argument("--ref-label", default="reference")
    ap.add_argument("--remake-label", default="remake")
    ap.add_argument("--no-labels", action="store_true")
    a = ap.parse_args()
    a.ref, a.remake = Path(a.ref).expanduser(), Path(a.remake).expanduser()
    if not a.out and not a.stills:
        a.out = "compare.mp4"
    if a.out:
        video(a)
    if a.stills:
        stills(a)


if __name__ == "__main__":
    main()
