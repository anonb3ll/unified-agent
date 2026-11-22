"""Privacy-bounded redaction and string bounding for telemetry spans (Task #480)."""

from __future__ import annotations

import bisect
import json
import re
from typing import Any

from . import jwk

BOUNDED_STRING_LIMIT = 500
BOUNDED_PAYLOAD_LIMIT = 8192

_SECRET_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z-_]{16,}"),
    re.compile(r"sk-[a-zA-Z0-9_-]{16,}"),
    re.compile(r"Bearer\s+[a-zA-Z0-9_\-\.]{16,}", re.IGNORECASE),
    re.compile(r"ghp_[a-zA-Z0-9]{20,}"),
    re.compile(r"xox[baprs]-[0-9a-zA-Z]{10,48}"),
    re.compile(
        r"(?:api_key|apikey|secret|password|token)\s*[:=]\s*['\"]?([a-zA-Z0-9_\-]{8,})['\"]?",
        re.IGNORECASE,
    ),
]

# Multiline PEM private-key blocks. Shared with tools/gcloud.py (instance
# metadata scrubbing) and tools/public_relay.py (public-reply gating).
PEM_PRIVATE_KEY_BLOCK_RE = re.compile(
    # The body stops at the next BEGIN, so a header with no END cannot make
    # each scan run to the end of the text (quadratic on many headers).
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----(?:(?!-----BEGIN )[\s\S])*?"
    r"-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----",
    re.IGNORECASE,
)
# Building blocks for scanning key material that has lost its END line, in
# whatever encoding it reached us (task #1994).
#
# A line break: CR or LF (CRLF is two), a JSON escape of either at any depth of
# escaping (``\n``, ``\\n``, ``\u000a``), an HTML break (``<br>``, ``&#10;``),
# or the ``', '`` between the items of a Python repr list. (Not JSON's
# ``", "``: that would carry the scan into the next JSON member.)
_KEY_BREAK = (
    r"(?:[\r\n]"
    r"|\\++(?:[nr]|u000[ad])"
    r"|<br[ \t]*+/?>"
    r"|&#(?:1[03]|x0*+[ad]);"
    r"|',[ \t]*+')"
)
# Between two pieces of key material: any run of line breaks and blanks. A run
# of spaces alone counts, so a key flattened onto one line is still followed.
_KEY_GAP = rf"(?:[ \t]|{_KEY_BREAK})++"
# One base64 character; a JSON encoder may write '/' as '\/'.
_KEY_B64 = r"(?:[A-Za-z0-9+/]|\\++/)"
# What may follow a piece of key material: a gap, a quote, a backslash or
# '<' (the end of an encoded string or an HTML tag), the end of the text, a
# truncation mark, or the END line.
_KEY_PIECE_END = r"(?=[ \t\r\n\"'\\<&]|$|\.\.\.|…|-----END)"
# A line break in an encoding (anything but a real CR or LF).
_KEY_ENCODED_BREAK = (
    r"(?:\\++(?:[nr]|u000[ad])"
    r"|<br[ \t]*+/?>"
    r"|&#(?:1[03]|x0*+[ad]);"
    r"|',[ \t]*+')"
)
# Where a piece of key material ends its line.
_KEY_LINE_END = rf"(?=[ \t]*+(?:[\r\n]|{_KEY_BREAK}|$|[\"']|\.\.\.|…|-----END))"
# Base64 lines that unmistakably continue a key body.
_KEY_LINE_START = (
    rf"{_KEY_B64}{{16,}}+={{0,2}}{_KEY_LINE_END}"
    rf"|{_KEY_B64}*+={{1,2}}{_KEY_LINE_END}"
)


