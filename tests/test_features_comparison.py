"""The comparison table: kelp at *t* against the environment at *t − lag*.

The case this file exists for is the lag direction. Getting it backwards raises
nothing and loses nothing -- it produces a correlation matrix that reads as kelp
predicting temperature, which is a *result*, not an error, and the only place it
can be caught is here. So the direction is asserted on a hand-built pair where
the two readings give different answers.

Frames are constructed in the two quarterly shapes rather than built by the
stages above, so a change in either half's arithmetic cannot quietly change what
this file claims about the join.
"""

from __future__ import annotations

import pandas as pd

from kelpcompare.features.comparison import (
    COMPARISON_KEY,
    LAGS,
    build_comparison,
    comparison_columns,
)
from kelpcompare.polygons import Polygon, Polygons

KELP_ANOMALIES = ("kelp_area_m2_anom",)
ENV_ANOMALIES = ("mean_anom", "days_above_20c_anom")


def polygons(*records: Polygon) -> Polygons:
    """A registry with no geometry -- none of this stage reads any."""
    made = records or (
        Polygon(
            polygon_id="KELP:A",
            purpose="regional",
            site_ids=("NDBC:LJAC1",),
            source_file="kelp_a.csv",
        ),
    )
    return Polygons(path=None, polygons=made, frame=None)


def kelp(rows, polygon_id: str = "KELP:A") -> pd.DataFrame:
    """`(year, quarter, area_anom, usable)` in the `quarterly_kelp` shape."""
    return pd.DataFrame(
        [
            {
                "polygon_id": polygon_id,
                "year": year,
                "quarter": quarter,
                "usable": usable,
                "kelp_watch_revision": 23,
                "kelp_area_m2_anom": anomaly,
            }
            for year, quarter, anomaly, usable in rows
        ]
    )


def env(rows, *, site_id="NDBC:LJAC1", parameter="sea_water_temperature", depth_m=3.4):
    """`(year, quarter, mean_anom, usable)` in the `quarterly_env` shape."""
    return pd.DataFrame(
        [
            {
                "source": "ndbc",
                "site_id": site_id,
                "parameter": parameter,
                "depth_m": depth_m,
                "year": year,
                "quarter": quarter,
                "usable": usable,
                "mean_anom": anomaly,
                "days_above_20c_anom": None,
            }
            for year, quarter, anomaly, usable in rows
        ]
    )


def build(kelp_frame, env_frame, registry=None, **kwargs):
    return build_comparison(
        kelp_frame,
        env_frame,
        registry or polygons(),
        kelp_anomalies=KELP_ANOMALIES,
        env_anomalies=ENV_ANOMALIES,
        **kwargs,
    )


def at_lag(frame: pd.DataFrame, lag: int) -> pd.Series:
    match = frame.loc[frame["lag"] == lag]
    assert len(match) == 1, f"expected one lag-{lag} row, got {len(match)}"
    return match.iloc[0]


# --------------------------------------------------------------------------
# The lag direction -- the case this file exists for
# --------------------------------------------------------------------------


def test_the_environment_leads_and_kelp_responds():
    """Kelp in 2020Q3 at lag 2 is joined to the water in 2020Q1.

    The environmental series is built so the two readings differ: only 2020Q1
    carries a value, so a row that finds it proves the join reached backwards.
    Reversed, this row would have looked for 2021Q1 and found nothing.
    """
    built = build(
        kelp([(2020, 3, -5.0, True)]),
        env([(2020, 1, 2.5, True), (2021, 1, 99.0, True)]),
    )
    row = at_lag(built, 2)

    assert (row["year"], row["quarter"]) == (2020, 3)
    assert (row["env_year"], row["env_quarter"]) == (2020, 1)
    assert row["mean_anom"] == 2.5
    assert row["kelp_area_m2_anom"] == -5.0


def test_the_lagged_quarter_is_recorded_on_the_row():
    """So the lag is auditable by reading a row rather than by re-deriving it.

    The environmental series is far from the kelp quarter on purpose: every lag
    lands on a quarter with no environmental row, and the recorded pair still
    has to say which quarter was looked for.
    """
    built = build(kelp([(2020, 1, -5.0, True)]), env([(1999, 1, 1.0, True)]))
    lagged = {int(r.lag): (int(r.env_year), int(r.env_quarter)) for r in built.itertuples()}

    assert lagged == {
        0: (2020, 1),
        1: (2019, 4),
        2: (2019, 3),
        3: (2019, 2),
        4: (2019, 1),
    }


