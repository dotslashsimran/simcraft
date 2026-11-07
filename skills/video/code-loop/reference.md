# Code loop: reference

Depth behind [SKILL.md](SKILL.md). Each section stands on its own.

## Looping techniques

**Whole cycles.** For any periodic motion, write it as `f(k·p)` where `f` has period 1 and `k` is an integer. Two motions with `k = 2` and `k = 3` together still loop, because both are back at the start when `p = 1`. Mixing `k = 1` and `k = 1.5` does not. If something must feel slower than once per loop, make the loop longer rather than using a fraction.

**Rotation by symmetry.** An object with n-fold symmetry only needs to turn `1/n` of a turn per loop to look like a full cycle. A square grid needs 90 degrees, a hexagon 60, a 5-armed star 72. This keeps rotations slow and calm.

**Travelling waves.** `sin(2π(k·p - r/λ))` travels outward from the centre, `k` crests per loop. It loops for any wavelength `λ`, because `p` only appears multiplied by an integer.

**Looping noise.** Noise is not periodic in any of its inputs. To make a field change over time and come back, walk round a circle in extra dimensions: for a 2D field use 4D noise `n(x, y, R cos 2πp, R sin 2πp)`. For a 1D signal (a wobble, a camera shake) use 2D or 3D noise `n(seed, R cos 2πp, R sin 2πp)`. The circle radius `R` is the amount of change per loop: about 0.3 is a gentle breathing, about 1.5 is a busy churn. For a field that flows instead of morphs, combine a looping noise with a whole-number scroll of a tiling texture, or warp the coordinates with a looping noise (domain warping, as in the shader starter).

**Tileable noise in space.** For a texture that must also tile (a scrolling background), make the spatial coordinates circles too: 4D noise of `(cos 2πx, sin 2πx, cos 2πy, sin 2πy)` tiles in x and y. For space and time together you need 6D noise, or tile a 2D noise image and scroll it.

**Easing that loops.**
- Out and back: `e(p) = 0.5 - 0.5 cos(2πp)` goes 0, 1, 0 with zero speed at the seam.
- Ping-pong any ease: `ease(1 - |2p - 1|)`. Speed is zero at the ends only if the ease has zero slope at 0 and 1.
- Holds that do not freeze: a smoothstep plateau `smoothstep(a, b, p) - smoothstep(c, d, p)`, plus a small looping drift on top so the picture never stands fully still (a freeze reads as a broken video).
- Keyframes: use a closed Catmull-Rom spline (indices wrap) or a monotone cubic over keys whose last value equals the first, and give the first and last segment matching tangents. A linear lerp between keys has corners in speed, which the eye sees as small jolts.

**Things that wrap.** Particles, walkers and scrolling items that leave one side and enter the other must wrap when they are off screen, or during the fastest camera move, where the motion blur hides any pop. Each item's position is `start + speed·LOOP·p` with the total distance a whole number of wrap lengths.

**Loops from things that are not periodic.** A simulation (fluid, particles with forces, growth) does not come back to its start. Two ways to loop one:
- Crossfade with itself: render time `T` to `2T`, and over the last part of the loop blend each frame with the frame one loop earlier, `out(p) = (1 - w(p))·A(T + pT) + w(p)·A(pT)`, with `w` going from 0 to 1 near the end. The motion stays continuous because both copies move the same way. This works well for soft, cloudy things and badly for hard objects, which ghost.
- Precompute the simulation state for one loop, then blend the state, not the image (for example, particle positions interpolated toward their start positions over the last 20%).

## The sample pattern

`render_loop.mjs` builds each frame from `k` sub-renders.

- **Offsets.** Point `j` of the Halton (2, 3) sequence, each coordinate passed through the inverse of a tent distribution, which gives offsets in -1..1 pixel weighted toward the centre. Averaging them is the same as filtering with a tent one pixel wide on each side: smoother than a box filter, without the softness of a wide Gaussian. The pattern is the same on every frame. Changing it per frame (as games do with TAA) would make residual aliasing shimmer, which is the opposite of what a video wants.
- **Count.** `--mink` samples on still frames (8 for finals). When `probe` shows that the fastest visible point moves more than `--step` pixels per sample during the shutter, the count doubles, up to `--maxk`.
- **Time.** Samples are spread evenly across the open shutter, centred on the frame time. `--shutter=1` (a 360 degree shutter) leaves no gap between frames, so streaks join into smooth motion. `--shutter=0.5` is the film look, with crisper motion that strobes a little on fast pans. Time and offset are paired through a fixed shuffle, so the streak is not skewed to one side.
- **Probe design.** Return a handful of points (5 to 50) on whatever moves fastest on screen: the scene corners for camera moves, the fastest objects, a few points far from the camera. Points behind the camera (`|ndcZ| > 1`) are ignored. A shader with no geometry can return analytic positions of its fastest features, as the shader starter does with its pearls. Without `probe`, every frame gets `--mink` samples, which is fine for slow loops.

## The accumulation buffer

- The scene renders into a half-float target, which is added into a 32-bit float target with additive blending at weight `1/k`. Blending into 32-bit float needs `EXT_float_blend`. Both starters check for it and fall back to half float, which is fine up to about 32 samples.
- MSAA on the scene target is wasted work once jitter is on: turn it off (`samples: 0`, `antialias: false`).
- `preserveDrawingBuffer: true` is needed so the screenshot sees the last draw.
- Shadow maps do not move with the camera jitter, so their edges are not smoothed by it. Use a soft filter (`PCFSoftShadowMap` with a radius), or jitter the light position a little per sample inside `renderMany` for real soft shadows (then use 16 or more samples, or the penumbra shows steps).
- Anything that keeps history (TAA, temporal reprojection, feedback buffers, physics) breaks the pure-function rule. Turn it off, or run it deterministically from the loop start for every frame.

