"""Phase-gating helper for subcommands scheduled in later phases."""

from typing import NoReturn


def todo(phase: str, what: str) -> NoReturn:
    """Fail loudly for work a later phase owns."""
    raise SystemExit(
        f"regrag: {what} is not implemented yet - arrives in {phase} (see README status table)"
    )


__all__ = ["todo"]