def _key_field_value(next_piece: str) -> str:
    """The value of an armor or PuTTY header field: the rest of its line.

    It may hold '<', '&', '\\' or a quote (``Comment: <a@b> & co``), and it may
    hold text that only looks like an encoded line break, such as the ``\\n``
    of ``Comment: C:\\new-key`` in raw text. An encoded break ends the value
    only when what follows it starts another piece of the key (``next_piece``:
    a header field, a base64 line, the END line); otherwise it is read as part
    of the value. A real CR or LF always ends it. So an ambiguous value never
    stops the scan short of the key body: when unsure, redact (PR #1146
    reviews). Each alternative consumes a run or one guarded character, and an
    encoded run is scanned at most twice, so it stays linear.
    """
    return (
        r"(?:[^\r\n\\<&']++"
        r"|\\++(?![nr]|u000[ad])"
        r"|<(?!br[ \t]*+/?>)"
        r"|&(?!#(?:1[03]|x0*+[ad]);)"
        r"|'(?!,[ \t]*+')"
        rf"|{_KEY_ENCODED_BREAK}(?:[ \t]|{_KEY_ENCODED_BREAK})*+(?!{next_piece}))*+"
    )


# The PEM and PGP armor header fields, and what may start the next PEM piece.
_PEM_FIELD = (
    r"(?:(?-i:[A-Z][A-Za-z-]*+)|Proc-Type|DEK-Info|Version|Comment|Hash|Charset|MessageID)"
    r"[ \t]*+:"
)
_PEM_FIELD_VALUE = _key_field_value(
    rf"(?:{_PEM_FIELD}|-----END|=[A-Za-z0-9+/]{{4}}{_KEY_LINE_END}|{_KEY_LINE_START})"
)
# A short unpadded last line: one to three base64 quads (a 64-character line
# leaves 4, 8 or 12), so a prose word of another length is not taken. A
# 4, 8 or 12 letter word that goes on to more text on its line ("That is all")
# opens prose: a key's last line ends its line, so that word is not taken.
_KEY_SHORT_LAST = (
    r"(?!(?-i:[A-Z]?[a-z]++)[ \t]++(?!-----END)\S)"
    rf"(?:{_KEY_B64}{{4}}){{1,3}}+"
)

# A private-key header whose END line is missing (cut text, a partial paste):
# the header plus every following piece that looks like key material: a PEM or
# PGP armor header field (capitalised, or one of the known names in any case),
# a base64 line (16+ characters, or padded with "="), a PGP checksum, or a short
# last line. The body may sit on the header's own line, be flattened onto one
# line, or be separated by any ``_KEY_BREAK``. A short plain word is not taken
# unless it directly follows an unpadded full line, where it is the key's last
# line; so ordinary text after a key is kept. Possessive quantifiers keep every
# scan linear.
PEM_PRIVATE_KEY_UNTERMINATED_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
    rf"(?:(?:{_KEY_GAP}|(?<=-----))"
    rf"(?:{_PEM_FIELD}{_PEM_FIELD_VALUE}"
    r"|=[A-Za-z0-9+/]{4}"  # a PGP checksum line (before the padded branch)
    # A full line; when unpadded, a short line right after it is its last line.
    rf"|{_KEY_B64}{{16,}}+(?:={{1,2}}|{_KEY_GAP}{_KEY_SHORT_LAST}{_KEY_PIECE_END})?"
    rf"|{_KEY_B64}*+={{1,2}}"
    # A short unpadded last line (or a mid-line cut) when the text ends there.
    rf"|{_KEY_B64}{{2,15}}+(?=[ \t]*+(?:$|[\"']|\.\.\.|…|(?:[ \t]|{_KEY_BREAK})*+-----END))"
    rf"){_KEY_PIECE_END})*",
    re.IGNORECASE,
)

# The fields of a PuTTY key file other than its closing MAC line.
_PPK_FIELD = r"(?:Encryption|Comment|Public-Lines|Private-Lines|Key-Derivation|Argon2-[A-Za-z]++)"
# The closing line: Private-MAC (versions 2 and 3) or Private-Hash (version 1).
_PPK_MAC = r"Private-(?:MAC|Hash)"
_PPK_FIELD_VALUE = _key_field_value(rf"(?:(?:{_PPK_FIELD}|{_PPK_MAC}):|{_KEY_LINE_START})")

