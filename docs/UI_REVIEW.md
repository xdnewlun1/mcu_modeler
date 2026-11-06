# Web UI review: layout, panes, consoles, log, data extraction

Scope of this review: the chrome around the board drawing (layout, panels, UART console, event log, pins table,
nets/links/parts lists, search, copy/export, run state). The board/package SVG drawing and the colour/typography
tokens are reviewed separately (agent 2).

Reviewed as an engineering tool, against logic-analyser, oscilloscope and IDE conventions: dense, scannable,
monospace for tabular data, fixed-height scrollable panes, resizable splits, nothing that grows without bound,
keyboard-driven, every pane searchable and exportable.

## Method

`chipem serve ... --start` on throwaway ports, 3 s of running, then paused; headless Firefox through a proxy that
delays the load event until the API data is in (the proxy blocks `/api/stream`, so the live dot shows red in every
shot; that is an artefact). Three setups:

| tag | command |
|---|---|
| `chat` | `--board boards/c2000_stm32_chat_c.yaml` (2 chips, C2000 + STM32) |
| `three` | `--board boards/three_chip_demo.yaml` (3 chips, 4 links, 5 nets) |
| `withings` | `s1_off000084_503080.bin --chip mk22fx512vll12 --spi-flash SPI0:cs=PTC4 --set PTD2=1 --adc ADC0_SE18=40000 --at '100ms:UART2=help\r\n'` (UART2 selected) |

Screenshots are in `docs/ui_review/` (`before_<tag>_<W>x<H>.png`). The `x3200` / `x2400` shots use a tall window to
show the whole page; the board SVG is sized in `vh`, so it is also inflated in those, but the unbounded UART pane is
not a `vh` effect (it is content-sized).

## Findings (by severity)

### S1: blocks the work

**1. The UART console grows without bound and drags the whole layout with it.**
`docs/ui_review/before_withings_1366x3200.png`, `before_withings_1366x768.png`.
`.side` is a grid with rows `minmax(260px, 1fr)` and `.term` is `flex: 1` with no max height, so a `<pre>` holding
the Withings console (~1.2 KB, 80 lines after `help`) sizes the row to its content. The grid row is shared with the
package panel, so the board panel stretches to the same ~2000 px, and the event log and pins table end up more than
3200 px down the page. This is the user's complaint. A technical user must scroll the page to reach the log, and
loses the board while reading the console.
*Fix:* app shell of exactly `100vh` with no page scroll; every pane gets an explicit height (px, persisted) and
`overflow: auto` on its body; content never sizes a pane.

**2. The page scrolls as a document instead of being an app shell.** `before_three_1366x768.png`,
`before_chat_1366x768.png`. At 1366x768 the header takes 125 px (it wraps to two rows), the board panel the rest; the
UART console, event log and pins table are all below the fold. Reading the log means scrolling the board out of view,
so you can never watch pins and the log together, which is the main use of a simulator UI.
*Fix:* fixed layout: one-row header, board in the main area, a resizable right sidebar (UART, events, wiring) and a
resizable, collapsible bottom tray (pins / discovery). Drag handles between them; sizes persisted in localStorage.

**3. The event log is effectively unreachable and has no search.** Same shots: in single-chip mode it sits below an
unbounded UART pane; in board mode it is the third panel down. There is no text filter, only kind toggles; `mmio` is
dropped in the browser with no toggle; the DOM keeps 1500 rows and silently discards the rest with no indication;
there is no copy or export although the server keeps 20000 events.
*Fix:* text filter (substring, or `/regex/`), kind toggles that include `mmio` (and show counts), chip toggles on a
board, "shown / total" counter, copy visible rows, download `.txt` / `.csv` from the server (`/api/events?format=`).

### S2: makes data hard to get at

**4. No search, copy or export in the UART console.** Raw mode is a bare `<pre>`; lines mode has no filter either.
You cannot grep the Withings `help` output, copy a log, or save it next to a bug report. Lines have timestamps
internally (`ln.t`) but they are never shown.
*Fix:* filter box (hides non-matching lines, highlights matches), timestamps toggle, copy and download `.txt`
(raw: full server buffer via `/api/uart?format=txt`; lines: the transcript with or without timestamps).

