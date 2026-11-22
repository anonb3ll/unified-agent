#!/usr/bin/env bash
# Run CI for one exact commit on GCP Agent20 Build and post GitHub commit
# statuses for it (task #2115).
#
#   scripts/cloud-ci.sh <pr-number | branch | sha>
#   scripts/cloud-ci.sh --reconcile <build-id> <pr-number | branch | sha>
#
# --reconcile re-attaches to a build an earlier watcher submitted (task #2205)
# and submits nothing. It refuses unless the build's _SOURCE_SHA is the
# resolved head, waits while the build is unfinished (the same deadlines as a
# fresh run), then posts the final statuses. It settles the base contexts from
# that build's own step results (a missing step reads as failure) plus each
# optional context (js, desktop-core) the submitting run's classifier enabled,
# recorded on the build as the tags ua-ci-js-<0|1> and ua-ci-native-<0|1>. A
# skipped leg gets no status, as in a fresh run. No status need name the
# build: a watcher whose every write failed can still be re-attached. For a
# build without those tags, an optional context is settled only when a status
# names the build for it. It refuses, before waiting and again before posting,
# when any status in the history of those contexts is from a run it cannot
# prove older (no build named, an empty description, or a build whose
# createTime cannot be read): statuses are last-write-wins per context, and an
# older build must never overwrite a newer run's result. Which run is newer is
# decided by build createTime, never by status order. Contexts match as on
# GitHub, without case (and without surrounding whitespace).
# A fresh run makes the same check after its build finishes (task #2211), so a
# zombie watcher and a fresh run on one head cannot leave a stale result last.
# It notes the newest status id on the head before submitting; any status
# from another run past that baseline refuses unless it names a build created
# before this one (an older watcher finishing late), even when none of this
# run's own writes landed and even when this run's own status is newest on
# that context: its whole history is read, not only the newest row.
# The check also runs before this run's first (pending) write; if the
# statuses cannot be read there, it stops with nothing posted and prints the
# --reconcile command for its build.
#
# Why: GitHub-hosted Actions jobs are refused (account billing) and the Depot
# CI minute cap is reached (2026-10-16). This runs the same gates, the four
# balanced pytest shards and the enabled JS/Rust jobs, using
# deploy/cloudbuild/ci.yaml from THIS checkout. It posts the commit-status
# contexts the review gate reads through statusCheckRollup
# (pr_models.rollup_check_name passes `context` through unchanged):
#   CI / gates, CI / test (shard=0..3), CI / js, CI / full-verification
#
# The statuses are posted from this machine with the operator's `gh` login; no
# GitHub credential enters the build. Each points at the build's log.
#
# Env: UA_CI_GCP_PROJECT is required. UA_CI_REPO defaults to the current
# GitHub repository; UA_CI_GCP_REGION defaults to us-central1.
# UA_CI_NO_STATUS=1 skips posting (the build still runs and is billed).
set -euo pipefail
# Matching here is over ASCII: build ids, `[A-Za-z]` ranges, and context names
# folded with ${x,,}. The caller's locale must not change it: under tr_TR,
# `CI` lowercases to a dotless `cı` and ranges follow its collation, which
# disabled the supersession guard (review r4). Python children still read and
# write UTF-8 (its UTF-8 mode is on under the C locale).
export LC_ALL=C
# A prompt on a TTY would block the watcher with statuses left pending.
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

# Bash otherwise resumes reading this pathname after each external command.
# Capture the complete program before any billed operation; -c executes only
# these captured bytes, even if the checkout's script changes during polling.
# BASH_SOURCE is empty for the -c invocation, so no environment bypass is needed.
if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
    program="$(cat -- "${BASH_SOURCE[0]}")"
    exec "$BASH" -c "$program" "$0" "$@"
fi

usage="usage: scripts/cloud-ci.sh [--reconcile <build-id>] <pr-number|branch|sha>"
attach=""
if [ "${1:-}" = "--reconcile" ]; then
    attach="${2:-}"
    # Cloud Build IDs are UUIDs; the ID is also matched inside status text.
    if ! [[ "$attach" =~ ^[A-Za-z0-9][A-Za-z0-9-]*$ ]]; then
        echo "$usage" >&2
        exit 2
    fi
    shift 2
