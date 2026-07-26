#!/usr/bin/env python3
"""Regenerate the committed figures in ``docs/assets/``.

    python3 docs/assets/make_figures.py

Standard library only, no arguments, deterministic: the same source produces byte-identical
SVGs. Each figure is emitted twice — a light and a dark variant — because GitHub cannot style
an SVG that is referenced as an image, so light/dark is selected by the ``<picture>`` element
in the surrounding Markdown instead.

**Every number in this file is quoted from ``docs/RESULTS.md``**, with the section it comes from
named in a comment. Nothing here is computed from vendor data, and nothing here is a figure that
is not already stated in the committed prose: these are drawings of published aggregates, which
is the only thing the licence position in ``docs/RESULTS.md`` § "Reproducibility, honestly"
permits. If a number in ``RESULTS.md`` is ever corrected, correct it here and re-run.

One honest gap in that guarantee. ``social-card.png`` is a rasterisation of the committed
``social-card.svg``, produced out of band because GitHub's social-preview upload will not take a
vector, and it is **not** covered by the CI reproducibility check — the check re-runs this script
and diffs ``docs/assets``, and this script does not write the PNG. Editing the card therefore
means re-exporting the raster by hand. It was produced with::

    python3 docs/assets/make_figures.py
    # then, from a directory holding social-card.svg and an <img> wrapper at 1280x640:
    "…/Google Chrome" --headless --disable-gpu --hide-scrollbars \\
        --force-device-scale-factor=1 --screenshot=social-card.png \\
        --window-size=1280,640 file://…/wrap.html

The card is uploaded under the repository's Settings → Social preview; nothing reads it from the
tree.
"""

from __future__ import annotations

import os
from typing import Iterable

# --------------------------------------------------------------------------------------------
# Data — all of it quoted from docs/RESULTS.md
# --------------------------------------------------------------------------------------------

# docs/RESULTS.md § "Family 2 — intraday cross-sectional relative strength", measured table.
# One gate, two benchmarks: the criterion is "active P&L > 0 vs *both* benchmarks", so beating
# the equal-weight basket alone does not pass it.
BENCHMARK_ROWS = [
    ("Net P&L, modelled", "> 0", -839.68, False),
    ("Active vs exposure-matched", "> 0", -120.65, False),
    ("Active vs equal-weight basket", "> 0", 405.64, True),
]

# Same section. Each row is measured against a threshold that is not zero, so each can be drawn
# as a percentage of its own predeclared limit with a single shared goalpost at 100 %.
#   (label, requirement, measured, display, threshold, direction, passed)
# direction "floor" = measured must be >= threshold; "cap" = measured must be <= threshold.
LIMIT_ROWS = [
    ("Profit factor", "≥ 1.10", 0.55, "0.55", 1.10, "floor", False),
    ("Max drawdown", "≤ 1.50 %", 0.87, "0.87 %", 1.50, "cap", True),
    ("Worst day", "≤ 0.75 %", 0.11, "0.11 %", 0.75, "cap", True),
    ("p95 realism gap", "≤ 15 bps", 29.82, "29.82 bps", 15.0, "cap", False),
    ("Max single-fill divergence", "≤ 50 bps", 97.48, "97.48 bps", 50.0, "cap", False),
]

# docs/RESULTS.md § "Family 1 — intraday momentum", the cost-decomposition block.
# (label, amount, kind) — "delta" floats from the running total, "total" is drawn from zero.
WATERFALL = [
    ("Signal, mid-to-mid", 21.70, "delta"),
    ("Round-trip half-spread", -6939.02, "delta"),
    ("Gross modelled", -6917.32, "total"),
    ("Fees", -388.34, "delta"),
    ("Net", -7305.66, "total"),
]

# --------------------------------------------------------------------------------------------
# Theme
# --------------------------------------------------------------------------------------------

