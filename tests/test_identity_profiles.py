"""Identity -> default profile mapping for the always-on Hermes surfaces.

Regression guard for the A2A coordination gap: ``gcp_agent5`` (cloud Agent18 on the
GCP worker) and ``server_agent5`` (the MBP that hosts the hub) were absent from
``DEFAULT_PROFILES_BY_IDENTITY`` and therefore fell through to DEVELOPER, whose
deny + LEGACY_* + DEFAULT_PROFILE_PRUNED sets withhold every coordination
primitive. Measured on the live hub before the fix: 100 tools served to both
identities (identical) versus 190 to Desktop_Agent5.

The second half of this file matters as much as the first: the fix must be a
targeted identity mapping, NOT a blanket widening of DEVELOPER. Engineering
identities that fall through to the developer default keep their posture.
"""

from __future__ import annotations

import pytest

from unified_agent import config
from unified_agent.profiles import (
    ALWAYS_ON_HERMES_IDENTITIES,
    CHIEF_OF_STAFF,
    CTO,
    CTO_EXTRA_TOOLS,
    DEFAULT_PROFILES_BY_IDENTITY,
    DEVELOPER,
    DEVELOPER_DENIED_TOOLS,
    PUBLIC,
    RESEARCH,
    _allowed_tools_for_profile,
    consolidated_action_is_allowed_for,
    default_for_identity,
    profiles_by_identity,
    tool_is_allowed_for,
)

# The coordination primitives that were withheld from the always-on surfaces.
A2A_TOOLS = frozenset(
    {
        "handoff",
        "handoff_list",
        "delegate",
        "delegate_list",
        "agent_list",
        "agent_activity",
        "agent_heartbeat",
    }
)

# A tool the developer default GENUINELY denies, used as a discrimination
# control: a test that accidentally asserted "everything is reachable" must
# fail. Do not use `run_cmd` here -- despite appearances it is NOT denied for
# developer (task #498: that is precisely why no profile denied it), so it
# discriminates nothing. DEVELOPER_DENIED_TOOLS is the source of truth.
DEVELOPER_DENIED_CONTROL = "approval_resolve"


ALWAYS_ON_PROFILES = {
    "gcp_agent5": RESEARCH,
    "gcp_agent20": DEVELOPER,
    "gcp_agent18": RESEARCH,
    "gcp_agent19": RESEARCH,
    "server_agent5": CTO,
}


@pytest.mark.parametrize("identity", sorted(ALWAYS_ON_PROFILES))
def test_always_on_hermes_surfaces_keep_their_non_cos_profiles(identity: str) -> None:
    assert default_for_identity(identity) == ALWAYS_ON_PROFILES[identity]
    assert default_for_identity(identity) != CHIEF_OF_STAFF


def test_server_Agent5_is_the_hub_hosted_cto_surface() -> None:
    """The operator moved server_agent5 to the CTO office's profile (#1884)."""
    assert default_for_identity("server_agent5") == CTO


def test_server_Agent5_coordinates_through_the_managers() -> None:
    """An always-on CTO surface must still reach every A2A action."""
    for manager, action in (
        ("delegation_manage", "delegate"),
        ("delegation_manage", "delegate_list"),
        ("coordination_manage", "handoff"),
        ("coordination_manage", "handoff_list"),
        ("observability_manage", "agent_list"),
        ("observability_manage", "agent_activity"),
        ("observability_manage", "agent_heartbeat"),
    ):
        assert consolidated_action_is_allowed_for(
            default_for_identity("server_agent5"), manager, action
        ), (manager, action)


def test_demoted_hermes_surfaces_do_not_keep_the_cos_broadcast_tools() -> None:
    """Leaving chief-of-staff drops the CoS-only tools with the role."""
    for identity in ("gcp_agent5", "gcp_agent18", "gcp_agent19", "gcp_agent20"):
        profile = default_for_identity(identity)
        assert not tool_is_allowed_for(profile, "notify"), identity
        assert not tool_is_allowed_for(profile, "approval_resolve"), identity
        assert not tool_is_allowed_for(profile, DEVELOPER_DENIED_CONTROL), identity