fi
ref="${1:?$usage}"
project="${UA_CI_GCP_PROJECT:?set UA_CI_GCP_PROJECT to the intended GCP project}"
repo="${UA_CI_REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"
region="${UA_CI_GCP_REGION:-us-central1}"
# Where a fresh run executes (task #2252). `spot` (the default) runs ci.yaml on
# a self-stopping Spot VM through scripts/spot_ci.py at a small fraction of
# Cloud Build's per-minute price; `cloudbuild` submits it to Cloud Build.
# Statuses, supersession and --reconcile are the same for both: a run id
# starting `spot-` is read from the VM backend, any other from Cloud Build, so
# history from either backend still orders correctly.
backend="${UA_CI_BACKEND:-spot}"
case "$backend" in
    spot|cloudbuild) ;;
    *) echo "UA_CI_BACKEND must be spot or cloudbuild, not '$backend'" >&2; exit 2 ;;
esac
root="$(git rev-parse --show-toplevel)"
config="$root/deploy/cloudbuild/ci.yaml"
spot_ci="$root/scripts/spot_ci.py"

# The describe command for a run, as an argv in `dcmd`, so callers keep a single
# `bounded` call: the whole run as `gcloud builds describe --format=json`, or one
# field of it (`status`, `createTime`) as a bare value.
describe_cmd() { # run-id [field]
    if [[ "$1" == spot-* ]]; then
        dcmd=(python3 -I "$spot_ci" describe "$1" ${2:+"$2"})
    else
        dcmd=(gcloud builds describe "$1" --project="$project" --region="$region" --quiet)
        if [ -n "${2:-}" ]; then dcmd+=("--format=value($2)"); else dcmd+=(--format=json); fi
    fi
}
run_url() { # run-id; sets url
    if [[ "$1" == spot-* ]]; then
        url="https://console.cloud.google.com/storage/browser/_details/${UA_CI_SPOT_BUCKET:-$project-spot-ci}/runs/$1/log.txt?project=$project"
    else
        url="https://console.cloud.google.com/cloud-build/builds;region=$region/$1?project=$project"
    fi
}

git -C "$root" fetch -q origin
if [[ "$ref" =~ ^[0-9]+$ ]]; then
    sha="$(gh pr view "$ref" -R "$repo" --json headRefOid -q .headRefOid)"
    git -C "$root" fetch -q origin "pull/$ref/head" || true
    # The test budget counts against the PR's own base branch, so a stacked
    # PR is measured from its parent PR, not from main (task #2233).
    read -r pr_base_name pr_base_oid < <(gh pr view "$ref" -R "$repo" \
        --json baseRefName,baseRefOid -q '.baseRefName + " " + .baseRefOid')
    if ! [[ "${pr_base_oid:-}" =~ ^[0-9a-f]{40}$ ]]; then
        echo "Could not resolve the base branch of PR #$ref; nothing was submitted." >&2
        exit 1
    fi
    git -C "$root" cat-file -e "$pr_base_oid^{commit}" 2>/dev/null \
        || git -C "$root" fetch -q origin "$pr_base_name"
    git -C "$root" cat-file -e "$pr_base_oid^{commit}"
elif git -C "$root" rev-parse -q --verify "origin/$ref^{commit}" >/dev/null; then
    sha="$(git -C "$root" rev-parse "origin/$ref^{commit}")"
else
    sha="$(git -C "$root" rev-parse "$ref^{commit}")"
fi
git -C "$root" cat-file -e "$sha^{commit}"
short="${sha:0:8}"
src=""

