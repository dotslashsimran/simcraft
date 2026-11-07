"""Fail if any stretch of the video sits still for more than 0.5 s (after the first second).

Usage: python motion_check.py video.mp4
A held frame in an explainer reads as frozen or broken, so every beat must keep moving.
Samples 10 frames a second at 160 px wide and compares neighbours.
"""
import os, shutil, subprocess, sys
from pathlib import Path

FF = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
W, H = 160, 90
raw = subprocess.run([FF, "-v", "error", "-i", sys.argv[1], "-vf", f"fps=10,scale={W}:{H},format=gray", "-f", "rawvideo", "-"],
                     capture_output=True).stdout
n = len(raw) // (W * H)
frames = [raw[i * W * H:(i + 1) * W * H] for i in range(n)]
# "still" means the picture barely changes across half a second: compare each frame with the one 0.5 s earlier
# (consecutive frames of a slow, visible push-in differ too little to count, which gave false failures)
still, worst = [], (0, 0)
run = 0
for i in range(5, n):
    diff = sum(abs(a - b) for a, b in zip(frames[i][::7], frames[i - 5][::7])) / (len(frames[i]) / 7)
    if diff < 1.0 and i > 10:  # first second: the opening is still landing
        still.append(i / 10); run += 1
        if run > worst[0]: worst = (run, i / 10)
    else:
        run = 0
if still:
    print(f"MOTION CHECK FAILED: {len(still)} still samples, longest {(worst[0] + 5) / 10:.1f}s ending at {worst[1]:.1f}s. "
          f"Keep something drifting through the end of every beat.")
    sys.exit(1)
print(f"motion check passed ({n} samples, nothing still for 0.5s+)")