def test_always_on_hermes_roster_matches_the_mapping() -> None:
    """The roster is explicit, so the mapping cannot drift from it unnoticed.

    The 2026-09-26 identity split minted gcp_agent20/gcp_agent18/gcp_agent19. Minting
    a label does not map it, and an unmapped identity falls through to
    DEVELOPER: the three served 100 tools instead of 190, withholding
    agent_list / agent_activity / agent_heartbeat / handoff* / delegate* /
    notify, so their sessions reported that the coordination tools did not
    exist at all.

    This guard reads ALWAYS_ON_HERMES_IDENTITIES rather than the mapping, and
    that is the whole point: a guard that scans DEFAULT_PROFILES_BY_IDENTITY can
    only see identities someone already mapped, which is precisely the
    regression it exists to catch. Both directions are checked, so a surface
    added to either side without the other fails here.
    """
    for label in ALWAYS_ON_HERMES_IDENTITIES:
        assert label in DEFAULT_PROFILES_BY_IDENTITY, (
            f"{label} is an always-on Hermes surface with no profile decision; an "
            "unmapped identity falls through to DEVELOPER"
        )
        assert default_for_identity(label) == ALWAYS_ON_PROFILES[label]
        assert default_for_identity(label) != CHIEF_OF_STAFF

    unlisted = sorted(
        label
        for label in DEFAULT_PROFILES_BY_IDENTITY
        if (label.startswith("gcp_") or label == "server_agent5")
        and label not in ALWAYS_ON_HERMES_IDENTITIES
    )
    assert unlisted == [], (
        "unattended Hermes surface(s) given a profile without joining "
        f"ALWAYS_ON_HERMES_IDENTITIES: {unlisted}"
    )


def test_laptop_Agent5_is_the_cos_backup_and_desktop_Agent5_is_cto() -> None:
    """Laptop_Agent5 is the on-device chief-of-staff backup. Desktop_Agent5 stays the CTO.

    The operator's 2026-10-02 decision made Desktop_Agent5 the CTO / Head
    Engineer (#1788). On 2026-11-14 the operator kept Laptop_Agent5 on
    chief-of-staff (task #2583); it is attended and not cloud-hosted.
    """
    assert default_for_identity("Desktop_Agent5") == CTO
    assert default_for_identity("Laptop_Agent5") == CHIEF_OF_STAFF
    assert tool_is_allowed_for(CHIEF_OF_STAFF, "notify")
    assert not config.is_cloud_hosted_identity("Laptop_Agent5")


def test_chief_of_staff_is_cloud_Agent13_with_the_laptop_backup() -> None:
    """Every code-default mapping: only cloud_agent13 and Laptop_Agent5 are chief-of-staff."""
    chiefs = sorted(
        identity
        for identity in profiles_by_identity()
        if default_for_identity(identity) == CHIEF_OF_STAFF
    )
    assert chiefs == ["Laptop_Agent5", "cloud_agent13"]


GROK_BOT_PROFILES = {
    "cloud_agent13": CHIEF_OF_STAFF,
    "cloud_agent8": RESEARCH,
    "cloud_agent14": CTO,
    "cloud_agent15": DEVELOPER,
    "cloud_agent16": DEVELOPER,
}


@pytest.mark.parametrize("identity", sorted(GROK_BOT_PROFILES))
def test_grok_bot_defaults_to_its_profile(identity: str) -> None:

    assert default_for_identity(identity) == GROK_BOT_PROFILES[identity]
    assert config.is_cloud_hosted_identity(identity)
    assert identity in config.CLOUD_HOSTED_IDENTITIES


def test_cloud_Agent14_shares_desktop_Agent5_cto_and_cloud_Agent15_is_developer() -> None:
    """Engineering lead uses Desktop_Agent5's CTO profile. SWE stays developer."""
    assert default_for_identity("Desktop_Agent5") == CTO
    assert default_for_identity("cloud_agent14") == default_for_identity("Desktop_Agent5")
    assert default_for_identity("cloud_agent14") == CTO
    assert default_for_identity("cloud_agent15") == DEVELOPER
    assert config.is_cloud_hosted_identity("cloud_agent14")
    assert config.is_cloud_hosted_identity("cloud_agent15")


def test_cloud_Agent8_is_research_and_cloud_Agent17_is_developer() -> None:
    """cloud_agent8 is research. cloud_agent17 is a developer.

    server_agent8 stays on the public profile and is not a cloud-hosted identity.
    """

    assert default_for_identity("cloud_agent8") == RESEARCH
    assert config.is_cloud_hosted_identity("cloud_agent8")
    assert default_for_identity("cloud_agent17") == DEVELOPER
    assert default_for_identity("server_agent8") == PUBLIC


def test_cto_identity_keeps_a2a_through_the_consolidated_managers() -> None:
    """Desktop_Agent5 as CTO must still be able to see and route to the fleet.

    The bare A2A names are pruned from developer, but developer holds the
    consolidated managers and each manager authorizes its actions against the
    underlying name (consolidated_action_is_allowed). Without this, a reader
    of the deny lists could conclude the CTO seat lost fleet enumeration and
    handoff, and "fix" it with a carve-out that duplicates existing authority.
    Verified by dispatch as well as by the permission table.
    """
    assert default_for_identity("Desktop_Agent5") == CTO

    for manager in ("delegation_manage", "coordination_manage", "observability_manage"):
        assert tool_is_allowed_for(CTO, manager), manager

    for manager, action in (
        ("delegation_manage", "delegate"),
        ("delegation_manage", "delegate_list"),
        ("delegation_manage", "delegate_claim"),
        ("delegation_manage", "delegate_release"),
        ("coordination_manage", "handoff"),
        ("coordination_manage", "handoff_list"),
        ("observability_manage", "agent_list"),
        ("observability_manage", "agent_activity"),
        ("observability_manage", "agent_heartbeat"),
    ):
        assert consolidated_action_is_allowed_for(CTO, manager, action), (manager, action)