if [ -z "$attach" ]; then
# Pin both trees before creating the upload; later ref movement cannot change
# either the local classification or the build's refs/ci/base-to-HEAD diff.
base="$(git -C "$root" merge-base origin/main "$sha")"
src="$(mktemp -d)"
trap 'python3 -I -c "import shutil,sys; shutil.rmtree(sys.argv[1])" "$src"' EXIT
# A one-commit shallow checkout retains exact source identity and codegen's
# Git comparisons without uploading local branches, credentials or history.
git init -q --template= "$src/repo"
git -C "$src/repo" fetch -q --depth=1 "$root" "$sha"
git -C "$src/repo" fetch -q --depth=1 "$root" "$base"
git -C "$src/repo" update-ref refs/ci/base FETCH_HEAD
git -C "$src/repo" -c core.logAllRefUpdates=false checkout -q --detach "$sha"
# A pull request run carries the test budget's inputs (task #2233): the merge
# base with the PR's own base branch as refs/ci/budget-base, and the messages
# of the PR's commits since it, one NUL-terminated record each (the one-commit
# checkout has no history). Branch and SHA runs carry neither and skip it.
if [ -n "${pr_base_oid:-}" ]; then
    budget_base="$(git -C "$root" merge-base "$pr_base_oid" "$sha")"
    git -C "$src/repo" fetch -q --depth=1 "$root" "$budget_base"
    git -C "$src/repo" update-ref refs/ci/budget-base FETCH_HEAD
    git -C "$root" log -z --format=%B "$budget_base..$sha" > "$src/commit-messages"
fi
# FETCH_HEAD contains a local machine path; it is not needed by the build.
python3 -I -c 'from pathlib import Path; import sys; Path(sys.argv[1]).unlink(missing_ok=True)' "$src/repo/.git/FETCH_HEAD"

# Run the submitted classifier in the very checkout that will be uploaded,
# with the same isolated Python invocation, cwd and diff as ci-step.sh setup.
# Older heads without this standalone CLI/contract are unsupported: fail
# before billing or posting rather than fall back to the operator's rules.
# A regular leaf can still resolve through a symlinked scripts directory.
# Check the fixed layout and Git mode, then bind checkout bytes to HEAD's
# blob: an untracked/replaced regular file is not submitted source either.
python3 -I - "$src/repo" <<'PY'
import stat
import subprocess
import sys
from pathlib import Path

repo = Path(sys.argv[1])
try:
    if not stat.S_ISDIR((repo / "scripts").lstat().st_mode):
        raise ValueError
    classifier = repo / "scripts/ci_changes.py"
    if not stat.S_ISREG(classifier.lstat().st_mode):
        raise ValueError
    entry = subprocess.check_output(
        ["git", "ls-tree", "-z", "HEAD", "--", "scripts/ci_changes.py"], cwd=repo
    )
    metadata, path = entry.removesuffix(b"\0").split(b"\t")
    mode, kind, _oid = metadata.split()
    if path != b"scripts/ci_changes.py" or mode not in {b"100644", b"100755"} or kind != b"blob":
        raise ValueError
    blob = subprocess.check_output(
        ["git", "cat-file", "blob", "HEAD:scripts/ci_changes.py"], cwd=repo
    )
    if classifier.read_bytes() != blob:
        raise ValueError
except (OSError, ValueError, subprocess.CalledProcessError):
    sys.exit("Submitted head needs a regular scripts/ci_changes.py matching its Git blob, without symlinks.")
PY
classification="$src/classification"
(
    cd "$src/repo"
    git diff --name-only refs/ci/base HEAD | python3 -I "$src/repo/scripts/ci_changes.py" classify
) > "$classification"
# Parse data, never shell code. Require the standalone classifier's contract;
# substring matches, duplicate keys and missing/invalid switches are unsafe.
flags="$(python3 -I - "$classification" <<'PY'
import json
import sys
from pathlib import Path

try:
    # ci.yaml uses grep on LF-delimited bytes. Universal-newline conversion
    # or splitlines() would wrongly accept CRLF/Unicode-separated switches.
    lines = Path(sys.argv[1]).read_bytes().decode("utf-8").removesuffix("\n").split("\n")
    pairs = [line.split("=", 1) for line in lines]
    if any(len(pair) != 2 for pair in pairs):
        raise ValueError
    values = dict(pairs)
    if len(values) != len(pairs) or set(values) != {
        "python", "python_scope", "shards", "js", "connectors", "native"
    }:
        raise ValueError
    if any(values[key] not in {"true", "false"} for key in ("python", "js", "connectors", "native")):
        raise ValueError
    if values["python_scope"] not in {"full", "docs"}:
        raise ValueError
    shards = json.loads(values["shards"])
    if not isinstance(shards, list) or any(type(shard) is not int for shard in shards):
        raise ValueError
    if shards != ([0, 1, 2, 3] if values["python_scope"] == "full" else [0]):
        raise ValueError
