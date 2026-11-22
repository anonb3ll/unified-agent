"""The ``budgets`` block of the reviewed policy file, parsed and validated.

``budgets`` enforces these policies on claims; ``config_revisions`` validates
a proposed policy file with the same parser before it is committed. Parsing
needs nothing but configuration, so it lives below both. A malformed block
fails closed with ``BudgetConfigError``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from mcp.server.mcpserver.exceptions import ToolError

from . import config

PERIODS = ("day", "month")
ACTIONS = ("block", "warn")
DEFAULT_WARN_PCT = 80.0


class BudgetConfigError(ToolError):
    """The ``budgets`` block of the policy file is malformed."""


@dataclass(frozen=True)
class BudgetPolicy:
    name: str
    agents: tuple[str, ...]
    period: str
    limit_usd: float
    warn_pct: float = DEFAULT_WARN_PCT
    run_reserve_usd: float = 0.0
    action: str = "block"

    def applies_to(self, agent: str) -> bool:
        return not self.agents or "*" in self.agents or agent in self.agents

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "agents": list(self.agents) or ["*"],
            "period": self.period,
            "limit_usd": self.limit_usd,
            "warn_pct": self.warn_pct,
            "run_reserve_usd": self.run_reserve_usd,
            "action": self.action,
        }


def _number(item: dict[str, Any], key: str, default: float | None, where: str) -> float:
    raw = item.get(key, default)
    if raw is None:
        raise BudgetConfigError(f"{where}: '{key}' is required")
    if isinstance(raw, bool):
        raise BudgetConfigError(f"{where}: '{key}' must be a number")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise BudgetConfigError(f"{where}: '{key}' must be a number") from exc
    if not math.isfinite(value) or value < 0:
        raise BudgetConfigError(f"{where}: '{key}' must be a finite number >= 0")
    return value


def _parse(raw: Any) -> list[BudgetPolicy]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise BudgetConfigError("policy 'budgets' must be a list")
    out: list[BudgetPolicy] = []
    names: set[str] = set()
    for i, item in enumerate(raw):
        where = f"budgets[{i}]"
        if not isinstance(item, dict):
            raise BudgetConfigError(f"{where} must be a mapping")
        name = str(item.get("name") or "").strip()
        if not name:
            raise BudgetConfigError(f"{where}: 'name' is required")
        if name in names:
            raise BudgetConfigError(f"{where}: duplicate budget name {name!r}")
        names.add(name)
        where = f"budget {name!r}"
        # Omitted means every agent; a given list must name labels (or "*").
        # A blank or empty list is an error, never a silent wildcard.
        agents_raw = item.get("agents", ["*"])
        if isinstance(agents_raw, str):
            agents_raw = [agents_raw]
        if (
            not isinstance(agents_raw, list)
            or not agents_raw
            or not all(isinstance(a, str) and a.strip() for a in agents_raw)
        ):
            raise BudgetConfigError(f"{where}: 'agents' must be a non-empty list of labels")
        agents = tuple(a.strip() for a in agents_raw)
        period = str(item.get("period", "month")).strip().lower()
        if period not in PERIODS:
            raise BudgetConfigError(f"{where}: 'period' must be one of {PERIODS}")
        action = str(item.get("action", "block")).strip().lower()
        if action not in ACTIONS:
            raise BudgetConfigError(f"{where}: 'action' must be one of {ACTIONS}")
        limit = _number(item, "limit_usd", None, where)
        if limit <= 0:
            raise BudgetConfigError(f"{where}: 'limit_usd' must be > 0")
        warn = _number(item, "warn_pct", DEFAULT_WARN_PCT, where)
        if warn > 100:
            raise BudgetConfigError(f"{where}: 'warn_pct' must be between 0 and 100")
        reserve = _number(item, "run_reserve_usd", 0.0, where)
        if reserve > limit:
            raise BudgetConfigError(f"{where}: 'run_reserve_usd' exceeds 'limit_usd'")
        out.append(
            BudgetPolicy(
                name=name,
                agents=agents,
                period=period,
                limit_usd=limit,
                warn_pct=warn,
                run_reserve_usd=reserve,
                action=action,
            )
        )
    return out


def load_budgets(path: Path | None = None) -> list[BudgetPolicy]:
    """Budget policies from the policy file; ``[]`` when there are none.

    Raises ``BudgetConfigError`` when the file exists but cannot be read or
    its ``budgets`` block is malformed.
    """
    p = path if path is not None else config.policy_file_path()
    if not p.exists():
        return []
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - any read/parse failure fails closed
        raise BudgetConfigError(f"cannot read budgets from {p}: {exc}") from exc
    if data is None:
        return []
    if not isinstance(data, dict):
        raise BudgetConfigError(f"policy configuration at {p} must be a YAML mapping")
    return _parse(data.get("budgets"))