# A PuTTY private key file (``.ppk``, versions 1-3): the header line, then its
# fields and the base64 lines of ``Public-Lines`` and ``Private-Lines``,
# through ``Private-MAC`` when present. Only the header is a credential shape
# below, so without this the private body would pass (task #1994). Like the
# PEM scan, a body line is a base64 run of 16+ characters or a padded one; a
# short unpadded line is taken only as the last line of a block, right before
# another field, the MAC, or the end of the text. So prose after a key is
# kept rather than read as key material (PR #1117 review).
PUTTY_PRIVATE_KEY_RE = re.compile(
    rf"PuTTY-User-Key-File-\d+:{_PPK_FIELD_VALUE}"
    rf"(?:{_KEY_GAP}"
    rf"(?:{_PPK_FIELD}:{_PPK_FIELD_VALUE}"
    # A full line; when unpadded, a short line right after it is its last line.
    rf"|{_KEY_B64}{{16,}}+(?:={{1,2}}|{_KEY_GAP}{_KEY_SHORT_LAST}{_KEY_PIECE_END})?"
    rf"|{_KEY_B64}*+={{1,2}}"
    rf"|{_KEY_B64}{{1,15}}+(?={_KEY_GAP}(?:{_PPK_FIELD}|{_PPK_MAC}):|[ \t]*+(?:$|[\"']))"
    rf"){_KEY_PIECE_END})*+"
    rf"(?:{_KEY_GAP}{_PPK_MAC}:[^\r\n\\<&\"']*+)?"
)

# Structural, fail-closed private-key regions (PR #1146 review round 3). The
# patterns above read key material piece by piece and must guess where a key
# ends; a header value can always be written to fool that guess. These rules do
# not parse the key at all. From a private-key opener:
# (a) when a closing marker follows later (an END line; for PuTTY a
#     Private-MAC/Private-Hash line that starts a line after the
#     Private-Lines line), everything through it is redacted;
# (b) otherwise, when the opener starts a string literal, everything to the end
#     of that string; else (raw text) everything to the end of the text.
# An opener starts a string only when a quote directly precedes it, possibly
# with escaped line breaks between (``"\n-----BEGIN``); a single quote after a
# word character is an apostrophe (Here's, user's), never a string (round 4).
# Over-redaction is accepted. Closing markers are found once and bisected, and
# each opener looks back a bounded distance, so it stays linear.
_KEY_OPENER_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|(?P<ppk>PuTTY-User-Key-File-\d+:)",
    re.IGNORECASE,
)
_PEM_END_RE = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----", re.IGNORECASE)
# The start of a line: the text start, a real break, or an encoded one.
_LINE_START = (
    r"(?:\A|(?<=[\r\n])|(?<=\\[nr])|(?<=\\u000[ad])|(?<=<br>)|(?<=<br/>)"
    r"|(?<=<br />)|(?<=&#10;)|(?<=&#13;))"
)
_PPK_PRIVATE_LINES_RE = re.compile(_LINE_START + r"Private-Lines:")
_PPK_CLOSE_RE = re.compile(_LINE_START + r"Private-(?:MAC|Hash):[ \t]*+[0-9A-Fa-f]*+")
# A quote that opens the string holding an opener, then escaped breaks only.
_STRING_OPENER_RE = re.compile(r"""(?:(?<!\w)(')|("))(?:\\++(?:r|n|u000[ad]))*+\Z""")
_STRING_OPENER_LOOKBACK = 256
_STRING_CLOSE_RE = {
    '"': re.compile(r'(?:[^"\\]++|\\[\s\S]?)*+"'),
    "'": re.compile(r"(?:[^'\\]++|\\[\s\S]?)*+'"),
}


def _next_after(ends: list[tuple[int, int]], position: int) -> int | None:
    """The end of the first marker that starts at or after ``position``."""
    index = bisect.bisect_left(ends, (position, -1))
    return ends[index][1] if index < len(ends) else None


