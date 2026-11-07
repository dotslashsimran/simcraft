# Scripted screencast: reference

## How it works

The render happens in two passes.

**Pass 1, capture** (`scripts/capture.mjs`). Playwright opens the app at a fixed viewport, at 2x device pixels for the final render, with a fixed locale, time zone, colour scheme and date. Before any app code runs, the page gets:

- a fake clock (`page.clock`). `Date`, `setTimeout`, `setInterval` and `requestAnimationFrame` only move when the renderer advances them, by exactly one frame at a time.
- control of every CSS animation, CSS transition and Web Animation. Each one is paused when it starts and stepped forward one frame per screenshot. Finished animations are completed with `finish()`, so `transitionend` and `animationend` still fire.
- a seeded `Math.random`, and CSS that turns off smooth scrolling and hides scrollbars.

Then each step runs frame by frame. The mouse moves along an eased bezier curve with a slight arc, and the real mouse goes there too, so hover styles work. Clicks are a real press and release. Keys are typed one at a time on a schedule with small random gaps (seeded, so they are the same every run). Scrolling sets the scroll position each frame with easing. After each frame the page is screenshotted. If nothing changed (no DOM change, no animation, no cursor move, no input) the previous screenshot is reused, which makes pauses cheap. The pass writes `frames/*.jpg` and `timeline.json`, which holds the cursor position, cursor type and button state for every frame, plus every click, zoom, caption and small-target action.

