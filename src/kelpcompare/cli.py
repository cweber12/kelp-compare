"""CLI entry point: kelpcompare ingest|qc|features|rebuild (docs/01 s5, ADR-002).

Stub only. Each command must write a run manifest to data/raw/_manifests/.
"""

import click


@click.group()
def main() -> None:
    """kelpcompare pipeline commands."""


@main.command()
@click.option("--source", required=True, help="Source name per docs/02 (e.g. ndbc, hobo).")
def ingest(source: str) -> None:
    """Fetch/parse one source into raw/ and observations/. Not yet implemented."""
    raise SystemExit(f"ingest({source!r}) not implemented — see docs/02 and .claude/skills/")


@main.command()
def rebuild() -> None:
    """Regenerate all derived zones from raw/. Not yet implemented."""
    raise SystemExit("rebuild not implemented — see docs/03 integrity rules")


if __name__ == "__main__":
    main()