def private_key_regions(text: str) -> list[tuple[int, int]]:
    """Spans to redact for every private-key opener in ``text`` (rules above)."""
    openers = list(_KEY_OPENER_RE.finditer(text))
    if not openers:
        return []
    pem_ends = [m.span() for m in _PEM_END_RE.finditer(text)]
    ppk_lines = [m.span() for m in _PPK_PRIVATE_LINES_RE.finditer(text)]
    ppk_ends = [m.span() for m in _PPK_CLOSE_RE.finditer(text)]
    regions: list[tuple[int, int]] = []
    covered = 0  # openers before this are inside a region already
    for opener in openers:
        start = opener.start()
        if start < covered:
            continue
        if opener.group("ppk"):
            private_lines = _next_after(ppk_lines, opener.end())
            close = None if private_lines is None else _next_after(ppk_ends, private_lines)
        else:
            close = _next_after(pem_ends, opener.end())
        if close is None:
            quote = _STRING_OPENER_RE.search(text, max(0, start - _STRING_OPENER_LOOKBACK), start)
            if quote is not None:
                string_end = _STRING_CLOSE_RE[quote.group(1) or quote.group(2)].match(
                    text, opener.end()
                )
                close = string_end.end() - 1 if string_end else None
        end = len(text) if close is None else close
        regions.append((start, end))
        covered = end
    return regions


def redact_private_keys(text: str) -> str:
    """Remove private keys whole: structural regions plus every key pattern.

    The union of :func:`private_key_regions` and the terminated, unterminated
    and PuTTY patterns, each found on the original text, so one rule ending
    early never shortens another.
    """
    spans = private_key_regions(text)
    for pattern in (
        PEM_PRIVATE_KEY_BLOCK_RE,
        PEM_PRIVATE_KEY_UNTERMINATED_RE,
        PUTTY_PRIVATE_KEY_RE,
    ):
        spans.extend(m.span() for m in pattern.finditer(text) if m.end() > m.start())
    if not spans:
        return text
    spans.sort()
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    pieces: list[str] = []
    position = 0
    for start, end in merged:
        pieces.append(text[position:start])
        pieces.append("[REDACTED_SECRET]")
        position = end
    pieces.append(text[position:])
    return "".join(pieces)


# The private members of a JSON Web Key (RFC 7518 6.2.2, 6.3.2 and 6.4.1): the
# RSA private exponent and CRT values, the EC/OKP private scalar ``d`` and the
# symmetric key value ``k``. Only the value is replaced, so the JSON stays
# readable. A value must be 40+
# base64url characters (a P-256 scalar is 43) with at least one upper-case
# letter and at least one digit, '-' or '_': random base64url of that length
# all but always has both, while an ordinary ``"q": "..."`` member, a
# kebab-case slug or camel-case text lacks one (PR #1117 and #1144 reviews).
# This is the fallback for text with no JWK context (a key cut before its
# ``kty``); ``jwk.private_members`` handles keys whose ``kty`` it can see.
# The value may end at its closing quote or, cut short, at a truncation mark
# or the end of the text. Quotes may be escaped, as in JSON
# inside JSON; a match starts only at the first backslash of a run, so a long
# run is scanned once.
JWK_PRIVATE_MEMBER_RE = re.compile(
    r"(?P<name>(?<![A-Za-z0-9_\\])\\*+[\"'](?:d|p|q|dp|dq|qi|k)\\*+[\"']"
    r"[ \t\r\n]*+:[ \t\r\n]*+\\*+[\"'])"
    r"(?=[a-z0-9_+/-]*+[A-Z])"
    r"(?=[A-Za-z+/]*+[0-9_-])"
    r"[A-Za-z0-9_+/-]{40,}+={0,2}"
    r"(?=\\*+[\"']|\.\.\.|…|$)"
)
# Shell-style credential assignments (e.g. DB_PASS=...). Shared like the above.
ENV_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:[A-Z_]*(?:PASSWORD|PASS|PWD|SECRET|TOKEN)|API_?KEY)\s*=\s*['\"]?[^\s'\"#]{4,}",
    re.IGNORECASE,
)


