---
version: alpha
name: Pit Wall
description: >
  Visual identity for the racecar-35 cloud review UI. A dark, instrument-cluster
  aesthetic that mirrors the in-car dash so the at-home review surface feels
  continuous with the at-track experience.
colors:
  primary: "#FFB020"
  secondary: "#8A92A3"
  tertiary: "#6CD07A"
  neutral: "#E6E8EE"
  bg: "#0E1014"
  surface: "#181B22"
  surface-2: "#20242E"
  surface-3: "#2A2F3A"
  line: "#2A2F3A"
  on-surface: "#E6E8EE"
  on-surface-muted: "#8A92A3"
  on-primary: "#1A1300"
  accent-dim: "#6A4A10"
  error: "#FF5D5D"
  warn: "#F2C14E"
  good: "#6CD07A"
  track-line: "#FFB020"
  car-dot: "#FFB020"
typography:
  display:
    fontFamily: Inter
    fontSize: 28px
    fontWeight: 600
    lineHeight: 1.1
    letterSpacing: -0.01em
  headline:
    fontFamily: Inter
    fontSize: 16px
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: 0.02em
  body-md:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: 400
    lineHeight: 1.45
  body-sm:
    fontFamily: Inter
    fontSize: 13px
    fontWeight: 400
    lineHeight: 1.45
  label-caps:
    fontFamily: Inter
    fontSize: 11px
    fontWeight: 600
    lineHeight: 1
    letterSpacing: 0.08em
  telemetry-lg:
    fontFamily: JetBrains Mono
    fontSize: 36px
    fontWeight: 600
    lineHeight: 1
    fontFeature: "'tnum' 1, 'zero' 1"
  telemetry-md:
    fontFamily: JetBrains Mono
    fontSize: 18px
    fontWeight: 500
    lineHeight: 1.1
    fontFeature: "'tnum' 1, 'zero' 1"
  telemetry-sm:
    fontFamily: JetBrains Mono
    fontSize: 13px
    fontWeight: 400
    lineHeight: 1.3
    fontFeature: "'tnum' 1, 'zero' 1"
rounded:
  none: 0px
  sm: 4px
  md: 8px
  lg: 12px
  xl: 16px
  full: 9999px
spacing:
  xs: 4px
  sm: 8px
  md: 16px
  lg: 24px
  xl: 32px
  xxl: 48px
  gutter: 16px
  margin: 24px
components:
  surface-card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.on-surface}"
    rounded: "{rounded.md}"
    padding: 16px
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-primary}"
    rounded: "{rounded.sm}"
    padding: 8px
    typography: "{typography.label-caps}"
  button-primary-hover:
    backgroundColor: "#FFC04A"
  button-ghost:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.on-surface}"
    rounded: "{rounded.sm}"
    padding: 8px
  input-text:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.on-surface}"
    rounded: "{rounded.sm}"
    padding: 8px
  pill:
    backgroundColor: "{colors.surface-2}"
    textColor: "{colors.on-surface-muted}"
    rounded: "{rounded.full}"
    padding: 2px
    typography: "{typography.label-caps}"
  row-hover:
    backgroundColor: "#1F1A0F"
  telemetry-tile:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.on-surface}"
    rounded: "{rounded.md}"
    padding: 16px
  slider-track:
    backgroundColor: "{colors.surface-3}"
    height: 4px
    rounded: "{rounded.full}"
  slider-thumb:
    backgroundColor: "{colors.primary}"
    size: 16px
    rounded: "{rounded.full}"
---

## Overview

**Pit Wall** is the visual identity for the at-home review surface of the
racecar-35 telemetry system. The driver sees a dark dash in the car (RPM bar
across the top, big saffron speed digits, technical labels in mono). The
review UI is its continuation: when an engineer pulls up a session after
the session, the screen should feel like *the same instrument*, not a
generic web app.

The product feels closest to **a pit-wall timing tool** (think MoTeC i2,
AiM RaceStudio, Garmin Catalyst): purposeful, dense without being crowded,
high-contrast, and unambiguously technical. Levity and decoration are
deliberately absent. The brand's whole personality is in:

- the saffron accent (`#FFB020`) inherited from the in-car RPM bar,
- monospaced telemetry numerals with tabular figures,
- generous negative space around dense data so the eye can lock on
  one value at a time.

Target user is a driver/engineer reviewing a session on a laptop or a tablet
trackside, usually outdoors, usually in a hurry. Decisions favor legibility
under glare over visual delight.

## Colors

Two-tier dark palette with a single warm accent. Every saturated color does
exactly one job — there is no "decorative" color in this system.

