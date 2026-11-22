"""Operator and automation client; importing it never composes the hub."""


def entrypoint() -> None:
    from .main import run

    raise SystemExit(run())