# Credential shapes beyond the original telemetry list (security review of
# PR #953). Named, so scanners can report which shape matched. Used by
# redact_sensitive_content, the audit engine's check_secrets and the public
# relay. A ``val`` group, where present, names the credential part of the match.
CREDENTIAL_SHAPE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GitHub fine-grained token", re.compile(r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{20,}")),
    (
        "GitHub token",
        re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{36,}(?![A-Za-z0-9])"),
    ),
    ("AWS access key id", re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])")),
    (
        "AWS secret access key",
        re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*['\"]?(?P<val>[^\s'\"]{8,})['\"]?"),
    ),
    (
        "credential in URL",
        # The scheme is bounded and every part possessive (task #1994): an
        # unbounded scheme rescanned a long ``a-a-a-...`` run from each word
        # boundary in it, quadratic in the run (28 s on 100 KB).
        # No word-boundary anchor: with the bounded scheme it made a 33+
        # character scheme (or one glued to a word) unmatchable; each start
        # still scans at most 32 characters, so the pattern stays linear.
        re.compile(r"[a-z][a-z0-9+.-]{0,31}+://[^\s/:@]++:(?P<val>[^\s/@]++)@", re.IGNORECASE),
    ),
    (
        # Signed or tokenised URLs carry the credential in the query string
        # (presigned S3/GCS/Azure links, ``?access_token=``, ``?sig=``); the
        # userinfo rule above never sees them. The match is the parameter, so a
        # redacted URL keeps its host and path.
        "credential in URL query",
        re.compile(
            r"(?<=[?&;])(?:x-amz-(?:signature|credential|security-token)"
            r"|x-goog-(?:signature|credential)|x-ms-signature"
            r"|(?:access|refresh|id|auth|session)[_-]?token|api[_-]?key|client[_-]?secret"
            r"|signature|sig|token|secret|password|passwd|authorization|auth|jwt)"
            r"=(?P<val>[^\s&#'\"<>]{6,})",
            re.IGNORECASE,
        ),
    ),
    (
        "JSON web token",
        re.compile(
            r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
        ),
    ),
    ("Stripe key", re.compile(r"(?<![A-Za-z0-9])[sr]k_(?:live|test)_[A-Za-z0-9]{10,}")),
    ("Slack app token", re.compile(r"(?<![A-Za-z0-9])xapp-[0-9A-Za-z-]{10,}")),
    ("Slack webhook", re.compile(r"hooks\.slack\.com/services/[A-Za-z0-9_/-]{8,}")),
    ("Google OAuth token", re.compile(r"(?<![A-Za-z0-9])ya29\.[0-9A-Za-z_-]{10,}")),
    (
        "HTTP Basic credentials",
        # A digit or base64 punctuation is required, so "Basic authentication"
        # in prose is not a credential.
        re.compile(r"\bBasic (?=[A-Za-z0-9+/=]*[0-9+/=])(?P<val>[A-Za-z0-9+/]{12,}={0,2})"),
    ),
    (
        # Session-scoped hub MCP credential (task #1744): ``uas_`` plus 43
        # base64url characters (32 random bytes). Named so the pre-push audit
        # reports it and node events are scrubbed by the hub.
        "hub session credential",
        re.compile(r"(?<![A-Za-z0-9_-])uas_[A-Za-z0-9_-]{40,}(?![A-Za-z0-9_-])"),
    ),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY", re.IGNORECASE)),
    ("PuTTY private key", re.compile(r"PuTTY-User-Key-File-\d+:")),
)


#: A hub session credential in free text (task #1893): exactly ``uas_`` plus 43
#: base64url characters, as ``session_credentials`` mints them. No guard before
#: it, so a value glued after another word (``key_uas_...``) is caught; one after
#: it, so a longer identifier or base64 run that merely contains ``uas_`` is not.
#: Used where an agent writes shared text.
SESSION_CREDENTIAL_TEXT_RE = re.compile(r"uas_[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])")


def redact_session_credentials(text: str) -> tuple[str, int]:
    """Replace every hub session credential in ``text``; return it and the count."""
    if not text or "uas_" not in text:
        return text, 0
    return SESSION_CREDENTIAL_TEXT_RE.subn("[REDACTED_SECRET]", text)


