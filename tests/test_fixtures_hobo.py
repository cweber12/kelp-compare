"""Sanity checks on the reference HOBO files, pinning the docs/06 findings.

These pass before any kelpcompare code exists; they define what the
hobo_xlsx adapter must reproduce.
"""

from pathlib import Path

import pandas as pd

FIX = Path(__file__).parent / "fixtures"
ORIGINAL = FIX / "Tidbit_1__22506632__2026-08-01_07_44_27_PDT__Data_PDT_.xlsx"
EDITED = FIX / "yellow_buoy_temps.xlsx"
WINDOW = ("2026-07-11 08:00", "2026-08-01 07:30")  # registry deployment window


def test_original_structure_and_stats():
    df = pd.read_excel(ORIGINAL, sheet_name="Data")
    assert list(df.columns) == ["#", "Date-Time (PDT)", "Tidbit 1 , °F"]
    assert len(df) == 3029
    t = df["Tidbit 1 , °F"]
    # Must match the Details-sheet export statistics (docs/06 s5 check 1)
    assert round(t.min(), 2) == 58.60
    assert round(t.max(), 2) == 75.35
    spacing = df["Date-Time (PDT)"].diff().dropna().dt.total_seconds().unique()
    assert list(spacing) == [600.0]


def test_registry_window_reproduces_hand_edit():
    orig = pd.read_excel(ORIGINAL, sheet_name="Data")
    edited = pd.read_excel(EDITED, sheet_name="Data")
    edited = edited.dropna(subset=["Date-Time (PDT)"])[["Date-Time (PDT)", "Tidbit 1 , °F"]]
    m = (orig["Date-Time (PDT)"] >= WINDOW[0]) & (orig["Date-Time (PDT)"] <= WINDOW[1])
    windowed = orig.loc[m, ["Date-Time (PDT)", "Tidbit 1 , °F"]].reset_index(drop=True)
    assert windowed.equals(edited.reset_index(drop=True))
    assert len(windowed) == 3022
    assert round(windowed["Tidbit 1 , °F"].min(), 2) == 63.96  # install transient excluded
