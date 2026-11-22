#!/usr/bin/env bash
# Hermes governing-file drift audit (deterministic — no LLM involved).
#
# hermes-agent autonomously creates and self-edits its own skills; on 2026-07-12
# it self-patched a SKILL.md to bless a boundary drift. That incident is why
# this check exists. But Hermes is *also* designed to learn continuously —
# every real use of Hermes tends to touch skills/**/SKILL.md — so treating
# every skill edit as unapproved drift meant an alert (and an investigation)
# almost every run. That's the failure mode this version fixes: two tiers.
#
#   FROZEN tier   — ~/.hermes/{SOUL.md,config.yaml,hooks/**}. Rare, high-stakes.
#                   Whole-file sha256 manifest, same as before: ANY change
#                   requires --rebaseline --approved-by <name>.
#
#   MONITORED tier — ~/.hermes/skills/**/SKILL.md. Expected to change often.
#                   Only the YAML *frontmatter* is tracked, key-by-key (same
#                   value-hash-only technique as config.yaml below — key
#                   NAMES are visible, values are hashed and never stored).
#                   A key whose path doesn't look security-relevant (not in
#                   SECURITY_KEY_RE) is auto-accepted into the baseline every
#                   run, silently — Hermes free-editing a skill's body or
#                   description costs nothing. A key that DOES look
#                   security-relevant (allowed-tools, permission, scope,
#                   boundary, mcp, access, exec, secret, token, ...) still
#                   requires --rebaseline approval, exactly like the frozen
#                   tier — that's the tripwire that would have caught the
#                   2026-07-12 incident.
#
# Drift is appended to ~/.unified_agent/audit.log and escalated as a vault
# inbox note, same as before.
#
# Usage:
#   hermes_drift_check.sh                                    # compare against baseline
#   hermes_drift_check.sh --rebaseline --approved-by <name>  # accept current state
#       Requires the human operator's per-change approval; <name> is recorded in audit.log.
#       Prior baselines are archived (never overwritten) so a rebaseline can't
#       launder drift: the pre-approval snapshot stays reviewable.
#
# Install: crontab -e → 5 9 * * * /path/to/unified_agent/scripts/hermes_drift_check.sh
set -u
export LC_ALL=C

HERMES_HOME="$HOME/.hermes"
BASELINE="$HOME/.unified_agent/hermes-drift.baseline"                # frozen tier, whole-file
KEY_BASELINE="$HOME/.unified_agent/hermes-drift.keys.baseline"       # config.yaml, key-level detail
SKILL_KEY_BASELINE="$HOME/.unified_agent/hermes-drift.skills.baseline" # skills frontmatter, key-level
LOG="$HOME/.unified_agent/audit.log"
INBOX="$HOME/vaults/main/00 - Inbox"

# Key-path substrings (case-insensitive) that mark a skill-frontmatter key as
# security-relevant. Deliberately broad — a false positive costs one
# --rebaseline approval; a false negative is the 2026-07-12 failure mode again.
SECURITY_KEY_RE='allowed-tools|allowed_tools|permission|scope|boundary|capabilit|mcp|access|network|exec|delete|admin|secret|token|credential|sudo|shell|filesystem|workspace'

if command -v sha256sum >/dev/null 2>&1; then
    _sha256() { sha256sum "$@"; }
elif command -v shasum >/dev/null 2>&1; then
    _sha256() { shasum -a 256 "$@"; }
else
    _sha256() { openssl dgst -sha256 "$@"; }
fi

now=$(date -u +%FT%TZ)
today=$(date -u +%F)

[ -d "$HERMES_HOME" ] || { echo "$now DRIFT-ERR ~/.hermes missing" >>"$LOG"; exit 2; }
mkdir -p "$(dirname "$BASELINE")"

# --- frozen tier: whole-file sha256 over SOUL.md, config.yaml, hooks/** -----

manifest() {
    {
        for f in "$HERMES_HOME/SOUL.md" "$HERMES_HOME/config.yaml"; do
            [ -f "$f" ] && _sha256 "$f"
        done
        if [ -d "$HERMES_HOME/hooks" ]; then
            while IFS= read -r -d '' hf; do
                _sha256 "$hf"
            done < <(find "$HERMES_HOME/hooks" -type f -print0)
        fi
    } | sort -k2
}

