# DESIGN.md — evaluator UI (`/evaluate`, `/history`, compare, dashboard leaderboard)

Rules for every evaluator page. They come from 10 real sites in the LLM-evaluation / benchmark / observability niche, captured on 2026-11-24 at a 1440×900 window. Screenshots are in `docs/design/refs/`.

"Measured" values were read from the live page with `getComputedStyle`. "≈" values are estimated from the screenshot.

## 1. What each site does well

### 01 LMArena — `docs/design/refs/01-lmarena.jpg`
- **Layout:** two-column hero (large headline left, "New release rankings" list right). Below it a pill tab row switches leaderboards, then one large rounded container with a thin border holds three KPI columns separated by hairline vertical rules, and under those a chart (left) and a compact Top 15 table (right).
- **Type:** the headline is a light serif, 60px, weight 300 (measured). Body is a grotesk, 16px. Table headers are 10px, uppercase, wide letter-spacing, muted (measured). Numbers use tabular figures and are right-aligned.
- **Color:** warm charcoal `rgb(37,37,35)` background, warm off-white text `rgb(244,240,235)`, header text `rgb(199,194,188)` (all measured). One blue accent for the chart line. Ranks 1–3 get gold, silver and bronze chips.
- **Spacing:** large gaps between sections (≈ 60–70px). Table rows are tight (≈ 25px) and sit under a heavier rule below the header.
- **Why it works:** the answer ("Frontier / Daily driver / Efficient" plus a number) comes first, and the dense table is secondary.

### 02 Artificial Analysis — `02-artificial-analysis.jpg`
- **Layout:** headline plus a short explanation, five summary cards (each a pill label, one sentence of insight and logos), then a filter bar, then the table.
- **Type:** serif headline, 36px weight 400. Sans body, 16px. Table header 13px weight 500 on `rgb(241,242,244)` (all measured).
- **Table:** column headers are grouped under a band (Intelligence / Price / Speed / Latency). Units sit in small text under each column name ("USD", "Minutes"). Sort arrows are on every column. Each row starts with a thin colored bar for the model's vendor.
- **Why it works:** grouped headers with units mean the numbers need no explanation. Filters are labelled chips showing their current value ("Weights: All").

### 03 Hugging Face Open LLM Leaderboard — `03-hf-open-llm.jpg`
- **Layout:** centered hero, a prominent search bar with the result count ("4576 / 4576") and an Advanced Filters button, quick-filter chips with counts, then the table.
- **Table:** rank medals for the top three, a green status dot beside the Average, and a **data bar inside each benchmark cell** whose color runs from red to green. The first column pins rows. "table options" and "column visibility" controls sit above the table.
- **Why it works:** you can scan a wide table by color before reading any numbers. The cost is that color carries the signal, so it needs a number beside it.

### 04 Scale Labs — `04-scale-labs.jpg`
- **Layout:** one big hero, a segmented control (All / Agentic / Safety / Frontier), then a grid of square-cornered boxed cards. Each card is a mini top-3 leaderboard.
- **Type:** headline 60px weight 300 in the sans (measured). Section labels are small, uppercase, monospaced and in brackets (`[LEADERBOARDS]`).
- **Detail:** every score shows a ± confidence value and a bar with an error whisker. Rank is a solid dark circle. No shadows, only 1px hairlines.
- **Why it works:** it is honest about uncertainty and the restraint makes the data stand out.

### 05 OpenRouter Rankings — `05-openrouter.jpg`
- **Layout:** app-style. A top tab row has an underline on the active tab, a left sub-nav has icons with a tinted active item, and the content column is ≈ 1,000px wide.
- **Type:** Plus Jakarta Sans at **14px** body, page title only 20px weight 600, table header 14px weight 500 in `rgb(159,161,163)` (all measured). Small, calm headings suit a tool page.
- **Color:** near-black `rgb(3,8,10)` background, text `rgb(252,252,254)`, one lime accent for active states and the primary button.
- **Detail:** a Linear/Log toggle is a small segmented control at the chart's top right.
- **Why it works:** it behaves like software, not a landing page. Navigation is persistent and the title doesn't compete with the data.

