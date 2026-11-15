"""Final export: one encode that matches the source (or a platform), keeps HDR or tone-maps it properly, burns captions
and swaps in the final mix in the same pass, then checks the file it wrote.

Usage:
  python export.py <video> [--like <original camera file>] [--preset match|youtube|reels|sdr] [--ass <captions.ass>]
                   [--audio <mix.mp4|.wav>] [--fps <n>] [--out <path>] [--crf <n>]
  <video>     the edited video (e.g. rough-cut's final render, or final-mix's _mix.mp4)
  --like      the camera original to match (codec, bit depth, colour, frame rate, bitrate). Default: <video> itself.
  --preset    match   same codec family, bit depth, colour (HDR stays HDR), frame rate, and at least the source bitrate
              youtube H.264 SDR or HEVC HDR as the source is, 48 kHz AAC 320k, generous bitrate ceiling
              reels   Instagram Reels / TikTok / Shorts: H.264 8-bit SDR (HDR tone-mapped), max 1080 wide, <= 30 fps* ,
                      AAC 256k. *60 fps kept if the source is 60.
              sdr     like match, but HDR is tone-mapped to SDR Rec.709
  --ass       captions from the captions skill, burned in during this encode
  --audio     replace the audio (final-mix output); the video timeline is the reference, audio is padded/trimmed to it

Writes <name>_<preset>.mp4 and <name>_<preset>.md (target vs what came out). Never touches the inputs.

Why one encode: every lossy encode costs quality. Captions and the music mix go in here, at the end, not as extra
renders. Why the HDR handling: phone footage is usually 10-bit HLG. Squeezing it to 8-bit while still tagging it HLG
bands the sky and skin, and platforms that don't do HDR show it washed out. So HDR is either kept as real 10-bit
HEVC HDR, or tone-mapped to SDR on purpose.
"""
import subprocess, sys
from pathlib import Path
from media import ffmpeg, probe, loudness

arg = lambda k, d=None: sys.argv[sys.argv.index(k) + 1] if k in sys.argv else d
if len(sys.argv) < 2 or sys.argv[1].startswith("--"):
    raise SystemExit(__doc__)
video = Path(sys.argv[1]).expanduser()
like = Path(arg("--like", video)).expanduser()
preset = arg("--preset", "match")
if preset not in ("match", "youtube", "reels", "sdr"):
    raise SystemExit("--preset must be match, youtube, reels or sdr")
ass = Path(arg("--ass")).expanduser() if arg("--ass") else None
audio = Path(arg("--audio")).expanduser() if arg("--audio") else None
FF = ffmpeg()
vin, src = probe(video), probe(like)
V, S = vin["video"], src["video"]
if not V or not S:
    raise SystemExit("both the video and --like need a video stream")
notes, warn = [], []

# ---------- target ----------
STD = [23.976, 24, 25, 29.97, 30, 48, 50, 59.94, 60, 120]
snap = lambda f: min(STD, key=lambda s: abs(s - f))
fps = float(arg("--fps", 0)) or snap(S["fps"] or V["fps"] or 30)
if S["vfr"]:
    notes.append(f"source is variable frame rate (phone footage): output is constant {fps:g} fps")
hdr_in = V["hdr"]  # what the edited video actually carries
if S["hdr"] and not hdr_in:
    warn.append(f"the original is {S['hdr'].upper()} HDR but this video is SDR-tagged: an earlier render dropped HDR, "
                "so it is exported as SDR")
keep_hdr = bool(hdr_in) and preset in ("match", "youtube")
if hdr_in and not keep_hdr:
    notes.append(f"{hdr_in.upper()} HDR tone-mapped to SDR Rec.709 ({preset})")
if keep_hdr and ass:
    warn.append("captions burned into HDR render at full HDR white (very bright on HDR screens); use --preset sdr if "
                "that looks harsh")
fr = {23.976: "24000/1001", 29.97: "30000/1001", 59.94: "60000/1001"}.get(fps, f"{fps:g}")
w, h = V["w"], V["h"]
if preset == "reels":
    if fps > 30 and abs(fps - 60) > 0.1 and abs(fps - 59.94) > 0.1:
        fps, fr = 30, "30"
    if min(w, h) > 1080:
        s = 1080 / min(w, h); w, h = round(w * s / 2) * 2, round(h * s / 2) * 2
        notes.append(f"scaled to {w}x{h} (platforms re-encode anything bigger)")
codec = "hevc" if keep_hdr or (preset == "match" and S["codec"] == "hevc") else "h264"
tenbit = keep_hdr or (preset == "match" and S["bit_depth"] >= 10)
if codec == "h264" and tenbit:
    codec = "hevc"  # 10-bit H.264 plays almost nowhere

# bitrate ceiling: never starve it below the source, never let CRF spike past what platforms ingest
px = w * h * fps
floor = {"youtube": 0.12, "reels": 0.10}.get(preset, 0.0) * px  # bits/pixel/frame -> bits/s
src_v = S["bit_rate"] or src["bit_rate"]
maxrate = int(max(src_v * 1.2 if preset in ("match", "sdr") else 0, floor, 4e6))
if preset == "reels":
    maxrate = min(maxrate, 25_000_000)
crf = arg("--crf", "18" if codec == "h264" else "20")

# ---------- filters ----------
vf = []
if keep_hdr:
    pass  # stays in its own colour space; x265 gets the tags below
