"""The manifest type. Apart from ``__init__`` so manifests import it with no cycle."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Component:
    """One component's manifest: its name and the tools it registers, in order.

    ``tools`` holds the bare tool functions. Each is registered under its
    ``__name__`` and wrapped by the hub's policy guard at registration, exactly
    as when ``server.py`` named it directly.
    """

    name: str
    tools: tuple[Callable[..., Any], ...] = ()
