"""Execute a notebook in place, the one way that cannot quietly damage it.

`jupyter execute --inplace` is the obvious command and it is unsafe here, in two
ways that compound:

* `nbclient.cli` opens the notebook with the *platform default* encoding rather
  than UTF-8. On a cp1252 console every em dash, section sign and multiplication
  sign in the markdown decodes to mojibake, the notebook runs perfectly well,
  and `--inplace` writes the damage back. **Exit code 0.** It only fails loudly
  when a byte turns up that cp1252 cannot decode at all, by which point the file
  has already been rewritten.
* It writes CRLF back. `.gitattributes` now pins notebooks to LF, so this is no
  longer a diff problem, but the file on disk still ends up in a form the repo
  does not store.

The executed notebook is the analysis of record (`notebooks/README.md`), so a
silent corruption of it is a corruption of the record. Rather than asking anyone
to remember an environment variable, this runs the executor under UTF-8 mode,
normalises the endings afterwards, and then checks the one invariant that makes
the whole class of failure visible: **executing a notebook rewrites outputs and
must never touch a cell's source.** A source that changed is proof the file was
mangled rather than run, whatever the exit code said.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NOTEBOOKS = REPO_ROOT / "notebooks"


def cells(path: Path) -> list[dict]:
    """The notebook's cells, read as UTF-8 because that is what the format is."""
    return json.loads(path.read_text(encoding="utf-8"))["cells"]


def sources(path: Path) -> list[str]:
    """Every cell's source, joined -- the before and after of the damage check."""
    return ["".join(cell["source"]) for cell in cells(path)]


def normalise(path: Path) -> None:
    """CRLF back to LF, matching what the repository stores."""
    raw = path.read_bytes()
    if b"\r\n" in raw:
        path.write_bytes(raw.replace(b"\r\n", b"\n"))


def execute(path: Path) -> None:
    """Run one notebook in place, or raise saying which way it went wrong."""
    before = sources(path)

    # PYTHONUTF8 in the environment, *not* `-X utf8` on the command line, and the
    # difference is not stylistic: `jupyter` dispatches its subcommands by finding
    # and running a separate `jupyter-execute` executable, so an interpreter flag
    # given here applies to the launcher and is gone by the time the notebook is
    # read. An environment variable is inherited by the child that does the
    # reading. Tested the wrong way round first, and the source check below is
    # what caught it -- every markdown cell came back mojibake, exit code 0.
    subprocess.run(
        [sys.executable, "-m", "jupyter", "execute", "--inplace", str(path)],
        env={**os.environ, "PYTHONUTF8": "1"},
        check=True,
    )
    normalise(path)

    after = sources(path)
    if len(before) != len(after):
        raise SystemExit(
            f"{path.name}: executing it changed the cell count from {len(before)} to "
            f"{len(after)}. The file has been damaged rather than run -- restore it with "
            "`git checkout -- ` and do not commit this."
        )
    changed = [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]
    if changed:
        raise SystemExit(
            f"{path.name}: executing it changed the source of cells {changed}. Execution "
            "rewrites outputs and never source, so this is corruption -- most likely the "
            "encoding bug above. Restore it with `git checkout -- ` and do not commit this."
        )

    # nbclient stops at the first failing cell and exits non-zero, so `check=True`
    # above has already caught that. This re-reads the file rather than trusting
    # it, because a notebook saved with an error output in it is not an analysis
    # of record whatever the exit code was.
    failed = sorted(
        {
            index
            for index, cell in enumerate(cells(path))
            for output in cell.get("outputs", ())
            if output.get("output_type") == "error"
        }
    )
    if failed:
        raise SystemExit(f"{path.name}: cells {failed} raised. The notebook did not run clean.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Execute notebooks in place, safely. See the module docstring for why."
    )
    parser.add_argument(
        "notebooks",
        nargs="*",
        type=Path,
        help="Notebooks to run. Defaults to every notebook in notebooks/.",
    )
    paths = parser.parse_args().notebooks or sorted(NOTEBOOKS.glob("*.ipynb"))
    if not paths:
        raise SystemExit(f"no notebooks found in {NOTEBOOKS}")

    for path in paths:
        if not path.exists():
            raise SystemExit(f"{path} does not exist")
        print(f"running {path}")
        execute(path)
        print(f"clean   {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
