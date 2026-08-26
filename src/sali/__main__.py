"""Console entrypoint: ``sali`` → the Typer app."""

from __future__ import annotations

from sali.cli.main import app


def main() -> None:
    app()


if __name__ == "__main__":
    main()
