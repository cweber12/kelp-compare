"""The renderer's two jobs: a ramp that reads the sign, and a hole that is not a zero."""

from __future__ import annotations

from itertools import pairwise

import matplotlib
import numpy as np
import pandas as pd
import pytest

matplotlib.use("Agg")

from matplotlib.colors import Normalize
from matplotlib.patches import Rectangle

from kelpcompare import figures

FEATURES = ["mean", "min", "max", "days_above_20c"]

#: The axis a `statistics` family draws against -- `days_above_20c` is docs/04 s2's
#: and belongs to `sea_water_temperature` alone, so a met block never carries it.
STATISTICS = ["mean", "min", "max"]
LAGS = [0, 1, 2, 3, 4]


def temperature_matrix() -> pd.DataFrame:
    """A full matrix -- every feature applies to this parameter."""
    values = np.linspace(-0.4, 0.4, len(FEATURES) * len(LAGS)).reshape(len(FEATURES), len(LAGS))
    return pd.DataFrame(values, index=FEATURES, columns=LAGS)


def wave_matrix() -> pd.DataFrame:
    """A sparse one -- `days_above_20c` is not defined for a wave parameter."""
    return temperature_matrix().drop(index=["days_above_20c"])


def panel_axes(fig) -> list:
    """The matrix axes, which the colourbar axes is not -- only a panel has a lag axis."""
    return [ax for ax in fig.axes if ax.get_visible() and ax.get_xlabel() == "lag (quarters)"]


def cells(fig) -> list[Rectangle]:
    return [patch for ax in panel_axes(fig) for patch in ax.patches if isinstance(patch, Rectangle)]


def render(panels, features=None, **overrides):
    """One block of panels -- the single-family case most of these tests are about."""
    return render_blocks([figures.Block("test", features or FEATURES, panels)], **overrides)


def render_blocks(blocks, **overrides):
    return figures.plot_screen(
        blocks,
        **{"lags": LAGS, "title": "test", "caption": "test", "limit": 0.5, **overrides},
    )


# --- the ramp ------------------------------------------------------------------


def test_the_two_arms_are_mirror_images_in_lightness():
    """The red arm is the blue arm re-hued, so their lightness profiles must agree.

    This is what stops the positive half of the scale reading as stronger than the
    negative half at equal |r| -- the failure a hand-picked red ramp invites.
    """
    blue = figures.arm_lightness(figures.BLUE_ARM)
    red = figures.arm_lightness(figures.red_arm())

    assert len(blue) == len(red)
    for blue_step, red_step in zip(blue, red, strict=True):
        assert blue_step == pytest.approx(red_step, abs=0.005)


def test_each_arm_darkens_monotonically():
    """Lightness monotonicity is the documented check for a diverging ramp."""
    for arm in (figures.BLUE_ARM, figures.red_arm()):
        lightness = figures.arm_lightness(arm)
        assert lightness == sorted(lightness, reverse=True)
        assert len(set(lightness)) == len(lightness)


def test_the_ramp_darkens_outward_from_the_midpoint_in_both_directions():
    colormap = figures.diverging_colormap()
    samples = [
        figures.arm_lightness((figures._rgb_to_hex(np.array(colormap(x)[:3])),))[0]
        for x in np.linspace(0, 1, 41)
    ]
    left, right = samples[:20][::-1], samples[21:]

    assert all(a >= b for a, b in pairwise(left))
    assert all(a >= b for a, b in pairwise(right))


def test_zero_lands_on_the_neutral_gray_rather_than_a_hue():
    """A hue at the midpoint would make "no association" read as a weak one."""
    colormap = figures.diverging_colormap()
    midpoint = colormap(Normalize(-0.5, 0.5)(0.0))

    _, a, b = figures._oklab(figures._rgb_to_hex(np.array(midpoint[:3])))
    assert float(np.hypot(a, b)) < 0.01


def test_the_poles_take_opposite_hues():
    colormap = figures.diverging_colormap()
    norm = Normalize(-0.5, 0.5)

    negative = figures._oklab(figures._rgb_to_hex(np.array(colormap(norm(-0.5))[:3])))
    positive = figures._oklab(figures._rgb_to_hex(np.array(colormap(norm(0.5))[:3])))
    assert negative[2] < 0 < positive[2]


def test_ink_flips_to_white_where_the_cell_is_too_dark_for_primary_ink():
    colormap = figures.diverging_colormap()
    norm = Normalize(-0.5, 0.5)

    assert figures._ink_for(colormap(norm(0.0))) == figures.INK
    assert figures._ink_for(colormap(norm(-0.5))) == figures.INK_ON_DARK
    assert figures._ink_for(colormap(norm(0.5))) == figures.INK_ON_DARK


# --- the hole ------------------------------------------------------------------


