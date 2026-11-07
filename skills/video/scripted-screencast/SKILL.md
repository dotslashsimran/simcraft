---
name: scripted-screencast
description: Turn a browser product demo written as a script (click, type, hover, scroll, zoom, caption) into a polished Screen Studio style recording. You get a smooth human-like cursor, click ripples, automatic zoom on small targets, typing at human speed, a padded background with a rounded window frame, burned-in captions and an optional 9:16 version that follows the cursor. It is frame-perfect and needs no retakes, and you can re-render it in minutes after the UI changes. Use when the user says "record a demo", "screen recording", "product walkthrough video", "Screen Studio style", "demo video of my app", "make a screencast", or wants to re-record a demo after a UI change.
---

# Scripted screencast

You write the demo as a list of steps. The renderer drives the app in a headless browser on a virtual clock and takes one screenshot per frame. It then adds the cursor, the zoom, the frame and the captions, and encodes the video. Every run of the same script gives the same video.

Setup, once: `cd ~/simcraft && npm i` (Playwright), and ffmpeg on the PATH or in `FFMPEG`.

## Steps

1. **Stage the app first.** Run it locally and fill it with believable data under a fictional brand: real-looking names, a day already half done, no empty states and no "test123". Put the staging in the script's `setup` (seeded localStorage, an API call, a login with a demo account) so every render starts from the same state. Never type real credentials into any site.
2. **Write the script** next to the project as `demo.mjs` (or `.json`). Start from [examples/tidy-demo.mjs](examples/tidy-demo.mjs). Use one caption per idea and keep the whole thing between 20 and 60 seconds.
3. **Preview.** This runs at 24 fps and half size, and takes about 30 seconds:
   ```
   node ~/simcraft/skills/video/scripted-screencast/scripts/render.mjs demo.mjs --preview
   ```
   Then check the zoom and framing at a few moments, and look at the stills:
   ```
   node .../render.mjs demo.mjs --preview --stills 1,4.5,9
   ```
   If a step fails, the error names the step and saves `failed-step-N.png` showing the page at that moment.
4. **Final.** This renders 60 fps at 2x pixels into a 1920x1080 video. Add `--vertical` for a 1080x1920 version as well:
   ```
   node .../render.mjs demo.mjs --vertical
   ```
5. **Report** what was staged (data, fake brand, anything faked) and give the output paths.

## Script

```js
export default {
  url: "http://localhost:3000",          // or a file path relative to the script
  viewport: { width: 1280, height: 720 }, // 1280x720 keeps text big enough for phones
  look: { background: "peach", title: "Tidy" },
  setup: [ { localStorage: { "tasks": [...] } }, { reload: true } ],  // not recorded
  steps: [
    { caption: "Add a task in seconds" },
    { click: "#newBtn" },
    { type: "Sign the gallery contract", into: "#title" },
    { click: { role: "button", name: "High" } },
    { press: "Enter" },
    { wait: ".toast" },
    { scroll: 400 },
    { zoom: ".chart" }, { pause: 1500 }, { zoom: "off" },
  ],
};
```

Targets can be a Playwright selector (`"#id"`, `"text=Save"`) or `{ role, name }`, `{ text }`, `{ label }` or `{ placeholder }`. Prefer role and visible text, because they survive redesigns better than CSS classes. The full step list, every option and all the look settings are in [reference.md](reference.md).

## Pacing rules

- Cursor moves take 400 to 900 ms, depending on the distance. This is automatic. Only set `moveMs` for a deliberate slow move.
- Hold about 600 ms after each result so people can read it. This is automatic after click, type and press. Use `hold` on a step for a bigger moment, such as 1200 ms after the main payoff.
- Zoom only when the target is small. Auto zoom does this for targets under 4% of the screen and stays in while actions are close together. Use an explicit `zoom` for a result you want people to read, and `zoom: "off"` before a wide view.
- Give the opening a second of stillness. The default `head` does this.
- One caption per idea, under 8 words, shown before the action it describes.

## After a UI change

Run the same command again. The script finds elements by role and text, so most changes need no edits. If a step fails, fix only that target (the error names it and saves a screenshot), preview, then do the final render. To change only the look (background, zoom, captions) without capturing again, render the final with `--keep` once, then use `--compose-only`.

## Rules

- Fictional brands and data only. Never record real customer data, real credentials or someone else's product presented as yours.
- Always preview before the final render and look at the stills yourself.
- Don't fake speed with `pace`: if the app is slow, stage the result or `wait` for it on camera.
