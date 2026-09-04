"""Rendering for the docs/04 s4.1 lag x feature screen.

This module draws matrices; it does not compute them. Nothing here reads
`observations/`, aggregates a quarter, or decides a coefficient -- a caller hands
it a finished `feature x lag` frame and it returns a figure. That split is the
same one hard rule 6 draws around the dashboard, for the same reason: a change to
a colour ramp must not be able to move a number in an analysis of record.

Pearson r is *signed*, so it takes a **diverging** ramp -- two hues either side of
a neutral gray midpoint -- rather than a sequential one, which would put r = -0.4
and r = +0.4 at the same distance from nothing and hide the sign the screen exists
to show.

The project data-viz reference palette publishes a blue ramp and names the
diverging pair as blue <-> red with equal step count per arm, but ships no red
ramp. So the red arm is the blue arm re-hued: each step keeps its OKLCh lightness
and chroma and takes red's hue angle, which is the palette's own "hold the hue,
move the lightness" construction run the other way round. The arms are then mirror
images by construction rather than by eye, and `arm_lightness` exposes the profile
so a test can hold it to the lightness monotonicity that is the documented check
for a diverging ramp.

Panels are grouped into `Block`s, each drawn against the feature axis its family
of parameters actually produces, because docs/04 s2 gives the ecological features
to `sea_water_temperature` alone. One axis for all of them made every wave and met
panel about 45% hatching -- a page of holes to protect an alignment between rows
that do not exist in both panels. Rows still line up *within* a block, which is
where a reader compares, and the label carries what the hatching used to say.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import matplotlib as mpl
import numpy as np
import pandas as pd
from matplotlib import pyplot as plt
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.font_manager import FontProperties
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Rectangle
from matplotlib.textpath import TextPath
from matplotlib.ticker import FuncFormatter, MaxNLocator

# --- the reference palette, by role ------------------------------------------

SURFACE = "#fcfcfb"
NEUTRAL = "#f0efec"
INK = "#0b0b0b"
INK_ON_DARK = "#ffffff"
SECONDARY = "#52514e"
MUTED = "#898781"
GRIDLINE = "#e1e0d9"

#: Blue ramp steps 100..700, light -> dark. The negative arm, and the profile the
#: positive arm mirrors.
BLUE_ARM = ("#cde2fb", "#9ec5f4", "#5598e7", "#2a78d6", "#184f95", "#0d366b")

#: Categorical slot 8. Only its hue angle is used; lightness and chroma come from
#: the blue arm, so the two arms cannot drift apart.
RED_ANCHOR = "#e34948"

_LINEAR_TO_LMS = np.array(
    [
        [0.4122214708, 0.5363325363, 0.0514459929],
        [0.2119034982, 0.6806995451, 0.1073969566],
        [0.0883024619, 0.2817188376, 0.6299787005],
    ]
)
_LMS_TO_OKLAB = np.array(
    [
        [0.2104542553, 0.7936177850, -0.0040720468],
        [1.9779984951, -2.4285922050, 0.4505937099],
        [0.0259040371, 0.7827717662, -0.8086757660],
    ]
)


def _hex_to_rgb(value: str) -> np.ndarray:
    value = value.lstrip("#")
    return np.array([int(value[index : index + 2], 16) / 255 for index in (0, 2, 4)])


def _rgb_to_hex(rgb: np.ndarray) -> str:
    return "#" + "".join(f"{round(255 * float(channel)):02x}" for channel in np.clip(rgb, 0, 1))


def _to_linear(rgb: np.ndarray) -> np.ndarray:
    return np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)


def _from_linear(rgb: np.ndarray) -> np.ndarray:
    return np.where(rgb <= 0.0031308, rgb * 12.92, 1.055 * np.abs(rgb) ** (1 / 2.4) - 0.055)


def _oklab(hex_colour: str) -> np.ndarray:
    """OKLab (L, a, b) for an sRGB hex."""
    return _LMS_TO_OKLAB @ np.cbrt(_LINEAR_TO_LMS @ _to_linear(_hex_to_rgb(hex_colour)))


def _from_oklab(lab: np.ndarray) -> str:
    lms = np.linalg.solve(_LMS_TO_OKLAB, lab) ** 3
    return _rgb_to_hex(_from_linear(np.linalg.solve(_LINEAR_TO_LMS, lms)))


def _rehue(hex_colour: str, hue: float) -> str:
    """`hex_colour`'s lightness and chroma, carried to a new OKLCh hue angle."""
    lightness, a, b = _oklab(hex_colour)
    chroma = float(np.hypot(a, b))
    return _from_oklab(np.array([lightness, chroma * np.cos(hue), chroma * np.sin(hue)]))


def red_arm() -> tuple[str, ...]:
    """The blue arm's lightness and chroma at red's hue angle, light -> dark."""
    _, a, b = _oklab(RED_ANCHOR)
    hue = float(np.arctan2(b, a))
    return tuple(_rehue(step, hue) for step in BLUE_ARM)


def arm_lightness(arm: tuple[str, ...]) -> list[float]:
    """OKLab L per step -- the documented check for a diverging ramp is on these."""
    return [float(_oklab(step)[0]) for step in arm]


def diverging_colormap() -> LinearSegmentedColormap:
    """Blue (negative) -> neutral gray (zero) -> red (positive), arms mirrored."""
    return LinearSegmentedColormap.from_list(
        "kelpcompare_diverging", [*reversed(BLUE_ARM), NEUTRAL, *red_arm()]
    )


def _relative_luminance(hex_colour: str) -> float:
    red, green, blue = _to_linear(_hex_to_rgb(hex_colour))
    return float(0.2126 * red + 0.7152 * green + 0.0722 * blue)


