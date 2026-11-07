# Why does a network that has already memorized its homework suddenly understand it?

We gave a small network 40% of an addition table mod 97. Pairs like 45 + 60 = 8, because the numbers wrap around at 97. The other 60% we kept hidden, to test it later.

By step 1,700 it got every training pair right. On the hidden pairs it scored zero. It had memorized a lookup table, which is the easiest way to fit the data. Then nothing visible happened for thousands of steps. Training accuracy was already perfect, so the score had nothing left to show, even though the training loss kept shrinking. At step 4,000 hidden accuracy was 5%. By step 6,100 it was 99%.

The change was happening inside long before the score moved. We tracked each number's learned vector and asked how much of it is made of a few smooth waves around the circle of 97 numbers. At the start, 12% of the power sat in the top five waves, which is what noise looks like. It reached 22% at step 3,000 and 32% at step 4,000, while hidden accuracy was still near zero. When we lay the 97 numbers out along the strongest wave, a shapeless cloud turns into a ring, with each number at its place on the clock. A ring is exactly what you need to add mod 97: adding means turning around the circle.

What drives this is weight decay. Every step it shrinks all the weights a little. A lookup table tends to need bigger weights than a compact rule, so this steady shrinking slowly favors the rule. Total weight size peaked at step 5,600 and then fell as the ring took over. With weight decay off, the same network memorized faster, its weights kept growing to almost twice the size, and it scored 0% on hidden pairs for all 10,000 steps.

The lesson: perfect training accuracy is not the end of learning. A slow pressure toward simpler solutions can keep reshaping a network long after its score has stopped changing.

What this toy does not show: it is one seed, an MLP instead of the paper's transformer, and 10,000 steps instead of up to a million. We do not know whether the run without weight decay would have generalized eventually, as the paper's Figure 1 run did with plain Adam.