- **Primary / Saffron (#FFB020):** The single interaction and live-data accent.
  Used for the car dot on the map, the slider thumb, the recording indicator,
  the in-car RPM bar, and exactly one primary action per screen. Never use
  for body text or decorative elements.
- **Secondary / Slate (#8A92A3):** Captions, axis labels, metadata, table
  headers. The "quiet" text that supports without competing.
- **Tertiary / Track Green (#6CD07A):** Reserved for positive state — fix
  acquired, upload complete, recording started.
- **Background (#0E1014):** Near-black. Not pure black, to avoid OLED blooming
  on glossy laptop screens and to leave room for a deeper "void" below cards.
- **Surface / Surface-2 / Surface-3 (#181B22 / #20242E / #2A2F3A):** Three
  flat elevation tiers. Cards sit on surface; nested controls (chips,
  pills, slider track) sit on surface-2; pressed/hovered states use surface-3.
- **Error (#FF5D5D):** Reserved for upload failure, RPM redline, no-fix.
  Never used for ambient state.

## Typography

The system uses two type families:

- **Inter** for all UI prose, headlines, labels, table content.
- **JetBrains Mono** for any telemetry numeral, timestamp, lat/lon, RPM,
  speed, file size. Tabular figures (`tnum`) are mandatory so digits do
  not shift left/right as values tick — the dash uses a fixed-width
  background pad for the same reason, and the review surface inherits
  the discipline.

Sizes are deliberately few. The hierarchy is `display` (page title) →
`headline` (card title) → `body-md` (default prose) → `label-caps`
(uppercase, tracked-out, used for column headers and unit annotations).
Telemetry comes in three sizes: `telemetry-lg` for the current-value
hero readout (speed, RPM), `telemetry-md` for secondary tiles (lat, lon,
heading), `telemetry-sm` for in-table cells.

**Label-caps** is uppercase with `0.08em` letter-spacing. This is the
single typographic "tell" of the system; it should appear on every
card title strip and column header.

## Layout

Max content width is **1400px**, centered. Below that the layout is fluid.

The review page uses a two-pane composition:
- **Left pane (60%)**: map. Track centerline drawn from the GPS samples,
  car dot rendered at the currently-scrubbed sample.
- **Right pane (40%)**: telemetry tiles, stacked. Hero speed at top,
  RPM + lat/lon + heading + fix below in a 2×2 grid.

Below both panes spans the full-width **scrub bar**: a slider whose value
is the sample index, plus play/pause, plus a timestamp readout.

Spacing follows a strict 8-px scale (`spacing.sm` = 8, `md` = 16, `lg` = 24).
Card internal padding is always `md`. Gap between cards in a grid is `md`.
Page padding (`margin`) is `lg`.

## Elevation & Depth

Flat. No shadows, no blurs. Hierarchy is conveyed by **tonal layering**:
content on `surface` is above the page, nested controls move up to
`surface-2`, hover/pressed states move up to `surface-3`. The 1-px
`line` border between cards and rows replaces the role a shadow would
play in a light theme.

The single exception is the saffron accent glow on the header status dot
(`box-shadow: 0 0 8px primary`) — a deliberate echo of an instrument-cluster
indicator lamp, used only on the small "live" indicator and the car dot
on the map.

## Shapes

Slightly soft. `rounded.sm` (4px) for inputs, buttons, pills inside dense
tables. `rounded.md` (8px) for cards and tiles. `rounded.full` for the
small status dot, the slider thumb, and the car dot. **Never mix sharp
and rounded corners in the same composition** — every interactive shape
in this system has at least `rounded.sm`.

## Components

- **Buttons.** Primary buttons use the saffron accent on near-black text
  (`on-primary`). One per screen — usually "Play" on the review page or
  "Download" on the index. Ghost buttons (surface-2 background) for
  everything else.
- **Input fields.** Surface background, 1-px line border. On focus the
  border switches to saffron — no glow, no shadow. The search input on
  the index page is the canonical example.
- **Pills.** Tiny rounded-full chips used for inline metadata (sample
  count, file size badges). Uppercase label-caps text in muted slate
  on surface-2.
- **Telemetry tiles.** Surface card with a label-caps title strip at the
  top in muted slate, followed by the telemetry-lg/md value in saffron
  for "live" readouts and on-surface white for static metadata.
- **Slider (scrub bar).** 4-px tall surface-3 track, 16-px saffron thumb,
  no tick marks. Below the track sits a tiny mono-typed timestamp that
  updates as you drag.
- **Map.** OpenStreetMap "dark" tiles. Track centerline drawn as a 3-px
  saffron polyline at 40% opacity. Car position drawn as an 8-px saffron
  filled circle with a 2-px solid stroke at 100% opacity — high enough
  contrast to stay readable when the centerline runs underneath it.

## Do's and Don'ts

- **Do** use the saffron accent for exactly one purpose per surface:
  current state / current position / primary action.
- **Don't** use saffron for prose, dividers, or decoration. If it appears
  more than ~3 times on a screen, something is wrong.
- **Do** use JetBrains Mono with tabular figures for every numeral that
  changes over time (RPM, speed, lat/lon, file size, timestamp).
- **Don't** use Inter for telemetry numerals — proportional digits will
  jitter horizontally as values tick.
- **Do** maintain WCAG AA contrast (4.5:1) for all body text against
  surface. The neutral-on-surface pair clears 13:1.
- **Don't** introduce a second saturated accent color. New states map onto
  the existing tertiary (green, success) or error (red, failure).
- **Do** uppercase + letter-space all card and column titles (`label-caps`).
- **Don't** add drop shadows, glows, or gradients to cards. Tonal layering
  only. The single permitted glow is the header status dot.
