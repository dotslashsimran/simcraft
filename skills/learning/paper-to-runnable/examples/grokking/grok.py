"""Grokking at toy scale, from scratch in numpy.

Paper: Power et al. 2022, "Grokking: Generalization Beyond Overfitting on Small
Algorithmic Datasets", arXiv 2201.02177. The paper has no numbered equations, so
comments cite sections, figures and appendix A.1.2 (the training recipe).

  python grok.py --check            sanity ladder: shapes, gradient check, overfit one batch
  python grok.py                    main run (weight decay 1) and ablation (weight decay 0)
  python grok.py --wd 1 --steps 500 a single quick run

Writes out/run_wd<wd>.npz with the training curves and embedding snapshots.
"""
import argparse
import os
import time

import numpy as np

P = 97  # Sec 2 and Fig 1: residues mod 97, each one its own symbol


# ---------- data: Sec 2, binary operation tables a o b = c ----------

def make_data(frac, seed):
    """Every pair (a, b) with label (a + b) mod P. Split into train and validation.
    Fig 1 uses division; we use addition (the paper also trains it, Fig 4 right)
    because its learned structure is easy to see with a Fourier transform."""
    a, b = np.meshgrid(np.arange(P), np.arange(P), indexing="ij")
    a, b = a.ravel(), b.ravel()
    c = (a + b) % P
    idx = np.random.default_rng(seed).permutation(len(a))
    n = int(frac * len(a))
    tr, va = idx[:n], idx[n:]
    return (a[tr], b[tr], c[tr]), (a[va], b[va], c[va])


# ---------- model ----------
# The paper uses a 2 layer decoder-only transformer, width 128, 4 heads (A.1.2).
# We swap it for the smallest network that shows the same effect: a shared token
# embedding, one ReLU hidden layer, and an output layer. The input is the pair
# of symbols (a, b), the output is a score for each of the P possible answers c.

def init(d=128, h=256, seed=0, scale=1.0):
    r = np.random.default_rng(seed)
    return {
        "E": r.normal(0, scale / np.sqrt(d), (P, d)),           # token embedding
        "W1": r.normal(0, scale / np.sqrt(2 * d), (2 * d, h)),  # [E[a], E[b]] -> hidden
        "b1": np.zeros(h),
        "W2": r.normal(0, scale / np.sqrt(h), (h, P)),          # hidden -> logits over c
        "b2": np.zeros(P),
    }


def forward(w, a, b):
    x = np.concatenate([w["E"][a], w["E"][b]], axis=1)  # (n, 2d)
    z = x @ w["W1"] + w["b1"]                            # (n, h)
    r = np.maximum(z, 0)
    logits = r @ w["W2"] + w["b2"]                       # (n, P)
    return logits, (a, b, x, z, r)


def loss_and_grads(w, a, b, c):
    """Cross entropy on the answer token only (A.1.2: loss is computed on the answer)."""
    logits, (a, b, x, z, r) = forward(w, a, b)
    n = len(c)
    sh = logits - logits.max(1, keepdims=True)
    prob = np.exp(sh)
    prob /= prob.sum(1, keepdims=True)
    loss = -np.log(prob[np.arange(n), c] + 1e-12).mean()
    # backward pass, written out by hand
    dlog = prob
    dlog[np.arange(n), c] -= 1
    dlog /= n
    g = {"W2": r.T @ dlog, "b2": dlog.sum(0)}
    dz = (dlog @ w["W2"].T) * (z > 0)
    g["W1"], g["b1"] = x.T @ dz, dz.sum(0)
    dx = dz @ w["W1"].T
    d = w["E"].shape[1]
    g["E"] = np.zeros_like(w["E"])
    np.add.at(g["E"], a, dx[:, :d])
    np.add.at(g["E"], b, dx[:, d:])
    return loss, g


def accuracy(w, a, b, c):
    return float((forward(w, a, b)[0].argmax(1) == c).mean())


# ---------- optimiser: AdamW, the recipe in A.1.2 ----------
# lr 1e-3, weight decay 1, beta1 0.9, beta2 0.98, linear warmup over 10 updates,
# minibatch 512 or half the training set. Update rule from Loshchilov and Hutter
# (AdamW, Algorithm 2): theta -= lr * (m_hat / (sqrt(v_hat) + eps) + wd * theta).
# Weight decay is decoupled: it shrinks weights directly, not through the gradient.

class AdamW:
    def __init__(self, w, lr=1e-3, wd=1.0, b1=0.9, b2=0.98, eps=1e-8, warmup=10):
        self.lr, self.wd, self.b1, self.b2, self.eps, self.warmup = lr, wd, b1, b2, eps, warmup
        self.m = {k: np.zeros_like(v) for k, v in w.items()}
        self.v = {k: np.zeros_like(v) for k, v in w.items()}
        self.t = 0

    def step(self, w, g):
        self.t += 1
        lr = self.lr * min(1.0, self.t / self.warmup)
        for k in w:
            self.m[k] = self.b1 * self.m[k] + (1 - self.b1) * g[k]
            self.v[k] = self.b2 * self.v[k] + (1 - self.b2) * g[k] ** 2
            mh = self.m[k] / (1 - self.b1 ** self.t)
            vh = self.v[k] / (1 - self.b2 ** self.t)
            w[k] -= lr * (mh / (np.sqrt(vh) + self.eps) + self.wd * w[k])


def weight_norm(w):
    return float(np.sqrt(sum((v ** 2).sum() for v in w.values())))


