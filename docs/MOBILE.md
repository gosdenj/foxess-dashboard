# The dashboard on a phone

The dashboard is laid out for a Raspberry Pi's 7" panel (800×480), for an
ESP32 (320×240) and for a desktop. A phone fell through none of those
breaks and landed at the smallest one. This is what that was, what was
done about it, and what the alternatives would have cost.

**Option A is now shipped** — the phone in portrait, the numbers as the
hero, and the slide allowed to scroll. The mock-ups below are the
investigation that led to it, and they are kept as the record of what the
other two options would have looked like.

- [`docs/mockups/index.html`](mockups/index.html) — the gallery, with every
  photograph
- [`mockups/mobile.css`](../../mockups/mobile.css) — the proposal stylesheet
- [`mockups/render_mobile.py`](../../mockups/render_mobile.py) — the renderer

---

## What a phone used to get

A phone in portrait is 390×844. That is narrower than 560 px, so it landed
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

So a phone got the leftovers, and they were measured:

| | the phone used to get | the Pi, for comparison |
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
meter says — are gone from the usage slide. The phone was not a small Pi;
it was the Pi with the interesting parts taken out.

And there was a lot of empty screen. The power slide put five numbers in
the middle of a 390×844 screen and left the rest blank, because the
layout was drawn for a panel with no height to spare.

## Why it was this way, and what made it hard

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

## What ships

**A — portrait, the numbers are the hero.** The Live panel is the hero:
the battery ring on the left, the four live figures in a two-up grid
beside it, today's figures as a table underneath. The usage slide keeps
the chart, the Octopus figures and today's tiles; the weather slide keeps
the conditions, the solar forecast and the week. The page scrolls.

- **Touches:** the root overflow and `touch-action`, and the gesture
  arbitration — a vertical drag has to reach the browser without the
  horizontal swipe being lost. The slide model, the data and the ESP32
  client are untouched.
- **Gains:** the most legible option; figures at 26 px; every panel of
  every slide is present.
- **Costs:** the highest-risk option. `touch-action:none` on the body and
  `overflow:hidden` on the root are what make the kiosk reliable, and
  both are shared with the Pi. A mistake here is a kiosk that scrolls.

The rules live in one media query, and it is deliberately narrow:

```css
@media (max-width:560px) and (min-height:600px){ ... }
```

A phone in portrait is the only thing that matches it: the Pi is 800×480,
the small LCD 480×320, the ESP32 320×240 and the narrow panel 320×480, so
none of them is both narrow enough and tall enough. The band cannot reach
them, and `tests/test_phone_layout.py` says so.

The photographs of it are at the top of the
[gallery](mockups/index.html), taken of the shipped page with nothing
injected: the battery ring and the live figures, then today's figures as a
table underneath.

Three things had to be got right, and each of them was got wrong first:

1. **The swipe.** `touch-action:pan-y` gives the browser the vertical drag
   and leaves the horizontal one, so a sideways swipe is still the deck's.
   The gesture code itself was not touched: the same 60 px, the same
   1.4 sideways slop, the same pointercancel that forgets a gesture the
   browser has taken for a scroll.
2. **The panels.** A grid row cannot size itself to a panel's content
   when the panel is allowed to shrink, and a panel that shrinks clips
   its own last row — the "Today" panel was losing its last line by two
   pixels at 360×800. The phone's slide is therefore a **column of
   panels**, each as tall as it needs to be, rather than a grid.
3. **The tiles.** The usage slide puts today's tiles in six columns on a
   narrow screen, and six columns of "9,999.99 kWh" is a number with its
   end cut off. On a phone they are two columns.

A fourth thing was wrong as well, and it was found by using it: **the
stage did not scroll.** The slide was a grid item of the stage, and a
grid hands its items a definite height — so the slide was exactly as tall
as the screen and everything below the fold was clipped by its own
`overflow:hidden`. Clipped is not the same as scrollable: the content
was gone, and the page sat still while the reader pushed on it. The
stage is a plain block on a phone now, so the slide grows with its
content and the stage scrolls it.

And a fifth, which only a scrolling stage can have: **a new slide has
to start at the top.** The reader scrolls the usage slide down to reach
the figures at the bottom, swipes for the next one, and lands in the
middle of it. So the stage is put back to the top when the slide
changes.

The chart legend was also cut off at 390 px — one flex row with no wrap.
On a phone it wraps; the smaller screens are left exactly as they were,
because at 320 px there is no room for a second row either.

### How it was checked that nothing else moved

The layout guarantees — no scrolling, nothing truncated, type never below
the floor — are checked by `layout_check.py`, which now knows about the
phone and has **higher** floors for it (body 14 px, figures 20 px), because
a phone is read closer than a panel on a wall.

It also measures two things about the scroll itself, which it did not
measure before, and which is how the stage was found not to be scrolling:

- **`dashClipped`** — the slide must not be shorter than its own content.
- **`dashReach`** — everything in the slide has to be reachable by
  scrolling the stage.

Against the layout as it first shipped, the first of those reports
`SLIDE-CLIPPED:19` on the usage slide at 360×800, and the check fails.

On top of that, the geometry of every existing display was measured before
and after the change — the position, size and type size of every panel,
tile, table row and SVG label, at seven sizes on three slides — and
compared:

```
geometry identical: 21/27
  (the six that changed are the two phone sizes on the three slides)
```

Not one pixel of the Pi, the desktop, the laptop, the tablet, the small
LCD, the ESP32 or the narrow panel moved.

## The two options that were not taken

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

It is still there to be taken up: the mock-up is in the gallery, and the
rules are in `mockups/mobile.css`.

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

## What all three agree about

- **The settings page needs nothing.** It is already a single column
  with large tap targets, a sticky action bar and no horizontal overflow.
  It is the one part of the dashboard that is already phone-shaped.
- **The usage slide is the one that already worked.** The chart is drawn
  to fit and scales properly, and it is legible on a phone. The problem
  was the panels around it, not the chart.

## Still open

1. **A phone in landscape** still gets the old small-screen layout. It
   is the one size that has not been designed: option B is the answer,
   and it is one media query away.
2. **The daylight band** is dropped on a phone, because it is a strip
   for the whole day and there is no width for it.
3. **How far does the phone drive?** It turns the deck: the sideways
   swipe still works, so a phone can move the slides on and the server
   follows. It cannot *choose* a slide from a list, or wake the
   inverter, or change a setting without going to the settings page —
   those would be a different piece of work, and they would want the
   same no-scroll rule the Pi has.