except (ValueError, UnicodeError):
    sys.exit("Submitted classifier has an incompatible or invalid output contract.")
print(int(values["js"] == "true"), int(values["native"] == "true"))
PY
)"
read -r js native <<< "$flags"
# Cloud Build always runs gates and all four Python shards, independently of
# the classifier's Python scope. Only JS/native are conditional in ci.yaml.
fi

post() { # context state description url
    if [ "${UA_CI_NO_STATUS:-0}" = "1" ]; then
        echo "  [dry-run] $1: $2"
        return
    fi
    local url_args=()
    [ -n "${4:-}" ] && url_args=(-f "target_url=$4")
    local _
    for _ in 1 2; do
        if bounded gh api -X POST "repos/$repo/statuses/$sha" -f state="$2" -f context="$1" \
            -f description="$3" "${url_args[@]}" >/dev/null; then
            return 0
        fi
    done
    echo "Could not publish $1=$2 for $sha; inspect build $build_id before retrying CI." >&2
    return 1
}

# GitHub lists every historical status per context. A status names a build
# when its description starts `cloud-build <id> ` or `cloud-build <id>:`; this
# run's build is $build_id, which a --reconcile sets to the attached build up
# front. Contexts are compared as GitHub does, without case, and here also
# without surrounding whitespace (review r3): `context_key` gives that form.
declare -A foreign_since=()
max_id=0
baseline_id=0
names_build() {
    case "$1" in
        "cloud-build $build_id "*|"cloud-build $build_id:"*) return 0 ;;
    esac
    return 1
}
context_key() { # context; sets key
    key="${1,,}"
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
}
read_statuses() { # sets foreign_since, max_id, js_named, native_named
    local listed id ctx desc key
    listed="$(bounded gh api --paginate "repos/$repo/commits/$sha/statuses?per_page=100" \
        --jq '.[] | [.id, .context, (.description // "")] | @tsv')" || return 1
    foreign_since=()
    max_id=0
    js_named=0
    native_named=0
    while IFS=$'\t' read -r id ctx desc; do
        [[ "$id" =~ ^[0-9]+$ ]] || continue
        (( id > max_id )) && max_id="$id"
        context_key "$ctx"
        case "$key" in
            "ci / "*) ;;
            *) continue ;;
        esac
        # Another run's status on this context: for a fresh run only those after
        # its baseline (anything earlier predates its submission), for a
        # --reconcile the whole history. One per line, each led by `|` so an
        # empty (or null) description is kept, not mistaken for no status.
        if ! names_build "$desc" && { [ -n "$attach" ] || (( id > baseline_id )); }; then
            foreign_since[$key]+="|${desc//$'\n'/ }"$'\n'
        fi
        names_build "$desc" || continue
        case "$key" in
            "ci / js") js_named=1 ;;
            "ci / desktop-core") native_named=1 ;;
        esac
    done <<< "$listed"
}
# Which run is newer is decided by build creation, not by status order: an
# older build's watcher can post its result after this run's statuses (review
# r2). `build_age <description>` sets `age` to older, newer or unknown relative
# to this run's build; a status that names no build, or a build whose
# createTime cannot be read, is unknown. Creation times are cached per run.
declare -A created=() ages=()
build_created() { # id; sets ctime
    if [ -z "${created[$1]+x}" ]; then
        local t
        describe_cmd "$1" createTime
        t="$(bounded "${dcmd[@]}" </dev/null)" || return 1
        [ -n "$t" ] || return 1
        created[$1]="$t"
    fi
    ctime="${created[$1]}"
}
build_age() { # description; sets age
    local pattern='^cloud-build ([A-Za-z0-9][A-Za-z0-9-]*)[ :]' theirs mine
    age=unknown
    [[ "$1" =~ $pattern ]] || return 0
    local id="${BASH_REMATCH[1]}"
    if [ -n "${ages[$id]+x}" ]; then
        age="${ages[$id]}"
        return 0
    fi
    build_created "$id" || return 0
    theirs="$ctime"
    build_created "$build_id" || return 0
    mine="$ctime"
    # RFC 3339 with up to nine fractional digits; text order is not time
    # order ("10:00:00Z" sorts after "10:00:00.5Z").
    age="$(python3 -I -c '
import re, sys
from datetime import datetime

def key(t):
    m = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)", t)
    if not m:
        raise ValueError(t)
    zone = "+00:00" if m[3] == "Z" else m[3]
    return int(datetime.fromisoformat(m[1] + zone).timestamp()), int((m[2] or "").ljust(9, "0"))