def fourier_concentration(E, top=5):
    """Share of the embedding's non-constant power that sits in the top few
    frequencies over the token axis. Near top/48 for a random embedding, near 1
    once the network has found a few clean waves (the 'circle' in Fig 4 right)."""
    F = np.abs(np.fft.rfft(E - E.mean(0), axis=0)) ** 2  # (P//2+1, d)
    power = F[1:].sum(1)                                  # skip frequency 0
    return float(np.sort(power)[::-1][:top].sum() / power.sum())


# ---------- training ----------

def train(wd, steps, frac=0.4, seed=0, scale=1.0, lr=1e-3, log_every=50, snap_every=100, quiet=False):
    (ta, tb, tc), (va, vb, vc) = make_data(frac, seed)
    w = init(seed=seed, scale=scale)
    opt = AdamW(w, lr=lr, wd=wd)
    bs = min(512, len(ta) // 2)
    rng = np.random.default_rng(seed + 1)
    log = {k: [] for k in ["step", "train_acc", "val_acc", "train_loss", "val_loss", "wnorm", "fourier"]}
    snaps, snap_steps = [], []
    t0 = time.time()
    for s in range(steps + 1):
        if s % log_every == 0:
            tl, _ = loss_and_grads(w, ta, tb, tc)
            vl, _ = loss_and_grads(w, va, vb, vc)
            row = [s, accuracy(w, ta, tb, tc), accuracy(w, va, vb, vc), tl, vl,
                   weight_norm(w), fourier_concentration(w["E"])]
            for k, v in zip(log, row):
                log[k].append(v)
            if not quiet and s % (log_every * 20) == 0:
                print(f"  wd={wd} step {s:6d}  train {row[1]:.3f}  val {row[2]:.3f}  "
                      f"|w| {row[5]:6.1f}  fourier top5 {row[6]:.2f}  ({time.time() - t0:.0f}s)")
        if s % snap_every == 0:
            snaps.append(w["E"].copy())
            snap_steps.append(s)
        if s == steps:
            break
        i = rng.choice(len(ta), bs, replace=False)
        _, g = loss_and_grads(w, ta[i], tb[i], tc[i])
        opt.step(w, g)
    out = {k: np.array(v) for k, v in log.items()}
    out.update(E_snaps=np.array(snaps, dtype=np.float32), snap_steps=np.array(snap_steps),
               W2=w["W2"], wd=wd, frac=frac)
    return out


def first(steps, values, thr=0.99):
    hit = np.nonzero(values >= thr)[0]
    return int(steps[hit[0]]) if len(hit) else None


# ---------- sanity ladder ----------

def check():
    print("1. shapes")
    w = init(d=8, h=16)
    a, b = np.array([0, 5, 96]), np.array([1, 2, 3])
    logits, _ = forward(w, a, b)
    assert logits.shape == (3, P), logits.shape
    (ta, tb, tc), (va, vb, vc) = make_data(0.4, 0)
    assert len(ta) + len(va) == P * P and np.all((ta + tb) % P == tc)
    print(f"   logits {logits.shape}, train {len(ta)} + val {len(va)} = {P * P} pairs: ok")

    print("2. gradient check (analytic vs finite difference, float64)")
    c = np.array([1, 7, 99 % P])
    _, g = loss_and_grads(w, a, b, c)
    rng = np.random.default_rng(0)
    worst = 0.0
    for k in w:
        for _ in range(5):
            i = tuple(rng.integers(0, s) for s in w[k].shape)
            if k == "E":
                i = (int(rng.choice(np.r_[a, b])), i[1])  # rows that are actually used
            old = w[k][i]
            w[k][i] = old + 1e-5; lp, _ = loss_and_grads(w, a, b, c)
            w[k][i] = old - 1e-5; lm, _ = loss_and_grads(w, a, b, c)
            w[k][i] = old
            num = (lp - lm) / 2e-5
            rel = abs(num - g[k][i]) / max(1e-8, abs(num) + abs(g[k][i]))
            worst = max(worst, rel)
    print(f"   worst relative error {worst:.1e}: {'ok' if worst < 1e-5 else 'FAIL'}")
    assert worst < 1e-5

    print("3. overfit one batch of 64 (no weight decay)")
    w = init()
    opt = AdamW(w, wd=0.0)
    i = np.arange(64)
    for s in range(300):
        loss, g = loss_and_grads(w, ta[i], tb[i], tc[i])
        opt.step(w, g)
    acc = accuracy(w, ta[i], tb[i], tc[i])
    print(f"   after 300 steps: loss {loss:.4f}, batch accuracy {acc:.2f}: {'ok' if acc == 1 else 'FAIL'}")
    assert acc == 1.0

    print("4. chance level at init")
    w = init()
    acc = accuracy(w, va, vb, vc)
    print(f"   val accuracy {acc:.3f} vs chance {1 / P:.3f}: {'ok' if acc < 0.05 else 'FAIL'}")
    print("all checks passed")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--wd", type=float, nargs="*", default=[1.0, 0.0])
    ap.add_argument("--steps", type=int, default=10000)
    ap.add_argument("--frac", type=float, default=0.4)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    if args.check:
        return check()
    os.makedirs("out", exist_ok=True)
    for wd in args.wd:
        r = train(wd, args.steps, args.frac, args.seed, args.scale, args.lr)
        np.savez_compressed(f"out/run_wd{wd:g}.npz", **r)
        print(f"wd={wd:g}: train>=99% at step {first(r['step'], r['train_acc'])}, "
              f"val>=99% at step {first(r['step'], r['val_acc'])}, "
              f"final val {r['val_acc'][-1]:.3f}, final fourier top5 {r['fourier'][-1]:.2f}")


if __name__ == "__main__":
    main()
