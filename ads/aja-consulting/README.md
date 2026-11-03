# AJA Consulting – animated ad

A 20-second vertical (1080×1920) ad for AJA Consulting's service: a sales agency for gyms that
takes their web leads, incoming calls and walk-in leads and turns them into new members.
It was built from [`prompts/animated-ad-template.md`](../../prompts/animated-ad-template.md).

| File | What it is |
|---|---|
| `aja-annons.mp4` | The rendered video, 30 fps, no audio (add music in TikTok/CapCut/Reels) |
| `aja-annons.html` | The ad itself: one HTML file, Three.js + GSAP + CSS |
| `render.cjs` | Renders the HTML frame by frame into the MP4 (or stills) |

All copy comes from the AJA Consulting website (`alternativ-1.html`), except the notification
cards and the "Obesvarade leads" counter in the opening, which dramatise the problem.

## Viewing and editing

- Open `aja-annons.html` in a browser. It plays with a scrubber, play/pause and replay.
  `?clean=1` hides the controls (for screen recording), `?t=12` starts at 12 s,
  `?guides=1` shows the TikTok/Reels safe zones.
- All copy, colours and timings are in the `CONFIG` object at the top of the script.
  For example, change `ctaContact` to the website address once the domain is live.

## Re-rendering the MP4

```sh
npm install                      # gsap, three and the Archivo font, used offline by the renderer
node render.cjs                  # -> aja-annons.mp4
node render.cjs --stills 3,9.5   # -> frames/still-3.00.png, frames/still-9.50.png
```

Needs Node.js, Playwright with Chromium, and ffmpeg. The renderer calls `window.seek(t)` for
every frame, so GSAP, the CSS animations and Three.js land on exactly the same moment, and the
video is perfectly smooth regardless of how fast the machine is.

## Beat sheet

| Time | What the viewer sees | Layer |
|---|---|---|
| 0–2.6 s | "Ni har leads." Notification cards (web leads, missed calls, guest passes) pile up; the "Obesvarade leads" counter climbs to 38 | GSAP cards and type, CSS pulses and ringing phones, Three.js dust |
| 2.6–4.8 s | The cards fall away: "Men ingen tid att ringa tillbaka?" | GSAP |
| 4.8–8.4 s | A gold line connects; three particle streams (Webben, Telefonen, Receptionen) merge into one gold flow: "Tre leadkällor. Ett säljflöde." | Three.js streams with bloom, GSAP labels and line drawing |
| 8.4–13.2 s | The flow collapses into a gold point that becomes the dot of the website's lead card; Sara L. goes from lead to "Ny medlem"; "+1 medlem" bursts into gold dust | Three.js match cut, GSAP card build, clip-path reveal and line drawing |
| 13.2–16 s | 20 000+ members activated · 25+ years of sales experience · 100% focus on fitness, with the website's gold merit band scrolling | GSAP counters, CSS marquee, Three.js ring |
| 16–20 s | The particles assemble into the AJA monogram; "Era leads. Våra samtal. Fler medlemmar."; "Boka ett samtal" button, email, "Svar inom 24 timmar" | Three.js logo formation, GSAP type, CSS button shine and pulse |

## The brief used

```text
Product:        AJA Consulting — sales agency (telemarketing) for the fitness industry
Audience:       gym and fitness centre owners/managers in Sweden
One message:    Era leads. Våra samtal. Fler medlemmar.
Proof points:   the lead flow from the website (lead → samtal → ny medlem, dokumenterat i ert CRM) ·
                20 000+ medlemmar aktiverade · 25+ års samlad säljerfarenhet · 100% fokus på fitnessbranschen
Call to action: "Boka ett samtal" — info@ajaconsulting.se
Tone:           premium, confident, calm
Format:         1080×1920 vertical · 20 seconds · no loop · Swedish
Brand:          colors #D4AF5F (gold) #F2EEE6 (cream) #0E0E0D (iron) · font Archivo (62% headlines, 125% labels)
Logo:           the AJA gold monogram from the website
Hero 3D object: a particle system: every particle is a lead, going from chaos to one gold flow
Vibe/reference: the website's "Performance" look: iron black bands, cream type, gold as the only accent
Avoid:          claims that aren't on the website, other brands' logos
```
