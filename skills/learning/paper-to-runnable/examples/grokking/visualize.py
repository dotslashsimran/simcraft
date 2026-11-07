"""Plots and an animation of what happens inside the network while it groks.

  python visualize.py            needs out/run_wd1.npz and out/run_wd0.npz from grok.py
  python visualize.py --preview  quick low-res video first

Writes:
  out/curves.png      accuracy, loss, weight size and Fourier concentration over training,
                      with and without weight decay (compare with Fig 1 left of the paper)
  out/mechanism.mp4   the embedding of the 97 numbers turning from a random cloud into a
                      circle, next to the accuracy curves, for both runs
"""
import argparse
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from animate import animate  # noqa: E402

P = 97
RUNS = [("wd1", "weight decay 1 (paper recipe)", "#c0392b"), ("wd0", "no weight decay", "#7f8c8d")]


def load():
    return {k: dict(np.load(f"out/run_{k}.npz")) for k, _, _ in RUNS}


def spectrum(E):
    """Power of the embedding at each frequency k = 1..48 over the token axis."""
    F = np.abs(np.fft.rfft(E - E.mean(0), axis=0)) ** 2
    p = F[1:].sum(1)
    return p / p.sum()


def circle(E, k):
    """Project every token onto the plane of frequency k. If the embedding holds a
    clean wave at k, token t lands at angle 2*pi*k*t/97 and the points form a ring."""
    t = np.arange(P)
    C = (np.exp(-2j * np.pi * k * t / P)[:, None] * (E - E.mean(0))).sum(0)  # (d,) complex
    u, v = C.real / (np.linalg.norm(C.real) + 1e-12), C.imag / (np.linalg.norm(C.imag) + 1e-12)
    X = (E - E.mean(0)) @ np.stack([u, v], 1)
    return X / (np.abs(X).max() + 1e-12)


def curves(R):
    fig, ax = plt.subplots(2, 2, figsize=(11, 7))
    for key, label, col in RUNS:
        r = R[key]
        s = np.maximum(r["step"], 1)
        ax[0, 0].plot(s, r["train_acc"], "--", color=col, label=f"train, {label}")
        ax[0, 0].plot(s, r["val_acc"], "-", color=col, label=f"validation, {label}")
        ax[0, 1].plot(s, r["train_loss"], "--", color=col)
        ax[0, 1].plot(s, r["val_loss"], "-", color=col)
        ax[1, 0].plot(s, r["wnorm"], color=col, label=label)
        ax[1, 1].plot(s, r["fourier"], color=col, label=label)
    ax[0, 0].set(title="Accuracy (dashed train, solid validation)", ylabel="accuracy")
    ax[0, 1].set(title="Loss", ylabel="cross entropy", yscale="log")
    ax[1, 0].set(title="Size of all weights", ylabel="L2 norm")
    ax[1, 1].set(title="Share of embedding power in top 5 frequencies", ylabel="share", ylim=(0, 1))
    ax[0, 0].legend(fontsize=8, loc="center left")
    ax[1, 1].axhline(5 / 48, color="k", lw=0.8, ls=":")
    ax[1, 1].text(1.2, 5 / 48 + 0.02, "power spread evenly (5/48)", fontsize=8)
    for a in ax.flat:
        a.set_xscale("log")
        a.set_xlabel("optimisation step")
        a.grid(alpha=0.3)
    fig.suptitle("Addition mod 97, 40% of pairs for training, small MLP, AdamW", fontsize=12)
    fig.tight_layout()
    fig.savefig("out/curves.png", dpi=120)
    plt.close(fig)
    print("wrote out/curves.png")


def movie(R, preview):
    k_star = int(np.argmax(spectrum(R["wd1"]["E_snaps"][-1]))) + 1
    n = len(R["wd1"]["snap_steps"])
    order = (k_star * np.arange(P)) % P  # colour = the angle the wave assigns to token t

    def draw(fig, i):
        gs = fig.add_gridspec(2, 3, width_ratios=[1.4, 1.2, 1])
        for row, (key, label, col) in enumerate(RUNS):
            r = R[key]
            step = r["snap_steps"][i]
            E = r["E_snaps"][i]
            a = fig.add_subplot(gs[row, 0])
            m = r["step"] <= step
            a.plot(np.maximum(r["step"][m], 1), r["train_acc"][m], "--", color=col)
            a.plot(np.maximum(r["step"][m], 1), r["val_acc"][m], "-", color=col)
            a.set(xscale="log", xlim=(1, r["step"][-1]), ylim=(-0.03, 1.03), ylabel="accuracy")
            a.set_title(f"{label}: step {step}", fontsize=10, loc="left")
            a.text(1.3, 0.9, f"train {r['train_acc'][m][-1]:.2f}\nval     {r['val_acc'][m][-1]:.2f}",
                   fontsize=9, family="monospace", va="top")
            b = fig.add_subplot(gs[row, 1])
            b.bar(np.arange(1, P // 2 + 1), spectrum(E), color=col, width=0.9)
            b.set(ylim=(0, 0.6), xlabel="frequency over the 97 tokens", ylabel="share of power")
            b.set_title("embedding spectrum", fontsize=10, loc="left")
            c = fig.add_subplot(gs[row, 2])
            X = circle(E, k_star)
            c.scatter(X[:, 0], X[:, 1], c=order, cmap="twilight", s=14)
            c.set(xlim=(-1.15, 1.15), ylim=(-1.15, 1.15), xticks=[], yticks=[], aspect="equal")
            c.set_title(f"tokens in the frequency-{k_star} plane", fontsize=10, loc="left")
        fig.tight_layout()

    os.makedirs("out", exist_ok=True)
    out = "out/mechanism_preview.mp4" if preview else "out/mechanism.mp4"
    animate(draw, n, out, fps=12, size=(13, 7), preview=preview)
    return k_star


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true")
    a = ap.parse_args()
    R = load()
    curves(R)
    k = movie(R, a.preview)
    for key, label, _ in RUNS:
        sp = spectrum(R[key]["E_snaps"][-1])
        top = np.argsort(sp)[::-1][:5] + 1
        print(f"{label}: top frequencies {top.tolist()} hold {sp[top - 1].sum():.0%} of embedding power")
    print(f"ring plotted at frequency {k}")


if __name__ == "__main__":
    main()
