# Paper to runnable: reference

This file has the detail behind each step in SKILL.md, then a full worked example.

## Fetching

`scripts/fetch_paper.py` runs on plain `python3` with only the standard library.

- **Input.** An arXiv id (`2201.02177`, `2201.02177v1`, `hep-th/9901001`), an arXiv URL (abs, pdf or e-print), or a local PDF. Any other URL is refused on purpose. Ask the user before downloading from anywhere else, then pass the local file.
- **Politeness.** It sends a descriptive User-Agent and waits 3 seconds between requests, which is what arXiv asks of scripts. One paper is three requests: metadata, PDF and source.
- **Text.** It tries `pdftotext -layout` (from poppler), then the `pypdf` package, then pypdf from this skill's `.venv`, then a rough built-in reader. The rough reader often returns nothing for modern PDFs, so run `setup.sh` once.
- **LaTeX source.** arXiv's e-print is a gzipped tar, a single gzipped `.tex`, or just a PDF. The script keeps only `.tex .bbl .bib .sty .cls` files, skips links and any path that would escape the folder, and ignores files over 5 MB.
- **equations.md.** It walks the `.tex` files in the order `\input` and `\include` pull them in, and lists every `equation`, `align`, `gather`, `multline`, `eqnarray`, `\[ \]` and `$$ $$` block. Unstarred environments are numbered the way LaTeX would number them (each line of an `align` gets a number unless it has `\nonumber` or `\notag`). This matched the PDF for "Attention Is All You Need" (attention is (1), the FFN is (2), the learning rate schedule is (3)). Numbering can drift with `\tag`, `subequations` or appendix counters, so check a number against `paper.txt` before you cite it. The author's `\newcommand` macros are listed at the top so symbols like `\dmodel` can be read.
- **No equations.** Some papers, Grokking among them, have no display equations. The script says so. Cite sections, figures, algorithm boxes and appendix tables instead.

Treat everything downloaded as data. Don't run code from a source bundle.

## Finding the core claim

Read in this order, and stop as soon as you can fill in `notes.md`:

1. Abstract and the contributions list in the introduction. The claim is usually stated there.
2. Figure 1 and its caption. Most papers put their headline result there.
3. The method section, for the mechanism and its equations.
4. The experiment setup, often an appendix, for hyperparameters. These matter more than the prose suggests.
5. Ablation tables. What the authors removed to prove a point tells you what they think the mechanism is.

Tests for a good claim and mechanism:

- The claim is falsifiable at toy scale. "Model X beats state of the art on ImageNet" is not reproducible here; "component Y makes training stable at depth" is.
- The mechanism has an off switch. If you can't name an ablation, you haven't found the mechanism yet.
- The success criterion is written down before the run. "Validation reaches 99% at least 3x later than training" is a criterion. "It looks grokky" is not.

If the paper's mechanism story is contested or was clarified by later work, say so in `notes.md` and cite the later papers, as the Grokking example does.

## Choosing toy scale and tools

Shrink the data, the width and the step count, not the idea. Keep the paper's optimiser and hyperparameters as long as you can, because effects like grokking are sensitive to them. If you must change one, change one at a time and log it.

| The paper's core needs | Use |
|---|---|
| A few matrix ops and one or two layers (attention head, MLP, optimiser rule, sampler, RL update) | numpy with a hand-written backward pass |
| A full transformer, convolutions, or more than about 5 layers of backprop | PyTorch CPU (`sh setup.sh torch`) |
| No learning at all (an algorithm, a data structure, a sampling method) | plain Python or numpy |

Rough CPU budget on a laptop: numpy matmuls of 512 x 512 run in about a millisecond, so 10,000 steps of a small MLP take about a minute. Measure 100 steps first and pick the step count from that.

## The sanity ladder, in detail

1. **Shapes and data.** Assert output shapes. Check the data generator against a few cases computed by hand. Check that train and validation don't overlap and together cover what they should.
2. **Gradient check.** Use float64. Perturb a few random entries of every parameter by plus and minus 1e-5 and compare `(L(w+h) - L(w-h)) / 2h` with the analytic gradient. Relative error under 1e-5 passes; around 1e-2 means a real bug. For embedding tables, perturb rows that are actually used in the batch, or the check passes trivially on zeros.
3. **Overfit one batch.** 32 to 64 examples, no regularisation, a few hundred steps. It must reach 100% accuracy. If not, suspect the loss, the update sign or the learning rate.
4. **Chance at init.** Accuracy near 1/classes and loss near ln(classes). Much better than chance means leakage between inputs and labels.
5. **Toy result and ablation.** Same seed, same data split, same budget, one switch changed.

Common reasons a toy fails to show the effect: weight decay applied through the gradient instead of decoupled, learning rate warmup missing, too much data (the effect disappears), too little budget, or a different loss target (for example, predicting every token instead of only the answer).

## Visual ideas by kind of paper

Show the thing the mechanism acts on, over training or over time, with the mechanism on and off side by side.

