# The dashboard on a phone

The dashboard is laid out for a Raspberry Pi's 7" panel (800×480), for an
ESP32 (320×240) and for a desktop. A phone falls through none of those
breaks and lands at the smallest one. This is what that looks like, what
could be done about it, and what it would cost in risk.

Nothing here is implemented. The mock-ups were made by injecting a
stylesheet into the running dashboard, so the shipped code is untouched
and the photographs are of the real thing.

- [`docs/mockups/index.html`](mockups/index.html) — the gallery, with every
  photograph
- [`mockups/mobile.css`](../../mockups/mobile.css) — the proposal stylesheet
- [`mockups/render_mobile.py`](../../mockups/render_mobile.py) — the renderer

---

## What a phone gets today

A phone in portrait is 390×844. That is narrower than 560 px, so it lands
in the breakpoint that was written for a 3.5" LCD:

```css
@media(max-width:560px),(max-height:360px){
  .dash{grid-template-columns:minmax(0,1fr);
        grid-template-rows:minmax(0,1fr);
        grid-template-areas:"side"}
  .flow{display:none}          /* the diagram goes */
  .tiles{grid-template-columns:repeat(3,minmax(0,1fr))}
}
```

and, one breakpoint up, in the one written for anything under 640 px:

```css
@media(max-width:640px),(max-height:400px){
  body .dash[data-slide="power"] .flow{display:none}   /* the diagram goes */
  body .dash[data-slide="usage"] .octopus,
  body .dash[data-slide="usage"] .today{display:none}  /* the figures go */
  .totals{display:none}                                /* today's figures go */
}
```

So a phone gets the leftovers, and they are measured:

| | today on a phone | the Pi, for comparison |
|---|---|---|
| body text | 13 px | 15 px |
| tile figures | 17 px | 24 px |
| the diagram | dropped | rings, 25 px values, 31 px hub |
| today's figures | dropped (power slide) | a table |
| the Octopus figures | dropped (usage slide) | four tiles |
| the solar forecast | dropped (weather slide) | seven rows |
| the daylight band | dropped | drawn |
| the chart legend | cut off at the right | fits |

Two of those are not "small", they are *missing*: the solar forecast is
gone from the weather slide, and the Octopus figures — the rates, what the
meter says — are gone from the usage slide. The phone is not a small Pi;
it is the Pi with the interesting parts taken out.

And there is a lot of empty screen. The power slide puts five numbers in
the middle of a 390×844 screen and leaves the rest blank, because the
layout was drawn for a panel with no height to spare.

## Why it is this way, and what makes it hard

The dashboard has two rules that were written for a fixed panel:

**One screen, no scrolling.** `html,body{overflow:hidden}` and
`body{touch-action:none}`. The page is exactly one viewport, and the
browser is told to leave a swipe to the dashboard — a swipe turns the
deck. On a phone this is the wrong rule: a phone can scroll, and in
portrait it has height to spare.

**The diagram is the main event.** It is a 900-unit-wide drawing in an
SVG, scaled to whatever box it is given. On an 800 px panel the scale is
0.89 and its 25-unit type comes out at 22 px. On a 390 px phone the scale
is 0.43 and the same type comes out at **11 px** — below the legibility
floor the layout check enforces (14 px). No amount of CSS makes that
drawing legible at that width; it has to be drawn for the width.

## Three options

Each was built and photographed. The CSS is in `mockups/mobile.css`; each
block is scoped to `body[data-candidate="..."]`, and each is written to be
as specific as the rules it has to beat, so the layer can be applied to
the page as it stands.

### A — portrait, the numbers are the hero

The Live panel becomes the hero: the battery gauge on the left, the four
live figures in a two-up grid beside it, today's figures as a table
underneath. The usage slide keeps the chart, the Octopus figures and
today's tiles; the weather slide keeps the conditions, the solar forecast
and the week.

The page scrolls.

- **Touches:** the root overflow and `touch-action`, and the gesture
  arbitration — a vertical drag has to reach the browser without the
  horizontal swipe being lost. The slide model, the data, the diagram and
  the ESP32 client are untouched.
- **Gains:** the most legible option; figures at 26 px; every panel of
  every slide is present.
- **Costs:** the highest-risk option. `touch-action:none` on the body and
  `overflow:hidden` on the root are what make the kiosk reliable, and
  both are shared with the Pi. A mistake here is a kiosk that scrolls.
- **Also:** with scrolling, the phone needs a way to say that it can
  scroll — a scrollbar is not discoverable on a touch screen, and the
  panel's own `.scroll` regions already scroll silently.

### B — landscape, laid out like the Raspberry Pi