try:
    theirs, mine = key(sys.argv[1]), key(sys.argv[2])
except ValueError:
    print("unknown")
else:
    print("older" if theirs < mine else "newer" if theirs > mine else "unknown")
' "$theirs" "$mine" </dev/null)" || age=unknown
    ages[$id]="$age"
}
superseded() { # true when a context to settle holds a status this run cannot prove older
    local c d key
    for c in "${contexts[@]}"; do
        context_key "$c"
        [ -n "${foreign_since[$key]+x}" ] || continue
        while IFS= read -r d; do
            [ -n "$d" ] || continue
            d="${d#|}"
            # Every other run's status considered (after the baseline for a
            # fresh run, the whole history for a --reconcile, which has no
            # baseline) must name a build created before this one. A status
            # naming no build, an empty description, or a build or creation
            # time that cannot be read refuses, wherever it sits (review r3).
            build_age "$d"
            [ "$age" = "older" ] && continue
            echo "Refusing: '$c' on $sha holds a status from another run that is not older" \
                "than build $build_id (\"$d\"); a later run superseded it. No result was posted." >&2
            return 0
        done <<< "${foreign_since[$key]}"
    done
    return 1
}
# True when this run must not settle its contexts now: a newer run has written
# one since (superseded), or the statuses cannot be read to tell. Checked
# before every final write, fresh run or --reconcile (task #2211), including
# when none of this run's own writes landed: a newer build's status must not
# be overwritten either way (review round 3). A dry run posts nothing.
must_not_settle() {
    [ -n "$attach" ] || [ "${UA_CI_NO_STATUS:-0}" != "1" ] || return 1
    if ! read_statuses; then
        echo "Could not re-read the statuses on $sha; nothing was posted." >&2
        # Not final: print the re-attach command on exit.
        errored=1
        return 0
    fi
    superseded
}

# All recovery uses the original build ID. Never submit a replacement build
# because a local watcher or GitHub write failed. Every external recovery call
# has a deadline, and one failed status write must not strand other contexts.
bounded() {
    python3 -I -c '
import subprocess, sys
try:
    sys.exit(subprocess.run(sys.argv[1:], timeout=30).returncode)
except subprocess.TimeoutExpired:
    sys.exit(124)
' "$@"
}

errored=0
error_contexts() {
    local c failed=0
    errored=1
    for c in "${contexts[@]}"; do
        post "$c" error "cloud-build $build_id: watcher interrupted; rerun cloud-ci.sh --reconcile" "$url" || failed=1
    done
    return "$failed"
}

declare -A step_state
all_ok=1
map() { # context step
    local st="${step_state[$2]:-MISSING}"
    if [ "$st" = "SUCCESS" ]; then
        post "$1" success "cloud-build $build_id: $2 passed" "$url" || all_ok=0
    else
        all_ok=0
        post "$1" failure "cloud-build $build_id: $2 $st" "$url" || all_ok=0
    fi
    printf '  %-26s %s\n' "$1" "$st"
}

