"""Turn a matplotlib drawing function into an mp4, one PNG frame at a time.

Use it from your own script:

    import sys; sys.path.insert(0, "<this skill>/scripts")
    from animate import animate

    def draw(fig, i):           # draw frame i onto an empty figure
        ax = fig.add_subplot()
        ax.plot(xs[: i + 1])

    animate(draw, n_frames=120, out="out/mechanism.mp4", fps=24, size=(12, 4))

Or stitch a folder of PNG frames you already have:

    python animate.py frames/ out.mp4 --fps 24

Frames are written to <out>_frames/ and kept, so you can open any single frame
to check it before trusting the video. ffmpeg comes from the FFMPEG environment
variable, or "ffmpeg" on the PATH.
"""
import argparse
import glob
import os
import shutil
import subprocess
import sys


def ffmpeg_bin():
    exe = os.environ.get("FFMPEG") or shutil.which("ffmpeg")
    if not exe:
        sys.exit("ffmpeg not found: install it, or set FFMPEG to its path")
    return exe


def stitch(frames_dir, out, fps=24, pattern="%05d.png"):
    """PNG frames -> H.264 mp4 that plays everywhere (even pixel sizes, yuv420p)."""
    cmd = [ffmpeg_bin(), "-y", "-loglevel", "error", "-framerate", str(fps),
           "-i", os.path.join(frames_dir, pattern),
           "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
           "-c:v", "libx264", "-crf", "20", "-movflags", "+faststart", out]
    subprocess.run(cmd, check=True)
    return out


def animate(draw, n_frames, out, fps=24, size=(12, 4), dpi=100, hold_last=1.5, preview=False):
    """Call draw(fig, i) for each frame, save PNGs, then stitch them.
    hold_last repeats the final frame for that many seconds so the result can be read.
    preview=True renders every 4th frame at half resolution, for a fast look first."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    frames_dir = os.path.splitext(out)[0] + "_frames"
    shutil.rmtree(frames_dir, ignore_errors=True)
    os.makedirs(frames_dir)
    idx = list(range(0, n_frames, 4 if preview else 1))
    if idx[-1] != n_frames - 1:
        idx.append(n_frames - 1)
    fig = plt.figure(figsize=size, dpi=dpi // 2 if preview else dpi)
    k = 0
    for i in idx:
        fig.clear()
        draw(fig, i)
        fig.savefig(os.path.join(frames_dir, f"{k:05d}.png"))
        k += 1
    last = os.path.join(frames_dir, f"{k - 1:05d}.png")
    for _ in range(int(hold_last * (fps / 4 if preview else fps))):
        shutil.copyfile(last, os.path.join(frames_dir, f"{k:05d}.png"))
        k += 1
    plt.close(fig)
    stitch(frames_dir, out, fps // 4 if preview else fps)
    print(f"wrote {out} ({k} frames, {k / (fps // 4 if preview else fps):.1f}s); frames in {frames_dir}/")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Stitch numbered PNG frames into an mp4.")
    ap.add_argument("frames_dir")
    ap.add_argument("out")
    ap.add_argument("--fps", type=int, default=24)
    a = ap.parse_args()
    files = sorted(glob.glob(os.path.join(a.frames_dir, "*.png")))
    if not files:
        sys.exit("no PNG frames found")
    tmp = a.frames_dir.rstrip("/") + "_numbered"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    for i, f in enumerate(files):  # renumber so any naming works
        shutil.copyfile(f, os.path.join(tmp, f"{i:05d}.png"))
    stitch(tmp, a.out, a.fps)
    shutil.rmtree(tmp)
    print(f"wrote {a.out} ({len(files)} frames)")