# Flatten simple-indentation YAML to "<key.path><TAB><value>" lines. Deliberately
# a small awk program rather than a YAML library: this script is a security
# control that must run from cron with no dependency beyond coreutils, and its
# output feeds a report, not a decision. Nested maps and list items are handled;
# anchors, flow mappings and multi-line scalars are not, and degrade to attaching
# the text to the enclosing key — which costs detail in the report, never safety.
_YAML_KEYS_AWK='
/^[[:space:]]*(#|$)/ { next }
{
    line = $0
    sub(/[[:space:]]+$/, "", line)
    match(line, /^[[:space:]]*/)
    ind = RLENGTH
    body = substr(line, ind + 1)

    if (body ~ /^-/)                        { if (path != "") print path "\t" body; next }
    if (body !~ /^[A-Za-z0-9_.-]+:/)        { if (path != "") print path "\t" body; next }

    key = body; sub(/:.*$/, "", key)
    val = body; sub(/^[A-Za-z0-9_.-]+:[[:space:]]*/, "", val)

    while (top > 0 && indents[top] >= ind) top--
    top++; indents[top] = ind; keys[top] = key
    path = keys[1]
    for (i = 2; i <= top; i++) path = path "." keys[i]
    if (val != "") print path "\t" val
}'

# One "<value-sha256>  <id>" line per key across YAML governing files, where
# <id> is "<file>::<key.path>". The value is hashed on the way past and never
# retained. Reused for both config.yaml (key_manifest) and skill frontmatter
# (skill_key_manifest) below — same format, so the same substr(...,67) /
# cut -c67- convention (64 hex chars + 2 spaces) works on both baselines.
key_manifest() {
    local f keypath val
    for f in "$HERMES_HOME/config.yaml"; do
        [ -f "$f" ] || continue
        awk "$_YAML_KEYS_AWK" "$f" | while IFS=$'\t' read -r keypath val; do
            printf '%s  %s::%s\n' "$(printf '%s' "$val" | _sha256 | cut -c1-64)" "$f" "$keypath"
        done
    done | sort -k2
}

# Key paths whose value changed between the baseline and now, as
# "  keys added/changed/removed: a.b, c.d" detail lines. Empty when there is no
# key baseline yet (first run after this script gained the feature) — reported
# as such rather than silently, so a missing baseline never reads as "no change".
key_detail() {
    local cur_keys base_paths cur_paths added removed changed p old new
    [ -f "$KEY_BASELINE" ] || { echo "  keys: no key baseline yet — rebaseline to enable key-level detail"; return; }
    cur_keys=$(key_manifest)

    base_paths=$(cut -c67- "$KEY_BASELINE" | sort)
    cur_paths=$(printf '%s\n' "$cur_keys" | cut -c67- | sort)

    # Manifest ids are "<file>::<key.path>"; the MODIFIED line above already
    # names the file, so only the key path is worth printing. A YAML list under
    # one key produces one manifest line per list item, all with the same path
    # — dedupe with sort -u so a changed 20-item list reports the key once, not
    # 20 times. `paste -sd', ' -` is also wrong on its own: paste treats a
    # multi-char -d argument as a cycle of single-char delimiters (alternating
    # ',' then ' '), not the literal string ", " — hence `paste -sd, -` then a
    # separate substitution to get a real ", " separator.
    strip() { sed 's/^.*:://' | sort -u | paste -sd, - | sed 's/,/, /g'; }

    added=$(comm -13 <(printf '%s\n' "$base_paths") <(printf '%s\n' "$cur_paths") | strip)
    removed=$(comm -23 <(printf '%s\n' "$base_paths") <(printf '%s\n' "$cur_paths") | strip)
    changed=$(
        comm -12 <(printf '%s\n' "$base_paths") <(printf '%s\n' "$cur_paths") | while IFS= read -r p; do
            old=$(awk -v p="$p" 'substr($0,67)==p {print substr($0,1,64)}' "$KEY_BASELINE")
            new=$(printf '%s\n' "$cur_keys" | awk -v p="$p" 'substr($0,67)==p {print substr($0,1,64)}')
            [ "$old" != "$new" ] && echo "$p"
        done | strip
    )

    [ -n "$added" ] && echo "  keys added:   $added"
    [ -n "$changed" ] && echo "  keys changed: $changed"
    [ -n "$removed" ] && echo "  keys removed: $removed"
    return 0
}

# --- monitored tier: skills/**/SKILL.md frontmatter, key-level -------------

_frontmatter() {
    # Lines strictly between the first two "---" delimiters.
    awk 'BEGIN{c=0} /^---[[:space:]]*$/{c++; if(c==1){next} else if(c==2){exit}} c==1{print}' "$1"
}

skill_key_manifest() {
    [ -d "$HERMES_HOME/skills" ] || return 0
    local f keypath val
    while IFS= read -r -d '' f; do
        _frontmatter "$f" | awk "$_YAML_KEYS_AWK" | while IFS=$'\t' read -r keypath val; do
            printf '%s  %s::%s\n' "$(printf '%s' "$val" | _sha256 | cut -c1-64)" "$f" "$keypath"
        done
    done < <(find "$HERMES_HOME/skills" -name 'SKILL.md' -type f -print0) | sort -k2
}

# Diffs current skill-frontmatter keys against SKILL_KEY_BASELINE. Prints
# security-relevant added/changed/removed keys (one "SKILL-SECURITY-KEY <id>"
# line each) to stdout. Sets SKILL_AUTOACCEPT_OK=1 (global) iff nothing
# security-relevant changed, signaling the caller it's safe to silently fold
# the current manifest back into the baseline.
skill_key_diff() {
    SKILL_AUTOACCEPT_OK=1
    local cur base_paths cur_paths p old new flagged
    cur=$(skill_key_manifest)

    if [ ! -f "$SKILL_KEY_BASELINE" ]; then
        # First run under the tiered design: bootstrap silently. Nothing to
        # compare against yet, so nothing counts as drift.
        printf '%s\n' "$cur" >"$SKILL_KEY_BASELINE"
        chmod 600 "$SKILL_KEY_BASELINE"
        return 0
    fi

    base_paths=$(cut -c67- "$SKILL_KEY_BASELINE" | sort)
    cur_paths=$(printf '%s\n' "$cur" | cut -c67- | sort)

    flagged=""
    while IFS= read -r p; do
        [ -z "$p" ] && continue
        echo "$p" | grep -qiE "$SECURITY_KEY_RE" || continue
        flagged="$flagged$p (added)"$'\n'
    done < <(comm -13 <(printf '%s\n' "$base_paths") <(printf '%s\n' "$cur_paths"))

    while IFS= read -r p; do
        [ -z "$p" ] && continue
        echo "$p" | grep -qiE "$SECURITY_KEY_RE" || continue
        flagged="$flagged$p (removed)"$'\n'
    done < <(comm -23 <(printf '%s\n' "$base_paths") <(printf '%s\n' "$cur_paths"))

    while IFS= read -r p; do
        [ -z "$p" ] && continue
        old=$(awk -v p="$p" 'substr($0,67)==p {print substr($0,1,64)}' "$SKILL_KEY_BASELINE")
        new=$(printf '%s\n' "$cur" | awk -v p="$p" 'substr($0,67)==p {print substr($0,1,64)}')
        [ "$old" = "$new" ] && continue
        echo "$p" | grep -qiE "$SECURITY_KEY_RE" || continue
        flagged="$flagged$p (changed)"$'\n'
    done < <(comm -12 <(printf '%s\n' "$base_paths") <(printf '%s\n' "$cur_paths"))

    if [ -n "$flagged" ]; then
        SKILL_AUTOACCEPT_OK=0
        printf '%s' "$flagged" | sed '/^$/d' | sed 's/^/SKILL-SECURITY-KEY /'
    else
        # Nothing security-relevant changed — auto-accept the current manifest
        # (including any non-security key edits) into the baseline. This is
        # what makes routine skill self-editing silent.
        printf '%s\n' "$cur" >"$SKILL_KEY_BASELINE"
        chmod 600 "$SKILL_KEY_BASELINE"
    fi
}

current=$(manifest)

if [ "${1:-}" = "--rebaseline" ]; then
    if [ "${2:-}" != "--approved-by" ] || [ -z "${3:-}" ]; then
        echo "$now DRIFT-REBASELINE-REFUSED missing --approved-by" >>"$LOG"
        echo "Rebaseline requires explicit owner approval:" >&2
        echo "  $0 --rebaseline --approved-by <name>" >&2
        echo "Get the operator's sign-off for the specific change first." >&2
        exit 2
    fi
    approver="$3"
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    for b in "$BASELINE" "$KEY_BASELINE" "$SKILL_KEY_BASELINE"; do
        if [ -f "$b" ]; then
            archive="$b.$stamp"
            cp -p "$b" "$archive"
            chmod 600 "$archive"
        fi
    done
    printf '%s\n' "$current" >"$BASELINE"
    chmod 600 "$BASELINE"
    key_manifest >"$KEY_BASELINE"
    chmod 600 "$KEY_BASELINE"
    skill_key_manifest >"$SKILL_KEY_BASELINE"
    chmod 600 "$SKILL_KEY_BASELINE"
    echo "$now DRIFT-REBASELINE $(printf '%s\n' "$current" | wc -l) frozen files + skills approved-by=$approver" >>"$LOG"
    echo "Baseline written: $BASELINE (previous archived)"
    exit 0
fi

if [ ! -f "$BASELINE" ]; then
    echo "$now DRIFT-ERR no baseline — run with --rebaseline first" >>"$LOG"
    echo "No baseline at $BASELINE. Run: $0 --rebaseline" >&2
    exit 2
fi

diff_out=$(diff <(cat "$BASELINE") <(printf '%s\n' "$current"))

# Summarize frozen tier: a path only in baseline = removed; only in current =
# added; both = modified.
frozen_changed=$(
    {
        comm -13 <(cut -c67- "$BASELINE" | sort) <(printf '%s\n' "$current" | cut -c67- | sort) | sed 's/^/ADDED    /'
        comm -23 <(cut -c67- "$BASELINE" | sort) <(printf '%s\n' "$current" | cut -c67- | sort) | sed 's/^/REMOVED  /'
        comm -12 <(cut -c67- "$BASELINE" | sort) <(printf '%s\n' "$current" | cut -c67- | sort) | while IFS= read -r p; do
            old=$(awk -v p="$p" 'substr($0,67)==p {print substr($0,1,64)}' "$BASELINE")
            new=$(printf '%s\n' "$current" | awk -v p="$p" 'substr($0,67)==p {print substr($0,1,64)}')
            if [ "$old" != "$new" ]; then
                echo "MODIFIED $p"
                # Only YAML carries a key manifest; anything else stays a bare line.
                case "$p" in (*.yaml|*.yml) key_detail ;; esac
            fi
        done
    }
)

skill_flagged=$(skill_key_diff)

changed="$frozen_changed"
[ -n "$skill_flagged" ] && changed="$changed
$skill_flagged"
changed=$(printf '%s\n' "$changed" | sed '/^$/d')

if [ -z "$diff_out" ] && [ -z "$changed" ]; then
    echo "$now DRIFT-PASS hermes governing files unchanged" >>"$LOG"
    exit 0
fi
[ -z "$changed" ] && exit 0   # ordering-only diff, nothing real

echo "$now DRIFT-FAIL hermes governing files changed:" >>"$LOG"
printf '%s\n' "$changed" | sed "s/^/$now   /" >>"$LOG"

# Push alert via iMessage bridge notify.sh
NOTIFY_SH="$HOME/Projects/agent/projects/imessage-bridge/notify.sh"
if [ -x "$NOTIFY_SH" ]; then
    if ! printf '⚠️ Hermes Governing-File Drift Detected!\n\n%s\n\nCheck your Obsidian inbox for details.' "$changed" | "$NOTIFY_SH" >/dev/null 2>&1; then
        echo "$now DRIFT-NOTIFY-ERR notifier failed: $NOTIFY_SH" >>"$LOG"
    fi
else
    echo "$now DRIFT-NOTIFY-ERR notifier unavailable: $NOTIFY_SH" >>"$LOG"
fi

if [ -d "$INBOX" ]; then
    note="$INBOX/Hermes Drift $today.md"
    if [ ! -f "$note" ]; then
        printf -- '---\ndate: %s\nsource: hermes-drift-check\ntags: [security, steward, hermes]\n---\n\n# Hermes governing-file drift — %s\n\nUnapproved changes to [[Hermes-Agent]] governing files (SOUL.md / config.yaml / hooks) or security-relevant skill frontmatter keys.\nRoutine skill self-editing (non-security keys) is auto-accepted and does not appear here.\nIf the operator approved these, rebaseline: `hermes_drift_check.sh --rebaseline --approved-by <name>`.\n' \
            "$today" "$today" >"$note"
    fi
    printf -- '\n*Run at %s:*\n\n```\n%s\n```\n' "$now" "$changed" >>"$note"
fi
exit 1
