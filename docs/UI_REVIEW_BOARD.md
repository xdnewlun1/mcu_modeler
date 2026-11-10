# UI review: board and package drawing

Scope: the SVG board canvas (`chipem/web/static/board.js`, `package.js`, `parts.js` symbols), the design
tokens at the top of `style.css`, and the SVG rules. Layout, panels, consoles, event log and the pin table
are reviewed separately (agent 1).

Reviewed as a user of KiCad / EDA schematic viewers / logic analysers: can I read every pin, tell levels
apart without colour, find a pin, pull its data out, and follow a net?

Sessions (throwaway servers, headless Firefox, viewport screenshots after ~4 s of running):

| session | command |
|---|---|
| three | `chipem serve --board boards/three_chip_demo.yaml --start` |
| c2000chat | `chipem serve --board boards/c2000_stm32_chat_c.yaml --start` |
| withings | `chipem serve s1_off000084_503080.bin --chip mk22fx512vll12 --spi-flash SPI0:cs=PTC4 --set PTD2=1 --adc ADC0_SE18=40000 --at '100ms:UART2=help\r\n' --start` |

Screenshots (`SHOTS` = `/tmp/claude-1000/-home-xnewlun-Documents-Programming-CHIP-EM/5ec06a94-38c8-4e6c-b3e7-e77b1301aa59/scratchpad/a2/shots`):

- `SHOTS/before/{three,c2000chat,withings}_{1366x768,1920x1080,2560x1440}.png`
- crops: `crop_three1366_pkgA.png` (package at 1366), `crop_withings1366_pins.png` (single chip pins at 1366),
  `crop_c2000_linklabels.png` (net/link labels), `crop_three1920_wirebundle.png` (wire bundle),
  `crop_three2560_wire_over_labels.png` and `crop_three2560_top_labels.png` (wires through pin labels)

## Findings, by severity

### S1: blocks technical use

1. **Pin labels are unreadable at board fit, and nothing replaces them.**
   `three_1366x768.png`, `crop_three1366_pkgA.png`: with three chips at 1366x768 the pin labels render at about
   3 px and the pin numbers at about 2.5 px. Even at 1920 they are about 4 px (`three_1920x1080.png`). At this size
   they are noise: they cost rendering time and clutter the view without carrying information.
   *Why it matters*: the whole board view is grey hatching unless the user zooms, and nothing on screen says which
   pin is which. *Fix*: level-of-detail rendering. Hide pin labels below about 6 screen px and pin numbers below
   about 5 px, and show them again as the user zooms in. At overview the pads and wires stay, and a hover card plus
   pin search (S1.3, S1.4) answer "which pin is this".

2. **Wires run through pin labels.** `crop_three2560_wire_over_labels.png` (A.PTC3 "CMP1_IN1" struck through
   by net C_TX) and `crop_three2560_top_labels.png` (A's top pins PTC5/PTC6 and C's top row). Each wire leaves its
   pad straight outward, which is exactly where the pin's own label sits.
   *Fix*: draw pin labels with a background-coloured halo (`paint-order: stroke`) above the wire layer, so the wire
   visibly passes under the text and the text stays legible. Mark netted pins too: name in full foreground and
   bold, and a dot at the pad tip.

3. **No pin data card that can be copied.** The hover tooltip has good content (owner, direction, drive, pull,
   external, net, configuring code, alternate functions), but it has `pointer-events: none` and disappears on
   mouse-out. You cannot select or copy from it, it shows no change count or last-change time, and a click
   *drives* the pin instead of inspecting it. *Fix*: hover shows the card. A click pins it (it stays open, can be
   selected, and has a **Copy** button that puts a `key: value` text block on the clipboard). The pinned card has
   H / L / open drive buttons. Alt-click keeps the old quick drive cycle. The card also shows the change count and
   the last change time, counted by the board view from the live rows.

4. **No way to find a pin in the drawing.** The pin table filter only filters the table. In the drawing, finding
   "PTC6" or "B.PA10" means scanning hundreds of 3 px labels. *Fix*: a "find pin" box on the canvas toolbar
   (`/` focuses it). It matches the short name, the qualified name, the datasheet name and the current owner (for
   example `UART0_TX`). Enter centres and zooms on the match, cycles through further matches, and marks the pin
   with an accent ring until Esc.

