# Animated ad prompt template (Three.js + GSAP + CSS)

Fill in the `[BRACKETS]` in the BRIEF section and paste the whole block into Claude.
Delete any brief line you don't care about and Claude will decide it.
The other sections are fixed rules; change them only if you know you want something different.

The result is one HTML file. It plays in a browser, has a scrubber for reviewing, and exposes
`window.seek(t)` so it can be rendered frame by frame into a smooth MP4 (headless Chrome + ffmpeg).

```text
You are a senior motion designer and creative technologist. Create a finished,
ad-quality animated video ad as ONE self-contained HTML file that combines
Three.js (3D hero), GSAP (all choreography) and CSS (texture and ambient motion).

=== BRIEF (fill in; delete any line to let Claude decide) ===
Product:        [NAME] — [what it is, in one line]
Audience:       [who it's for]
One message:    [the single thing viewers must remember]
Proof points:   [benefit 1] · [benefit 2] · [benefit 3]
Call to action: [e.g. "Download free"] — [URL or @handle]
Tone:           [premium & calm | bold & energetic | playful | ...]
Format:         [1080×1920 vertical] · [15] seconds · [no loop]
Brand:          colors [#primary] [#secondary] [#background] · font [Google Font name]
Logo:           [paste the logo's SVG code, or describe it for a simple wordmark]
Hero 3D object: [the product, or a symbol of it: "glass perfume bottle", "phone", "glowing orb"]
Vibe/reference: [e.g. "Apple product film", "Nike energy", "Stripe gradients"]
Avoid:          [anything off-limits]

=== STORY (scale the timings to the length) ===
1. HOOK (0–2s)     Motion in the first 0.3s. A bold visual surprise or question.
2. TENSION (2–4s)  The problem or desire, max 6 words of kinetic type.
3. REVEAL (4–7s)   The hero 3D object enters: camera push, light sweep across it.
4. PROOF (7–12s)   One beat per proof point: icon draws on, short headline,
                   a 3D or UI moment that shows it.
5. CTA (12–15s)    Logo, CTA button, URL/handle. Hold the final frame at least 1s.

=== WHO DOES WHAT ===
Three.js: three@0.170.0 via an import map from jsDelivr (with three/addons/)
- Hero object built from primitives or procedural geometry (no external models/textures).
- MeshPhysicalMaterial, RoomEnvironment + PMREMGenerator lighting, ACESFilmic tone
  mapping, subtle UnrealBloomPass (strength ~0.4, threshold ~0.8) + OutputPass.
- Particles or abstract shapes for depth; camera moves. Transparent canvas at the back.
GSAP: gsap@3.13.0 from jsDelivr with SplitText, DrawSVGPlugin, MorphSVGPlugin,
MotionPathPlugin and CustomEase (all free since 3.13)
- ONE master timeline with a label per story beat. Nothing animates outside it.
- Kinetic type (SplitText with mask: "lines" or "words"), SVG icons drawing on,
  shape morphs, UI cards, transitions.
- Drives Three.js by tweening a plain STATE object (camera position, rotations,
  light intensity, shader uniforms) that the render function reads.
CSS
- Stage layout, gradients, glass panels, film grain + vignette overlay.
- Small ambient loops (button shimmer, gradient drift, pulse rings) with
  @keyframes and @property.

=== MOTION RULES ===
- Easing: expo.out for arrivals, power3.inOut for camera moves and transitions,
  back.out(1.6) for pops. No linear easing except progress bars.
- Stagger 0.04–0.08s. Arrivals overshoot slightly, then settle.
- One focal point at a time; at most two things start together.
- Hold every readable moment at least 0.8s. Max 6 words on screen at once;
  text at least 64px tall at 1080px width.
- Motivated transitions: match cuts (the 3D object becomes a UI shape), mask wipes,
  camera pushes through objects. No plain crossfades.
- Depth: parallax between layers, soft shadows, light sweeps across the hero.
- Safe zone (vertical video): keep text and logo out of the top 8%, the bottom 20%
  and the right 12%, where TikTok/Reels put their buttons and captions.

=== TECHNICAL CONTRACT ===
- One HTML file. External files only from jsDelivr (pinned versions above) and
  Google Fonts. No other images, models, textures or audio.
- Fixed [1080×1920] stage, scaled to fit the window with a CSS transform, letterboxed.
- All copy, colors and timings in a CONFIG object at the top of the script.
- Deterministic: seeded random; never use Date.now() or performance.now() for
  animation; continuous 3D motion (spins, drifts) is computed from the master
  timeline's time.
- One render loop: gsap.ticker renders Three.js. Pixel ratio capped at 2,
  preserveDrawingBuffer: true.
- Build everything after document.fonts.ready (SplitText must split after fonts load).
- Expose on window, for frame-by-frame video export:
    DURATION  total length in seconds
    ready     a Promise that resolves once everything is built
    seek(t)   pauses everything, sets the master timeline to t, pauses every CSS
              animation from document.getAnimations() and sets its currentTime to
              t*1000, then renders one Three.js frame
- Controls: play/pause, replay, scrubber, time readout. ?clean=1 hides them;
  ?t=5 jumps to 5s.
- prefers-reduced-motion shows the static end card. No console errors.

=== OUTPUT ===
1. A beat sheet table: time | what the viewer sees | layer (3D / GSAP / CSS) | easing.
2. The complete HTML file in one code block. No placeholders, no "..." omissions.
3. Three ideas to make it even better.
```

## Example brief

What a filled-in BRIEF section looks like, for a made-up product:

```text
Product:        Lumen — a smart desk lamp that adapts to how you work
Audience:       remote workers and students
One message:    Light that keeps you focused
Proof points:   follows daylight automatically · built-in focus timer · warm light after sunset
Call to action: "Pre-order now" — lumenlamp.example
Tone:           premium & calm
Format:         1080×1920 vertical · 15 seconds · no loop
Brand:          colors #F5B544 #1E2A4A #0B0D14 · font Manrope
Logo:           simple lowercase wordmark "lumen" with a dot above the u
Hero 3D object: a minimal arc desk lamp with a softly glowing head
Vibe/reference: Apple product film, soft studio light, lots of negative space
Avoid:          busy backgrounds, stock-photo people
```

## Tips for follow-up messages

- Ask for one or two changes per message, in motion terms:
  "the CTA arrives too flat, use back.out(1.6) over 0.5s", "hold the reveal 0.5s longer".
- Because every timing lives in `CONFIG`, you can also ask Claude to retime a single beat
  without touching the rest.
- If the 3D looks washed out, ask for less bloom (lower strength, higher threshold).