THEMES = {
    "light": {
        "fg": "#1f2328",
        "muted": "#636c76",
        "faint": "#8c959f",
        "rule": "#d1d9e0",
        "axis": "#57606a",
        "fail": "#cf222e",
        "fail_soft": "#ffcecb",
        "pass": "#1a7f37",
        "pass_soft": "#c5f2ce",
        "neutral": "#57606a",
        "neutral_soft": "#d8dee4",
    },
    "dark": {
        "fg": "#e6edf3",
        "muted": "#9198a1",
        "faint": "#6e7681",
        "rule": "#30363d",
        "axis": "#8b949e",
        "fail": "#ff7b72",
        "fail_soft": "#5c1f1c",
        "pass": "#3fb950",
        "pass_soft": "#12341c",
        "neutral": "#8b949e",
        "neutral_soft": "#30363d",
    },
}

SANS = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"
)
MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, monospace"


# --------------------------------------------------------------------------------------------
# Tiny SVG helpers
# --------------------------------------------------------------------------------------------


def esc(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def txt(
    x: float,
    y: float,
    body: str,
    *,
    fill: str,
    size: float = 13,
    weight: str = "400",
    anchor: str = "start",
    family: str = SANS,
    opacity: float | None = None,
) -> str:
    extra = f' opacity="{opacity}"' if opacity is not None else ""
    return (
        f'<text x="{num(x)}" y="{num(y)}" font-family="{family}" font-size="{num(size)}" '
        f'font-weight="{weight}" fill="{fill}" text-anchor="{anchor}"{extra}>{esc(body)}</text>'
    )


def rect(x: float, y: float, w: float, h: float, *, fill: str, rx: float = 2,
         opacity: float | None = None) -> str:
    extra = f' opacity="{opacity}"' if opacity is not None else ""
    return (
        f'<rect x="{num(x)}" y="{num(y)}" width="{num(w)}" height="{num(h)}" '
        f'rx="{num(rx)}" fill="{fill}"{extra}/>'
    )


def line(x1: float, y1: float, x2: float, y2: float, *, stroke: str, width: float = 1,
         dash: str | None = None) -> str:
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<line x1="{num(x1)}" y1="{num(y1)}" x2="{num(x2)}" y2="{num(y2)}" '
        f'stroke="{stroke}" stroke-width="{num(width)}"{d}/>'
    )


def num(value: float) -> str:
    """Format a coordinate deterministically and without trailing zero noise."""
    rounded = round(float(value), 2)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:g}"


def money(value: float) -> str:
    sign = "−" if value < 0 else "+"
    return f"{sign}${abs(value):,.2f}"


def svg(width: float, height: float, title: str, desc: str, body: Iterable[str]) -> str:
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {num(width)} {num(height)}" '
        f'width="{num(width)}" height="{num(height)}" role="img" '
        f'aria-labelledby="figtitle figdesc">',
        f'<title id="figtitle">{esc(title)}</title>',
        f'<desc id="figdesc">{esc(desc)}</desc>',
    ]
    parts.extend(body)
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


# --------------------------------------------------------------------------------------------
# Figure A — family 2 against the criteria fixed before the run
# --------------------------------------------------------------------------------------------

FIG_A_W = 780
NAME_X = 24
TRACK_X0 = 288          # left edge of every track, both bands
TRACK_HALF = 121        # pixels from TRACK_X0 to the reference column
REF_X = TRACK_X0 + TRACK_HALF   # zero in band A, the threshold in band B
NOTE_X = 545            # uniform column for the "× the cap" annotations
VALUE_X = 694           # right-aligned measured value
CHIP_X = 710
ROW_H = 30
SOURCE = "source: docs/RESULTS.md"


def verdict_chip(x: float, y: float, passed: bool, t: dict) -> list[str]:
    colour = t["pass"] if passed else t["fail"]
    soft = t["pass_soft"] if passed else t["fail_soft"]
    glyph = "✓" if passed else "✗"
    return [
        rect(x, y - 12, 46, 18, fill=soft, rx=9),
        txt(x + 23, y + 1, f"{glyph} {'PASS' if passed else 'FAIL'}", fill=colour, size=9,
            weight="700", anchor="middle"),
    ]