**5. Autoscroll is invisible.** Both views stick to the bottom only if you were within 24 px of it, which is right,
but there is no sign that following stopped, no count of new lines, and no "jump to end" control. In a fast console
you cannot tell whether output stopped or you scrolled away.
*Fix:* a "following / paused, N new ↓" pill in the pane footer; click or `End` jumps to the end and resumes.

**6. Raw mode refetches the whole UART buffer on every growth.** `refreshUart` downloads `/api/uart` in full each time
`uart_out_len` changes (O(n²) over a run; MBs for a chatty firmware left running).
*Fix:* `/api/uart?since=N` returns only the bytes after offset N; the client appends and caps the DOM.

**7. Wrong default UART on a single chip.** `before_withings_1920x1080.png` shows "no output yet": the console picks
the first UART (UART0) while the firmware prints on UART2. *Fix:* pick the first UART with output (from
`uart_out_len`), and switch to it automatically once if the selected one stays empty.

**8. Nets / UART links / parts: no filter, no collapse, wasted height.** `before_three_1366x3200.png`,
`before_chat_1920x2400.png`: the net list has `flex: 1`, so 2 nets leave 400 px of empty space before the links
list; the panel is `max-height: calc(80vh + 90px)`. A board with 50 nets has no search.
*Fix:* each list is its own collapsible pane with a filter (name or any pin), a count, copy and download (nets/links
as CSV; the board YAML export stays).

**9. Pins table: fixed 440 px, no export, filter is substring only.** It cannot be made taller to see all 144 rows
of the STM32, cannot be collapsed, cannot be copied as a table. *Fix:* in the bottom tray (resizable), regex
filter, copy as TSV (pastes into a spreadsheet), download CSV; same for the Discovery table.

**10. Run state is not readable at a glance.** The status is 13.5 px grey "paused" among five same-looking readouts;
"running" is the same text in green. The stream connection is an unlabelled 9 px dot. There is no speed (virtual
time per wall second), which matters when a run "seems stuck". "cycles" shows `468,842,455 insn · 468,842,455` with
no unit on the second number. On a board the `CHIP A` readout repeats the chip tabs.
*Fix:* one status badge (RUNNING / PAUSED / FAULT, solid background), then `t`, speed `×0.21 rt`, `clk`, `pc`,
`insn`, `cyc`, each label inline (`t 19.546371 s`) so the header is one row; the dot gets a text label (`live` /
`offline`).

### S3: AI-slop tells and wasted space

| # | where | what | fix |
|---|---|---|---|
| 11 | `.logo` | gradient-bordered rounded square with a dot: decoration, no meaning | remove; the chip/board name is the title |
| 12 | `.panel`, `.layout` | 10 px radius cards, 16 px gaps, 22 px page padding, 10/14 px header padding: ~70 px of 1366 lost to gutters, plus 40 px per panel header | 1 px borders between tiled panes, 3 px radius on controls only, 26 px pane headers, 0 gap |
| 13 | `.panel-head h2` | uppercase, letter-spaced headers; "UART CONSOLE" wraps to two lines in board mode at 1366 (`before_three_1366x768.png`) | short sentence-case labels that name the data: `UART`, `Events`, `Nets`, `Links`, `Pins` |
| 14 | `.chip-toggle`, `.auto-tag` | pill-shaped (999 px radius) toggles and tags | small square toggles with counts (`pin 120`) |
| 15 | `#pkgHint` | a 120-character instruction sentence in the board header ("wheel: zoom · drag: pan · ...") | move to the `?` keyboard-help popover; header keeps tools only |
| 16 | `.legend` | always-visible 2-row legend (50–70 px of board height) | collapsible (`legend` toggle in the board header), collapsed state persisted |
| 17 | readouts | `VIRTUAL TIME` etc. as uppercase captions stacked over values (two rows each) | inline `label value` pairs, monospace values |
| 18 | `.btn` | 32 px buttons with icon + word in the header; `Export board YAML` as a big button in the nets header | 24 px buttons; export as `yaml ⤓` in the wiring pane |
| 19 | native widgets | light scrollbar track in the pins table (right edge of `before_three_2560x1440.png`), light date/select popups | `color-scheme: dark` (token block, agent 2) |
| 20 | empty states | "no output yet" / "no events yet" do not say whether the run has started or the stream is connected | "no bytes on B.USART1" / "no events (12 hidden by filter)" |

