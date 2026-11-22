"""The coarse JWK fallback: over-redaction where the grammar decides nothing (task #2441).

``jwk.private_members`` reads private members through a grammar (the JSON
scan, ``oct_blocks``, ``yaml_jwk``), and every syntax it cannot read was a
leak until a new rule taught it that syntax. This module reads no syntax. In a
text window around a key-type marker it masks every long, mixed-alphabet
base64url run that no earlier rule reported. A newly reported syntax is
answered here, by over-redaction, instead of by a new grammar rule. The scope
and the stop rule are in ``docs/security.md`` (JWK redaction scope).

Marker. ``kty`` as a whole word (not a suffix of a longer word such as
``mykty``; ``jwk_kty`` counts), followed within ``MARKER_REACH`` characters by
``RSA``, ``EC``, ``OKP`` or ``oct`` as a whole word, in any case. The marker is
read on two views of the text. In both, ``\\uXXXX``, ``\\u{X}``, ``\\xXX`` and
``\\UXXXXXXXX`` escapes are decoded and an escaped line break with the
indentation after it is dropped. An identity escape (``\\k``) is its letter in
one view and a blank in the other (``\\nkty`` is a line break before ``kty``).
Quoting, tags, anchors, comments and separators between the name and the type
are not read: they are just characters inside the reach.

Run. A maximal ``[A-Za-z0-9_-]`` run of at least ``MIN_RUN`` characters holding
an upper-case letter, a lower-case letter and a digit, ``-`` or ``_``. The
characters of an escape the run starts on (``\\n``, ``\\u0041``) are not part
of it, so the escape survives masking.

Window. ``WINDOW`` characters before the marker's start and after its end.
Windows that touch are merged. Every run inside a window, or overlapping it,
is masked whole, minus the bytes already reported by an earlier rule.

Residue (task #2445). An object the JSON scan decided (a plain-string
``kty``) opens no window, but the scan can type an object and still miss a
private member whose name is spelled in a syntax it does not read (a computed
key, ``"d" = ...``, a pair array). Inside each such object, read in the
source text, every run is masked unless it is a plainly written public parameter (below).
A public JWKS stays readable; anything else in a typed object goes. Objects
decided inside an encoded string or a comment are left to their own scans.

What stays exactly as it was:

* A public parameter in a decided object, written plainly: a JSON or
  JavaScript member named ``n``, ``e``, ``x``, ``y``, ``crv``, ``kid``,
  ``alg``, ``use``, ``x5t``, ``x5t#S256``, ``x5u``, ``key_ops`` or ``x5c``
  whose value is one plain string (an array of them for ``x5c`` and
  ``key_ops``). This is an allowlist of public forms, not a grammar of private
  ones: a form it does not read is masked, never left visible. A member
  with a private name that the object's type makes public (``k`` in an RSA
  key) is kept too, because the JSON scan read it by name.
* A decided object's neighbours: a marker inside a decided object opens no
  window, so a public JWKS is never a reason to mask the text around it.
* Bytes an earlier rule reported: masked values never overlap.
* A double-quoted mapping key (``oct_blocks``' reading): a key spelled with
  key material stays, as it does in an oct block, so a JSON object keeps its
  keys.

Over-redaction is the point. Inside a window an ``n``, an ``x``, a ``kid``, a
request id or a twenty-character camelCase identifier with a digit goes too.
Limits: a value written inside a double-quoted key, a key with no readable marker (a type computed at runtime, split by
interpolation, or more than ``MARKER_REACH`` characters from ``kty``), a
private value further than ``WINDOW`` characters from its marker, or one shorter
than ``MIN_RUN`` or written in a single letter case is not masked here.

Cost. At most two escape-normalising passes, a marker scan of each view whose
lookahead is bounded by ``MARKER_REACH``, one run scan and a forward merge:
linear in the text.
"""

from __future__ import annotations

import re
from array import array
from bisect import bisect_right
from collections.abc import Sequence

from . import oct_blocks, yaml_jwk