## Colour

- **Linear in, sRGB out once.** In three.js, rendering into a render target gives linear values without tone mapping. `THREE.Color` converts hex codes from sRGB to linear for you. In raw GLSL, square a colour (or use `pow(c, 2.2)`) to bring an sRGB-picked palette into linear.
- **Tone mapping.** The starters use Khronos PBR Neutral, which keeps hues and saturation close to the colours you picked and only compresses highlights. AgX (built into three.js) gives a more filmic roll-off with desaturated highlights, good for glow and fire. ACES Filmic shifts hues (blues turn purple, saturated reds turn orange), which is usually not wanted for designed palettes.
- **Grade last.** Exposure before the tone map; saturation, contrast curve and vignette after it; then sRGB encoding; then dither.
- **Dither.** 8-bit output bands in smooth gradients, and video compression makes the bands worse. The output passes add a fixed triangular dither of about one step. It is fixed per pixel, so it cannot blink.
- **YUV conversion.** ffmpeg converts RGB to YUV with the BT.601 matrix unless told otherwise, while players decode HD video as BT.709, which shifts reds and greens. `render_loop.mjs` converts with `out_color_matrix=bt709` and tags the file as BT.709.

## Performance

- Merge static meshes by material at load. Keep anything that moves, and anything you toggle, out of the merge (`userData.noMerge`, or pass it in `keep`).
- Use `InstancedMesh` for many copies and update only the matrices that change.
- A preview should take seconds to a couple of minutes. If it does not, look at draw calls first (`renderer.info.render.calls`), then shader cost.
- Expensive shaders: `--scale=0.75` renders smaller and resizes to the output size on encode. It is a little softer, which soft shaders hide well. Keep full scale for anything with thin lines or text.
- `--from` and `--to` render a short section for testing a fix.

## Reading check_loop.py

| Line | Good | If not |
|---|---|---|
| seam (exact) | mean 0.000. A handful of edge pixels may differ from float rounding of `sin(2π)`, which is harmless. | Some input is not periodic: a fractional cycle count, `Math.random()`, `Date.now()`, a noise walked along a line, a keyframe track whose last key differs. |
| seam (video) | local jump under 2 | Part of the picture jumps at the wrap. Same causes as above, or `--to` does not match the loop. This test needs the jump to cover an area of a few tiles, so a single small object that teleports can slip past it. The exact test catches those. |
| pops | none | A sudden change inside the loop: an object wrapping on screen, a keyframe with a corner, a texture swap, a shadow map refit. |
| freeze | 0 spans | Add a slow drift to holds, or raise `--still` if the loop is meant to be very calm. |
| duplicated frames | 0 | The frame rate given to the encoder does not match the render, or the page skipped a draw. |
| blink | 0 to a few hundred pixels | Look at the heatmap. Usual causes are sub-pixel detail (thin lines, far grids, specular sparkle), screen-space effects, surfaces too close together, and hard edges on glows or reflections. |

Previews have one sample per frame and shimmer by design. Run the blink check on finals. The seam and freeze checks are valid on previews.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Fine detail crawls or sparkles | More samples (`--mink=16`), fade detail by its size in pixels (`fwidth`), or remove detail smaller than a pixel. |
| Specular sparkle on bumpy surfaces | Raise roughness, lower normal-map strength with distance, or soften the normals of a procedural surface. |
| Flicker between two surfaces | z-fighting. Separate them, merge them into one surface, or push `near` out. |
| Motion looks stuttery in the final | `probe` misses the fastest thing, so its frames got too few samples. Add points on it. |
| Blur streaks look dashed | `--step` too large or `--maxk` too small for the speed. Lower `--step` to 1. |
| Colours differ between preview and final | Unseeded random, or something depends on canvas size. Make sizes relative to the output height. |
| The page renders black in headless Chrome | Look at `[page error]` lines. Float render targets need WebGL2 and `EXT_color_buffer_float`; `--use-angle=metal` is set for macOS, use `--use-angle=gl` or `vulkan` elsewhere. |
| GIF is too big | Smaller width, `--gif-fps=20`, a calmer background (large flat areas compress well), shorter loop. |
| GIF seam stutters | The loop is not a whole number of frames at the GIF rate. 25 fps needs a loop length that is a multiple of 0.04 s, 20 fps a multiple of 0.05 s. |
| Platform loop shows a hitch | Some players pause on the last frame. Upload `--loops=3` or more so the restart happens less often. |

## GIF details

- GIF frame delays are whole hundredths of a second, so only rates that divide 100 play evenly: 50, 25, 20, 10. 30 fps becomes a mix of 3 and 4 hundredths and judders. Most browsers treat delays under 2 hundredths as 10, so 50 fps is risky.
- Ordered (bayer) dither puts the same pattern on unchanged areas every frame, so they compress to nothing and do not crawl. Error-diffusion dither (Floyd-Steinberg, Sierra) is smoother on stills but changes everywhere every frame.
- `palettegen` with `stats_mode=full` makes one palette for the whole loop, so colours do not shift between frames.
- For the best GIF, render at the GIF frame rate directly (`--fps=25`), so the motion blur matches the frame time.

## Using other engines

Any canvas page can join the contract. The only hard part is `renderMany`:
- Canvas 2D or p5: draw each sub-frame with a `translate(jx, jy)` into an offscreen canvas, read it with `getImageData`, convert to linear, add into a `Float32Array`, and write the average back. Slower than WebGL, but the same result.
- Other WebGL libraries (regl, OGL, Babylon): render into a float framebuffer and add into a second one with `blendFunc(ONE, ONE)`, exactly as the shader starter does.