**Pass 2, compose** (`scripts/compose.mjs` and `scripts/compositor.html`). This pass knows the whole timeline, so the camera can start moving slightly before an action, the way a human editor would cut it. The camera target at each moment is one of three things: the explicit zoom region, the auto zoom region, or the full view. While zoomed in, the view also follows the cursor, keeping it out of the outer 20% of the frame. The target goes through a critically damped spring on (x, y, log zoom), stepped in 1 ms slices, so the motion is smooth, never overshoots, and looks the same at 24 or 60 fps. Each output frame is drawn on a canvas: the background, the window with its shadow and title bar, the captured frame scaled down from 2x, then the click ripples and the cursor (arrow, hand or I-beam, taken from the page's CSS cursor), drawn as vectors at output resolution. The caption is drawn last, outside the camera. The frames are piped to ffmpeg (x264, CRF 16, BT.709).

Because the cursor and zoom are drawn in pass 2, they are sub-pixel smooth. Nothing depends on how fast the machine is.

## Steps

Every step is an object with one action key. Options shared by most steps: `hold` (ms to stay still afterwards) and `moveMs` (cursor travel time).

| Step | What it does |
|---|---|
| `{ click: target }` | Scrolls the target into view if needed, moves to it, presses and releases, then holds `pace.settle`. Options: `at: [0.1, 0.5]` (aim at that fraction of the box instead of near the middle), `zoom: true/false` (force or skip auto zoom). |
| `{ type: "text", into: target }` | Clicks the field if it isn't focused, then types at human speed. `\n` presses Enter. `clear: true` empties the field first. Without `into`, it types into whatever has focus. |
| `{ press: "Enter" }` | Presses one key or chord (`"Meta+K"`). |
| `{ hover: target }` | Moves to the target and holds `pace.hover`. |
| `{ move: [0.5, 0.4] }` | Moves the cursor to a point given as a fraction of the viewport. Use `ms` to set the travel time. |
| `{ scroll: 400 }` | Scrolls the area under the cursor by that many pixels (negative scrolls up). |
| `{ scroll: target, offset: 80 }` | Scrolls until the target is `offset` pixels from the top of its scroll area. |
| `{ wait: target, timeout: 10000 }` | Keeps recording until the target is visible. This is video time, so the wait is seen. |
| `{ wait: 800 }` or `{ pause: 800 }` | Records 800 ms of whatever is on screen. |
| `{ zoom: target, scale: 2 }` | Zooms the camera onto the target until `{ zoom: "off" }`. This uses no time by itself, so add `hold` or a pause. |
| `{ caption: "text", ms: 2500 }` | Shows a caption until the next caption step, or for `ms`. `{ caption: "" }` hides it. |
| `{ goto: "page.html" }` | Navigates while recording. |
| `{ eval: "js code" }` | Runs JavaScript in the page, for example to stage something mid-demo. |

A target is a Playwright selector string (`"#save"`, `".row >> nth=2"`, `"text=Export"`) or an object: `{ role: "button", name: "Save" }`, `{ text: "Export" }`, `{ label: "Email" }`, `{ placeholder: "Search" }`. Role and text targets survive redesigns. CSS classes often don't.

Setup steps run before recording and are never seen: `{ goto }`, `{ reload: true }`, `{ localStorage: { key: value } }` (objects are stored as JSON), `{ eval }`, `{ click }`, `{ fill: target, text }`, `{ press }`, `{ wait: target }`.

## Top-level options

| Option | Default | Notes |
|---|---|---|
| `url` | | An http(s) URL, or a file path relative to the script. |
| `viewport` | 1280x720 | The CSS size of the page. A smaller viewport gives bigger text in the video. |
| `fps` | 60 | Final render frame rate. The preview always uses 24. |
| `seed` | 7 | Changes cursor arcs, aim jitter and typing rhythm. |
| `date`, `timezone`, `locale`, `colorScheme` | 2026-03-12 09:41, UTC, en-US, light | What the app sees. |
| `autoZoom`, `autoZoomArea` | true, 0.04 | Zoom in on clicks and typing when the target is smaller than this fraction of the viewport. |
| `freezeAnimations` | false | Turns off all CSS animation and transitions. Use it only if an app animation misbehaves under the virtual clock. |
| `hideCaret`, `hideScrollbars` | false, true | |
| `head`, `tail` | 700, 1200 | Still time at the start and end, in ms. |
| `cursorStart` | [0.62, 0.72] | Where the cursor starts, as a fraction of the viewport. |
| `css` | | Extra CSS to inject, for example to hide a cookie banner or a dev-only badge. |
| `pace` | see below | `moveMin` 400, `moveMax` 900, `moveBase` 380, `movePerPx` 0.45, `press` 90, `settle` 600, `hover` 800, `typeWpm` 110. |

`look` options:

| Option | Default | Notes |
|---|---|---|
| `background` | `"aurora"` | A preset (`aurora`, `sky`, `peach`, `mint`, `sunset`, `slate`, `ink`) or a hex colour. |
| `backgroundImage` | | An image path relative to the script. It fills the background. |
| `frame` | `"mac"` | `"mac"` adds a title bar with three dots, `"none"` shows only the rounded page. |
| `title` | `""` | The title bar text. |
| `padding`, `radius`, `shadow`, `border`, `frameColor` | 0.065, 14, true, true, auto | |
| `cursor`, `cursorSize`, `clickColor` | true, 1.25, `"40,40,60"` | `clickColor` is an RGB triple for the ripple. Use a light one on dark apps. |
| `zoom`, `maxZoom` | 1.8, 2.2 | The auto zoom amount and the upper limit for any zoom. Above about 2.2 the 2x capture starts to look soft. |
| `zoomLead`, `zoomSpring` | 280, 7 | How early the camera moves, in ms, and how quickly it settles. 7 settles in about 0.65 s. |
| `autoZoomGap`, `autoZoomLinger`, `autoZoomBridge` | 1800, 1100, 1500 | Actions closer than the gap share one zoom if they fit together. The camera stays in for the linger time, and zoomed-out gaps shorter than the bridge are skipped so the camera doesn't pump in and out. |
| `verticalZoom` | 1.9 | The base zoom of the 9:16 version. It always follows the cursor. |
| `size` | [1920, 1080] | The landscape output size. |
| `font` | Inter, then system fonts | The caption and title font. |

## Command line

```
node scripts/render.mjs <demo.mjs|demo.json> [--preview] [--vertical] [--stills 1,4.5] [--compose-only] [--keep] [--out file.mp4]
```

- `--preview` uses 24 fps, 1x pixels and a 960x540 output with a quick encode. The timing is the same as the final, so a preview is an accurate check.
- `--stills` writes composed JPEGs at those seconds into `stills/` next to the output. It uses the last capture.
- `--compose-only` redoes only pass 2. Use it after changing `look` or zoom settings. A final render deletes its frames unless you pass `--keep`.
- Work files go in `.screencast/<name>-preview` or `.screencast/<name>-final` next to the script. Add `.screencast/` to `.gitignore`.

Speed on a recent laptop: the example's 17 second preview takes about 25 seconds. The final at 60 fps and 2x takes a few minutes, mostly for screenshots.

## Making the app deterministic

- Data: seed it in `setup`. Anything that calls a live API can change between renders. Point the app at a local fixture or mock the route.
- Time: the date is fixed, and all page timers follow the virtual clock. A "saving..." spinner that waits 800 ms for a timer takes exactly 800 ms of video.
- Network: real requests still take real time, but no frames are captured while the renderer waits. A slow request can therefore appear instant. Use `{ wait: target }` so the result shows up on a frame.
- Fonts: the renderer waits for `document.fonts.ready`. Self-host web fonts so a slow font CDN can't change the layout.
- Random: `Math.random` is seeded. `crypto.getRandomValues` is not.

## Known limits

- Content in cross-origin iframes, `<video>` elements and WebGL keeps running on real time, so it may not match the cursor exactly.
- The native text caret blinks on real time. It is usually fine. Set `hideCaret: true` if it flickers.
- Drag and drop, file pickers, native `<select>` menus and browser dialogs are not supported. Use a custom dropdown or stage the result.
- A few transitions that the browser runs off the main thread (such as `transform` on `:active`) can differ by a sub-pixel between runs. Outside those, consecutive captures are pixel-identical.
- Only one browser tab is recorded.

## Checks used while building it

The example (`examples/tidy-demo.mjs` with the sample app in `examples/tidy/`) was rendered as a preview and a final at 60 fps, with the vertical version. Two preview captures were compared frame by frame. All cursor positions, events and timing matched exactly. The raw frames matched pixel for pixel except two frames during a button press transition, with a difference below visibility (PSNR 42 dB or higher). An earlier version reused a frame when only the scroll position had changed, because scroll events arrive a frame late. Scroll frames are now always captured.