def figure_a(theme: str) -> str:
    t = THEMES[theme]
    b: list[str] = []

    b.append(txt(NAME_X, 36, "Family 2 against criteria fixed before the run", fill=t["fg"],
                 size=17, weight="700"))
    b.append(txt(NAME_X, 58,
                 "Cross-sectional relative strength · 1,144 trades · 21 sessions "
                 "· clean window 2026-03-10 → 2026-04-08",
                 fill=t["muted"], size=12))

    # ---- band A: the gates whose threshold is zero -------------------------------------------
    y = 100
    b.append(txt(NAME_X, y, "MUST BE POSITIVE", fill=t["faint"], size=9.5, weight="700"))
    b.append(txt(TRACK_X0, y, "one gate, two benchmarks — both must be beaten",
                 fill=t["faint"], size=9.5))
    b.append(line(NAME_X, y + 10, FIG_A_W - NAME_X, y + 10, stroke=t["rule"]))

    scale_a = TRACK_HALF / max(abs(v) for _, _, v, _ in BENCHMARK_ROWS)
    y += 34
    row_y: list[float] = []
    for label, req, value, beaten in BENCHMARK_ROWS:
        row_y.append(y)
        colour = t["pass"] if beaten else t["fail"]
        soft = t["pass_soft"] if beaten else t["fail_soft"]
        b.append(txt(NAME_X, y + 4, label, fill=t["fg"], size=12.5))
        b.append(txt(NAME_X + 248, y + 4, req, fill=t["faint"], size=11, anchor="end"))
        width = abs(value) * scale_a
        x0 = REF_X - width if value < 0 else REF_X
        b.append(rect(x0, y - 6, width, 14, fill=soft))
        b.append(txt(VALUE_X, y + 4, money(value), fill=colour, size=12.5, weight="700",
                     anchor="end", family=MONO))
        y += ROW_H

    # Row 0 is a gate of its own. Rows 1 and 2 are ONE gate — "active P&L > 0 vs *both*
    # benchmarks" — so beating the basket earns no pass chip of its own. Drawing one would
    # be the exact self-deception the two-benchmark rule exists to prevent.
    b.extend(verdict_chip(CHIP_X, row_y[0] + 4, False, t))
    brace_x = CHIP_X - 8
    b.append(line(brace_x, row_y[1] - 8, brace_x, row_y[2] + 8, stroke=t["rule"], width=1))
    b.append(line(brace_x, row_y[1] - 8, brace_x + 4, row_y[1] - 8, stroke=t["rule"], width=1))
    b.append(line(brace_x, row_y[2] + 8, brace_x + 4, row_y[2] + 8, stroke=t["rule"], width=1))
    b.extend(verdict_chip(CHIP_X, (row_y[1] + row_y[2]) / 2 + 4, False, t))

    b.append(line(REF_X, row_y[0] - 10, REF_X, y - 12, stroke=t["axis"], width=1))
    b.append(txt(REF_X, y + 6, "0", fill=t["faint"], size=9.5, anchor="middle", family=MONO))
    b.append(txt(NOTE_X, y + 6, "beaten — but the basket simply lost more",
                 fill=t["faint"], size=9.5))

    # ---- band B: the gates with a real threshold, drawn as a share of that threshold ----------
    y += 44
    b.append(txt(NAME_X, y, "AGAINST ITS OWN PREDECLARED LIMIT", fill=t["faint"], size=9.5,
                 weight="700"))
    b.append(txt(TRACK_X0, y, "bar length = measured, as a share of the threshold",
                 fill=t["faint"], size=9.5))
    b.append(line(NAME_X, y + 10, FIG_A_W - NAME_X, y + 10, stroke=t["rule"]))

    band_b_top = y + 22
    y += 34
    for label, req, value, display, threshold, direction, passed in LIMIT_ROWS:
        colour = t["pass"] if passed else t["fail"]
        soft = t["pass_soft"] if passed else t["fail_soft"]
        ratio = value / threshold
        b.append(txt(NAME_X, y + 4, label, fill=t["fg"], size=12.5))
        b.append(txt(NAME_X + 248, y + 4, req, fill=t["faint"], size=11, anchor="end"))
        bar_end = TRACK_X0 + ratio * TRACK_HALF
        b.append(rect(TRACK_X0, y - 6, ratio * TRACK_HALF, 14, fill=soft))
        if direction == "floor" and not passed:
            # A short bar means FAIL against a floor and PASS against a cap. Shading the
            # shortfall keeps the two readings from looking identical.
            b.append(rect(bar_end, y - 6, REF_X - bar_end, 14, fill=colour, rx=0, opacity=0.12))
        note = f"{ratio:.2f}× {'floor' if direction == 'floor' else 'cap'}"
        b.append(txt(NOTE_X, y + 4, note, fill=t["faint"], size=10, family=MONO))
        b.append(txt(VALUE_X, y + 4, display, fill=colour, size=12.5, weight="700",
                     anchor="end", family=MONO))
        b.extend(verdict_chip(CHIP_X, y + 4, passed, t))
        y += ROW_H

    b.append(line(REF_X, band_b_top, REF_X, y - 12, stroke=t["axis"], width=1, dash="3 3"))
    b.append(txt(REF_X, y + 6, "the threshold", fill=t["faint"], size=9.5, anchor="middle"))

    # ---- caption ------------------------------------------------------------------------------
    y += 34
    b.append(line(NAME_X, y, FIG_A_W - NAME_X, y, stroke=t["rule"]))
    y += 22
    b.append(txt(NAME_X, y,
                 "5 of 11 predeclared gates passed. The gate is an AND: one failure is a null.",
                 fill=t["fg"], size=12, weight="600"))
    b.append(txt(FIG_A_W - NAME_X, y, SOURCE, fill=t["faint"], size=9.5, anchor="end"))
    y += 18
    b.append(txt(NAME_X, y,
                 "Not shown: trades 1,144 ≥ 30, sessions 21 ≥ 20, traded sessions "
                 "21 ≥ 5 — all pass — and average trade −8.78 bps against a "
                 "> 0 requirement, which fails.",
                 fill=t["muted"], size=11))
    y += 16
    # PLAN.md records that the staged quotes predate fix A. The rerun's numbers differ and the
    # verdict does not; a scorecard captioned "measured" has to say so on its face.
    b.append(txt(NAME_X, y,
                 "Staged pre-fix-A run; the fix-A-compliant rerun — 1,147 trades, "
                 "−$858.01 net, p95 29.95 bps — fails every gate the same way.",
                 fill=t["faint"], size=10.5))

    height = y + 26
    desc = (
        "Scorecard of the relative-strength strategy family against the eleven pass criteria "
        "pinned before the run. Net P&L was minus $839.68 and active P&L against the "
        "exposure-matched benchmark was minus $120.65, both failing a greater-than-zero "
        "requirement; it beat the equal-weight basket by $405.64, but the criterion requires "
        "beating both benchmarks. Profit factor 0.55 reached only 0.50 times the 1.10 floor. "
        "Max drawdown 0.87 percent and worst day 0.11 percent passed their caps. The p95 "
        "realism gap of 29.82 basis points was 1.99 times its 15 basis point cap and the max "
        "single-fill divergence of 97.48 basis points was 1.95 times its 50 basis point cap. "
        "Five of eleven gates passed; the gate is an AND, so the family was nulled. The staged "
        "quotes predate fix A; the fix-A-compliant rerun gives 1,147 trades and minus $858.01 "
        "net, and fails every gate the same way."
    )
    return svg(FIG_A_W, height, "Family 2 against criteria fixed before the run", desc, b)


