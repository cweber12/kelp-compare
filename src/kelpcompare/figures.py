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
"""

from __future__ import annotations

from dataclasses import dataclass

import matplotlib as mpl
import numpy as np
import pandas as pd
from matplotlib import pyplot as plt
from matplotlib.colors import LinearSegmentedColormap
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


# --- the figure ---------------------------------------------------------------


@dataclass(frozen=True)
class Panel:
    """One series' matrix, and the two flags that qualify how to read its cells.

    `matrix` is `feature x lag`, as `matrix()` in notebooks/01-lag-screen.ipynb
    returns it. `low_resolution` and `n_eff` are aligned frames of the same shape.
    A feature absent from `matrix` is *not applicable* to this parameter -- docs/04
    s2's feature sets are wide and sparse by design -- which the renderer draws as
    an empty hatched cell rather than as a coefficient of zero.
    """

    title: str
    subtitle: str
    matrix: pd.DataFrame
    low_resolution: pd.DataFrame | None = None
    n_eff: pd.DataFrame | None = None


def plot_panels(
    panels: list[Panel],
    *,
    features: list[str],
    lags: list[int],
    title: str,
    caption: str,
    limit: float,
    ncols: int = 3,
    dpi: int = 200,
) -> mpl.figure.Figure:
    """A panel grid of lag x feature matrices sharing one scale and one feature axis.

    `features` is the union axis every panel is drawn against, so rows line up
    across panels and a feature not applicable to a parameter reads as a hole
    rather than as a shorter axis. `limit` sets the symmetric colour scale for
    *every* panel and is meant to be shared across a whole set of figures: a
    per-panel scale would repaint a weak bed to look exactly like a strong one.
    """
    if not panels:
        raise ValueError("no panels to draw")
    colormap = diverging_colormap()
    norm = mpl.colors.Normalize(vmin=-limit, vmax=limit)

    ncols = min(ncols, len(panels))
    nrows = -(-len(panels) // ncols)
    fig_w = ncols * (len(lags) * 0.62 + 1.95) + 0.6
    fig_h = nrows * (len(features) * 0.36 + 1.25) + 1.5
    fig = plt.figure(figsize=(fig_w, fig_h), dpi=dpi, facecolor=SURFACE)
    axes = fig.subplots(nrows, ncols, squeeze=False)

    for index, ax in enumerate(axes.flat):
        if index >= len(panels):
            ax.set_visible(False)
            continue
        _draw_panel(ax, panels[index], features, lags, colormap, norm)

    fig.suptitle(title, x=0.008, y=0.988, ha="left", fontsize=15, color=INK, weight="bold")
    fig.tight_layout(rect=(0.0, 0.075, 1.0, 0.945))
    _draw_key(fig, colormap, norm, limit)
    fig.text(0.008, 0.008, caption, ha="left", va="bottom", fontsize=7.5, color=SECONDARY)
    return fig


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
        pad=24,
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


def _draw_key(fig, colormap, norm, limit: float) -> None:
    bar = fig.add_axes((0.008, 0.042, 0.26, 0.011))
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
        bbox_to_anchor=(0.992, 0.026),
        ncols=2,
        frameon=False,
        fontsize=7.5,
        labelcolor=SECONDARY,
        handlelength=1.7,
        handleheight=1.2,
    )
