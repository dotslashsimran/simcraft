# Notes: Grokking (Power et al. 2022, arXiv 2201.02177)

## Claim

On small algorithmic datasets, a network can reach perfect training accuracy early, sit at chance on held-out data for a long time, and then suddenly generalize perfectly, long after it has overfit. Weight decay is the intervention that helps this the most (Sec 3.3, Fig 2 left).

## Mechanism

Two solutions fit the training data: a lookup table that memorizes each pair, and a compact rule that computes the answer. Memorizing is found first because it is easy to reach. Weight decay keeps shrinking the weights, which slowly penalizes the large-norm lookup table and favors the small-norm rule. When the rule takes over, validation accuracy jumps. Switch weight decay off and nothing pushes the network off the lookup table.

The paper itself only shows that weight decay matters most. The "smaller norm rule wins" reading comes from later work (Nanda et al. 2023, "Progress measures for grokking via mechanistic interpretability", arXiv 2301.05217, and Liu et al. 2022, "Omnigrok", arXiv 2210.01117). This toy measures it directly with the weight norm and the Fourier concentration of the embedding.

## Equations to code

The paper has no numbered equations. Code comments cite sections, figures and appendix A.1.2.

| Paper | What it says | Code |
|---|---|---|
| Sec 2 | data is a table `a o b = c`, each residue a separate symbol | `make_data`: all 97 x 97 pairs, `c = (a + b) % P` |
| Fig 1 | p = 97, 50% of pairs used for training | `P = 97`; we use `frac = 0.4` (see below) |
| A.1.2 | loss and accuracy only on the answer | `loss_and_grads`: cross entropy on `c` only |
| A.1.2 | AdamW, lr 1e-3, weight decay 1, beta1 0.9, beta2 0.98, warmup 10 updates | `AdamW` class, defaults match |
| A.1.2 | minibatch 512 or half the training set | `bs = min(512, len(ta) // 2)` |
| AdamW paper, Alg. 2 | `theta -= lr * (m_hat / (sqrt(v_hat) + eps) + wd * theta)` | `AdamW.step` |
| Fig 4 right | output layer shows the circular structure of addition | `fourier_concentration`, and the ring plot in `visualize.py` |

## Result to reproduce

Fig 1 left, in shape: train accuracy near 100% early, validation near chance for several times longer, then validation jumps to near 100%. Success means validation reaches 99% at least 3x later than training does.

## Ablation

Same model, seed and data, weight decay 0 (plain Adam). Expected: training accuracy reaches 100%, validation stays at chance for the whole budget. This is the paper's Fig 2 left claim in its simplest form.

## Not reproduced, and why

- **Architecture.** The paper uses a 2 layer transformer, width 128, 4 heads, about 400k parameters. We use an embedding, one ReLU layer of 256 and an output layer (about 103k parameters), written by hand in numpy. Grokking is about the training dynamics, not attention, and the MLP shows the same effect in about a minute on CPU.
- **Operation.** Fig 1 uses division mod 97. We use addition mod 97, which the paper also trains (Fig 4 right), because its learned structure is a clean Fourier pattern we can plot.
- **Train fraction.** 40% instead of 50%, because grokking is more delayed with less data and the effect is easier to see within 10,000 steps.
- **Time scale.** The paper's Fig 1 run uses plain Adam with no weight decay and 10^6 steps (A.1.2) to show how late generalization can start. Our wd 0 run does not generalize within 10,000 steps, and we do not test whether it would by 10^6.
- **Not attempted.** The data-fraction curve (Fig 1 center), the other operations (Fig 2 right), the S5 ablation grid (Fig 2 left) and t-SNE plots (Fig 4).

## Settings tried

All with the paper's AdamW recipe unless noted, 8,000 to 16,000 steps, seed 0.

| train fraction | lr | weight decay | train 99% at | validation 99% at |
|---|---|---|---|---|
| 0.5 | 1e-3 | 1 | 1,850 | 4,400 |
| 0.5 | 1e-3 | 0 | 1,150 | never (val 0.00 at 8k) |
| 0.4 | 1e-3 | 1 | 1,700 | 6,100 |
| 0.4 | 1e-3 | 0 | 900 | never (val 0.00 at 12k) |
| 0.3 | 1e-3 | 1 | 1,100 | 13,800 |
| 0.5 | 3e-3 | 1 | 700 | 1,450 |
| 0.4 | 3e-3 | 1 | 700 | 2,150 |
| 0.3 | 3e-3 | 1 | 450 | 5,000 |
| 0.5 | 3e-3 | 0 | 450 | never (val 0.00 at 8k) |

A side result: with less training data the wait gets much longer (4,400 then 6,100 then 13,800 steps at lr 1e-3), which matches the direction of Fig 1 center. This is one seed per setting, so treat it as a hint, not a measurement.

## Result (default run: `python grok.py`, seed 0, 10,000 steps each)

| run | train 99% at | validation 99% at | final validation | final weight norm | top 5 frequencies' share of embedding power |
|---|---|---|---|---|---|
| weight decay 1 | step 1,700 | step 6,100 | 100% | 64.3 (peak 76.0 at step 5,600) | 72% |
| weight decay 0 | step 900 | never | 0.0% | 119.7 | 13% |

Success criterion met: validation reached 99% about 3.6x later than training did. The ablation behaved as expected. On a log step axis this is a much smaller gap than the paper's (about 1,000x, Fig 1 left), which used plain Adam, 50% data, a transformer and 10^6 steps.

Hidden progress: the Fourier share rose from 12% at init to 22% at step 3,000 and 32% at step 4,000 while validation accuracy was still 0.1% and 5%. The internal structure forms before the score moves.
