# vox-explainer: research and design notes

## What makes the Vox look (research, Nov 2026)

- **The team and tools.** Vox's video team, founded in 2014 with Joss Fong as a founding member, built its style on motion graphics and narration instead of on-camera hosts. All motion graphics are made in After Effects, sometimes with Photoshop and Illustrator, and the music comes from a licensed library (APM).
- **Analog, not digital.** The look is deliberately imperfect: paper texture, film grain, torn edges, cut-out photos, hand-placed annotations. Graphics are animated "on twos", 12 drawings a second inside a 24 or 30 fps video, which gives a slight stop-motion stutter. Motion blur is off, and moves are snappy rather than glossy.
- **The highlighter** is the most copied part. It's a yellow marker sweep across one phrase, timed to the moment the narrator says it: about 0.35–0.5 s, left to right, with the text showing through. Use one highlight per scene.
- **Five devices carry most explainers:** a highlighted document, an archival photo with a slow push-in, a minimal map where a place lights up as it's named, a simple chart that builds, and a pull quote on a paper strip.
- **Structure.** Open on the surprising fact, not on setup. One question, answered in beats of one idea each. Every line has one piece of on-screen evidence. Narration runs at 140–160 words a minute.
- **Pace.** Short-form explainers that perform run about 2.8 s a beat. A 3.6 s pace read as a list.
- **Common mistakes:** too many highlighted words, bright UI colours mixed with newsprint textures, changing styles halfway through, frames that hold still, and caption bars stuck under the picture instead of type set inside it.

Sources:
- [Videotto: how to make a Vox-style explainer](https://www.videotto.com/blog/how-to-make-a-vox-style-explainer)
- [Video Consortium: Joss Fong](https://videoconsortium.org/mag/episode-1-joss-fong)
- [The Long Story: former Vox journalists on YouTube](https://thelongstory.substack.com/p/why-the-best-journalists-on-youtube)
- [Adobe community: posterize time to 12 fps](https://community.adobe.com/questions-529/after-effects-why-set-posterize-time-to-12-fps-in-24fps-comp-54684)
- [Pixflow: stop motion in After Effects](https://pixflow.net/blog/how-to-master-stop-motion-animation-in-after-effects/)
- [sadekxD vox-style-collage-video-skill](https://github.com/sadekxD/vox-style-collage-video-skill/blob/main/SKILL.md)
- [PremiumBeat: making maps for Johnny Harris](https://www.premiumbeat.com/blog/making-maps-for-johnny-harris/)
- The vox-animation skill by Charlie Hills (MIT)

## Why this engine is built this way

- **`render(t)` is a pure function of time.** The renderer asks for frame N, and the page draws exactly that moment. Frame-stepping is exact, any frame can be checked as a still, and nothing drifts. This is the same lesson as the vox-animation skill's long-form build.
- **Timing comes from the voice.** `build.py` resolves every `at`, `mark` and `items_at` to the millisecond using Whisper word times. If you change a line, re-record it and rebuild, and the picture follows.
- **Code, not generated video, for most beats.** At about 2.8 s a beat, generated video costs a lot, can't land an annotation on an exact word, and can't spell. Image generation is used only for cut-out objects.
- **No black bars, no caption bar.** All type sits inside the composition on the same paper surface.

## Problems hit while building, and the fixes

- **The motion check flagged held beats** (3.2 s still), because a 12 px drift is invisible at check resolution. Fix: a slow push-in about the centre on every beat (about 7% over 3 s), plus a drift of the colour blocks. Scaling about the centre never pushes content off screen; a whole-frame translate would.
- **The motion check gave false failures** on a visible slow push-in, because it compared frames 0.1 s apart. Fix: compare each frame with the one 0.5 s earlier, which is the actual rule. A frozen 4 s test clip still fails, as it should.
- **Highlighted white type** ("SOC 2" in cream on yellow) was unreadable. Fix: inside `big` and `sub`, the marked text turns to ink as the sweep passes.
- **The highlight's padding** added visible gaps around the marked words. Fix: a negative margin matching the padding.
- **Type on cards was too small** to read as Vox. Fix: serif text 4.6u (92 px at 1080p), labels 1.55u, card width 72%.
- **Screenshots** lost the top of the page to the push-in crop. Fix: anchor them to the top.
- **Cookie banners** in screenshots: shot.mjs declines consent where it can and hides leftover banners.

## Style C (her pick, Oct 28 2026)

She chose this over an editorial off-white serif style and a dark product-native style. She rejected beige cards on beige paper with lots of small objects, tape and shadows ("aesthetics down the drain"), an old-Mac CRT frame, and purple graphics. Grain stays light, around 0.1 opacity, because heavier looks muddy.