### 06 Epoch AI — `06-epoch-ai.jpg`
- **Layout:** left-aligned, a sub-nav with an underline on the active item, an "Updated Nov. 24, 2026" line with a status dot above the title, a full-width search field, then stat cards.
- **Type:** Messina Sans, headline 46px weight 600, body 16px (measured). **Stat cards put a big number next to a small caption** ("173 top ECI", "432 models tracked").
- **Color:** white, black text and one mint-teal accent for buttons and the active tab.
- **Why it works:** the freshness date is the first thing you see. Big numbers with small captions read at a glance.

### 07 Promptfoo — `07-promptfoo.jpg`
- **Layout:** centered marketing page. A product screenshot sits in a rounded card under an underline tab row.
- **Type:** Inter, headline 64px weight 900, body 16px (measured). Red is the only accent and is used for the primary button and the active tab.
- **Detail in the product shot:** results use severity levels (Critical / High / Medium / Low / Informational), each with a colored top or left border strip.
- **Why it works (for us):** one accent color for the primary action, and pass/fail categories shown as border strips with a text label.

### 08 Braintrust — `08-braintrust.jpg`
- **Layout:** left-aligned hero with a lot of whitespace, a pill primary button next to a grey secondary one.
- **Type:** headline 60px weight 400 in a custom sans, body Inter 16px (measured). Small uppercase monospaced labels ("TRUSTED BY…").
- **Color:** white, black text, one saturated indigo-blue accent.
- **Detail:** the hero art is rows of tick marks and dots, a visual metaphor for traces and events.
- **Why it works (for us):** a single saturated accent, mono for metadata labels, and pill buttons for primary vs secondary actions.

### 09 Vellum LLM Leaderboard — `09-vellum.jpg`
- **Layout:** large headline, a two-line explanation, an "Updated / Data as of" line, then one rounded card holding a segmented control (Compare all / Open source / Proprietary), a dropdown ("All reported setups") and a bar chart.
- **Type:** Manrope headline 64px weight 600, Geist body 16px in `rgb(23,30,41)`. Table header 11px on `rgb(247,248,249)` (all measured).
- **Chart:** the value is printed above every bar, a light axis shows the scale, and color means vendor.
- **Why it works:** two dates ("updated" and "data as of") separate when the page changed from when the data was collected. Values are labelled directly, so a hover isn't needed.

### 10 Langfuse — `10-langfuse.jpg`
- **Layout:** left sidebar with community stats and a changelog, and a centered content frame drawn with corner brackets.
- **Type:** headline 68px weight 500 with a highlighter-yellow band behind it (measured). Body Inter 16px in `rgb(2,8,23)` on warm `rgb(237,237,232)`.
- **Detail:** buttons carry a keyboard hint (S, D, L, G). Metadata links use small mono type.
- **Why it works (for us):** keyboard hints on primary actions, and a quiet warm neutral background.

## 2. Patterns shared by most of them

1. **Answer first.** A summary with 1–3 big numbers or a "who is best" line sits above the dense table (LMArena, Epoch, Artificial Analysis).
2. **One accent color** for primary actions and active state. Everything else is neutral (OpenRouter, Promptfoo, Braintrust, Epoch).
3. **Small, calm type for tools.** Body 14–16px, headings 20px on app pages. Large display headlines appear only on marketing pages.
4. **Right-aligned, tabular numbers** with units in the column header, not repeated in every cell.
5. **Freshness is shown.** "Updated" and "data as of" dates appear near the title (Epoch, Vellum).
6. **Filters are visible chips or segmented controls that show their current value**, not hidden menus.
7. **Uncertainty or provenance is visible** (Scale's ± values, Vellum's "data as of").
8. **Color never stands alone.** Rank badges, data bars and severity strips are always paired with a number or a label.

## 3. Rules for Evonic evaluator pages

Stack: server-rendered Jinja templates, compiled Tailwind (`static/css/tailwind.css`), and the CSS variables in `static/style.css` (`--bg-page`, `--bg-card`, `--text-heading`, `--text-secondary`, `--border-color`, with a dark theme). **Use those variables and the existing font stack. Do not add a webfont or a new CSS framework.**