def test_a_feature_the_parameter_does_not_carry_is_hatched_not_coloured():
    """Not applicable and r = 0 must not render as the same cell.

    The neutral midpoint sits close to the chart surface, so an absent feature
    drawn as a coefficient of zero would be indistinguishable from a measured one
    -- and docs/04 s2's feature sets are sparse by design, so this is the common
    case, not the edge one.
    """
    fig = render([figures.Panel("46254", "wave_peak_period", wave_matrix())])

    hatched = [patch for patch in cells(fig) if patch.get_hatch()]
    assert len(hatched) == len(LAGS)
    surface = figures._hex_to_rgb(figures.SURFACE)
    for patch in hatched:
        assert np.allclose(patch.get_facecolor()[:3], surface, atol=0.004)


def test_every_panel_is_drawn_against_its_block_feature_axis():
    """Panels line up only if a sparse one keeps the rows it has no values for.

    This was `test_every_panel_is_drawn_against_the_full_feature_axis`, and the
    "full" axis meant the union of every feature in the figure. docs/04 s2 gives
    the ecological features to `sea_water_temperature` alone, so that union bought
    alignment between panels sharing no such row, and paid for it with five
    permanently empty rows in every wave and met panel -- around 45% of each. The
    block is the scope where the property is worth its cost; inside one, it is
    unchanged, and a parameter declining a feature its own family defines still
    keeps the row.
    """
    fig = render([figures.Panel("46254", "wave_peak_period", wave_matrix())])
    (ax,) = panel_axes(fig)

    assert [label.get_text() for label in ax.get_yticklabels()] == FEATURES


def test_a_block_is_not_padded_out_to_another_blocks_axis():
    """The split's whole point: each family draws against the axis it produces."""
    fig = render_blocks(
        [
            figures.Block(
                "sea_water_temperature",
                FEATURES,
                [figures.Panel("LJAC1", "sea_water_temperature", temperature_matrix())],
            ),
            figures.Block(
                "wave / met",
                STATISTICS,
                [figures.Panel("LJAC1", "air_temperature", wave_matrix())],
            ),
        ]
    )
    temperature, met = panel_axes(fig)

    assert [label.get_text() for label in temperature.get_yticklabels()] == FEATURES
    assert [label.get_text() for label in met.get_yticklabels()] == STATISTICS
    assert not [patch for patch in cells(fig) if patch.get_hatch()]


def test_each_block_is_labelled():
    """A block boundary carries the distinction the hatching used to carry, so it
    has to be drawn -- an unlabelled split is two axes and no reason given."""
    fig = render_blocks(
        [
            figures.Block(
                "sea_water_temperature",
                FEATURES,
                [figures.Panel("LJAC1", "sea_water_temperature", temperature_matrix())],
            ),
            figures.Block(
                "wave / met",
                STATISTICS,
                [figures.Panel("LJAC1", "air_temperature", wave_matrix())],
            ),
        ]
    )
    drawn = {text.get_text() for text in fig.texts}

    assert {"sea_water_temperature", "wave / met"} <= drawn


def test_a_low_resolution_cell_is_marked_without_changing_its_colour():
    """docs/04 s2 keeps these cells and says not to read them alone, so the mark
    has to qualify the coefficient rather than hide or recolour it."""
    flags = pd.DataFrame(False, index=FEATURES, columns=LAGS)
    flags.loc["min", 0] = True
    panel = figures.Panel("LJAC1", "sea_water_temperature", temperature_matrix(), flags)

    marked = render([panel])
    plain = render([figures.Panel("LJAC1", "sea_water_temperature", temperature_matrix())])

    dashed = [patch for patch in cells(marked) if patch.get_linestyle() not in ("solid", "-")]
    assert len(dashed) == 1
    assert [patch.get_facecolor() for patch in cells(marked)] == [
        patch.get_facecolor() for patch in cells(plain)
    ]


def test_the_scale_is_the_one_passed_not_one_fitted_per_panel():
    """A per-panel scale would repaint a weak bed to look exactly like a strong one."""
    weak = pd.DataFrame(0.05, index=FEATURES, columns=LAGS)
    strong = pd.DataFrame(0.45, index=FEATURES, columns=LAGS)

    fig = render(
        [
            figures.Panel("weak", "sea_water_temperature", weak),
            figures.Panel("strong", "sea_water_temperature", strong),
        ]
    )
    faces = {tuple(patch.get_facecolor()) for patch in cells(fig)}
    assert len(faces) == 2


def test_it_refuses_to_draw_nothing():
    with pytest.raises(ValueError, match="no panels"):
        render([])
    with pytest.raises(ValueError, match="no panels"):
        render_blocks([])


def test_a_family_nothing_measures_draws_no_labelled_rule_over_blank_space():
    """An empty block is a feature set with no series on it -- for a bed with no
    met station, say. It is dropped rather than drawn, so a rule and a label never
    head a band of nothing."""
    fig = render_blocks(
        [
            figures.Block(
                "sea_water_temperature",
                FEATURES,
                [figures.Panel("LJAC1", "sea_water_temperature", temperature_matrix())],
            ),
            figures.Block("wave / met", STATISTICS, []),
        ]
    )

    assert len(panel_axes(fig)) == 1
    assert "wave / met" not in {text.get_text() for text in fig.texts}