@pytest.mark.parametrize("profile", [DEVELOPER, CTO])
def test_cto_identity_does_not_keep_the_cos_broadcast_surface(profile: str) -> None:
    """The other half of #1788: the CoS tools go with the role, not around it.

    The CTO profile (#1884) must stay below chief-of-staff on exactly these.
    """
    for tool in (
        "notify",
        "check_anomalies_and_notify",
        "routine_add",
        "routine_list",
        "approval_resolve",
        "config_revision_manage",
        "dispatch_confirm_cancellation",
        "dispatch_retry_cancellation",
        "git_merge_task",
        "profile_activate",
        "batch_notify",
        "vault_move",
        "vault_archive_plan",
        "git_commit_staged",
    ):
        assert not tool_is_allowed_for(profile, tool), (profile, tool)
    if profile == DEVELOPER:
        assert not tool_is_allowed_for(profile, "review_and_notify")


def test_cto_extra_tools_are_a_reviewed_grant_above_developer() -> None:
    """#1884: the CTO reaches every extra; the developer default reaches none.

    The extras must be registered tools. Only review_and_notify may come out
    of the developer deny list; the rest of that list is the privilege
    boundary the CTO shares with developers.
    """
    from unified_agent import server

    registered = set(server.REGISTERED_TOOLS)
    assert CTO_EXTRA_TOOLS <= registered, sorted(CTO_EXTRA_TOOLS - registered)
    # review_and_notify is the one grant taken out of the developer deny list.
    assert CTO_EXTRA_TOOLS & DEVELOPER_DENIED_TOOLS == {"review_and_notify"}
    for tool in CTO_EXTRA_TOOLS:
        assert tool_is_allowed_for(CTO, tool), tool
        assert not tool_is_allowed_for(DEVELOPER, tool), tool


def test_cto_surface_is_developer_plus_extras() -> None:
    """Nothing else changes: no tool is reachable under CTO but not developer
    except the named extras, and the CTO loses nothing a developer has."""
    from unified_agent import server

    registered = set(server.REGISTERED_TOOLS)
    cto = _allowed_tools_for_profile(CTO, registered)
    developer = _allowed_tools_for_profile(DEVELOPER, registered)
    assert developer <= cto
    assert cto - developer == CTO_EXTRA_TOOLS


def test_unknown_identity_still_falls_through_to_developer() -> None:
    """The change adds two named identities; it does not change the fallback."""
    assert default_for_identity("some_unmapped_agent") == DEVELOPER
    assert default_for_identity(None) == DEVELOPER
    assert default_for_identity("") == DEVELOPER


def test_developer_default_does_not_gain_the_coordination_primitives() -> None:
    """Guards against fixing this by weakening the developer posture instead.

    If someone later "fixes" the gap by deleting the A2A names from the legacy
    deny sets, every engineering identity silently widens. That is a different,
    reviewable decision -- this test makes it an explicit one.
    """
    reachable = _allowed_tools_for_profile(DEVELOPER, A2A_TOOLS)
    assert A2A_TOOLS - reachable, (
        "the developer default unexpectedly reaches all A2A tools; "
        "widening was applied at the wrong layer"
    )
    # Discrimination check. If the control were reachable for developer, the
    # assertion above would still pass under an overbroad exposure result --
    # exactly the vacuous-control failure mode this replaced.
    universe = A2A_TOOLS | {DEVELOPER_DENIED_CONTROL}
    assert DEVELOPER_DENIED_CONTROL not in _allowed_tools_for_profile(DEVELOPER, universe), (
        f"{DEVELOPER_DENIED_CONTROL} is not actually denied for developer, "
        "so it cannot serve as a control; pick a real DEVELOPER_DENIED_TOOLS entry"
    )


def test_grok_postures_are_unchanged() -> None:
    """Research/public identities are deliberately restricted; leave them so."""
    assert default_for_identity("Desktop_Agent8") not in (CHIEF_OF_STAFF, DEVELOPER)
    assert default_for_identity("server_agent8") not in (CHIEF_OF_STAFF, DEVELOPER)


def test_identity_map_is_constant_typed() -> None:
    """A stray typo'd profile name here fails closed at the wrong layer."""
    from unified_agent.profiles import VALID_PROFILES

    for identity, profile in DEFAULT_PROFILES_BY_IDENTITY.items():
        assert profile in VALID_PROFILES, f"{identity} -> unknown profile {profile!r}"