### S4: keyboard and minor

21. Shortcuts exist (Space, S, R, F, W, 1–9, Esc, Delete) but are not discoverable; there is no `/` to focus a
filter, no Esc to clear one. *Fix:* `/` focuses the filter of the pane under the pointer (or the last focused
pane), Esc clears and blurs; `?` toggles a shortcut list.
22. Panels cannot be collapsed. *Fix:* chevron on every pane header, state persisted.
23. On first load before `/api/state` returns, the header shows `—` and the canvas `0.000 ms paused`
(`before_chat_1920x2400.png`): indistinguishable from a real paused-at-zero state. *Fix:* status badge says
`CONNECTING` until the first state frame.
24. The link monitor sits under the UART input in the UART pane and grows with the link count. *Fix:* its own
collapsible section with a fixed row height.

## Proposed layout

```
+-- header (one row, 32 px) -------------------------------------------------------------------------+
| board three_chip_demo  [>Run][||][Step][Reset] [100ms][Run for]  A B C | PAUSED t 33.550 ms ×0.8 rt |
|                                                    clk 120 MHz pc 0x00000568 insn 4,026,112 ● live ? |
+-- main (100vh - header) -----------------------------------------+=+-- sidebar (resizable width) ---+
| Board  [wire][+][-][fit][legend]                                 |‖| ▾ UART [B.USART1▾][lines|raw]  |
|                                                                  |‖|   [ts] [all] /filter  ⧉ ⤓ ✕   |
|                                                                  |‖|   12.003 ms A.UART0 TX ping 1   |
|        board SVG: fills the remaining height                     |‖|   ... fixed height, scrolls ... |
|                                                                  |‖|   [paused · 34 new ↓]           |
|                                                                  |‖|   > send____________ CRLF Send  |
|                                                                  |‖|   ▸ link monitor (4)            |
|                                                                  |‖|=========== drag ================|
|                                                                  |‖| ▾ Events [pin uart irq ...]     |
|                                                                  |‖|   [A B board] /filter ⧉ ⤓ ✕     |
|                                                                  |‖|   ... fixed height, scrolls ... |
+================================ drag =============================+‖|=========== drag ================|
| ▾ Pins | Discovery   chip A  /filter  [changed][hide pwr]  ⧉ ⤓    |‖| ▸ Nets 5      /filter ⧉ ⤓       |
|   # pin owner dir level pull ext net name (sticky header)        |‖| ▸ Links 4     /filter ⧉ ⤓       |
|   ... fixed height, scrolls ...                                  |‖| ▸ Parts 0                       |
+------------------------------------------------------------------+-+---------------------------------+
```

* No page scroll at any size. Sidebar panes stack; each open pane has a persisted height (drag the bar under it),
  a collapsed pane is just its 26 px header, the sidebar scrolls only if the open panes exceed it.
* Below 1100 px wide the sidebar moves under the board (same panes, same heights) and the page may scroll; panes keep
  their fixed heights.
* Pane header pattern, identical everywhere: `▾ title count | view controls | filter | ⧉ copy | ⤓ download | ✕ clear`.

## Changes for phase 2