A phone in landscape is 844×390 — very close to the Pi's 800×480. Give
it the Pi's arrangement: the rings diagram across the top, the live
figures and today's underneath, with the type a little smaller because
the height is 390 rather than 480.

- **Touches:** one new media query. Nothing else — not the gesture code,
  not the root overflow, not the slide model, not the data.
- **Gains:** the full dashboard, diagram included, with 14 px figures and
  a 14 px diagram. It is the Pi's layout, so it inherits everything the
  Pi's layout already proves.
- **Costs:** the phone has to be turned sideways, and a phone in
  landscape is 390 px tall, which is a thin strip for a thumb.
- **Risk:** the lowest. A media query that only matches a band of
  viewports nobody else uses.

### C — portrait, with the diagram kept

The rings diagram stays, above the numbers, as a shape rather than a
chart: the hub in the middle, the four rings around it, the colours
telling the story. The numbers underneath carry the detail.

- **Touches:** the same layout work as A, minus the scrolling.
- **Gains:** the glanceable reading survives on a phone. The hub's
  100% is legible at 13 px; the ring values are not.
- **Costs:** the diagram's own type is 12 px, below the floor. A
  legible portrait diagram means a second drawing — a vertical stack of
  solar → inverter → house with the battery beside it — which is real
  work and is not mocked up here.

### What all three agree about

- **The settings page needs nothing.** It is already a single column
  with large tap targets, a sticky action bar and no horizontal overflow.
  It is the one part of the dashboard that is already phone-shaped.
- **The chart legend is cut off** at 390 px. It is a single flex row with
  no wrap, so the last label — "peak 16:00–19:00" — runs off the edge.
  A one-line fix, and it is a bug today, not a new one.
- **The usage slide is the one that already works.** The chart is drawn
  to fit and scales properly, and it is legible on a phone. The problem
  is the panels around it, not the chart.

## What could break, and how each option is insulated

The things that would be damaged by a mistake are the Raspberry Pi kiosk
and the ESP32 client, so the question of each option is: can it break
them?

| | Pi kiosk | ESP32 | desktop | phone |
|---|---|---|---|---|
| **A** | the shared root overflow and `touch-action` | untouched | untouched | improved |
| **B** | untouched | untouched | untouched | improved, sideways |
| **C** | untouched | untouched | untouched | improved |

The existing regression net:

- `tests/test_swipe.py` — the gesture: the 60 px distance, the 1.4
  sideways slop, the button exemption, and that the browser is told to
  leave the swipe to the page. Seventeen tests, and they read the real
  `static/index.html`, so a change to the gesture code fails them.
- `tests/test_diagram.py` — the rings geometry and the switching.
- `layout_check.py` — eight viewport sizes, asserting no scrolling, no
  truncation, and a legibility floor on the type. **It is not run by the
  test suite**, and it is run by hand.

That last point is the real gap. The layout guarantees — no scrolling,
nothing truncated, type never below the floor — are checked by a script
that nobody runs automatically. A phone implementation should add
390×844 and 844×390 to `layout_check.py`'s sizes, with their own
legibility floors (a phone is read closer than a wall panel, so its
floor should be *higher*, not lower), and the layout should be checked
by the same mechanism that keeps the Pi honest.

## A staged plan

Each stage is independently shippable, and each can be thrown away
without taking the others with it.

1. **Fix the legend.** One line, a bug today, no risk. It can go on its
   own and does not need any of the rest.
2. **Option B.** One media query, no shared code touched. Ship it, put
   the phone sizes in `layout_check.py`, and use a phone in landscape as
   a second display. This is the cheapest useful thing.
3. **Option A.** The proper portrait layout, and the first thing that
   touches shared code — so it goes in behind its own media query, with
   the Pi's breakpoints tested first and after.
4. **Option C, or a portrait diagram.** Only if the diagram is wanted on
   a portrait phone. It needs a second drawing, which is a piece of work
   in its own right.

## The questions to decide

1. **Which of the three?** B is the cheapest and the safest; A is the
   best on a phone and the riskiest to build; C is in between and needs
   more work to finish.
2. **May a phone scroll?** Option A says yes, and it is the only thing
   that makes the portrait layout worth having. If the answer is no,
   option C's shape is the ceiling.
3. **Does the diagram matter on a phone?** If yes, it needs a portrait
   drawing, and that is a piece of design work rather than a layout
   change.
4. **Is the phone a display or a control?** A phone can also be the one
   device used to change the settings — and to switch slides, and to
   wake the inverter. Today the answer is "display only", because the
   slides are the server's business and the settings page is a separate
   page. If a phone should be able to *drive* the dashboard, that is a
   different piece of work again, and it would want the same no-scroll
   rule the Pi has.