reconcile() {
    local report parsed id st status
    # Capture the producer status too: process substitution would discard a
    # failed describe and turn missing evidence into fabricated test failures.
    describe_cmd "$build_id"
    if ! report="$(bounded "${dcmd[@]}")" \
        || ! parsed="$(python3 -I -c '
import json, sys
try:
    data = json.load(sys.stdin)
    status = data["status"]
    steps = data["steps"]
    if not isinstance(status, str) or not isinstance(steps, list):
        raise ValueError
    seen = set()
    lines = [status]
    for step in steps:
        key, value = step["id"], step.get("status", "MISSING")
        if not isinstance(key, str) or not key or key in seen or not isinstance(value, str):
            raise ValueError
        if any(c in key + value for c in "\t\r\n"):
            raise ValueError
        seen.add(key)
        lines.append(key + "\t" + value)
    if any(c in status for c in "\t\r\n"):
        raise ValueError
    print("\n".join(lines))
except (KeyError, TypeError, ValueError):
    sys.exit(1)
' <<< "$report")"; then
        error_contexts || true
        return 1
    fi
    status="${parsed%%$'\n'*}"
    case "$status" in
        SUCCESS|FAILURE|INTERNAL_ERROR|TIMEOUT|CANCELLED|EXPIRED) ;;
        *) error_contexts || true; return 1 ;;
    esac
    step_state=()
    while IFS=$'\t' read -r id st; do
        step_state["$id"]="$st"
    done <<< "${parsed#*$'\n'}"
    all_ok=1
    map "CI / gates" gates
    for i in 0 1 2 3; do map "CI / test (shard=$i)" "shard-$i"; done
    [ "$js" = "1" ] && map "CI / js" js
    [ "$native" = "1" ] && map "CI / desktop-core" native
    if [ "$all_ok" = "1" ] && [ "$status" = "SUCCESS" ] \
        && [ "${step_state[full-verification]:-MISSING}" = "SUCCESS" ]; then
        post "CI / full-verification" success "cloud-build $build_id: all required jobs passed" "$url" || return 1
        echo "  CI / full-verification     SUCCESS"
    else
        post "CI / full-verification" failure "cloud-build $build_id: $status" "$url" || true
        echo "  CI / full-verification     FAILURE"
        return 1
    fi
}

build_id=""
url=""
finalized=0
finish() {
    local result=$?
    trap - EXIT
    # Preserve the original failure/signal while making one bounded attempt to
    # settle every context. The remote build may continue after this process.
    trap '' INT TERM HUP
    if [ -n "$build_id" ] && [ "$finalized" = "0" ]; then
        # A signal can arrive just after submit assigns the ID, before the
        # next ordinary command has constructed the link.
        run_url "$build_id"
        if must_not_settle; then
            # Even an `error` must not overwrite a newer run's statuses.
            echo "Leaving the statuses on $sha unchanged." >&2
        else
            echo "Watcher exited; reconciling existing build $build_id (no resubmission)." >&2
            reconcile || true
        fi
        [ "$result" != "0" ] || result=1
    fi
    if [ "$errored" = "1" ]; then
        # The statuses say `error`, not the build's result. Re-attach to the
        # same build later instead of submitting (and billing) another one.
        echo "Statuses for $short are not final. Once build $build_id finishes, re-attach with:" >&2
        printf '  UA_CI_GCP_PROJECT=%q UA_CI_GCP_REGION=%q UA_CI_REPO=%q scripts/cloud-ci.sh --reconcile %q %q\n' \
            "$project" "$region" "$repo" "$build_id" "$sha" >&2
    fi
    [ -z "$src" ] || python3 -I -c 'import shutil,sys; shutil.rmtree(sys.argv[1])' "$src" || true
    exit "$result"
}

if [ -n "$attach" ]; then
    # names_build matches statuses against the attached build from here on.
    build_id="$attach"
    # Refuse before any status write: the build must have tested this head.
    # Prints the tested SHA, then the js and native legs the submitting run
    # recorded as build tags (`ua-ci-js-<0|1>`, `ua-ci-native-<0|1>`): 1 or 0,
    # `-` for a build submitted before the tags, `!` for a malformed record.
    describe_cmd "$attach"
    if ! report="$(bounded "${dcmd[@]}")" \
        || ! described="$(python3 -I -c '
import json, re, sys
data = json.load(sys.stdin)
value = data.get("substitutions", {}).get("_SOURCE_SHA", "")
ok = isinstance(value, str) and value and not any(c.isspace() for c in value)
tags = data.get("tags", [])
legs = {"js": set(), "native": set()}
for tag in tags if isinstance(tags, list) else []:
    m = re.fullmatch(r"ua-ci-(js|native)-(.*)", tag) if isinstance(tag, str) else None
    if m:
        legs[m[1]].add(m[2])