Files: `chipem/web/static/index.html`, `app.js` (panes, console, log, search, export), `style.css` (layout and pane
rules, below agent 2's token block), `chipem/web/server.py`, `tests/test_web.py`.

1. **App shell**: `body` is a `100vh` grid (header / main); no document scroll. Main is `board | splitter | sidebar`
   with the pins tray under the board; vertical splitter for the sidebar width, horizontal for the tray height.
2. **Generic pane** (`.pane`): header with collapse chevron, title, count, tools; body with its own scroll; resize bar
   on the bottom edge. Heights, widths and collapsed flags persisted in `localStorage` (`chipem.ui.v1`).
3. **Pane toolbar helper** in `app.js`: filter input (substring; `/re/flags` = regex; invalid regex marks the box),
   copy (clipboard, fallback to a selected textarea), download. Used by UART, events, pins, discovery, nets, links.
4. **UART console**: fixed-height body; filter; timestamps toggle (lines mode, `t` per line from the uart events);
   raw mode appends incrementally via `/api/uart?since=`; DOM capped; follow/paused indicator with "N new ↓" and
   jump-to-end (also `End`); copy; download `.txt` (raw: full buffer from the server; lines: transcript, with
   timestamps if on); default UART = first with output; link monitor in a collapsible sub-section.
5. **Event log**: text filter, kind toggles including `mmio` (mmio fetched only when enabled), per-kind counts,
   "shown/total", jump-to-end, copy visible, download `.txt` / `.csv` from the server with the current filters.
6. **Pins / Discovery** in the bottom tray: regex filter, copy as TSV, download CSV; tray collapsible and resizable.
7. **Nets, Links, Parts** as separate collapsible panes with filters and copy/CSV; remove the `flex: 1` gap.
8. **Header**: one row, logo removed, status badge (CONNECTING / RUNNING / PAUSED / FAULT), inline readouts, speed
   (`×` virtual/wall, computed client-side from successive state frames), labelled live indicator; redundant `CHIP`
   readout removed (chip tabs carry the selection); board hint moved into a `?` shortcut popover; legend collapsible.
9. **Keyboard**: `/` focuses the filter of the pane under the pointer or last used, `Esc` in a filter clears it and
   blurs, `End` jumps the focused/hovered log to its end, `?` shows the shortcuts.
10. **Server** (`server.py`):
    * `GET /api/uart?uart=X&since=N` returns `{text, len, since}` with only bytes after offset N;
      `&format=txt` returns `text/plain` with `Content-Disposition: attachment`.
    * `GET /api/events?kinds=&chip=&q=&limit=&format=json|txt|csv`; `q` is a case-insensitive substring over the
      event's fields (or `re:` prefix for a regex); `txt`/`csv` are attachments.
11. **Tests** in `tests/test_web.py` for `/api/uart?since=`, `/api/uart?format=txt`, `/api/events?q=`,
    `?chip=`, `?format=csv|txt`, and a static check that `index.html` has a filter, copy and download control in
    every pane.
12. Screenshot again at 1366x768, 1920x1080 and 2560x1440 (same three setups) and add an "After" section here.

## After

Screenshots: `docs/ui_review/after_<tag>_<W>x<H>.png` (same setups and sizes as the "before" set; plus
`after_withings_1366x3200.png` and `after_three_1366x3200.png`, the tall-window check, and `after_withings_900x900.png`,
the stacked narrow layout). The canvas toolbar (find pin, zoom %), the pin card and the canvas legend in these shots
come from the board drawing review (agent 2); everything around them is from this review.

| before | after |
|---|---|
| `before_withings_1366x3200.png`: the UART pane is ~2000 px tall, the board stretches with it, events and pins are below 3200 px | `after_withings_1366x3200.png`: the page is exactly the window; UART, events and pins are fixed panes with their own scroll, whatever the console holds |
| `before_three_1366x768.png`: header two rows (125 px), only the board visible | `after_three_1366x768.png`: one-row header (38 px); board, pins tray, UART, events, nets, links, parts all on screen |
| `before_withings_1920x1080.png`: console on the empty UART0 | `after_withings_1920x1080.png`: switches to UART2 by itself (first UART with output) until the user picks one |
| no search, copy or export anywhere | every data pane: filter (substring or `/regex/`, matches highlighted in the UART view), copy, txt/csv download, collapse |

What changed (files: `chipem/web/static/index.html`, `app.js`, new `layout.css`, `chipem/web/server.py`,
`tests/test_web.py`):

1. **App shell.** `html, body` do not scroll; header / main / sidebar / tray are flex boxes sized by the window.
   Splitters: sidebar width (vertical bar), pins tray height (bar above the tray), and between sidebar panes (open
   panes share the height by weights; dragging moves pixels between the two neighbours). Sizes and collapsed state
   persist in `localStorage["chipem.ui.v1"]`. Below 1000 px wide the sidebar stacks under the board (page scrolls,
   panes keep fixed heights).
2. **Panes.** UART, Events, Nets, Links, Parts and Pins/Discovery share one header pattern:
   `▾ title count | view controls | filter | copy | txt/csv | clear`. Collapse with the chevron or a double click on
   the header. Links (and Parts when the board has none) start collapsed.
3. **UART console.** Fixed height; raw view fetches only new bytes (`/api/uart?since=`), keeps 1 MB in the browser and
   200 k characters in the DOM, and downloads the complete output from the emulator (`?format=txt`). Lines view keeps
   5000 lines with an optional virtual-time column (`time`). Autoscroll follows only while you are at the end;
   scrolled up, a `↓ N new` button (or `End`) jumps back. The link monitor is a collapsible section.
4. **Event log.** Text filter, kind toggles with counts (now including `mmio`, which re-opens the stream with
   `mmio=1`), chip toggles on boards, `shown/total · N older dropped`, copy of the visible rows, txt/csv download of
   every kept event matching the current kind/chip/text filter (server side, up to 20000).
5. **Pins / Discovery** in a resizable, collapsible bottom tray; regex filter applies to both tables; copy as TSV
   (pastes into a spreadsheet), CSV download; `59/100` count.
6. **Header.** Logo removed; status badge `CONNECTING / RUNNING / PAUSED / FAULT / ERROR` (with run target, sleeping
   or active handler); inline `t`, `speed` (virtual time per wall second, measured over ≥ 0.5 s), `clk`, `pc`,
   `insn`, `cyc`; the stream indicator says `live` / `offline`; the `CHIP A` readout is gone (the chip tabs show the
   selection). Less important items drop out first on narrow headers (meta, chip names, insn, clk).
7. **Help.** `?` button / key: shortcut list, filter syntax and the canvas hint that used to be a sentence in the
   board header.
8. **Keyboard.** `/` focuses the filter of the pane under the pointer (over the board, board.js focuses its pin
   finder), `Esc` in a filter clears it and blurs, `End` jumps the hovered log to its end, `?` toggles help.
9. **Server.** `GET /api/events` gains `chip=`, `q=` (substring, or `re:` regex; bad regex = 400) and
   `format=txt|csv` (attachments); `GET /api/uart` gains `since=` (and returns `since`) and `format=txt`. GET handlers
   now answer `ValueError` with 400 instead of 500.
10. **Tests.** `test_events_filter_and_export`, `test_uart_since_and_txt` (on `uart_link_sender.elf`) and
    `test_every_data_pane_has_filter_copy_download_collapse` (static check of `index.html`).

Not done / left:

* ~~Event rows are not highlighted (only filtered) for speed; the UART views highlight matches.~~ Done in phase 3:
  rows mark the matches.
* ~~The server-side event text filter matches `key=value` fields, the in-browser one matches the rendered row.~~
  Done in phase 3: the server formats the row text (`chipem/web/evfmt.py`, field `desc`) and filters and exports
  exactly that row.
* ~~`board.js` `chooseCols()` still sizes the layout from `window.innerHeight * 0.8`.~~ Replaced by `choosePlan()`,
  which uses the canvas size (docs/UI_REVIEW_BOARD.md, phase 3).
* The headless shots run through a proxy that blocks the event stream, so the indicator shows `offline` and speed `—`.

## Phase 3 (after)

Light theme (header toggle, saved; default from the system), info vs error toasts, highlighted event matches, one
event formatter for view / filter / download, and the board layout planner: see docs/UI_REVIEW_BOARD.md, "Phase 3",
for the screenshots (both themes), the contrast table and the measurements.