# Characters each side of a marker: a 4096-bit RSA private JWK is about 3,200
# characters, and its members may come before or after ``kty``.
WINDOW = 4096
# Shortest run masked: a 16-byte oct key encodes to 22 characters; ordinary
# identifiers and hex ids below this length stay readable.
MIN_RUN = 20
# Characters between ``kty`` and its type: room for quotes, escapes, a tag, an
# anchor, a short comment and a separator, while prose further away is not read.
MARKER_REACH = 64

NAME = "coarse"

_ESCAPE = re.compile(
    r"(?<!\\)\\++(?:u\{0*+(?P<braced>[0-9a-fA-F]{1,6})\}|u(?P<u4>[0-9a-fA-F]{4})"
    r"|x(?P<x2>[0-9a-fA-F]{2})|U(?P<u8>[0-9a-fA-F]{8})"
    r"|(?P<identity>[A-Za-z])|(?:\r\n|[\r\n\u2028\u2029])[ \t]*+)"
)
_MARKER = re.compile(
    rf"(?<![A-Za-z0-9])kty(?![A-Za-z0-9])[\s\S]{{0,{MARKER_REACH}}}?"
    r"(?<![A-Za-z0-9])(?:rsa|ec|okp|oct)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_RUN = re.compile(rf"[A-Za-z0-9_-]{{{MIN_RUN},}}+")
_ESCAPE_HEAD = re.compile(r"u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|U[0-9a-fA-F]{8}|[\s\S]")
_UPPER = re.compile(r"[A-Z]")
_LOWER = re.compile(r"[a-z]")
_OTHER = re.compile(r"[0-9_-]")


def _normalised(text: str, identity: bool) -> tuple[str, array]:
    """``text`` with letter escapes decoded, and each character's source offset.

    An identity escape (``\\k``) is its letter when ``identity`` is set and a
    blank otherwise: ``\\k\\t\\y`` spells ``kty`` in JavaScript, while
    ``\\nkty`` is a line break before ``kty``. The caller reads both views.
    """
    pieces: list[str] = []
    offsets = array("q")
    position = 0
    for match in _ESCAPE.finditer(text):
        start = match.start()
        pieces.append(text[position:start])
        offsets.extend(range(position, start))
        position = match.end()
        digits = match.group("braced") or match.group("u4") or match.group("x2")
        digits = digits or match.group("u8")
        if digits:
            code = int(digits, 16)
            char = chr(code) if code <= 0x10FFFF else " "
        elif match.group("identity"):
            char = match.group("identity") if identity else " "
        else:
            continue  # an escaped line break and its indentation: dropped
        pieces.append(char if char.isascii() and char.isalnum() else " ")
        offsets.append(start)
    pieces.append(text[position:])
    offsets.extend(range(position, len(text)))
    return "".join(pieces), offsets


def _markers(text: str) -> list[tuple[int, int]]:
    """Source extents of every marker, read on both escape views."""
    if "\\" not in text:
        return [match.span() for match in _MARKER.finditer(text)]
    found = []
    for identity in (True, False):
        view, offsets = _normalised(text, identity)
        for match in _MARKER.finditer(view):
            found.append((offsets[match.start()], offsets[match.end() - 1] + 1))
    found.sort()
    return found


def _windows(text: str, decided: oct_blocks._Spans) -> list[tuple[int, int]]:
    windows: list[tuple[int, int]] = []
    for start, end in _markers(text):
        if decided.contains(start):
            continue  # the grammar typed this key: its own scan decides it
        low, high = max(0, start - WINDOW), min(len(text), end + WINDOW)
        if windows and low <= windows[-1][1]:
            windows[-1] = (windows[-1][0], max(windows[-1][1], high))
        else:
            windows.append((low, high))
    return windows


def _run_start(text: str, start: int) -> int:
    """Skip the head of an escape the run begins inside (``\\n``, ``\\u0041``)."""
    if start and text[start - 1] == "\\":
        head = _ESCAPE_HEAD.match(text, start)
        return head.end() if head else start
    return start


def _random_looking(run: str) -> bool:
    return bool(_UPPER.search(run) and _LOWER.search(run) and _OTHER.search(run))


def _runs(text: str, low: int, high: int) -> list[tuple[str, int, int]]:
    pieces = []
    for match in _RUN.finditer(text, low, high):
        start = _run_start(text, match.start())
        if match.end() - start >= MIN_RUN and _random_looking(text[start : match.end()]):
            pieces.append((NAME, start, match.end()))
    return pieces


def _merged(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for low, high in sorted(spans):
        if merged and low <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], high))
        else:
            merged.append((low, high))
    return merged