def contains_session_credential(text: str) -> bool:
    return bool(text) and SESSION_CREDENTIAL_TEXT_RE.search(text) is not None


def secret_patterns() -> tuple[re.Pattern[str], ...]:
    """The telemetry secret patterns, for callers that scan rather than redact."""
    return tuple(_SECRET_PATTERNS)


# Key names that look like secrets — redact the value entirely at any nesting
# depth, regardless of whether the value matches a secret pattern.
_SENSITIVE_KEY_TOKENS = frozenset(
    {
        "password",
        "api_key",
        "apikey",
        "secret",
        "token",
        "authorization",
        "auth",
        "credential",
    }
)


def _key_looks_sensitive(key: str | None) -> bool:
    if not key:
        return False
    normalized = str(key).lower().replace("-", "_")
    if normalized in _SENSITIVE_KEY_TOKENS or normalized.replace("_", "") in {
        "apikey",
        "password",
        "secret",
        "token",
        "authorization",
        "auth",
        "credential",
    }:
        return True
    return any(part in _SENSITIVE_KEY_TOKENS for part in normalized.split("_") if part)


def _redact_jwk_members(text: str) -> str:
    """Replace each JWK private-member value, building the result in one pass.

    Splicing one member at a time re-copied the text per member, quadratic in
    a text of many small keys (PR #1146 review).
    """
    members = jwk.private_members(text)
    if not members:
        return text
    pieces: list[str] = []
    position = 0
    for member in members:
        if member.start < position:
            # Independent comment/snippet scans may partially overlap a live
            # private expression. The marker already emitted covers their union;
            # consume the newly covered tail as well instead of exposing it.
            position = max(position, member.end)
            continue
        pieces.append(text[position : member.start])
        # A value inside a string keeps the string's quotes (an oct-block value);
        # a whole member value gets quotes of its own.
        pieces.append("[REDACTED_SECRET]" if member.inline else '"[REDACTED_SECRET]"')
        position = member.end
    pieces.append(text[position:])
    return "".join(pieces)


def redact_sensitive_content(text: str | None, max_len: int = BOUNDED_STRING_LIMIT) -> str:
    """Redact known API keys, tokens, and secrets, and truncate to max_len."""
    if not text:
        return ""
    redacted = str(text)
    # Context identifies keys regardless of alphabet or length, before bounding.
    redacted = _redact_jwk_members(redacted)
    # Whole private-key blocks first: the single-line "private key" shape
    # below rewrites only the BEGIN header, which would leave the key body
    # behind and stop the block pattern from ever matching (task #1983).
    # PuTTY keys too, and structurally (redact_private_keys): an ambiguous
    # header value cannot end a key early.
    redacted = redact_private_keys(redacted)
    redacted = JWK_PRIVATE_MEMBER_RE.sub(r"\g<name>[REDACTED_SECRET]", redacted)
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    for _name, pattern in CREDENTIAL_SHAPE_PATTERNS:
        redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    if len(redacted) > max_len:
        redacted = redacted[: max_len - 3] + "..."
    return redacted


def sanitize_metadata(
    meta: dict[str, Any] | None, max_payload_bytes: int = BOUNDED_PAYLOAD_LIMIT
) -> dict[str, Any]:
    """Sanitize metadata dictionary, redacting strings and bounding overall payload size."""
    if not meta:
        return {}
    kty = meta.get("kty")
    out = {
        str(k)[:64]: (
            "[REDACTED_SECRET]" if jwk.is_private_member(kty, k) else _sanitize_value(v, key=str(k))
        )
        for k, v in list(meta.items())[:50]
    }

    # Check payload byte size
    while len(json.dumps(out).encode("utf-8")) > max_payload_bytes and len(out) > 1:
        out.pop(next(reversed(out)))
    if len(json.dumps(out).encode("utf-8")) > max_payload_bytes:
        out = {"_truncated": "telemetry metadata exceeded the payload limit"}
    return out


