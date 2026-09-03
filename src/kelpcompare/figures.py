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

from dataclasses import dataclass

import matplotlib as mpl
import numpy as np
import pandas as pd
from matplotlib import pyplot as plt
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Rectangle

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
