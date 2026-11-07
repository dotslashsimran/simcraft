# brand-kit: how it decides, and what testing showed

## Where each role comes from

| Role | Source, in order of trust |
|---|---|
| surface | body or html background, else the colour covering most of the first screen |
| ink | the h1 colour |
| muted | body text drawn see-through (e.g. `rgba(25,25,25,.56)`), blended onto the surface to get the colour you actually see |
| accent | CSS tokens named accent, primary or brand, then filled buttons, then `theme-color`, then the most saturated colour on screen. Only reasonably saturated colours count, so a monochrome brand falls back to ink. |
| extras | other saturated tokens, at least 60 apart in RGB distance from each other |
| display / body / mono font | the first h1 or h2, p and code element, on any page read, whose font the site actually ships. System fonts map to `system-ui`. |
| shape | the button border radius (above 100 px counts as a pill). Torn edges and grain are used only if the brand uses texture. |

## The explainer theme

- Style C's navy and red are close in brightness, so type can cross the diagonal split.
- Brand fields usually aren't (white and blue). So when the two fields differ by more than 0.35 in luminance, the second colour becomes a narrow band at the edge, and type always sits on one colour.
- Big type uses ink on a light field and paper on a dark one.
- On a field that is the accent colour itself, tags and highlights flip to ink and paper.
- Huge type fits itself: it wraps at 86% of the width and shrinks until nothing overflows, because brand faces are often much wider than style C's condensed headline face.

## Tested on (Nov 6, 2026)

- **Devin** (devin.ai), in 9 seconds:
  - Fonts: NB International Pro for display and body, STK Bureau Serif, Geist Mono.
  - Accent blue #317CFF from the `--color-accent` token, which was nested two levels deep.
  - Surface #FCFCFC, ink #191919, muted #7C7C7C.
  - Extras: orange, green, purple.
  - Pill buttons, SVG logo.
- **ChatGPT** (chatgpt.com plus openai.com/chatgpt/overview):
  - chatgpt.com alone gave system fonts. Reading the second page found OpenAI Sans (30 font files).
  - Monochrome: the accent is #0D0D0D, which is right for the brand.
  - Extras from openai.com: #FFBA08, #05A6F0, #155EEF, #F31C91.
  - The knot logo came from the site icon, because no header logo element was found.

Problems found in testing, and fixed:
- Nested tokens (`rgb(var(--text-accent-primary))`) came through unresolved. Fixed by resolving them in the browser.
- See-through body text was read as solid ink.
- Text crossed the white/blue split and half of it disappeared.
- A wide brand font ran off both edges of the frame.
- A black highlight was invisible on a black field.
- The display font was taken from the first page only, which missed the real brand font.