_PUBLIC_NAME = r"(?:n|e|x|y|crv|kid|alg|use|x5t|x5t#S256|x5u)"
_LIST_NAME = r"(?:x5c|key_ops)"
_PLAIN = r"(?:\"[^\"\\\n]*\"|'[^'\\\n]*')"
_PUBLIC_VALUE = re.compile(
    rf"(?<![\w$#\"'-])(?:\"{_PUBLIC_NAME}\"|'{_PUBLIC_NAME}'|{_PUBLIC_NAME}(?![\w#-]))"
    rf"[ \t]*:[ \t\r\n]*(?P<value>{_PLAIN})"
    rf"|(?<![\w$#\"'-])(?:\"{_LIST_NAME}\"|'{_LIST_NAME}'|{_LIST_NAME}(?![\w-]))"
    rf"[ \t]*:[ \t\r\n]*(?P<list>\[(?:[ \t\r\n]*{_PLAIN}[ \t\r\n]*,?)*+[ \t\r\n]*\])"
)


def public_values(text: str, residue: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Spans of plainly written public parameters inside ``residue`` objects."""
    spans = []
    for low, high in residue:
        for match in _PUBLIC_VALUE.finditer(text, low, high):
            group = "value" if match.group("value") is not None else "list"
            spans.append(match.span(group))
    return spans


def residue_objects(
    text: str, decided: list[tuple[int, int]], decoded: Sequence[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Decided objects read in the source text: an opening brace, not an encoded string."""
    skip = set(decoded)
    return _merged(
        [
            (low, high)
            for low, high in decided
            if (low, high) not in skip and text[low : low + 1] == "{"
        ]
    )


def coarse_pieces(
    text: str,
    decided: list[tuple[int, int]],
    reported: list[tuple[int, int]],
    residue: Sequence[tuple[int, int]] = (),
    public: Sequence[tuple[int, int]] = (),
) -> list[tuple[str, int, int]]:
    """Runs to mask: near a marker outside ``decided``, and inside ``residue`` objects.

    Window runs skip ``decided`` objects; residue runs skip ``public`` values.
    Both skip ``reported`` bytes and double-quoted mapping keys.
    """
    decided_spans = oct_blocks._Spans(decided)
    windows = _windows(text, decided_spans)
    near: list[tuple[str, int, int]] = []
    if windows:
        starts = [low for low, _high in windows]
        for match in _RUN.finditer(text):
            index = bisect_right(starts, match.end() - 1) - 1
            if index < 0 or windows[index][1] <= match.start():
                continue
            start = _run_start(text, match.start())
            if match.end() - start >= MIN_RUN and _random_looking(text[start : match.end()]):
                near.append((NAME, start, match.end()))
    inside = [piece for low, high in residue for piece in _runs(text, low, high)]
    if not near and not inside:
        return []
    # A double-quoted mapping key is kept whole, as in an oct block: JSON stays JSON.
    keys = oct_blocks._quote_context(text)[1]
    pieces = yaml_jwk.subtract_named(near, oct_blocks._Spans(decided + list(reported) + keys))
    if inside:
        cut = oct_blocks._Spans(list(public) + list(reported) + keys)
        pieces = sorted(pieces + yaml_jwk.subtract_named(inside, cut), key=lambda p: (p[1], -p[2]))
    return pieces


def members(
    text: str,
    decided: list[tuple[int, int]],
    found: list,
    decoded: Sequence[tuple[int, int]] = (),
    public: Sequence = (),
) -> list:
    """``coarse_pieces`` as ``jwk.PrivateMember`` values, each replaced by the bare marker."""
    from .jwk import PrivateMember  # jwk imports this module

    residue = residue_objects(text, decided, decoded)
    pieces = coarse_pieces(
        text,
        decided,
        [(member.start, member.end) for member in found],
        residue,
        public_values(text, residue) + [(member.start, member.end) for member in public],
    )
    return [PrivateMember(name, text[s:e], s, e, inline=True) for name, s, e in pieces]
