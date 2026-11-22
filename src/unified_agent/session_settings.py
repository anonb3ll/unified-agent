"""Bounded ACP v1 session settings, not a permission or identity authority.

Only explicit known native mode ids map to the operator's permission choices.
Display labels/descriptions cannot establish a permission policy. Unknown modes
stay unclassified; no "code"/"agent" mode is assumed safe.
"""

from __future__ import annotations

from typing import Any

FEATURE = "session_settings"
MODES = ("supervised", "auto-accept-edits", "full-access")
NATIVE_DEFAULT_HARNESSES = frozenset({"hermes", "cursor", "copilot", "opencode"})
_MODEL_PREFIXES = frozenset(
    {"anthropic", "openai", "google", "gemini", "nous", "mistral", "deepseek", "xai"}
)


def native_default_supported(harness: str) -> bool:
    """Compatibility only: keep native authority unknown, never grant a mode."""
    return harness in NATIVE_DEFAULT_HARNESSES


def select_model(
    settings: dict[str, Any], requested: str, *, aliases: bool = True
) -> tuple[str | None, str]:
    """Resolve only exact IDs or unique provider-qualified catalog aliases.

    A single recognized provider prefix may be omitted by an adapter. Model
    suffixes, version names, display labels and generic 'sonnet' names are never
    guessed; two qualified IDs with different providers never match.
    """
    ids = [m["id"] for m in settings.get("available_models", [])]
    if requested in ids:
        return requested, "exact"
    if not aliases:
        return None, "unavailable"

    def parts(value: str) -> tuple[str | None, str]:
        prefix, separator, leaf = value.partition("/")
        if separator and prefix in _MODEL_PREFIXES and leaf and "/" not in leaf:
            return prefix, leaf
        return None, value

    provider, leaf = parts(requested)
    matches = []
    for ident in ids:
        other_provider, other_leaf = parts(ident)
        if (
            leaf == other_leaf
            and (provider or other_provider)
            and (provider is None or other_provider is None or provider == other_provider)
        ):
            matches.append(ident)
    return (matches[0], "catalog_alias") if len(matches) == 1 else (None, "unavailable")


# Native aliases are deliberately harness-specific; adding one requires
# reviewing that adapter's semantics. ACP does not reserve permission ids.
_NATIVE_MODES = {
    "claude": {
        "default": "supervised",
        "acceptEdits": "auto-accept-edits",
        "bypassPermissions": "full-access",
    },
    "codex": {
        "read-only": "supervised",
        "workspace-write": "auto-accept-edits",
        "agent-full-access": "full-access",
    },
    "gemini": {"default": "supervised", "autoEdit": "auto-accept-edits", "yolo": "full-access"},
}


def mode_class(harness: str, native_id: Any) -> str | None:
    if not isinstance(native_id, str):
        return None
    return _NATIVE_MODES.get(harness, {}).get(native_id)


def _id(value: Any) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= 200 else None


def _choices(raw: Any, id_key: str) -> list[dict[str, str]]:
    out = []
    seen = set()
    items = raw[:100] if isinstance(raw, list) else []
    if id_key == "value":
        items = [
            choice
            for item in items
            for choice in (
                item.get("options", [])[:100]
                if isinstance(item, dict) and isinstance(item.get("options"), list)
                else [item]
            )
        ][:100]
    for item in items:
        if not isinstance(item, dict):
            continue
        ident = _id(item.get(id_key))
        if ident is None or ident in seen:
            continue
        seen.add(ident)
        name = item.get("name")
        out.append({"id": ident, "name": name[:200] if isinstance(name, str) else ident})
    return out