# --------------------------------------------------------------------------------------------
# Figure B — where family 1's money went
# --------------------------------------------------------------------------------------------

FIG_B_W = 780
COL_W = 86
COL_GAP = 42
PLOT_X0 = 118
ZERO_Y = 118
PLOT_H = 236


def figure_b(theme: str) -> str:
    t = THEMES[theme]
    b: list[str] = []

    b.append(txt(NAME_X, 36, "Where the money went — and it was not the signal",
                 fill=t["fg"], size=17, weight="700"))
    b.append(txt(NAME_X, 58,
                 "Family 1, intraday momentum · broader window, 21 sessions · "
                 "9,923 trades on $8,348,811 of traded notional",
                 fill=t["muted"], size=12))

    span = max(abs(v) for _, v, _ in WATERFALL)
    scale = PLOT_H / span

    b.append(line(NAME_X, ZERO_Y, FIG_B_W - NAME_X, ZERO_Y, stroke=t["axis"], width=1))
    b.append(txt(NAME_X, ZERO_Y - 6, "$0", fill=t["faint"], size=10, family=MONO))

    running = 0.0
    prev_x1: float | None = None
    prev_y: float | None = None
    for index, (label, value, kind) in enumerate(WATERFALL):
        x0 = PLOT_X0 + index * (COL_W + COL_GAP)
        if kind == "total":
            # A running total is anchored to the axis rather than floating from the last bar.
            top, bottom = 0.0, value
            colour = t["neutral"]
            soft = t["neutral_soft"]
        else:
            top, bottom = running, running + value
            colour = t["fail"] if value < 0 else t["pass"]
            soft = t["fail_soft"] if value < 0 else t["pass_soft"]
            running += value

        y_top = ZERO_Y - max(top, bottom) * scale
        y_bottom = ZERO_Y - min(top, bottom) * scale
        height = max(y_bottom - y_top, 2.0)          # a 0.7 px bar would vanish; keep a hairline
        b.append(rect(x0, y_top, COL_W, height, fill=soft, rx=1))
        accent_y = y_top if value >= 0 else y_top + height - 2   # accent marks where the move ends
        b.append(rect(x0, accent_y, COL_W, 2, fill=colour, rx=0))

        if prev_x1 is not None and prev_y is not None:
            b.append(line(prev_x1, prev_y, x0, prev_y, stroke=t["rule"], width=1, dash="2 3"))
        prev_x1 = x0 + COL_W
        prev_y = ZERO_Y - bottom * scale              # bottom is the cumulative total after this bar

        value_y = y_top - 8 if value >= 0 else y_bottom + 16
        b.append(txt(x0 + COL_W / 2, value_y, money(value), fill=colour, size=12,
                     weight="700", anchor="middle", family=MONO))
        b.append(txt(x0 + COL_W / 2, ZERO_Y + PLOT_H + 34, label, fill=t["fg"], size=11.5,
                     anchor="middle"))
        if kind == "total":
            b.append(txt(x0 + COL_W / 2, ZERO_Y + PLOT_H + 50, "running total", fill=t["faint"],
                         size=9.5, anchor="middle"))

    # The signal bar is 0.7 px tall at this scale. That is the finding, so it is called out
    # in words rather than exaggerated into visibility.
    b.append(txt(NAME_X, ZERO_Y - 34,
                 "The whole signal is the green hairline on the axis: +$21.70, "
                 "or +0.03 bps of notional.",
                 fill=t["muted"], size=11))

    y = ZERO_Y + PLOT_H + 78
    b.append(line(NAME_X, y, FIG_B_W - NAME_X, y, stroke=t["rule"]))
    y += 22
    b.append(txt(NAME_X, y,
                 "The strategy was not wrong. It had nothing — and paid the spread 9,923 "
                 "times to find that out.",
                 fill=t["fg"], size=12, weight="600"))
    y += 18
    b.append(txt(NAME_X, y,
                 "95 % of the loss is the round-trip half-spread, 5 % is fees. Modelled P&L "
                 "over historical data; no capital was ever at risk.",
                 fill=t["muted"], size=11))
    b.append(txt(FIG_B_W - NAME_X, y, SOURCE, fill=t["faint"], size=9.5, anchor="end"))

    height = y + 26
    desc = (
        "Waterfall decomposition of the momentum family's loss over 9,923 trades on $8.35 "
        "million of traded notional. The signal marked mid-to-mid, frictionlessly, earned "
        "plus $21.70 — 0.03 basis points, indistinguishable from zero and too small to be "
        "visible at this scale. The round-trip half-spread cost minus $6,939.02, giving a "
        "gross modelled result of minus $6,917.32. Fees cost a further minus $388.34, for a "
        "net of minus $7,305.66. The round-trip half-spread is 95 percent of the loss and "
        "fees are 5 percent."
    )
    return svg(FIG_B_W, height, "Where the money went", desc, b)


