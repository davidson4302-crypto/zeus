---
name: html-to-pdf-with-visual-qa
description: Use when building a designed PDF from HTML.
---

# HTML to PDF with visual QA

For documents where layout matters (org charts, briefs, distribution lists, branded reports). The `pdf` skill covers reportlab/pypdf work and defers pixel-accurate HTML rendering to a headless browser; this is that path.

## Procedure

1. **Facts first.** Collect the source of truth (the user's latest structure, corrections, screenshots) and list what is verified vs. unverifiable (e.g. a roster you cannot see). Corrections from the user override older notes; apply every one before laying anything out.
2. **One build script, not hand-edited HTML.** Keep `build.py` (writes `chart.html`) in the scratch dir; edit it with `patch`, rerun. Run with `python3 -I build.py` so site packages cannot interfere. Set `@page { size: 8.5in 11in (or landscape); margin: 0 }` and one `.page` div per sheet with `break-after: page; overflow: hidden`.
3. **Print.** macOS Chrome:
   `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless --disable-gpu --no-pdf-header-footer --virtual-time-budget=3000 --run-all-compositor-stages-before-draw --print-to-pdf="$PWD/out.pdf" "file://$PWD/out.html"`
   (The terminal security scan may flag this nested command.)
4. **Rasterize and look at every page.** `rm -f p-*.png; pdftoppm -png -r 100 out.pdf p`, then `vision_analyze` each PNG with a pointed question (overlaps? text clipped at page edges? anything touching the footer? lines connecting the right boxes?). Delete stale PNGs first so you never review an old render. Use `pdfinfo out.pdf | grep Pages` to confirm the page count matches the plan.
   If `vision_analyze` is unavailable (e.g. CLI sessions), run `scripts/layoutcheck.py chart.html` for an objective DOM check (boxes inside the page padding, clipped text, overlaps, footer gap), after first confirming it flags a known-bad version; then open the PNGs with `browser_exec` (`goto_url("file:///...png")`, `capture_screenshot()`) to look at them. For a tall source image, crop a region onto a canvas with `js()` and screenshot that to read small text.
5. **Iterate until clean.** Fix, rebuild, re-render, re-look. Do not send a page you have not seen after the last edit.
6. **Deliver.** Copy final PDFs to the workspace dir with descriptive names, run `shasum -a 256` and `pdfinfo`, then attach with bare `MEDIA:/absolute/path.pdf` lines in the reply.

## Layout pitfalls

- **Absolute-positioned trees collide with flow content.** Org-chart boxes placed with `position:absolute` do not push later content down, so legends and bottom rows overlap the deepest column. Give the chart container an explicit height at least as tall as its deepest column (and size the SVG wire layer to match), and put legends/side panels in normal flow below it, not inside the chart's coordinate space.
- **Wide rows overflow the page edge.** A row that is the sum of fixed-width boxes can silently clip the rightmost box; check the right edge in the render, and shrink box widths or wrap rather than trusting the arithmetic. Compare the summed column widths in the build script against the chart container width (e.g. 976px at 11in landscape with 0.42in side padding) before printing; the clip is often only on page 1, and a reviewer notices it before you do.
- **Look at every page after every rebuild, not just the one you edited.** Changing a shared header, badge or footer touches all pages, and `overflow:hidden` hides clipping from text extraction.
- **Footers.** Pin with `position:absolute; bottom` and keep the body height budget below it; long tables need a split into pages by row count, not by overflow.
- Fix one overlap per iteration and re-render; fixing several blind causes new collisions.

## Review documents going to people

- When a draft becomes the official version, change the badge and footer in the shared `header()`/`foot()` helpers, then confirm with `pdftotext chart.pdf - | grep -ci "draft\|not for distribution\|sign-off"` returning 0, and grep for any other value you changed (hours, dates).
- Attribute corrections precisely. A callout that says "from <owner>" must contain only what the owner said; classifications someone else supplied or you inferred go in separate wording until confirmed.
- Text cut off in a source image or screenshot is not yours to complete. Ask the author (or whoever holds the source) for the full text. If it cannot be had, leave it out of an official version and say so in that page's subtitle; ship it only in a review draft, marked as cut off. Trimming a cut-off line at a comma into a tidy sentence is still guessing.
- Compute every count in the build script from the listed names (e.g. `sum(len(cards) for ...)`), never type it: a hand-typed total silently drifts from the sections beside it. Cross-check totals against any count printed on the source ("22 here").
- Before a review draft becomes official, strip notes written to the reviewer ("the chart you sent", "I could not check", "your call"). `pdftotext -layout chart.pdf all.txt` and grep for those phrases plus `\.\.\.|…` to catch leftovers. pdftotext keeps CSS `text-transform: uppercase`, so grep headings case-insensitively.
- Mark drafts with a visible badge (e.g. "Draft for sign-off, not for distribution"). Put a "Nothing sent" badge on recipient lists.
- A distribution list is a separate small PDF: recipient, seat, how it would reach them, and a note column. Show gaps (no address, unverified roster) in a distinct colour and list the open decisions in a box on the last page. Do not email anyone until the owner approves both the chart and the list.
- Before distributing a chart that someone else calls final or approved, compare it with the owner's latest corrections (`shasum -a 256` against the file you were given, and the message timestamp against the correction). An approved copy that predates a later correction is stale, so send the corrected version and say why.
- State unverifiable parts plainly in the reply (e.g. a roster you could not see) and keep the reply to: what changed, what is still open, the attachments.

## Working style for this user

- When a clear next step exists (rebuild with the corrections, make the list), do it and bring the result; do not ask permission for reversible local work. Sending to others still waits for approval.
- Keep the final text short and plain; the detailed checks stay in your work, not the chat.