def test_every_lag_zero_to_four_gets_a_row():
    built = build(kelp([(2020, 1, -5.0, True)]), env([(2020, 1, 1.0, True)]))
    assert sorted(built["lag"]) == list(LAGS)


def test_lag_zero_is_the_same_quarter_on_both_sides():
    """Included on purpose: a same-quarter association is a hypothesis like any
    other, and leaving it out would make its absence look like a finding."""
    built = build(kelp([(2020, 1, -5.0, True)]), env([(2020, 1, 1.0, True)]))
    row = at_lag(built, 0)
    assert (row["env_year"], row["env_quarter"]) == (row["year"], row["quarter"])
    assert row["mean_anom"] == 1.0


# --------------------------------------------------------------------------
# What a row is, and which rows exist
# --------------------------------------------------------------------------


def test_the_table_is_the_documented_columns_in_order():
    built = build(kelp([(2020, 1, -5.0, True)]), env([(2020, 1, 1.0, True)]))
    assert tuple(built.columns) == comparison_columns(KELP_ANOMALIES, ENV_ANOMALIES)


def test_one_row_per_polygon_series_quarter_and_lag():
    built = build(
        kelp([(2020, 1, -5.0, True), (2020, 2, -6.0, True)]),
        pd.concat(
            [
                env([(2020, 1, 1.0, True)]),
                env([(2020, 1, 2.0, True)], parameter="air_temperature", depth_m=None),
            ],
            ignore_index=True,
        ),
    )
    assert len(built) == 2 * 2 * len(LAGS)  # two series x two quarters x five lags
    assert not built.duplicated(subset=list(COMPARISON_KEY)).any()


def test_a_met_series_with_no_depth_still_joins():
    """`depth_m` is null for every met parameter, so the join cannot be a merge
    on a key column holding nulls."""
    built = build(
        kelp([(2020, 1, -5.0, True)]),
        env([(2020, 1, 7.0, True)], parameter="air_temperature", depth_m=None),
    )
    assert at_lag(built, 0)["mean_anom"] == 7.0
    assert pd.isna(at_lag(built, 0)["depth_m"])


def test_a_lag_reaching_before_the_record_gives_a_null_row_not_a_missing_one():
    """ "The environmental record does not reach this quarter" has to be a
    queryable fact rather than an absence to infer."""
    built = build(kelp([(2007, 1, -5.0, True)]), env([(2007, 1, 1.0, True)]))

    assert len(built) == len(LAGS)
    assert at_lag(built, 0)["mean_anom"] == 1.0
    for lag in (1, 2, 3, 4):
        row = at_lag(built, lag)
        assert pd.isna(row["mean_anom"])
        assert pd.isna(row["env_usable"])
        # The pair itself is intact: only the environmental values are missing.
        assert (row["site_id"], row["parameter"]) == ("NDBC:LJAC1", "sea_water_temperature")
        assert row["kelp_area_m2_anom"] == -5.0


def test_a_kelp_quarter_with_no_environmental_counterpart_still_gets_its_rows():
    built = build(kelp([(1984, 1, -5.0, True)]), env([(2020, 1, 1.0, True)]))
    assert len(built) == len(LAGS)
    assert built["mean_anom"].isna().all()
    assert built["kelp_area_m2_anom"].notna().all()


# --------------------------------------------------------------------------
# Usability is carried, never applied
# --------------------------------------------------------------------------


def test_both_usability_flags_are_carried_onto_the_row():
    built = build(kelp([(2020, 1, -5.0, False)]), env([(2020, 1, 1.0, True)]))
    row = at_lag(built, 0)
    assert not row["kelp_usable"]
    assert row["env_usable"]


def test_a_row_where_either_side_is_unusable_is_kept():
    """`usable` stays the single gate, applied once by the analysis, rather than
    becoming a hidden deletion here."""
    built = build(kelp([(2020, 1, -5.0, False)]), env([(2020, 1, 1.0, False)]))
    row = at_lag(built, 0)

    assert not row["kelp_usable"]
    assert not row["env_usable"]
    assert row["mean_anom"] == 1.0  # nothing was filtered or nulled