def advertised(result: Any, harness: str) -> dict[str, Any]:
    """Normalize session/new, load, resume and v1 configOptions responses."""
    result = result if isinstance(result, dict) else {}
    modes = result.get("modes")
    models = result.get("models")
    modes = modes if isinstance(modes, dict) else {}
    models = models if isinstance(models, dict) else {}
    out: dict[str, Any] = {
        "available_modes": _choices(modes.get("availableModes"), "id"),
        "available_models": _choices(models.get("availableModels"), "modelId"),
        "current_mode_id": _id(modes.get("currentModeId")),
        "current_model_id": _id(models.get("currentModelId")),
        "mode_path": "session/set_mode" if modes.get("availableModes") else None,
        # models is the v1 legacy model-selection advertisement. The newer
        # category=model configOption takes precedence when supplied.
        "model_path": "session/set_model" if models.get("availableModels") else None,
    }
    out["mode_path"] = "session/set_mode" if out["available_modes"] else None
    out["model_path"] = "session/set_model" if out["available_models"] else None
    options = result.get("configOptions")
    for category in ("mode", "model"):
        matches = (
            [
                o
                for o in options[:50]
                if isinstance(o, dict)
                and o.get("category") == category
                and o.get("type") == "select"
            ]
            if isinstance(options, list)
            else []
        )
        if len(matches) != 1:
            continue
        option = matches[0]
        cid = _id(option.get("id"))
        choices = _choices(option.get("options"), "value")
        if not cid or not choices:
            continue
        out[f"available_{category}s"] = choices
        out[f"current_{category}_id"] = _id(option.get("currentValue"))
        out[f"{category}_path"] = "session/set_config_option"
        out[f"{category}_config_id"] = cid
    for mode in out["available_modes"]:
        mode["permission_mode"] = mode_class(harness, mode["id"])
    out["permission_mode"] = mode_class(harness, out["current_mode_id"])
    return out


def select_mode(settings: dict[str, Any], requested: str) -> str | None:
    matches = [
        m["id"]
        for m in settings.get("available_modes", [])
        if m.get("permission_mode") == requested
    ]
    # Preserve a compatible native mode where possible; otherwise the agent's
    # advertised order is stable and authoritative.
    current = settings.get("current_mode_id")
    return current if current in matches else (matches[0] if matches else None)


def reported(raw: Any, harness: str) -> dict[str, Any]:
    """Bound an untrusted node event to the public settings projection schema."""
    raw = raw if isinstance(raw, dict) else {}
    out = {
        "available_modes": _choices(raw.get("available_modes"), "id"),
        "available_models": _choices(raw.get("available_models"), "id"),
        "current_mode_id": _id(raw.get("current_mode_id")),
        "current_model_id": _id(raw.get("current_model_id")),
    }
    for kind in ("mode", "model"):
        path = raw.get(f"{kind}_path")
        out[f"{kind}_path"] = (
            path if path in (f"session/set_{kind}", "session/set_config_option") else None
        )
        out[f"{kind}_config_id"] = _id(raw.get(f"{kind}_config_id"))
    for mode in out["available_modes"]:
        mode["permission_mode"] = mode_class(harness, mode["id"])
    out["permission_mode"] = mode_class(harness, out["current_mode_id"])
    for key in ("requested_model", "source"):
        out[key] = _id(raw.get(key))
    out["requested_mode"] = (
        raw.get("requested_mode") if raw.get("requested_mode") in MODES else None
    )
    out["refused"] = raw.get("refused") is True
    fallback = (
        native_default_supported(harness)
        and out["requested_mode"] == "supervised"
        and out["permission_mode"] is None
        and raw.get("mode_control") == "native_default_unclassified"
    )
    out["mode_control"] = (
        "native_default_unclassified"
        if fallback
        else "negotiated"
        if raw.get("mode_control") == "negotiated" and out["permission_mode"]
        else "unclassified"
    )
    out["baseline_mode_id"] = _id(raw.get("baseline_mode_id")) if fallback else None
    resolution = raw.get("model_resolution")
    out["model_resolution"] = (
        resolution
        if resolution in ("exact", "catalog_alias", "native_default", "profile_default_unavailable")
        else None
    )
    return out