### Layout
- **Page order:** title and meta line → summary strip → controls → table or matrix.
- **Title area:** the page title uses the existing heading size. Under it goes a muted meta line: model, run id, date, and the test-set badge (see Test set). On the history and dashboard pages the meta line carries the same "updated" date.
- **Summary strip:** 3–4 stat cards in a responsive grid (`grid-cols-2 md:grid-cols-4`, gap 16px). Each card is a large number with a small caption beside it, like Epoch's. Card radius and border follow the existing cards.
- **Content width:** keep the existing container. The matrix and tables scroll horizontally inside their card on phone widths. The page itself never scrolls horizontally.
- **Sections:** 24–32px between sections. Table rows are compact (≈ 32–36px).

### Type
- Body is 14px, page titles use the existing heading size, and card captions are 12px.
- **Table headers and field labels** are 11–12px, uppercase, letter-spaced, in `--text-secondary`.
- IDs (run id, test id, hashes) are monospace.
- Every numeric column uses `tabular-nums` and is right-aligned.

### Color
- **Neutrals** come from the existing variables. The accent is the existing indigo (`#4f46e5` light, `#818cf8` dark, as used by the dashboard leaderboard).
- **Status colors:** pass = green, fail = red, partial = amber, neutral = grey. They are used only for status.
- **Color never stands alone.** Every colored cell also shows its number. Every pass/fail marker also shows a ✓ / ✗ or the word.
- Both light and dark themes must be checked.

### Scores: percentage in the matrix, one absolute number for the total
- **Matrix cells (domain × level) and the domain total** show a percentage inside a colored box, with the time in seconds in small text under it. A domain's percentage is its earned points divided by the points available in the levels that ran.
- **Box color** has four tones and the percentage is always printed inside, so color never stands alone:
  - grey: skipped, error or no result yet (the box says `skip`, `err` or `-`)
  - red: below 25%
  - yellow: 25% to 75%
  - green: above 75%
- **The overall score of a run** is one absolute number, `Σ (test score × level × weight)`, with at most one decimal and no maximum next to it (`14.5`, label "points"). It is not a percentage, and adding tests to the test set never changes it for a run already done. The same single number is used on the score card, the completion modal, the history list and run detail, the compare page, the leaderboard and the dashboard alert.
- **A single test** may still show `points / max_points` in its detail view.
- **Diffs** (compare page) have an explicit sign and a text marker: `+2.5 ▲` in green, `−1.0 ▼` in red, `0` in grey. Never color alone.
- **Rank** is shown with the number, and the top three get a chip as on LMArena and Hugging Face.
- **Leaderboard bars** use points on the axis, with the value printed at the end of each bar.
- **Matrix layout:** desktop rows are compact (one box plus one line of time). On mobile each domain is a two-line card: name and total on the first line, L1 to L5 under it.

### Test set
- **Every place that shows a run total** also shows its test-set status as a small badge next to the run's meta line: `Current set`, `Different set`, or `Legacy`. The badge has a tooltip with the number of tests and the max points.
- **Comparisons** state what they are based on: "Compared on N shared tests". Tests that changed or exist in only one run are listed in a collapsed section, not hidden. Legacy runs whose definitions can't be verified show an "unverified" note, following the provenance rule from Scale and Vellum.
- **Dates:** show both the run date and, where it applies, when the test set last changed.

### Controls
- **Filters and sorters** are chips or a segmented control that show their current value (`Test set: Current`).
- **Primary action** = one filled accent button per view. Secondary actions are neutral outline buttons.
- **Run selection for compare** uses checkboxes on the history table. The "Compare" button stays disabled until exactly two runs are selected, and says so.
- **Keyboard hints** (as on Langfuse) are optional and only for the primary action on `/evaluate`.

### States and accessibility
- Every table has an empty state with one line of explanation and the action that fixes it.
- Loading uses the existing skeleton style.
- Contrast of text on tinted cells must stay readable in both themes.
- Interactive elements have visible focus.
- At phone width the summary grid becomes 2 columns, the filter bar wraps, and the 16px side gutter is kept.

## 4. Applying it
Apply this document to: `templates/evaluate.html`, `history.html`, `history_detail.html`, the new `history_compare.html`, and `static/js/dashboard.js` (leaderboard). New CSS goes in `static/style.css` using the existing variables. Tailwind utility classes that are not already compiled need a Tailwind rebuild, so prefer classes the templates already use.