def _sanitize_value(value: Any, depth: int = 0, key: str | None = None) -> Any:
    """Recursively redact metadata while bounding nested containers."""
    if _key_looks_sensitive(key):
        return "[REDACTED_SECRET]"
    if isinstance(value, str):
        return redact_sensitive_content(value, max_len=200 if depth == 0 else 100)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if depth >= 3:
        return redact_sensitive_content(str(value), max_len=100)
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item, depth + 1) for item in value[:10]]
    if isinstance(value, dict):
        return _sanitize_dict(value, depth)
    return redact_sensitive_content(str(value), max_len=100)


def _sanitize_dict(value: dict[Any, Any], depth: int, limit: int = 10) -> dict[str, Any]:
    """Sanitize a mapping; a structured JWK has its private members withheld.

    Members are sanitized one at a time, so the private ``d`` of a JWK held as
    a dict never meets the contextual scan that catches it in text (PR #1146
    review). Its ``kty`` decides, read before the item limit applies.
    """
    kty = value.get("kty")
    return {
        str(k)[:32]: (
            "[REDACTED_SECRET]"
            if jwk.is_private_member(kty, k)
            else _sanitize_value(item, depth + 1, key=str(k))
        )
        for k, item in list(value.items())[:limit]
    }


#: Bounds of :func:`redacted_preview`. Generous: an approval payload is a tool's
#: own arguments, and the person approving must see all of them.
PREVIEW_MAX_DEPTH = 12
PREVIEW_MAX_ITEMS = 1_000
PREVIEW_MAX_STRING = 20_000
#: One budget for the whole answer: the preview and its omission metadata together.
PREVIEW_MAX_BYTES = 1_000_000
#: Omission records kept in a preview's metadata (the total is reported too).
PREVIEW_MAX_OMISSIONS = 50
#: Longest omission path, and longest key segment inside one.
PREVIEW_MAX_PATH = 256
PREVIEW_MAX_PATH_KEY = 64
#: Largest serialized omission record; the metadata reserve is this times the cap.
_OMISSION_RECORD_MAX = 400
#: What stands in for a value the preview leaves out. Never the value's own text.
PREVIEW_OMITTED = "[OMITTED]"


def _bounded_path(path: str) -> str:
    if len(path) <= PREVIEW_MAX_PATH:
        return path
    return path[: PREVIEW_MAX_PATH - 1] + "\u2026"