flags = ["!" if len(v) > 1 or v - {"0", "1"} else (next(iter(v)) if v else "-")
         for v in (legs["js"], legs["native"])]
if flags.count("-") == 1:
    flags = ["!", "!"]
print(value if ok else "-", *flags)
' <<< "$report")"; then
        echo "Could not read build $attach in $project/$region; nothing was posted." >&2
        exit 1
    fi
    read -r built js_leg native_leg <<< "$described"
    [ "$built" != "-" ] || built=""
    if [ "$built" != "$sha" ]; then
        echo "Refusing: build $attach tested _SOURCE_SHA=${built:-<none>}, not $sha. Nothing was posted." >&2
        exit 1
    fi
    if [ "$js_leg" = "!" ] || [ "$native_leg" = "!" ]; then
        echo "Refusing: build $attach has a malformed ua-ci-js/ua-ci-native classification tag." \
            "Nothing was posted." >&2
        exit 1
    fi
    if ! read_statuses; then
        echo "Could not list the statuses on $sha; nothing was posted." >&2
        exit 1
    fi
    if [ "$js_leg" = "-" ]; then
        # Submitted before the tags: settle an optional context only when a
        # status shows this build's run posted it, never assume it ran.
        js="$js_named"
        native="$native_named"
    else
        # The classification the submitting run recorded on the build, not
        # which statuses happen to name it (every write of a context can have
        # failed; Devin review), and not step presence: a skipped leg's step
        # runs and passes, so it proves nothing (review r2). A skipped leg
        # gets no status, as a fresh run posts none. Re-running the classifier
        # could disagree once origin/main has moved.
        js="$js_leg"
        native="$native_leg"
    fi
fi

contexts=("CI / gates" "CI / test (shard=0)" "CI / test (shard=1)" "CI / test (shard=2)" "CI / test (shard=3)")
[ "$js" = "1" ] && contexts+=("CI / js")
[ "$native" = "1" ] && contexts+=("CI / desktop-core")
contexts+=("CI / full-verification")

if [ -n "$attach" ] && superseded; then
    exit 1
fi

trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

if [ -n "$attach" ]; then
    build_id="$attach"
    run_url "$build_id"
    echo "re-attaching to build $build_id for $short (js=$js, native=$native); nothing is submitted"
    echo "$url"
else
# gcloud's generated ignore file excludes .git. This explicit empty policy
# includes the sanitized shallow metadata required by codegen's Git check.
printf '# Include the exact shallow source checkout, including Git metadata.\n' > "$src/.gcloudignore"
# The step script comes from this checkout, not the commit under test; older
# heads are supported only when they provide the classifier contract above.
cp "$root/deploy/cloudbuild/ci-step.sh" "$src/ci-step.sh"

# The newest status id before submitting: anything later from another build
# is a newer run (task #2211). Nothing is billed when it cannot be read.
if [ "${UA_CI_NO_STATUS:-0}" != "1" ]; then
    if ! read_statuses; then
        echo "Could not list the statuses on $sha; nothing was submitted." >&2
        exit 1
    fi
    baseline_id="$max_id"
fi
echo "cloud CI for $short (js=$js, native=$native) via $backend in $project"
# Record the classification on the build itself, as two tags in a copy of the
# config, so a --reconcile settles the same contexts (review r2). Fails before
# billing when the config's tags cannot be extended safely.
python3 -I - "$config" "$src/ci.yaml" "$js" "$native" <<'PY'
import sys
from pathlib import Path

source, target, js, native = sys.argv[1:]
lines = Path(source).read_text().split("\n")
if any("ua-ci-js-" in line or "ua-ci-native-" in line for line in lines):
    sys.exit("The CI config already carries a ua-ci-js/ua-ci-native tag.")
heads = [i for i, line in enumerate(lines) if line.startswith("tags:")]
tags = [f"- ua-ci-js-{js}", f"- ua-ci-native-{native}"]
if not heads:
    while lines and not lines[-1]:
        lines.pop()
    lines += ["tags:", *tags, ""]
elif (
    len(heads) == 1
    and lines[heads[0]].rstrip() == "tags:"
    and heads[0] + 1 < len(lines)
    and lines[heads[0] + 1].startswith("- ")
):
    lines[heads[0] + 1 : heads[0] + 1] = tags