elif hdr_in:
    tin = "arib-std-b67" if hdr_in == "hlg" else "smpte2084"
    # tag the frames first: zscale ignores tin/min hints on partly tagged input and fails with "no path between colorspaces"
    vf += [f"setparams=color_primaries=bt2020:color_trc={tin}:colorspace=bt2020nc:range=tv",
           "zscale=t=linear:npl=100", "format=gbrpf32le",
           "zscale=p=bt709", "tonemap=mobius:param=0.8:desat=0", "zscale=t=bt709:m=bt709:r=tv"]
    # mobius 0.8: SDR-range tones pass nearly unchanged, only highlights above roll off. Round-trip test (SDR -> HLG ->
    # back): mobius 28.8 dB PSNR, hard clip 30.5, hable 13.0 (hable darkens everything by ~1.5 stops).
if (w, h) != (V["w"], V["h"]):
    vf.append(f"scale={w}:{h}:flags=lanczos")
if ass:
    vf.append("ass=" + str(ass).replace("\\", "/").replace(":", "\\:").replace("'", "\\'"))
vf += [f"fps={fr}", "format=" + ("yuv420p10le" if tenbit else "yuv420p")]

if codec == "hevc":
    xp = ["log-level=error"]
    if keep_hdr:
        trc = "arib-std-b67" if hdr_in == "hlg" else "smpte2084"
        xp += ["colorprim=bt2020", f"transfer={trc}", "colormatrix=bt2020nc", "range=limited"]
        if hdr_in == "pq":
            xp += ["hdr10=1", "hdr10-opt=1"]
            warn.append("PQ/HDR10 source: mastering-display metadata is not copied; most players cope, but check on the target device")
    else:
        xp += ["colorprim=bt709", "transfer=bt709", "colormatrix=bt709"]
    venc = ["-c:v", "libx265", "-preset", "slow", "-crf", crf, "-tag:v", "hvc1",
            "-profile:v", "main10" if tenbit else "main", "-x265-params", ":".join(xp)]
else:
    venc = ["-c:v", "libx264", "-preset", "slow", "-crf", crf, "-profile:v", "high"]
venc += ["-maxrate", str(maxrate), "-bufsize", str(maxrate * 2)]
tags = (["-color_primaries", "bt2020", "-color_trc", "arib-std-b67" if hdr_in == "hlg" else "smpte2084",
         "-colorspace", "bt2020nc"] if keep_hdr else ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"])
tags += ["-color_range", "tv"]

A = (probe(audio)["audio"] if audio else vin["audio"])
abr = {"youtube": "320k", "reels": "256k"}.get(preset) or f"{max(256, (src['audio'] or {}).get('bit_rate', 0) // 1000)}k"
ains = ["-i", str(audio)] if audio else []
amap = ["-map", "1:a:0"] if audio else (["-map", "0:a:0"] if A else [])
D = vin["duration"]
n = round(D * 48000)
aenc = (["-af", f"aresample=48000,apad=whole_len={n},atrim=end_sample={n}", "-c:a", "aac", "-b:a", abr, "-ar", "48000"]
        if A else [])

out = Path(arg("--out", video.with_name(f"{video.stem}_{preset}.mp4"))).expanduser()
cmd = [FF, "-v", "error", "-y", "-i", str(video), *ains, "-map", "0:v:0", *amap, "-vf", ",".join(vf), *venc, *tags,
       *aenc, "-movflags", "+faststart", "-map_metadata", "-1", str(out)]
subprocess.run(cmd, check=True)

# ---------- check what came out ----------
o = probe(out); OV, OA = o["video"], o["audio"]
want_trc = ("arib-std-b67" if hdr_in == "hlg" else "smpte2084") if keep_hdr else "bt709"
checks = [("codec", codec, OV["codec"]), ("bit depth", 10 if tenbit else 8, OV["bit_depth"]),
          ("transfer", want_trc, OV["color_transfer"]), ("size", f"{w}x{h}", f"{OV['w']}x{OV['h']}"),
          ("frame rate", round(fps, 3), round(OV["fps"], 3))]
same = lambda a, b: abs(a - b) < 0.01 if isinstance(a, (int, float)) and isinstance(b, (int, float)) else str(a) == str(b)
bad = [c for c in checks if not same(c[1], c[2])]
vdur = o["duration"]
lines = [f"# Export: {out.name} ({preset})", "",
         f"- matched to {like.name}: {S['codec']} {S['bit_depth']}-bit {S['hdr'] or 'SDR'} {S['w']}x{S['h']} "
         f"{S['fps']:.3f} fps{' VFR' if S['vfr'] else ''}, {src_v / 1e6:.1f} Mb/s",
         f"- wrote {OV['codec']} {OV['bit_depth']}-bit {OV['color_transfer']} {OV['w']}x{OV['h']} {OV['fps']:.3f} fps, "
         f"{o['bit_rate'] / 1e6:.1f} Mb/s total (CRF {crf}, ceiling {maxrate / 1e6:.0f} Mb/s)"]
lines += [f"- {x}" for x in notes]
lines.append(f"- duration {vdur:.3f}s vs input {D:.3f}s ({'OK' if abs(vdur - D) <= 1.5 / fps else 'MISMATCH'})")
if OA:
    L = loudness(out)
    lines.append(f"- audio {OA['codec']} {OA['sample_rate']} Hz, {L['I']:.1f} LUFS, true peak {L['TP']:.1f} dBTP"
                 + ("  <- peak over -1 dBTP after AAC encoding; re-run final-mix with --target 0.5 lower" if L["TP"] > -1.0 else ""))
if ass:
    lines.append(f"- captions burned in from {ass.name}")
lines += [f"- **{x}**" for x in warn]
lines += ([f"- **{k}: wanted {a}, got {b}**" for k, a, b in bad] or ["- checks: codec, bit depth, colour, size and frame rate as intended"])
out.with_suffix(".md").write_text("\n".join(lines) + "\n")
print("\n".join(lines))