def _ink_for(rgba: tuple[float, ...]) -> str:
    """Primary ink or white, whichever carries more contrast on this cell."""
    red, green, blue = _to_linear(np.array(rgba[:3]))
    luminance = float(0.2126 * red + 0.7152 * green + 0.0722 * blue)
    on_ink = (luminance + 0.05) / (_relative_luminance(INK) + 0.05)
    on_white = (_relative_luminance(INK_ON_DARK) + 0.05) / (luminance + 0.05)
    return INK if on_ink >= on_white else INK_ON_DARK


# --- the geometry, in inches ---------------------------------------------------
#
# Laid out explicitly rather than by `tight_layout`, which solves for the tight
# bounding boxes of the axes and their titles and so has nowhere to put a band
# that belongs to neither: a block rule needs room *above* a panel title, and a
# layout engine that has already packed the title against the row above it has
# none to give. Every number below is a measured allowance, and the figure size is
# their sum, so what the page contains is decided here rather than negotiated.

CELL_W = 0.62  #: one lag column
CELL_H = 0.36  #: one feature row
TITLE_H = 0.76  #: the lag axis and the two-line panel title above each row
BLOCK_LABEL_H = 0.44  #: the rule and label heading each block
TOP_H = 0.78  #: the figure title
BOTTOM_H = 1.32  #: the colour key, the legend and the caption
LEFT_W = 0.10  #: the page margin outside the feature labels
RIGHT_W = 0.20
COLUMN_GAP = 0.50  #: a panel's right edge to the next column's feature labels
LABEL_PT = 7.5  #: feature and lag tick labels
TITLE_PAD = 24  #: points, panel title clear of the lag axis above the grid


@dataclass(frozen=True)
class Panel:
    """One series' matrix, and the two flags that qualify how to read its cells.

    `matrix` is `feature x lag`, as `matrix()` in notebooks/01-lag-screen.ipynb
    returns it. `low_resolution` and `n_eff` are aligned frames of the same shape.
    A feature absent from `matrix` is *not applicable* to this parameter, which the
    renderer draws as an empty hatched cell rather than as a coefficient of zero.
    """

    title: str
    subtitle: str
    matrix: pd.DataFrame
    low_resolution: pd.DataFrame | None = None
    n_eff: pd.DataFrame | None = None


@dataclass(frozen=True)
class Block:
    """The panels that share one feature axis, and the label naming why they do.

    docs/04 s2 defines the ecological features -- the threshold counts, the degree
    days, the spell length -- for `sea_water_temperature` alone, so a wave or met
    panel drawn against the union of every feature is around 45% hatching. A block
    draws each family against the axis its `feature_set` actually produces
    (`features.windowed.measured_columns`), so rows line up wherever a comparison
    is made -- between panels measuring the same family -- and the "not applicable,
    not zero" distinction is stated once in the label instead of being spelled out
    five empty rows at a time in every panel that cannot carry them.

    Alignment across *blocks* is given up deliberately, and it is the weaker of the
    two properties: two panels on different feature sets share no row below the six
    general statistics, so lining them up buys a reader nothing they can read
    across. Within a block the union axis still holds and still does its original
    job -- a sparse panel keeps the rows it has no values for, hatched, because a
    parameter can decline a feature its own family defines.
    """

    label: str
    features: list[str]
    panels: list[Panel]


def plot_screen(
    blocks: list[Block],
    *,
    lags: list[int],
    title: str,
    caption: str,
    limit: float,
    ncols: int = 3,
    dpi: int = 200,
) -> mpl.figure.Figure:
    """A stack of blocks of lag x feature matrices, all on one colour scale.

    `limit` sets the symmetric colour scale for *every* panel in *every* block, and
    is meant to be shared across a whole set of figures: a per-panel or per-figure
    scale would repaint a weak bed to look exactly like a strong one.
    """
    drawn = [block for block in blocks if block.panels]
    if not drawn:
        raise ValueError("no panels to draw")
    colormap = diverging_colormap()
    norm = mpl.colors.Normalize(vmin=-limit, vmax=limit)
    ncols = min(ncols, max(len(block.panels) for block in drawn))

    # Room for the longest feature label in the figure, taken once and applied to
    # every column, so a temperature block and a met block start their grids at the
    # same x and a cell keeps its width down the page.
    longest = max(len(feature) for block in drawn for feature in block.features)
    label_w = 0.0075 * LABEL_PT * longest + 0.16
    panel_w = len(lags) * CELL_W
    column_w = label_w + panel_w + COLUMN_GAP

    fig_w = LEFT_W + ncols * column_w - COLUMN_GAP + RIGHT_W
    fig_h = TOP_H + BOTTOM_H + sum(_block_height(block, ncols) for block in drawn)
    fig = plt.figure(figsize=(fig_w, fig_h), dpi=dpi, facecolor=SURFACE)

    # Inches from the bottom, walking down the page: the reading order, and the
    # only order in which "this block starts here" is a statement about a page
    # whose height nothing has yet decided.
    cursor = fig_h - TOP_H
    for block in drawn:
        cursor -= BLOCK_LABEL_H
        _draw_block_label(fig, block, cursor / fig_h)
        rows = [block.panels[start : start + ncols] for start in range(0, len(block.panels), ncols)]
        grid_h = len(block.features) * CELL_H
        for row in rows:
            cursor -= TITLE_H + grid_h
            for column, panel in enumerate(row):
                left = LEFT_W + column * column_w + label_w
                ax = fig.add_axes((left / fig_w, cursor / fig_h, panel_w / fig_w, grid_h / fig_h))
                _draw_panel(ax, panel, block.features, lags, colormap, norm)

    fig.suptitle(
        title, x=0.008, y=1 - 0.34 / fig_h, ha="left", fontsize=15, color=INK, weight="bold"
    )
    _draw_key(fig, colormap, norm, limit, fig_h)
    fig.text(0.008, 0.14 / fig_h, caption, ha="left", va="bottom", fontsize=7.5, color=SECONDARY)
    return fig