def redacted_preview(value: Any) -> tuple[Any, dict[str, Any]]:
    """A redacted copy of ``value`` and an account of everything it leaves out.

    Unlike :func:`sanitize_metadata`, which silently keeps ten items per list, ten
    keys per mapping and three levels, this keeps everything up to generous bounds
    and records each cut: ``{complete, omissions: [{path, kind, omitted}],
    omissions_total, redacted}``. ``kind`` is ``items`` (list items or mapping
    keys past ``PREVIEW_MAX_ITEMS``), ``depth`` (a container below
    ``PREVIEW_MAX_DEPTH``), ``chars`` (a string past ``PREVIEW_MAX_STRING``) or
    ``bytes`` (the rest of a container, or a string, once the size budget ran out).
    A value left out is replaced by ``PREVIEW_OMITTED``; the text of an unwalked
    container is never emitted, so nothing escapes key-based redaction.

    One budget, ``PREVIEW_MAX_BYTES``, covers the serialized preview and its
    metadata together, enforced during traversal: the metadata's share is reserved
    up front (at most ``PREVIEW_MAX_OMISSIONS`` records of bounded size, each path
    at most ``PREVIEW_MAX_PATH`` characters with key segments at most
    ``PREVIEW_MAX_PATH_KEY``) and the preview spends the rest. Redaction is not an
    omission: a secret-shaped key or value becomes ``[REDACTED_SECRET]`` and is
    counted in ``redacted``. Paths are ``$`` then ``.key`` or ``[index]``.
    """
    omissions: list[dict[str, Any]] = []
    total = 0
    redacted = 0
    # Metadata reserve: the records plus the fixed fields around them.
    budget = PREVIEW_MAX_BYTES - PREVIEW_MAX_OMISSIONS * _OMISSION_RECORD_MAX - 512
    used = 0

    def cut(path: str, kind: str, omitted: int) -> None:
        nonlocal total
        total += 1
        if len(omissions) >= PREVIEW_MAX_OMISSIONS:
            return
        record = {"path": _bounded_path(path), "kind": kind, "omitted": int(omitted)}
        while len(json.dumps(record)) > _OMISSION_RECORD_MAX:
            record["path"] = record["path"][: len(record["path"]) // 2] + "\u2026"
        omissions.append(record)

    def spend(cost: int) -> bool:
        nonlocal used
        if used + cost > budget:
            return False
        used += cost
        return True

    def segment(label: str) -> str:
        return (
            label if len(label) <= PREVIEW_MAX_PATH_KEY else label[:PREVIEW_MAX_PATH_KEY] + "\u2026"
        )

    def child_path(path: str, part: str) -> str:
        # Built bounded, so a deep chain of long keys never grows a long string.
        return path if len(path) > PREVIEW_MAX_PATH else path + part

    def leaf(item: Any, path: str) -> Any:
        if spend(len(json.dumps(item)) + 1):
            return item
        cut(path, "bytes", 1)
        spend(len(PREVIEW_OMITTED) + 3)
        return PREVIEW_OMITTED

    def text(raw: str, path: str) -> str:
        nonlocal redacted
        clean = redact_sensitive_content(raw, max_len=len(raw) * 4 + 64) if raw else ""
        if clean != raw:
            redacted += 1
        if len(clean) > PREVIEW_MAX_STRING:
            cut(path, "chars", len(clean) - PREVIEW_MAX_STRING)
            clean = clean[:PREVIEW_MAX_STRING]
        return leaf(clean, path)

    def walk(item: Any, path: str, depth: int, key: str | None) -> Any:
        nonlocal redacted
        if _key_looks_sensitive(key):
            redacted += 1
            return leaf("[REDACTED_SECRET]", path)
        if isinstance(item, str):
            return text(item, path)
        if isinstance(item, (int, float, bool)) or item is None:
            return leaf(item, path)
        if isinstance(item, (list, tuple, dict)):
            if depth >= PREVIEW_MAX_DEPTH:
                cut(path, "depth", 1)
                return leaf(PREVIEW_OMITTED, path)
            if not spend(2):
                cut(path, "bytes", 1)
                return PREVIEW_OMITTED
        if isinstance(item, (list, tuple)):
            out_list: list[Any] = []
            for i, child in enumerate(item[:PREVIEW_MAX_ITEMS]):
                if used >= budget:
                    cut(path, "bytes", len(item) - i)
                    return out_list
                out_list.append(walk(child, child_path(path, f"[{i}]"), depth + 1, None))
            if len(item) > PREVIEW_MAX_ITEMS:
                cut(path, "items", len(item) - PREVIEW_MAX_ITEMS)
            return out_list
        if isinstance(item, dict):
            kty = item.get("kty")
            entries = list(item.items())
            out: dict[str, Any] = {}
            for i, (name, child) in enumerate(entries[:PREVIEW_MAX_ITEMS]):
                label = str(name)
                if not spend(len(json.dumps(label)) + 2):
                    cut(path, "bytes", len(entries) - i)
                    return out
                sub = child_path(path, f".{segment(label)}")
                if jwk.is_private_member(kty, name):
                    redacted += 1
                    out[label] = leaf("[REDACTED_SECRET]", sub)
                else:
                    out[label] = walk(child, sub, depth + 1, label)
            if len(entries) > PREVIEW_MAX_ITEMS:
                cut(path, "items", len(entries) - PREVIEW_MAX_ITEMS)
            return out
        return text(str(item), path)

    preview = walk(value, "$", 0, None)
    return preview, {
        "complete": total == 0,
        "omissions": omissions,
        "omissions_total": total,
        "redacted": redacted,
    }