else:
    sys.exit("The CI config's top-level tags are not a plain block list; nothing was submitted.")
Path(target).write_text("\n".join(lines))
PY
if [ "$backend" = "spot" ]; then
    build_id="$(python3 -I "$spot_ci" submit "$src" "$sha")"
else
    build_id="$(gcloud builds submit "$src" --project="$project" --region="$region" \
        --config="$src/ci.yaml" --substitutions="_SOURCE_SHA=$sha" --async --format='value(id)')"
fi
run_url "$build_id"
echo "build $build_id"
echo "$url"
# Before this run's first write: pending over a newer run's result would hide
# it (review round 4), so a listing that fails here stops the run too, with
# nothing posted (round 5). The build keeps running; `finish` prints the
# --reconcile command that settles it later without resubmitting.
if [ "${UA_CI_NO_STATUS:-0}" != "1" ]; then
    if ! read_statuses; then
        echo "Could not list the statuses on $sha before the first write; nothing was posted." >&2
        errored=1
        finalized=1
        exit 1
    fi
    if superseded; then
        finalized=1
        exit 1
    fi
fi
for c in "${contexts[@]}"; do post "$c" pending "cloud-build $build_id running" "$url"; done
fi

# Builds run one at a time here, so a submitted build can sit QUEUED behind
# another for a whole build. The run deadline starts when the build leaves the
# queue and outlasts ci.yaml's own `timeout`; queueing has its own bound (#2189).
# A failed or timed-out describe is not an interruption. `set -e` used to exit
# on the first one, and finish() then stamped every context "watcher
# interrupted" while the build kept running (a hung read of ~45s plus the
# recovery call's 30s bound, ~75s after the pending posts). Retry until the
# deadline. Tests pass UA_CI_QUEUE_DEADLINE_S=0 to take that path immediately.
status=""
queue_allow=15000
run_allow=11700
[[ "${UA_CI_QUEUE_DEADLINE_S:-}" =~ ^[0-9]+$ ]] && queue_allow=$UA_CI_QUEUE_DEADLINE_S
[[ "${UA_CI_RUN_DEADLINE_S:-}" =~ ^[0-9]+$ ]] && run_allow=$UA_CI_RUN_DEADLINE_S
queue_deadline=$((SECONDS + queue_allow))
run_deadline=""
while :; do
    # `||` keeps a failed describe from tripping `set -e`. The call stays a
    # single substitution so the subshell is replaced by python; a wrapper
    # around it would record the wrong pid for a later signal.
    describe_cmd "$build_id" status
    status="$(bounded "${dcmd[@]}")" || status=""
    case "$status" in
        SUCCESS|FAILURE|INTERNAL_ERROR|TIMEOUT|CANCELLED|EXPIRED) break ;;
        WORKING)
            [ -n "$run_deadline" ] || run_deadline=$((SECONDS + run_allow))
            limit=$run_deadline
            ;;
        # QUEUED, PENDING, STATUS_UNKNOWN, an empty read, or anything new: the
        # run clock starts only once the build is seen WORKING.
        *) limit=${run_deadline:-$queue_deadline} ;;
    esac
    if (( SECONDS >= limit )); then
        echo "Timed out waiting for build $build_id ($status); inspect it before retrying." >&2
        exit 1
    fi
    sleep 30
done
echo "build status: $status"

# A newer run may have started on this head while this build ran: a fresh run
# after a zombie watcher, or any run while a --reconcile waited (task #2211).
#
# Residual window: a newer run can still post between this check and the
# writes in reconcile below. GitHub commit statuses have no compare-and-set,
# so it cannot be closed from here; it is seconds wide (one describe and a few
# posts), against a whole build's duration before this check existed. A newer
# run that loses it posts its own final statuses after its build, which then
# win, since statuses are last-write-wins per context.
if must_not_settle; then
    finalized=1
    exit 1
fi

# Keep EXIT recovery armed throughout final describe/publication. A signal in
# either must still settle the remaining contexts. A completed failure report,
# however, is final and must not trigger an additional describe or status pass.
if reconcile; then
    finalized=1
else
    finalized=1
    exit 1
fi