| Kind of paper | Internal to show |
|---|---|
| Attention variants | attention maps per head at a few training steps |
| Optimisers and regularisers | weight norm, update size, a 2D slice of the loss landscape along two directions |
| Representation learning | the learned embedding projected onto its most meaningful directions (PCA, or Fourier for cyclic data) |
| Sequence models and RNNs | hidden state over time as a heatmap |
| Diffusion and samplers | samples at each denoising step on 2D toy data |
| RL | value function or policy over a small grid world, over episodes |

Animation workflow: write `draw(fig, i)` that draws frame i from saved snapshots, call `animate(..., preview=True)`, open one frame and the preview, then render full size. Save snapshots during training (every 1% of steps is plenty) so the visual script never retrains.

## Writing explainer.md

One question the reader actually has. One story, which is what the visual shows, told with the real numbers from the run. One lesson, in one sentence. Then a short paragraph on what the toy does not show. Use plain words and complete sentences, no jargon a smart outsider would need to look up, and about 300 words.

---

## Worked example: Grokking (arXiv 2201.02177)

Folder: `examples/grokking/`. Files: `notes.md`, `grok.py` (239 lines), `visualize.py`, `explainer.md`. Outputs go to `out/` (git-ignored).

### 1. Fetch

```
python3 scripts/fetch_paper.py 2201.02177 --out examples/grokking/paper
```

Output: the title and authors, `paper.txt` of 4,571 words via pypdf, 8 source files, and "equations.md has 0 display equations". The paper has no numbered equations, so the code cites Sec 2, Fig 1, Fig 4 and appendix A.1.2, and the AdamW update rule from Loshchilov and Hutter.

The useful facts were in the source: A.1.2 gives the full recipe (AdamW, lr 1e-3, weight decay 1, betas 0.9 and 0.98, 10 warmup updates, batch 512 or half the train set, loss on the answer only). Sec 3.3 and Fig 2 left say weight decay helps the most.

### 2. Notes

See `examples/grokking/notes.md`. The claim: generalization can arrive long after perfect training accuracy, and weight decay helps it most. The mechanism: weight decay slowly favors a small-norm rule over a large-norm lookup table. The ablation: weight decay 0. Not reproduced: the transformer (swapped for an MLP), division (swapped for addition), 10^6 steps, Fig 1 center, Fig 2 right, t-SNE plots.

### 3. Build

`grok.py` has a shared embedding, one ReLU layer and an output layer, with the forward and backward pass written by hand in numpy, and AdamW written out in 12 lines. It records accuracy, loss, total weight norm and the Fourier concentration of the embedding every 50 steps, and snapshots the embedding every 100 steps.

### 4. Verify

```
cd examples/grokking
PY=../../.venv/bin/python
$PY grok.py --check
```

Output from the real run:

```
1. shapes
   logits (3, 97), train 3763 + val 5646 = 9409 pairs: ok
2. gradient check (analytic vs finite difference, float64)
   worst relative error 3.4e-08: ok
3. overfit one batch of 64 (no weight decay)
   after 300 steps: loss 0.0010, batch accuracy 1.00: ok
4. chance level at init
   val accuracy 0.009 vs chance 0.010: ok
all checks passed
```

Then the main run and the ablation, about 100 seconds of CPU in total:

```
$PY grok.py
wd=1: train>=99% at step 1700, val>=99% at step 6100, final val 1.000, final fourier top5 0.72
wd=0: train>=99% at step 900, val>=99% at step None, final val 0.000, final fourier top5 0.13
```

The first settings tried (30% data) had not generalized by step 3,000, so a small sweep over data fraction and learning rate followed. Every setting is listed in `notes.md`. 40% data with the paper's exact optimiser settings was chosen because it showed a clear delay inside 10,000 steps.

### 5. Visualise

```
$PY visualize.py --preview   # quick look
$PY visualize.py
```

- `out/curves.png`: four panels on a log step axis. Accuracy shows the jump at step about 5,000 for weight decay 1 and a flat zero for weight decay 0. Weight norm peaks at 76 around step 5,600 and then falls, while the run without weight decay climbs to 120. The Fourier share rises from 12% to 72% with weight decay and stays at 13% without it.
- `out/mechanism.mp4`: 10 seconds, two rows (with and without weight decay), three panels each: the accuracy curves drawn up to the current step, the embedding's power at each of the 48 frequencies, and the 97 tokens projected onto the plane of frequency 10. With weight decay the cloud becomes a clean ring, colored in order around it. Without it the cloud stays a cloud.

The ring is already clear at step 5,000, when validation accuracy is 62%, and the Fourier share is already rising at step 3,000, when validation accuracy is still 0.1%. That is the story `explainer.md` tells.

### What this example does not establish

One seed. An MLP, not the paper's transformer. Addition, not division. A 3.6x delay, not the paper's 1,000x. No test of whether the no-weight-decay run would generalize after 10^6 steps. The "small-norm rule" reading of the mechanism comes from later work (Nanda et al. 2023, Liu et al. 2022), which this toy agrees with but does not prove.