def _block_height(block: Block, ncols: int) -> float:
    """Inches: the block's rule and label, then a title and a grid per row."""
    rows = -(-len(block.panels) // ncols)
    return BLOCK_LABEL_H + rows * (TITLE_H + len(block.features) * CELL_H)


def _draw_block_label(fig, block: Block, y: float) -> None:
    """The rule and label heading one block.

    The rule runs the full width rather than sitting over the first panel: it is a
    statement about every panel below it -- that these are the features this family
    of parameters produces -- and a short rule would read as a heading for one.
    """
    fig.add_artist(Line2D([0.008, 0.992], [y, y], color=GRIDLINE, linewidth=0.9, zorder=0))
    fig.text(
        0.008,
        y + 4 / 72 / fig.get_figheight(),
        block.label,
        ha="left",
        va="bottom",
        fontsize=8.5,
        color=SECONDARY,
        weight="bold",
    )


def _draw_panel(ax, panel: Panel, features, lags, colormap, norm) -> None:
    ax.set_facecolor(SURFACE)
    ax.set_xlim(0, len(lags))
    ax.set_ylim(len(features), 0)
    ax.set_xticks([index + 0.5 for index in range(len(lags))], [str(lag) for lag in lags])
    ax.set_yticks([index + 0.5 for index in range(len(features))], features)
    ax.tick_params(length=0, labelsize=7.5, colors=SECONDARY)
    ax.xaxis.set_ticks_position("top")
    ax.xaxis.set_label_position("top")
    ax.set_xlabel("lag (quarters)", fontsize=7, color=MUTED, labelpad=3)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_title(
        f"{panel.title}\n{panel.subtitle}",
        fontsize=8.5,
        color=INK,
        loc="left",
        pad=TITLE_PAD,
        linespacing=1.6,
    )

    for row, feature in enumerate(features):
        for column, lag in enumerate(lags):
            value = _at(panel.matrix, feature, lag)
            corner = (column + 0.03, row + 0.05)
            if value is None or pd.isna(value):
                ax.add_patch(
                    Rectangle(
                        corner,
                        0.94,
                        0.90,
                        facecolor=SURFACE,
                        edgecolor=GRIDLINE,
                        hatch="////",
                        linewidth=0.6,
                    )
                )
                continue
            colour = colormap(norm(value))
            flagged = bool(_at(panel.low_resolution, feature, lag))
            ax.add_patch(
                Rectangle(
                    corner,
                    0.94,
                    0.90,
                    facecolor=colour,
                    edgecolor=INK if flagged else SURFACE,
                    linewidth=1.0 if flagged else 0.8,
                    linestyle=(0, (1.6, 1.4)) if flagged else "solid",
                )
            )
            ink = _ink_for(colour)
            ax.text(
                column + 0.5,
                row + 0.40,
                f"{value:+.2f}".replace("+0.", "+.").replace("-0.", "-."),
                ha="center",
                va="center",
                fontsize=7.2,
                color=ink,
            )
            effective = _at(panel.n_eff, feature, lag)
            if effective is not None and not pd.isna(effective):
                ax.text(
                    column + 0.5,
                    row + 0.71,
                    f"{effective:.0f}",
                    ha="center",
                    va="center",
                    fontsize=5.4,
                    color=ink,
                    alpha=0.7,
                )


def _at(frame: pd.DataFrame | None, row, column):
    """One cell, or None where the frame has no such row, no such column, or is absent."""
    if frame is None or row not in frame.index or column not in frame.columns:
        return None
    return frame.loc[row, column]


def _draw_key(fig, colormap, norm, limit: float, fig_h: float) -> None:
    """The colourbar, and the two marks that are not coefficients.

    Placed in inches off the bottom rather than at a figure fraction, so the key
    sits the same distance from the caption whether the figure is three rows tall
    or seven. A fraction would slide it up the page as blocks are added.
    """
    bar = fig.add_axes((0.008, 0.72 / fig_h, 0.26, 0.19 / fig_h))
    colorbar = fig.colorbar(
        mpl.cm.ScalarMappable(norm=norm, cmap=colormap), cax=bar, orientation="horizontal"
    )
    colorbar.outline.set_visible(False)
    bar.set_xticks([-limit, 0, limit], [f"-{limit:g}", "0", f"+{limit:g}"])
    bar.tick_params(length=0, labelsize=7, colors=SECONDARY, pad=2)
    bar.set_title(
        "Pearson r  -  kelp area anomaly vs feature anomaly"
        "      (smaller figure in each cell: n_eff)",
        fontsize=7.5,
        color=SECONDARY,
        loc="left",
        pad=4,
    )
    fig.legend(
        handles=[
            Patch(facecolor=SURFACE, edgecolor=GRIDLINE, hatch="////", label="not applicable"),
            Patch(
                facecolor=NEUTRAL,
                edgecolor=INK,
                linestyle=(0, (1.6, 1.4)),
                label="low resolution - not to be read alone",
            ),
        ],
        loc="lower right",
        bbox_to_anchor=(0.992, 0.45 / fig_h),
        ncols=2,
        frameon=False,
        fontsize=7.5,
        labelcolor=SECONDARY,
        handlelength=1.7,
        handleheight=1.2,
    )


# --- the ranked signal plot ----------------------------------------------------
#
# The screen's own output is a ranking, and until now nothing drew it. The grid
# above answers "what did every cell come out at"; this answers "which of them is
# worth a second look" -- the question docs/04 s4.1 exists to answer, and the one
# a 2,145-cell grid answers worst.

ROW_H = 0.30  #: one signal row
RANK_AXIS_W = 4.40  #: the signed r axis
SPARK_W = 0.95  #: the lag profile beside each row
NEFF_W = 0.66  #: the effective sample size, printed as well as sized
GUTTER = 0.26
LABEL_PT_RANK = 8.0
DETAIL_PT = 7.2
DOT_MIN = 14.0  #: point^2, at the smallest n_eff in the figure
DOT_MAX = 108.0


@dataclass(frozen=True)
class SignalRow:
    """One row of the ranked plot, and everything that qualifies it.

    `r` places the dot and `n_eff` sizes it -- the two numbers the grid crowds into
    one cell, separated onto position and area so the ranking reads down the page
    without anyone reading a number.

    `members` are the r of the other eligible cells this signal merged, drawn as
    ticks on the same row. docs/04 s5 collapses cells agreeing on polygon, feature
    and lag into one claim measured more than once; drawn, that claim can be
    checked against how closely they actually agree rather than taken on trust --
    which is what `notebooks/README.md` asks a reader to do by hand for the La
    Jolla signal, and what nothing in the repo drew.

    `profile` is r at every lag for the same series and feature, `profile_at` this
    row's lag. A signal flat across all five lags is one whose lag won a lottery
    rather than found a peak, and no ranking can tell those apart by itself.
    """

    label: str
    detail: str
    r: float
    n_eff: float
    members: tuple[float, ...] = ()
    profile: tuple[float, ...] = ()
    profile_at: int | None = None
    role: str = "predictor"
    registered: bool = False


def _text_width(text: str, size: float, weight: str = "normal") -> float:
    """Inches this string occupies, measured rather than estimated per character.

    A per-character estimate is what put `degree_days_above_18c` through the column
    beside it: proportional glyphs, and a bold row is wider than the same string
    unbold. `TextPath` reports the real advance width without needing a renderer,
    so the column can be sized before the figure it belongs to exists.
    """
    path = TextPath((0, 0), text, size=size, prop=FontProperties(size=size, weight=weight))
    return float(path.get_extents().width) / 72.0


def plot_ranked(
    rows: list[SignalRow],
    excluded: list[SignalRow] | None = None,
    *,
    title: str,
    caption: str,
    limit: float,
    subtitle: str = "",
    excluded_label: str = "withheld by the gate -- shown to be audited, not ranked",
    dpi: int = 200,
) -> mpl.figure.Figure:
    """Signals down the page, |r| descending, controls interleaved and marked.

    `excluded` rows are drawn under their own rule, below everything ranked and
    never among it. They are the cells the candidate gate withheld: worth seeing,
    so the gate can be audited, and not worth ranking -- ranking a withheld cell is
    the exact invitation the gate exists to remove.
    """
    excluded = list(excluded or [])
    if not rows and not excluded:
        raise ValueError("no rows to draw")

    every = [*rows, *excluded]
    label_w = (
        max(_text_width(_label_of(row), LABEL_PT_RANK, _weight_of(row)) for row in every) + 0.22
    )
    detail_w = max(_text_width(row.detail, DETAIL_PT) for row in every) + 0.26

    gap_rows = BLOCK_LABEL_H / ROW_H if excluded else 0.0
    total_rows = len(rows) + len(excluded) + gap_rows

    fig_w = LEFT_W + label_w + detail_w + RANK_AXIS_W + GUTTER + NEFF_W + SPARK_W + RIGHT_W
    body_h = total_rows * ROW_H
    head_h = TOP_H + (0.28 if subtitle else 0.0) + 0.44
    fig_h = head_h + body_h + BOTTOM_H
    fig = plt.figure(figsize=(fig_w, fig_h), dpi=dpi, facecolor=SURFACE)

    axis_left = LEFT_W + label_w + detail_w
    top = fig_h - head_h

    dots = fig.add_axes(
        (axis_left / fig_w, (top - body_h) / fig_h, RANK_AXIS_W / fig_w, body_h / fig_h)
    )
    _prepare_rank_axis(dots, limit, total_rows)

    # Headers for the two columns that are numbers rather than an axis. The key
    # below says what they mean; these say which is which without a trip to it.
    neff_x = axis_left + RANK_AXIS_W + GUTTER + NEFF_W
    for x, text, align in (
        (neff_x, "n_eff", "right"),
        (neff_x + 0.08 + (SPARK_W - 0.14) / 2, "lags 0-4", "center"),
    ):
        fig.text(
            x / fig_w,
            (top + 0.10) / fig_h,
            text,
            ha=align,
            va="bottom",
            fontsize=7.5,
            color=SECONDARY,
        )

    sizes = _dot_sizes([row.n_eff for row in every])
    for index, row in enumerate(every):
        # One row-unit coordinate for both halves of a row. Keeping the gap in
        # inches for the labels and in rows for the dots is what slid every
        # withheld row off its own dot the first time this was drawn.
        centre = index + 0.5 + (gap_rows if index >= len(rows) else 0.0)
        _draw_rank_row(
            fig,
            dots,
            row,
            centre=centre,
            size=sizes[index],
            limit=limit,
            withheld=index >= len(rows),
            geometry=(fig_w, fig_h, top, axis_left, label_w),
        )

    if excluded:
        y = (top - (len(rows) + gap_rows / 2) * ROW_H) / fig_h
        fig.add_artist(
            Line2D([LEFT_W / fig_w, 1 - RIGHT_W / fig_w], [y, y], color=GRIDLINE, linewidth=0.9)
        )
        fig.text(
            LEFT_W / fig_w,
            y + 4 / 72 / fig_h,
            excluded_label,
            ha="left",
            va="bottom",
            fontsize=7.5,
            color=MUTED,
            weight="bold",
        )

    fig.suptitle(
        title, x=0.008, y=1 - 0.34 / fig_h, ha="left", fontsize=15, color=INK, weight="bold"
    )
    if subtitle:
        fig.text(
            0.008, 1 - 0.64 / fig_h, subtitle, ha="left", va="top", fontsize=9, color=SECONDARY
        )
    _draw_rank_key(fig, fig_h)
    fig.text(0.008, 0.14 / fig_h, caption, ha="left", va="bottom", fontsize=7.5, color=SECONDARY)
    return fig


def _label_of(row: SignalRow) -> str:
    return f"* {row.label}" if row.registered else row.label


def _weight_of(row: SignalRow) -> str:
    return "bold" if row.registered else "normal"


def _dot_sizes(n_eff: list[float]) -> list[float]:
    """Area proportional to `n_eff`, over the range this figure actually spans.

    Area rather than radius, because area is what a reader compares. Over the
    figure's own range rather than an absolute one, because the question is which
    of *these* coefficients rests on more evidence: a fixed scale would put a pool
    running from 30 to 160 into one indistinguishable size.
    """
    low, high = min(n_eff), max(n_eff)
    if high - low < 1e-9:
        return [0.5 * (DOT_MIN + DOT_MAX)] * len(n_eff)
    return [DOT_MIN + (DOT_MAX - DOT_MIN) * (value - low) / (high - low) for value in n_eff]


def _prepare_rank_axis(ax, limit: float, total_rows: float) -> None:
    ax.set_facecolor(SURFACE)
    ax.set_xlim(-limit, limit)
    ax.set_ylim(total_rows, 0)
    ax.set_yticks([])
    ticks = [-limit, -limit / 2, 0, limit / 2, limit]
    ax.set_xticks(ticks, ["0" if value == 0 else f"{value:+.2f}" for value in ticks])
    ax.xaxis.set_ticks_position("top")
    ax.tick_params(length=0, labelsize=7.5, colors=SECONDARY, pad=3)
    for spine in ax.spines.values():
        spine.set_visible(False)
    for value in ticks:
        ax.axvline(
            value,
            color=INK if value == 0 else GRIDLINE,
            linewidth=0.9 if value == 0 else 0.6,
            zorder=0,
        )


def _draw_rank_row(fig, dots, row: SignalRow, *, centre, size, limit, withheld, geometry) -> None:
    """One row: its two labels, the cells it merged, its dot, its n_eff, its profile."""
    fig_w, fig_h, top, axis_left, label_w = geometry
    ink = MUTED if withheld else (SECONDARY if row.role == "control" else INK)

    # Members first, so the dot standing for the signal is drawn over them.
    if row.members:
        reach = (*row.members, row.r)
        dots.plot(
            [max(min(reach), -limit), min(max(reach), limit)],
            [centre, centre],
            color=ink,
            linewidth=0.8,
            alpha=0.35,
            zorder=1,
        )
    for member in row.members:
        if abs(member) <= limit:
            dots.plot(
                [member],
                [centre],
                marker="|",
                markersize=7,
                markeredgewidth=1.1,
                color=ink,
                alpha=0.6,
                zorder=2,
            )

    # A coefficient past the end of the scale is drawn as a caret at the edge, not
    # dropped. The strongest |r| in this screen belongs to a withheld cell resting
    # on an n_eff of 9, and a scale that silently swallowed it would hide the one
    # row that shows what the gate is for.
    if abs(row.r) > limit:
        dots.plot(
            [limit * 0.985 * np.sign(row.r)],
            [centre],
            marker=">" if row.r > 0 else "<",
            markersize=6.5,
            color=ink,
            zorder=3,
        )
    else:
        dots.scatter(
            [row.r],
            [centre],
            s=size,
            facecolor=SURFACE if withheld else ink,
            edgecolor=ink,
            linewidth=1.0,
            zorder=3,
        )

    baseline = (top - centre * ROW_H) / fig_h
    fig.text(
        LEFT_W / fig_w,
        baseline,
        _label_of(row),
        ha="left",
        va="center",
        fontsize=LABEL_PT_RANK,
        color=ink,
        weight=_weight_of(row),
    )
    fig.text(
        (LEFT_W + label_w) / fig_w,
        baseline,
        row.detail,
        ha="left",
        va="center",
        fontsize=DETAIL_PT,
        color=MUTED,
    )
    reading = f"{row.r:+.2f}" if abs(row.r) > limit else f"{row.n_eff:.0f}"
    fig.text(
        (axis_left + RANK_AXIS_W + GUTTER + NEFF_W) / fig_w,
        baseline,
        reading,
        ha="right",
        va="center",
        fontsize=DETAIL_PT,
        color=SECONDARY,
    )
    if len(row.profile) > 1:
        _draw_sparkline(fig, row, fig_w, fig_h, axis_left, baseline, ink)


def _draw_sparkline(fig, row: SignalRow, fig_w, fig_h, axis_left, baseline, ink) -> None:
    """r across every lag, on the profile's own scale, with this row's lag marked.

    Scaled to its own extent rather than to the figure's `limit`: a sparkline
    answers a question about shape -- peak or plateau -- and a shared scale would
    flatten every profile on a weak series into the same straight line and answer
    it wrongly.
    """
    left = axis_left + RANK_AXIS_W + GUTTER + NEFF_W + 0.08
    height = ROW_H * 0.62
    ax = fig.add_axes(
        (left / fig_w, baseline - height / 2 / fig_h, (SPARK_W - 0.14) / fig_w, height / fig_h)
    )
    values = [value for value in row.profile if not pd.isna(value)]
    span = max(abs(min(values)), abs(max(values))) if values else 1.0
    ax.set_facecolor(SURFACE)
    ax.set_ylim(-span * 1.3, span * 1.3)
    ax.set_xlim(-0.4, len(row.profile) - 0.6)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.axhline(0, color=GRIDLINE, linewidth=0.6)
    ax.plot(range(len(row.profile)), row.profile, color=ink, linewidth=1.0, alpha=0.85)
    if row.profile_at is not None:
        ax.plot(
            [row.profile_at],
            [row.profile[row.profile_at]],
            marker="o",
            markersize=3.0,
            color=ink,
        )


def _draw_rank_key(fig, fig_h: float) -> None:
    """What the dot, the tick, the caret and the star mean."""
    for offset, text in (
        (
            0.86,
            (
                "dot: Pearson r of the cell standing for the signal, sized by n_eff   |   "
                "tick: an eligible cell the signal merged   |   * pre-registered"
            ),
        ),
        (
            0.62,
            (
                "grey rows are docs/04 s5 controls, screened on the same gate and never "
                "registered   |   a caret is an r past the end of the scale, printed "
                "instead of n_eff   |   right-hand column: n_eff, then r across lags 0-4, "
                "each sparkline on its own scale -- read them for shape, not amplitude"
            ),
        ),
    ):
        fig.text(0.008, offset / fig_h, text, ha="left", va="bottom", fontsize=7.5, color=SECONDARY)


# --- the signal diagnostics ----------------------------------------------------
#
# Everything above draws coefficients. Nothing in this repo drew an observation,
# and r hides the two failure modes docs/01 s7 names for a short autocorrelated
# series identically: one event carrying the whole association, and one quarter of
# leverage carrying it. A time series answers the first and a scatter the second.

PAIR_SERIES_W = 5.20
PAIR_SCATTER_W = 2.60
PAIR_H = 1.50
PAIR_LABEL_W = 0.52  #: numeric y tick labels
PAIR_TITLE_H = 0.58  #: the two-line panel title above each row
PAIR_XTICK_H = 0.32  #: the year axis below it, which the next title must clear
PAIR_GAP = 0.62

#: The feature line and the year ramp. Kelp stays on the primary ink so the two
#: are told apart by lightness as well as hue.
FEATURE_INK = BLUE_ARM[3]


@dataclass(frozen=True)
class Pairing:
    """The quarters behind one coefficient: kelp at t, and the feature at t - lag.

    The arrays are the pairs the screen actually correlated, in quarter order, so a
    figure drawn from them cannot describe a different selection than the number it
    is captioned with.

    Both series are drawn standardised, each by its own standard deviation, rather
    than on twin axes. Pearson r is invariant under that rescaling -- it is exactly
    what r sees -- whereas two independently scaled axes can be slid until any two
    series appear to track, which is a way of drawing a correlation rather than
    showing one.
    """

    title: str
    subtitle: str
    year: tuple[int, ...]
    quarter: tuple[int, ...]
    kelp: tuple[float, ...]
    feature: tuple[float, ...]
    r: float
    n_eff: float
    role: str = "predictor"
    kelp_label: str = "kelp area anomaly"
    feature_label: str = "feature anomaly"


def plot_signals(
    pairings: list[Pairing],
    *,
    title: str,
    caption: str,
    subtitle: str = "",
    shade: tuple[int, int] | None = None,
    shade_label: str = "",
    dpi: int = 200,
) -> mpl.figure.Figure:
    """One row per signal: the paired quarters over time, and against each other.

    `shade` marks a period as context only -- docs/04 s4.2 owns event studies, and
    a composite drawn here would be that rung's work done on this rung's page.
    """
    if not pairings:
        raise ValueError("no pairings to draw")

    fig_w = (
        LEFT_W + PAIR_LABEL_W + PAIR_SERIES_W + PAIR_GAP + PAIR_LABEL_W + PAIR_SCATTER_W + RIGHT_W
    )
    head_h = TOP_H + (0.28 if subtitle else 0.0) + 0.20
    fig_h = head_h + len(pairings) * (PAIR_TITLE_H + PAIR_H + PAIR_XTICK_H) + BOTTOM_H
    fig = plt.figure(figsize=(fig_w, fig_h), dpi=dpi, facecolor=SURFACE)

    cursor = fig_h - head_h
    for pairing in pairings:
        cursor -= PAIR_TITLE_H + PAIR_H + PAIR_XTICK_H
        series = fig.add_axes(
            (
                (LEFT_W + PAIR_LABEL_W) / fig_w,
                (cursor + PAIR_XTICK_H) / fig_h,
                PAIR_SERIES_W / fig_w,
                PAIR_H / fig_h,
            )
        )
        scatter = fig.add_axes(
            (
                (LEFT_W + PAIR_LABEL_W + PAIR_SERIES_W + PAIR_GAP + PAIR_LABEL_W) / fig_w,
                (cursor + PAIR_XTICK_H) / fig_h,
                PAIR_SCATTER_W / fig_w,
                PAIR_H / fig_h,
            )
        )
        _draw_pair_series(series, pairing, shade, shade_label)
        _draw_pair_scatter(scatter, pairing)

    fig.suptitle(
        title, x=0.008, y=1 - 0.34 / fig_h, ha="left", fontsize=15, color=INK, weight="bold"
    )
    if subtitle:
        fig.text(
            0.008, 1 - 0.64 / fig_h, subtitle, ha="left", va="top", fontsize=9, color=SECONDARY
        )
    _draw_pair_key(fig, fig_h, shade_label)
    fig.text(0.008, 0.14 / fig_h, caption, ha="left", va="bottom", fontsize=7.5, color=SECONDARY)
    return fig


def _broken(when: np.ndarray, values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The same series with a break inserted wherever the record skips a quarter.

    Every observation survives; only the segment spanning the hole is removed. The
    obvious alternative -- nulling the point after a gap -- silently deletes a
    quarter the coefficient was computed on, which makes the figure describe a
    smaller record than its own caption.
    """
    x: list[float] = []
    y: list[float] = []
    for index, moment in enumerate(when):
        if index and moment - when[index - 1] > 0.30:
            x.append(np.nan)
            y.append(np.nan)
        x.append(float(moment))
        y.append(float(values[index]))
    return np.asarray(x), np.asarray(y)


def _standardised(values: tuple[float, ...]) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    spread = float(array.std(ddof=1))
    return (array - array.mean()) / spread if spread > 0 else array - array.mean()


def _compact(value: float, _position=None) -> str:
    """A tick a reader can hold: 100k rather than 100000.

    Kelp area anomalies run to six figures in m^2, and a column of those is a
    column nobody reads. The unit itself stays with the caller, which is what
    keeps this module from knowing what it is drawing.
    """
    for divisor, suffix in ((1e6, "M"), (1e3, "k")):
        if abs(value) >= divisor:
            return f"{value / divisor:g}{suffix}"
    return f"{value:g}"


def _bare(ax) -> None:
    ax.set_facecolor(SURFACE)
    ax.tick_params(length=0, labelsize=7, colors=SECONDARY, pad=2)
    for side, spine in ax.spines.items():
        spine.set_visible(side in {"left", "bottom"})
        spine.set_color(GRIDLINE)
        spine.set_linewidth(0.8)


def _draw_pair_series(ax, pairing: Pairing, shade, shade_label: str) -> None:
    """Both halves of the pair down the record, standardised, gaps left as gaps."""
    _bare(ax)
    ink = SECONDARY if pairing.role == "control" else INK
    when = np.asarray(pairing.year, dtype=float) + (np.asarray(pairing.quarter) - 1) / 4

    if shade:
        ax.axvspan(shade[0], shade[1] + 1, color=NEUTRAL, zorder=0, label=shade_label)
    ax.axhline(0, color=GRIDLINE, linewidth=0.8, zorder=1)
    # A quarter the pair does not carry is a hole, and a line drawn straight across
    # one asserts a value nothing measured -- the same reason docs/04 s2 breaks a
    # threshold spell at a gap rather than bridging it. The break goes *between*
    # the two observations either side, never on one of them: blanking the point
    # would drop a quarter the screen counted.
    kelp_x, kelp_y = _broken(when, _standardised(pairing.kelp))
    feature_x, feature_y = _broken(when, _standardised(pairing.feature))
    ax.plot(kelp_x, kelp_y, color=ink, linewidth=1.2, zorder=3, label="kelp area anomaly")
    ax.plot(
        feature_x,
        feature_y,
        color=FEATURE_INK,
        linewidth=1.2,
        linestyle=(0, (3.2, 1.6)),
        zorder=2,
        label="feature anomaly, lagged",
    )
    # Whole years. A short record left to the default locator gets 2.5-year
    # ticks, and a quarter is not a fraction of a year anyone reads.
    ax.xaxis.set_major_locator(MaxNLocator(integer=True, nbins=8))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:.0f}"))
    ax.set_ylabel("SD", fontsize=7, color=MUTED, labelpad=2)
    ax.set_title(
        f"{pairing.title}\n{pairing.subtitle}",
        fontsize=8.5,
        color=ink,
        loc="left",
        pad=10,
        linespacing=1.6,
    )


def _draw_pair_scatter(ax, pairing: Pairing) -> None:
    """The pairs themselves, coloured by year -- what the coefficient is made of.

    No fitted line and no band. docs/04 s4.1 is a screen, and a line through these
    points is the thing s4.3 is for; drawn here it would read as the model rather
    than as the data the model has not yet been fitted to.
    """
    _bare(ax)
    ramp = LinearSegmentedColormap.from_list("kelpcompare_years", BLUE_ARM)
    years = np.asarray(pairing.year, dtype=float)
    ax.axhline(0, color=GRIDLINE, linewidth=0.8, zorder=0)
    ax.axvline(0, color=GRIDLINE, linewidth=0.8, zorder=0)
    ax.scatter(
        pairing.feature,
        pairing.kelp,
        c=years,
        cmap=ramp,
        s=13,
        linewidth=0.4,
        edgecolor=SURFACE,
        zorder=2,
    )
    ax.xaxis.set_major_formatter(FuncFormatter(_compact))
    ax.yaxis.set_major_formatter(FuncFormatter(_compact))
    ax.set_xlabel(pairing.feature_label, fontsize=7, color=MUTED, labelpad=2)
    ax.set_ylabel(pairing.kelp_label, fontsize=7, color=MUTED, labelpad=2)
    ax.set_title(
        f"r = {pairing.r:+.3f}   n = {len(pairing.kelp)}   n_eff = {pairing.n_eff:.0f}",
        fontsize=8,
        color=SECONDARY,
        loc="left",
        pad=10,
    )


def _draw_pair_key(fig, fig_h: float, shade_label: str) -> None:
    lines = [
        (
            "solid: kelp area anomaly at quarter t   |   dashed: the feature anomaly at "
            "t - lag   |   both standardised, each by its own SD, which is what r sees"
        ),
        (
            "scatter coloured by year, light to dark   |   no fitted line: docs/04 s4.1 "
            "screens and does not model"
        ),
    ]
    if shade_label:
        lines.append(f"shaded: {shade_label} -- context only; docs/04 s4.2 owns event studies")
    for index, text in enumerate(lines):
        fig.text(
            0.008,
            (0.86 - 0.24 * index) / fig_h,
            text,
            ha="left",
            va="bottom",
            fontsize=7.5,
            color=SECONDARY,
        )


# ---------------------------------------------------------------------------
# Series over a window: the deployment profile (docs/03 `deployment_daily`)
# ---------------------------------------------------------------------------

SERIES_H = 2.40  #: the plotting area of one bands figure
SERIES_W = 9.20
SERIES_TOP_H = 0.86  #: title, and the subtitle under it
SERIES_BOTTOM_H = 1.00  #: the axis label, the legend and the caption
SERIES_LABEL_PT = 8.0


@dataclass(frozen=True)
class Band:
    """One series drawn as a line, optionally inside a shaded span.

    `centre` is the line and `low`/`high` the span around it -- a daily mean
    inside its daily minimum and maximum, say. The span is the reason this exists
    rather than a bare line plot: a deployment's daily mean alone hides the thing
    the record is interesting for, which is how far the water moved within each
    day.

    `low` and `high` are drawn and never computed. A renderer that derived a band
    from the centre would be inventing a spread, which is the line hard rule 6
    draws around this module.
    """

    label: str
    x: Sequence
    centre: Sequence[float]
    low: Sequence[float] | None = None
    high: Sequence[float] | None = None


def plot_bands(
    bands: list[Band],
    *,
    title: str,
    caption: str,
    ylabel: str,
    subtitle: str = "",
    gap: object | None = None,
    baseline: float | None = None,
    baseline_label: str = "",
    dpi: int = 200,
) -> mpl.figure.Figure:
    """Series over a shared x axis, each inside its own band.

    **Colour encodes the caller's order, not identity.** The bands take evenly
    spaced shades of `BLUE_ARM`, light to dark, so a caller that sorts by depth
    gets shallow-to-deep reading as pale-to-dark down the legend. That is a
    single-hue ramp rather than a categorical palette on purpose: the series this
    draws are the same quantity at different places on one ordered axis, and a
    categorical palette would suggest they are unlike each other in kind.

    **`gap` breaks the line rather than bridging it.** Where two consecutive `x`
    are further apart than `gap`, the line is cut. Drawing straight through would
    assert a value for the interval between them, and for a daily series the
    interval between two rows is exactly a day nobody measured -- the same
    reading `windowed._longest_spell` gives a gap, applied to a line. Left None,
    nothing is cut, which is right for a series with no notion of a regular step
    (an hour-of-day composite, for instance) and wrong for a dated one.

    Raises rather than drawing an empty axes: a figure of record with no series
    on it is a caption over blank space.
    """
    if not bands:
        raise ValueError("no bands to draw")

    fig_h = SERIES_TOP_H + SERIES_H + SERIES_BOTTOM_H
    fig = plt.figure(figsize=(SERIES_W, fig_h), dpi=dpi, facecolor=SURFACE)
    axes = fig.add_axes(
        (
            0.62 / SERIES_W,
            SERIES_BOTTOM_H / fig_h,
            1 - (0.62 + RIGHT_W) / SERIES_W,
            SERIES_H / fig_h,
        )
    )
    axes.set_facecolor(SURFACE)

    for index, band in enumerate(bands):
        colour = _band_colour(index, len(bands))
        x, centre, low, high = _cut(band, gap)
        if low is not None and high is not None:
            axes.fill_between(x, low, high, color=colour, alpha=0.16, linewidth=0)
        axes.plot(x, centre, color=colour, linewidth=1.4, label=band.label, solid_capstyle="round")

    if baseline is not None:
        axes.axhline(baseline, color=MUTED, linewidth=0.9, linestyle=(0, (2.4, 2.0)), zorder=0)
        if baseline_label:
            axes.annotate(
                baseline_label,
                xy=(1.0, baseline),
                xycoords=("axes fraction", "data"),
                xytext=(-2, 3),
                textcoords="offset points",
                ha="right",
                va="bottom",
                fontsize=7.0,
                color=MUTED,
            )

    axes.set_ylabel(ylabel, fontsize=SERIES_LABEL_PT, color=SECONDARY)
    axes.tick_params(length=0, labelsize=SERIES_LABEL_PT, colors=SECONDARY)
    axes.grid(True, color=GRIDLINE, linewidth=0.7, alpha=0.9)
    axes.set_axisbelow(True)
    for spine in axes.spines.values():
        spine.set_visible(False)

    legend = axes.legend(
        loc="upper left",
        bbox_to_anchor=(0.0, -0.14),
        ncols=min(len(bands), 4),
        frameon=False,
        fontsize=SERIES_LABEL_PT,
        handlelength=1.6,
        columnspacing=1.6,
    )
    for text in legend.get_texts():
        text.set_color(SECONDARY)

    fig.suptitle(
        title, x=0.008, y=1 - 0.34 / fig_h, ha="left", fontsize=15, color=INK, weight="bold"
    )
    if subtitle:
        fig.text(
            0.008, 1 - 0.62 / fig_h, subtitle, ha="left", va="top", fontsize=9, color=SECONDARY
        )
    fig.text(0.008, 0.13 / fig_h, caption, ha="left", va="bottom", fontsize=7.5, color=SECONDARY)
    return fig


def _band_colour(index: int, total: int) -> str:
    """Evenly spaced shades of the blue arm, light to dark, over however many
    bands there are. Two bands take the ends rather than two adjacent steps, so
    the commonest case is also the most legible one."""
    if total <= 1:
        return BLUE_ARM[3]
    step = (len(BLUE_ARM) - 1) / (total - 1)
    return BLUE_ARM[round(index * step)]


def _cut(band: Band, gap: object | None):
    """The band's arrays with a null inserted wherever `x` jumps further than
    `gap`, which is how matplotlib is asked to lift the pen."""
    x = list(band.x)
    centre = list(band.centre)
    low = list(band.low) if band.low is not None else None
    high = list(band.high) if band.high is not None else None
    if gap is None or len(x) < 2:
        return x, centre, low, high

    out_x: list = []
    out = ([], [] if low is not None else None, [] if high is not None else None)
    for index, position in enumerate(x):
        if index and position - x[index - 1] > gap:
            out_x.append(position)
            for series in out:
                if series is not None:
                    series.append(float("nan"))
        out_x.append(position)
        out[0].append(centre[index])
        if low is not None:
            out[1].append(low[index])
        if high is not None:
            out[2].append(high[index])
    return out_x, out[0], out[1], out[2]