### S2: misleading or hard to read

5. **Level encoding relies on colour, and power and analog collide for colour-blind users.**
   H is green fill, L dark grey fill, X amber hatch, power blue (#3b82f6) and analog purple (#a371f7). With
   protanopia or deuteranopia, blue and purple look the same, and green H vs blue power is only 1.45:1 in
   luminance. L pads are #3a414b on the canvas: **1.85:1**, so an L pad almost vanishes. Wires do the same: H green,
   L grey, X dotted amber. *Fix*: give each state its own fill pattern, so colour is redundant (see the token
   proposal). **H** = solid fill. **L** = hollow pad (outline only, mid grey, 5.9:1). **X / floating** = hatch.
   **power** = solid neutral grey with no level colour. **analog** = dotted outline. Nets follow the same scheme:
   H is a solid 2.4 px line, L a solid 1.4 px line, X a dotted line, and a conflict is red with a dash pattern.

6. **Owner/function labelling is inconsistent.** *(Correction after phase 2: in screen space the pin name already
   sits next to the pad and the owner further out on every side, which is the KiCad convention. Only the text
   order and the owner tiers needed fixing.)* Left-side labels put the owner *before* the name ("GPIO PE2",
   "ADC1_SE4a PTE0"), while right-side labels put it after ("PA13 JTMS-SWDIO"). GPIO-owned pins show a dim "GPIO",
   disabled pins show nothing, and analog pins get a purple pad with a cyan owner. *Fix*: the name always sits next
   to the pad and the owner always sits further out, on every side (mirrored text order on the left). There are
   three owner tiers: peripheral in the function colour, `GPIO` in secondary grey, and disabled with no owner text
   and a dimmed name. This matches the pin table's `owner-*` classes.

7. **Link labels collide with each other and with net labels.** `crop_c2000_linklabels.png`: "STM32_TX",
   "B.USART1 → A.SCIA · 8620 B" and "A.SCIA → B.USART1 · 8619 B" overlap on the top street. *Fix*: place labels
   greedily against a list of the boxes already taken. Try several positions along every segment and every
   polyline, and pick the first free one.

8. **Automatic and on-net links duplicate their nets.** In `three` and `c2000chat`, every auto link (TX and RX on
   one net) and the explicit link B.USART1→A.UART0 (whose TX and RX are both on net B_TX) is drawn as a second
   dashed line on its own track, parallel to the net wire it rides on. That doubles the wires in the streets
   (`crop_three1920_wirebundle.png`). *Fix*: when a link direction's TX and RX pins share a net, do not draw a
   separate line. Byte markers travel along an invisible path on the net's track, and the net label carries the
   byte count ("A_TX · 837 B"). Only links that do not ride on a net get a dashed line.

9. **Net and link labels shrink with the board.** They are 11 world units, so about 3 px at three-chip fit
   (`three_1366x768.png`: "A_TX", "B_TX" are illegible). *Fix*: counter-scale net and link labels so they stay at
   about 11 px on screen whatever the zoom (font-size = 11 / scale, recomputed on each zoom). Pin labels keep world
   size, with level of detail.

10. **Zoom/pan exists but is incomplete.** Wheel zoom, drag pan, double-click fit and the +/−/fit buttons work. There
    is no 1:1 (100 %) button, no keyboard `+`/`-`/`0`, and no zoom-level readout. The zoom limits are relative to
    the board size, so for one chip "3x" is not reached. *Fix*: add a canvas toolbar with − / zoom % / + / fit / 1:1,
    and keys `+` `-` `0` (fit) `1` (1:1, single-chip only, since 1–9 select chips on boards). Allow zoom up to
    8 screen px per world unit.

### S3: polish, density, AI-slop tells

11. **Decoration without information** ("AI-slop" tells):
    - `bodyGrad` linear gradient on every chip body; `drop-shadow` glow on H pads (`--hi-glow`), on the
      wire-pending pin and on link markers; tooltip `box-shadow` of 30 px.
    - The 54 px watermark letter "A"/"B"/"C" on the body at 22 % alpha (1.5:1 contrast). It is decorative and also
      hard to read. *Fix*: a crisp chip-id tag (mono, full contrast, boxed) at the body's top-left inside corner.
    - Body corner radius 10, inner dashed rect radius 6, pad radius 2: real packages are square. *Fix*: body
      radius 2, no inner dashed rectangle (it reads as a selection marquee).
    - `--radius: 10px` panels, pill shapes (999 px) on tags. *Fix*: radius token 3 px.
    - Five non-level hues (power blue, analog purple, periph cyan, accent blue, link orange) plus four pastel
      per-chip colours. *Fix*: one accent (interaction: selection, focus, search hit, pending wire); level colours
      (H green, X amber, conflict red); one function colour (teal) for owner text; greys for everything else. Link
      direction b→a uses the same accent with a different dash, not orange.
    - The link-dash animation is functional (bytes moving). It stays, but only while the link is busy, which it
      already is.
12. **Body text hierarchy.** The title is 22 px sans and the subtitle 13 px, all scaled with the body. Firmware
    and time sit in the middle, where they compete with nothing but are tiny at fit. *Fix*: keep them, but use mono
    for the time / pc and drop letter-spacing.
13. **Pin number contrast**: #5d6773 on the body is **2.68:1**. *Fix*: `--fg-3` #808a97 (4.7:1 on the body).
14. **Legend** is a 12-item row below the canvas, 12.5 px, about 60 px tall at 1366 and pushed off-screen in
    board mode. It uses colour swatches only. *Fix*: a compact legend overlay inside the canvas (bottom-left,
    collapsible, 11 px) that draws the *same* SVG pad and wire styles it explains, with the redundant pattern
    encoding.
15. **No light theme.** Only dark exists, so there is no consistency question. The token block below is laid out
    so that a light set can be added later as a single `@media (prefers-color-scheme: light)` block. That is not
    part of phase 2 unless asked, because agent 1's panels still use some literal colours.

## Token proposal

Dark palette, contrast measured against `--panel` #14181e (canvas background) unless noted:

| token | value | use | contrast |
|---|---|---|---|
| `--bg` | #0e1116 | page | — |
| `--panel` | #14181e | panels, canvas | — |
| `--raise` | #1a1f26 | chip body, hover rows, cards | — |
| `--line` | #2a313a | hairlines | 1.36 (decorative) |
| `--line-2` | #3d4652 | control borders, body outline | 1.86 (non-text) |
| `--fg` | #dde3ea | primary text, pin names | 13.8 |
| `--fg-2` | #a3adb9 | secondary text, GPIO owner, L wires | 7.8 |
| `--fg-3` | #808a97 | tertiary: pin numbers, hints | 5.1 (4.7 on `--raise`) |
| `--accent` | #4c9aff | interaction only: selection, focus, search hit, links | 6.3 |
| `--own` | #3ec4cf | peripheral owner text (UART0_TX, ADC0_SE4b) | 8.5 |
| `--lv-h` | #3fb950 | H fill / H wire | 7.0 |
| `--lv-l` | #8b95a3 | L outline / L wire | 5.9 |
| `--lv-x` | #d29922 | X hatch / X wire | 7.1 |
| `--danger` | #f85149 | conflict, fault | 5.3 |
| `--k-power` | #6b7581 | power pad fill (neutral) | 3.8 (non-text) |
| `--radius` | 3px | every box | — |

Encoding, so that no state depends on colour alone:

| state | pad | wire |
|---|---|---|
| H | solid `--lv-h` | solid, 2.4 |
| L | hollow, `--lv-l` outline | solid, 1.4, `--lv-l` |
| X / floating | `--lv-x` hatch | dotted `--lv-x` |
| power | solid `--k-power`, label `--fg-3` | — |
| analog | dotted `--fg-2` outline | — |
| conflict | — | `--danger`, dash 6 3 |
| input / output | ▶ / ◀ glyph on the pad (kept) | — |
| externally driven | dot at pad tip, filled H / hollow L | — |

Typography:

- `--mono`: `"JetBrains Mono", "Cascadia Mono", "SF Mono", ui-monospace, Menlo, Consolas, "DejaVu Sans Mono", monospace`.
  Use it for every identifier and value: pin, net, owner, time, pc, bytes.
- `--sans`: `system-ui, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif`. Use it for headings and prose.
- Size scale (px): `--fs-1` 11 (labels, legend, card keys), `--fs-2` 12 (body, card values), `--fs-3` 13 (panel
  titles), `--fs-4` 15 (chip name). In the SVG, pin labels are 11.5 world units with level of detail, and net and
  link labels are counter-scaled to 11 px on screen.
- Spacing scale: 2 / 4 / 6 / 8 / 12 px (`--sp-1`..`--sp-5`).

## Phase 2 change list (board.js, package.js, parts styling, style.css tokens + SVG rules)

1. Token block: the palette, fonts, sizes, spacing and radius above. Old names (`--hi`, `--lo`, `--x`, `--muted`,
   `--dim`, `--text`, `--periph`) stay as aliases, so agent 1's rules keep working.
2. Package: square body (radius 2), no gradient, no inner marquee, chip-id tag instead of the watermark, pad
   encoding per the table, no glow, consistent name/owner order, label halo, netted-pin emphasis, pin-number
   contrast.
3. Level of detail: a `zoom-*` class on the SVG from the screen px per world unit. Pin labels hide below 6 px and
   numbers below 5 px.
4. Canvas toolbar overlay (built by board.js inside `.svg-wrap`): find-pin box, − / % / + / fit / 1:1. Keys `+` `-`
   `0`, `/`. Wheel and drag as now. Agent 1 can remove the old `#zoomIn/#zoomOut/#zoomFit` buttons; they keep
   working until then.
5. Pin search: matches name, qualified name, datasheet name and owner. It centres and zooms, Enter goes to the
   next match, Esc clears, and the hit gets an accent ring.
6. Pin data card: hover shows it, a click pins it, Copy copies it, H / L / open buttons drive the pin, and alt-click
   quick-cycles. It shows the change count and last change time (tracked in BoardView). This needs a small hook in
   app.js: `onPinClick` → the card, and the drive handler passed in.
7. Wires: dash/width per level, halo kept, net label counter-scaled and placed once with collision avoidance,
   on-net links ride the net (no duplicate line; markers along the net; byte count in the net label),
   other links keep a single dashed accent line with a collision-avoided label.
8. Compact legend overlay inside the canvas, drawn with the real SVG styles. The old `<ul class="legend">` is
   hidden by CSS (agent 1 can delete it).
9. Parts symbols (parts.js): restyled with the same tokens (stroke `--fg-2`, levels as for pins, no glows).
   Only CSS changes, plus colour constants if they are hard-coded.

## Phase 2: what changed (before / after)

Screenshots: `SHOTS/before/*` vs `SHOTS/after/*` (same three sessions, same three sizes). After-only views:
`after/three_find_PTA2_1920.png` and `after/crop_three_find_PTA2.png` (search, centring, pinned card, plates behind
netted labels), `after/withings_find_UART2_1366.png` (single chip, owner search, card with history and drive buttons,
legend open), `after/c2000chat_find_GPIO29_1366.png`, `after/crop_three1366_labels.png` (net and link labels at
fit), `after/crop_three_parts.png` (part symbols).

| finding | before | after |
|---|---|---|
| S1.1 unreadable labels at fit | 3 px pin labels drawn everywhere | Level of detail: labels hidden below 5.5 px and numbers below 4.5 px; they come back as you zoom |
| S1.2 wires through labels | wire struck through the pin's own label | the label of a netted pin sits on a canvas-coloured plate, so the wire visibly passes under it; netted names are bold |
| S1.3 no copyable data | tooltip with pointer-events none | hover card; click pins it (selectable, `copy` → `key: value` text, `locate`, drive `H`/`L`/`open`); history from `/api/events` (level-change count, last change); alt-click = old drive cycle |
| S1.4 no pin search | — | `find pin` box (`/`) on the canvas: name, chip.name, datasheet name, current owner; Enter / Shift+Enter cycle, the drawing centres and zooms, the hit is ringed, all matches outlined, the card opens |
| S2.5 colour-only levels | green / grey / hatch / blue / purple fills | H solid, L hollow, X hatched, power neutral grey, analog dotted outline; wires: H thick solid, L thin solid, X dotted, conflict red dashed |
| S2.6 owner tiers | dim `GPIO`, cyan periph, purple pads | owner in `--own`, `GPIO` in `--fg-3`, disabled: no owner and a dimmed name |
| S2.7 colliding labels | link and net labels stacked | greedy placement against footprints, wires and earlier labels; a label that only fits on top of another label is hidden (the panes still list it) |
| S2.8 duplicate link lines | auto link drawn next to its net | a link whose TX and RX share a net rides on it: no second line, byte markers travel the net, and the net label reads `A_TX · A.UART0 17087 B` |
| S2.9 labels shrink | 11 world units | net and link labels are always 11 px on screen (`--ls`) |
| S2.10 zoom | wheel, drag, fit | plus a toolbar (− / % / + / fit; `%` = 1:1), keys `+` `-` `0`, limit 8 px/unit, zoom readout |
| S3.11 slop | gradient body, glows, watermark, radius 10, 5 hues | flat body radius 2, no shadows or glows (also on LEDs), solid mono chip id, radius 3, one accent |
| S3.13 pin numbers | 2.68:1 | `--fg-3`, 4.7:1 |
| S3.14 legend | 12-item row under the canvas | collapsible legend on the canvas, drawn with the canvas' own classes, plus a key reminder |

Implementation notes:

- `chipem/web/static/board.css` (new) holds every drawing, overlay and card rule, scoped to `.bv`. board.js loads it
  after style.css. The old `#pkg …` rules were removed from style.css; the `:root` block there now holds the
  tokens, with the old names kept as aliases.
- BoardView API for app.js: `setPinClickHandler(fn)` (the app's click: wire mode, pick mode and alt-click are routed
  to it, plain clicks open the card), `showCard(cid, num, e)`, `pinCard(cid, num, e)`, `hideCard(evenPinned)`,
  `zoomToPin(cid, num)`, `oneToOne()`, `zoomScale(pxPerUnit)`. Without any app.js change the card already replaces the
  `#tooltip` for pins (BoardView no longer forwards `onPinHover` / `onPinLeave`), and `onPinClick` is still used for
  wire mode, pick mode and alt-click.
- Parts (parts.js) are restyled in CSS only: neutral strokes, levels encoded as for pins, LED colour kept as data
  (filled = on, hollow = off, hatched = X), no blur halo.

Left open (both closed in phase 3, below):

- ~~The "changes seen" counter counts UI state frames (at most one per frame). The exact count comes from the event
  log when the card is pinned.~~ The card counts from the event log, hovered or pinned.
- ~~There is no light theme.~~ Light theme added.

## Phase 3: light theme, readable pin names at fit, leftovers

Screenshots (`FINAL` = `/tmp/claude-1000/-home-xnewlun-Documents-Programming-CHIP-EM/5ec06a94-38c8-4e6c-b3e7-e77b1301aa59/scratchpad/lt/final`,
1366x768, headless Firefox through the stream-blocking proxy, so the header says `offline`):

| session | dark | light |
|---|---|---|
| `--board boards/logic_demo.yaml` (event filter `PTC6`: matches highlighted) | `FINAL/logic_dark_1366x768.png` | `FINAL/logic_light_1366x768.png` |
| Withings (`s1_off000084_503080.bin --chip mk22fx512vll12 --spi-flash SPI0:cs=PTC4 --set PTD2=1 --adc ADC0_SE18=40000 --at '100ms:UART2=help\r\n'`) | `FINAL/withings_dark_1366x768.png` | `FINAL/withings_light_1366x768.png` |

Measurement shots (a yellow strip prints the fit scale, the layout plan and the on-screen name size per package):
`.../scratchpad/lt/before/lt_logic_b_1366x768.png` (before: 29 %, names hidden), `.../lt/after/lt_logic4_1366x768.png`,
`.../lt/after/withings_dark_1366x768.png`, `.../lt/after/lt_three4_1366x768.png`; light theme with the legend, a
pinned card, zoom and an info toast: `.../lt/after/withings_light_card_1366x768.png`.

### Light theme

`style.css` has two complete token sets: `:root, :root[data-theme=dark]` and `:root[data-theme=light]` (each with
its `color-scheme`, so scrollbars, inputs and selects follow). Shape, type and spacing tokens are shared. An inline
script in `index.html` sets `html[data-theme]` before the stylesheets load (saved choice in
`localStorage["chipem.theme"]`, else `prefers-color-scheme`), so there is no flash. The sun / moon button in the header
switches and saves; without a saved choice the page follows the system setting live. Every colour literal that was
left in `style.css`, `layout.css`, `board.css`, `app.js` and `parts.js` is now a token (buttons, toasts, consoles,
badges, chip colours, part icons, dialog, highlight marks, shadows). LED colours are data, but named colours map to
`--led-<name>` tokens with a light-theme variant (`parts.js` `ledColor` returns `var(--led-red)`; any other CSS colour
from a board file is used as given). `tests/test_web.py::test_theme_and_tokens_static` checks that both sets define
the same tokens and that no literal colour exists outside them.

Contrast (WCAG 2.x relative luminance; text needs 4.5:1, large / UI text and graphics 3:1). Background is the
canvas / panel colour unless noted.

| pair | use | dark | light |
|---|---|---|---|
| `--fg` on `--panel` | primary text, pin names | #dde3ea / #14181e **13.8** | #1b2129 / #ffffff **16.2** |
| `--fg` on `--bg` | page text | **14.6** | #f3f5f8: **14.8** |
| `--fg` on `--raise` | pane headers, cards, chip body | **12.8** | #f0f2f5: **14.5** |
| `--fg-2` on `--panel` | secondary text, GPIO owner | #a3adb9 **7.8** | #4a5562 **7.6** |
| `--fg-3` on `--panel` | pin numbers, hints, power-pin names (`.owner-power`) | #808a97 **5.1** | #5f6b78 **5.4** |
| `--fg-3` on `--raise` | tertiary text on cards and the chip body | **4.7** | **4.9** |
| `--accent` on `--panel` | selection, search hit, links, info toast edge, auto tag | #4c9aff **6.3** | #0b5fca **6.0** |
| `--own` on `--panel` | owner text (`UART0_TX`) | #3ec4cf **8.5** | #00727f **5.7** |
| `--lv-h` on `--panel` | H pad / wire / text | #3fb950 **7.0** | #1a7f37 **5.1** |
| `--lv-l` on `--panel` | L outline / wire | #8b95a3 **5.9** | #687483 **4.8** |
| `--lv-x` on `--panel` | X hatch / dotted wire | #d29922 **7.1** | #8f5f00 **5.5** |
| `--danger` on `--panel` | conflict, fault | #f85149 **5.3** | #c4202b **5.9** |
| `--k-power` on `--panel` | power pad fill (graphic) | #6b7581 **3.8** | #8b949e **3.1** |
| `--chip-a/b/c/d` on `--panel` | chip column in the event log | **9.2 / 7.0 / 9.2 / 11.6** | **5.2 / 5.5 / 5.9 / 5.1** |
| `--term-fg` on `--term-bg` | UART console, link monitor | **14.9** | **15.8** |
| `--port` on `--panel` | part ports in the nets pane | **9.2** | **5.5** |
| `--ok-fg` on `--ok-bg` | Run button | **5.9** | **5.1** |
| `--sel-fg` on `--sel-bg` | pressed toggles (wire, pick, segment) | **9.5** | **10.4** |
| `--err-fg` on `--err-bg` | error toast | **11.6** | **9.2** |
| `--bg` on `--lv-h` / `--danger` | RUNNING / FAULT badge, H level chip on the card | **7.4 / 5.6** | **4.7 / 5.4** |
| `--line-2` on `--panel` | control borders, body outline (decorative) | 1.9 | 2.1 |

Colour-blind safety is unchanged: the light palette keeps the redundant encoding (H solid, L hollow, X hatched /
dotted, power neutral, analog dotted outline, conflict dashed), so no state depends on hue.

### Pin names readable at fit (S1.1 revisited)

Before: logic_demo at 1366x768 fitted at 29 % (pitch 6.4 px, labels hidden), the drawing ~3:1 wide in a ~1.9:1
canvas. Changes:

1. **Readable minimum instead of a hard cut-off** (`package.js` `setScale`): a pin name is drawn at its native size,
   but never below 9 px on screen; it is capped at 1.04 x the pin pitch (mono capitals are ~0.7 em, so adjacent names
   keep ~2 px) and hidden only below 7.5 px. Names are counter-scaled through one CSS variable per package (`--pf`),
   not per element. Owners keep their native size; one is shown when name + owner fit the reserved label room at the
   current sizes, or once zoomed in (native name ≥ 11 px), and hidden below 5.5 px.
2. **Layout planned from the canvas** (`board.js` `choosePlan`): candidates are every column count, parts beside the
   chips (as before) or in a **parts row under the chips** (sources, logic, indicators left to right, stacked 1-4
   per column, each column flanked by a narrow gutter sized for the nets that use it; the street between chips and
   parts row is their trunk), four part scales, and the **compact footprint** (11 instead of 16 characters of label
   room). The score is: names readable (pitch on screen / 8.7 px) x 3, part symbols ≥ 0.55 px per unit, +0.4 for the
   full footprint only if owners would be drawn at that fit, then scale. The plan is re-chosen when the canvas
   changes size (window, splitters) and exposed as `svg[data-plan]`.
3. **More canvas**: the find / zoom toolbar moved into the board pane header (it used to reserve a 34 px strip),
   the fit uses a model bounding box (footprints with their label room, parts, wires) instead of `getBBox`, so it does
   not drift as labels change size, plus room for the outermost net labels.
4. Part ids and states have an on-screen minimum too (8.5 / 8 px; `max()` with `--lw` and `--pkinv`).

Measured at 1366x768 (canvas ~950 x 500 px):

| session | before | after |
|---|---|---|
| logic_demo (2 x 100 pins, 10 parts) | 29 %, names hidden (would be 3.3 px) | 39 %, compact footprint, parts row; names **9.0 px** on A and B |
| Withings (1 x 100 pins) | 53 %, names 6.1 px, owners 5.6 px | 54 %, names **9.0 px**, owners 5.7 px (44 shown) |
| three_chip_demo (100 + 144 + 176 pins) | labels ~3 px, hidden (phase 2 review) | 33 %: A names 7.6 px; B, C hidden (pitch 5.2 / 4.2 px: zoom, or the pin finder) |

### Leftovers

- Event rows highlight filter matches (`<mark>`, cell by cell; rows hidden by a kind toggle are re-marked when shown).
- One event formatter: the server adds `desc` (the row text) to every event (`chipem/web/evfmt.py`); the browser
  shows it as is, `GET /api/events?q=` matches the browser row `time \t [chip \t] kind \t desc` with the browser's rules
  (substring case-insensitive; `re:` regex with smart case, `(?i)` sent for `/X/i`), and the `.txt` download is those
  rows. `fmt_time` is the twin of `app.js fmtTime` (checked against node in the tests). The CSV gains a `text` column.
- Toasts: neutral info style (accent edge) for confirmations, the red style only for failures (`toastErr`).
- Pin card `changes`: level changes counted from the event log (fetched once per pin on hover or pin, then kept
  current from the live stream), with the last transition and its time.
- `.owner-power` uses `--fg-3` (5.1 / 5.4 : 1). Dead `.legend` / swatch rules and the old pin `showTip` / `hideTip`
  path in `app.js` are removed (the part / port tooltip stays).
- LED on chip B in `boards/three_chip_demo.yaml`: skipped. B's only output that changes level is PB0, which the
  wiring tests use as a free pin; the USART1 TX net does not toggle at pin level (UART bytes are modelled as
  bytes), so an LED there never lights.

Left open:

- ~~Streets and gutters are sized for all tracks of the board.~~ Done in phase 4: each gutter / street is sized from the
  routes that use it (three_chip_demo: gutter 124 -> 116, street 108 -> 100). UART links still reserve a slot
  everywhere (their routes depend on live pin owners).
- 144- and 176-pin packages on a three-chip board: readable at fit at 1920 since phase 4; at 1366x768 they still stay
  below the name threshold (three packages need ~0.68 scale, the fit gives ~0.45); zoom or find a pin.