# --------------------------------------------------------------------------------------------


# --------------------------------------------------------------------------------------------
# The social preview card — 1280x640, the size GitHub asks for
# --------------------------------------------------------------------------------------------

# This one commits to a single dark treatment rather than following a reader's theme: it is
# uploaded to GitHub's repository settings as a static image and rendered by X, Slack and
# LinkedIn on their own backgrounds, so it carries its own ground.
CARD = {
    "bg": "#0b0e13",
    "ink": "#e8eef4",
    "muted": "#9aa4b2",
    "faint": "#656e7c",
    "accent": "#7d9dc4",
}

# "Orders submitted by the agent" — not "orders submitted by this repository". The operator
# verification drill sent exactly one, outside the decision loop. The distinction is the whole
# posture, and a card that blurred it would be the first thing worth calling out.
CARD_STATS = [
    ("0", "orders submitted by the agent"),
    ("$0", "real money at risk, ever"),
    ("2,000", "offline tests, no install"),
]


def social_card() -> str:
    c = CARD
    w, h = 1280, 640
    pad = 84
    b: list[str] = [rect(0, 0, w, h, fill=c["bg"], rx=0)]

    b.append(txt(pad, 96, "US EQUITIES · PAPER-FIRST · ARCHIVED ARTIFACT", fill=c["accent"],
                 size=15, weight="600", family=MONO))

    b.append(txt(pad, 196, "An autonomous trading agent", fill=c["ink"], size=54, weight="650"))
    b.append(txt(pad, 258, "that has never placed a single trade", fill=c["ink"], size=54,
                 weight="650"))
    b.append(txt(pad, 318, "And that's the point.", fill=c["muted"], size=25))

    b.append(line(pad, 380, w - pad, 380, stroke="#232a34", width=1))

    for index, (value, label) in enumerate(CARD_STATS):
        x = pad + index * 400
        b.append(rect(x, 424, 28, 2, fill=c["accent"], rx=0))
        b.append(txt(x, 486, value, fill=c["ink"], size=44, weight="700", family=MONO))
        b.append(txt(x, 516, label, fill=c["muted"], size=15))

    b.append(txt(pad, 572,
                 "Apache-2.0 · two strategy families predeclared, measured and nulled on "
                 "their own criteria",
                 fill=c["faint"], size=15))

    desc = (
        "Social preview card. Headline: an autonomous trading agent that has never placed a "
        "single trade — and that's the point. Zero orders submitted by the agent, zero real "
        "money at risk ever, 2,000 offline tests with no install. Apache-2.0; two strategy "
        "families were predeclared, measured and nulled on their own criteria."
    )
    return svg(w, h, "An autonomous trading agent that has never placed a single trade", desc, b)


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    figures = {
        "criteria-vs-measured": figure_a,
        "cost-decomposition": figure_b,
    }
    for name, build in sorted(figures.items()):
        for theme in ("light", "dark"):
            path = os.path.join(here, f"{name}-{theme}.svg")
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(build(theme))
            print(f"wrote {os.path.relpath(path, os.path.dirname(here))}")

    card = os.path.join(here, "social-card.svg")
    with open(card, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(social_card())
    print(f"wrote {os.path.relpath(card, os.path.dirname(here))}")


if __name__ == "__main__":
    main()