def test_an_absent_environmental_row_leaves_usability_unknown_rather_than_false():
    """False would claim a verdict was reached about a quarter that has no row."""
    built = build(kelp([(2020, 1, -5.0, True)]), env([]))
    assert built["env_usable"].isna().all()


# --------------------------------------------------------------------------
# Which pairs exist comes from the registry
# --------------------------------------------------------------------------


def test_only_registry_declared_pairs_appear():
    """docs/03 integrity rule: no analysis code string-matches a polygon name
    against a station name."""
    registry = polygons(
        Polygon(
            polygon_id="KELP:A",
            purpose="regional",
            site_ids=("NDBC:LJAC1",),
            source_file="a.csv",
        ),
        Polygon(
            polygon_id="KELP:B",
            purpose="control",
            site_ids=("NDBC:OTHER",),
            source_file="b.csv",
        ),
    )
    built = build(
        pd.concat([kelp([(2020, 1, -5.0, True)]), kelp([(2020, 1, -6.0, True)], "KELP:B")]),
        env([(2020, 1, 1.0, True)]),
        registry,
    )
    assert set(built["polygon_id"]) == {"KELP:A"}


def test_a_polygon_paired_with_several_sites_gets_a_row_for_each():
    registry = polygons(
        Polygon(
            polygon_id="KELP:A",
            purpose="regional",
            site_ids=("NDBC:LJAC1", "PROJ:BUOY"),
            source_file="a.csv",
        )
    )
    built = build(
        kelp([(2020, 1, -5.0, True)]),
        pd.concat([env([(2020, 1, 1.0, True)]), env([(2020, 1, 2.0, True)], site_id="PROJ:BUOY")]),
        registry,
    )
    assert set(built["site_id"]) == {"NDBC:LJAC1", "PROJ:BUOY"}
    assert len(built) == 2 * len(LAGS)


def test_a_site_the_environment_never_measured_contributes_no_pair():
    """Inventing a pair for a parameter nobody measured would fill the table with
    rows that can never be anything but null."""
    registry = polygons(
        Polygon(
            polygon_id="KELP:A",
            purpose="regional",
            site_ids=("NDBC:LJAC1", "PROJ:NEVER-INGESTED"),
            source_file="a.csv",
        )
    )
    built = build(kelp([(2020, 1, -5.0, True)]), env([(2020, 1, 1.0, True)]), registry)
    assert set(built["site_id"]) == {"NDBC:LJAC1"}


# --------------------------------------------------------------------------
# Shape and determinism
# --------------------------------------------------------------------------


def test_the_environmental_source_is_named_as_such():
    """A row carries two sources' worth of provenance, so a bare `source` would
    not say which half it described."""
    built = build(kelp([(2020, 1, -5.0, True)]), env([(2020, 1, 1.0, True)]))
    assert set(built["env_source"]) == {"ndbc"}
    assert "source" not in built.columns
    assert set(built["kelp_watch_revision"]) == {23}


def test_two_builds_over_unchanged_input_are_identical():
    kelp_frame = kelp([(2020, 1, -5.0, True), (2020, 2, -6.0, True)])
    env_frame = env([(2019, 4, 1.0, True), (2020, 1, 2.0, True)])
    assert build(kelp_frame, env_frame).equals(build(kelp_frame, env_frame))


def test_an_empty_half_yields_an_empty_table_with_the_right_columns():
    columns = comparison_columns(KELP_ANOMALIES, ENV_ANOMALIES)

    no_kelp = build(kelp([]), env([(2020, 1, 1.0, True)]))
    assert no_kelp.empty
    assert tuple(no_kelp.columns) == columns

    no_env = build(kelp([(2020, 1, -5.0, True)]), env([]))
    assert no_env.empty
    assert tuple(no_env.columns) == columns


def test_the_lags_can_be_narrowed_for_a_sensitivity_run():
    """A notebook rebuilding at a different set of lags must not need to write a
    competing file of record."""
    built = build(kelp([(2020, 1, -5.0, True)]), env([(2020, 1, 1.0, True)]), lags=(0, 1))
    assert sorted(built["lag"]) == [0, 1]
