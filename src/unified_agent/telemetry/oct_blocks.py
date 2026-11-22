"""Symmetric JWK secrets written outside a JSON object: the oct-block rule (task #2093).

``jwk.private_members`` reads a JWK only as a JSON, Python or JavaScript object
literal. A symmetric key written as Python keyword arguments (``JWK(...)``,
``dict(...)``) or as a YAML mapping has no braces to read, so its ``k`` passed
every redactor. This module does not parse those forms. It finds *oct blocks*
lexically and masks every value in them, which is deliberately simple and fails
toward masking.

Trigger. A ``kty`` name (any quoting, ``\\uXXXX``/``\\xXX``/``\\UXXXXXXXX``
escapes at any escaping depth, any case) followed by ``:``, ``=`` or ``=>``,
literal or escaped the same ways, whose value is ``oct`` (any quoting, escaping
or case). An explicit YAML key (``? kty`` with ``: oct`` on the next significant
line, across blank/comment-only separation lines) counts too. Scalar anchors
whose value spells ``kty`` also supply that name at alias-key occurrences,
including quoted, escaped, tagged and later-line scalar definitions. Escaped
physical continuations and their indentation may surround or split ``kty``
names, ``oct`` values and anchored ``kty`` spellings. Their right identifier
boundaries follow any removed continuations. Between the
separator and ``oct`` may stand blanks (any escape
that decodes to whitespace too, derived from ``str.isspace``), quotes,
opening brackets, encoded line breaks, string prefixes (``b'``), YAML
properties (``&anchor``, ``!tag``), block-scalar headers (``|``, ``>-``) and a
``-`` sequence marker. When the rest of the ``kty`` line is empty or a
comment, the value is read from the next lines instead: up to
``OCT_LOOKAHEAD`` significant lines, where blank and comment-only lines, however
many, do not count. A ``kty`` whose value is an alias (``*name``) or a property
longer than ``LONG_PROPERTY`` characters also triggers. A ``kty`` inside an
object the JSON scan decided (one with a plain-string ``kty``) opens no local
block: that object keeps the JSON path's behaviour, byte for byte. Its oct
type can still seed alias lookup outside the object for a YAML merge consumer.
A literal object unpacked with Python ``**`` also supplies an oct trigger at
the unpacking operator, protecting sibling arguments of the enclosing call.

Block. Around each trigger on its first key line L (using the opening quote's
line when the name begins with a continuation):

* If an unclosed bracket on line L encloses the ``kty`` (a call's argument
  list, a flow mapping, an object in a log line) and closes within the window,
  the block runs from that bracket to its close, to the close of the bracket
  around it (or, when there is none, the end of line L), and through the lines
  below L that are indented deeper than L.
* Otherwise the block is the line window: up to ``WINDOW_LINES`` significant
  lines above and below L, cut only at a document marker (``---``, ``...``) and
  at a line indented less than L (by L's own indentation, so the lines of a
  ``- `` sequence item or a ``? `` explicit key stay in, including a key whose
  marker carries tags, anchors or a block-scalar header on property-only lines).
  Blank and comment-only
  lines neither count, cut nor end a block: a YAML mapping spans blank lines.
  Continuation lines within the key name belong to that one token; the lower
  window starts at the token's final physical line.
  An enclosing bracket opened on an earlier line inside the window, or one that
  does not close inside it, adds its extent to the line window.

Masking. Inside a block every value is masked: each run of characters that is
not structure is replaced, except a member name (a word at the start of a line,
after ``,``/``;``/an opening bracket/a quote, followed by ``:``, ``=`` or
``=>``) and the word ``oct`` itself. After a separator everything up to the
next ``,``, ``;``, bracket or real line break is the value: nothing in it is a
member name, and escapes are value characters (``\\x41``, ``\\u0041``,
``\\\\``, and once a run has started even ``\\"`` and ``\\n``), so a value
written as escapes, or split by an escaped quote or line break, is masked whole.
Python ``**`` at an argument/member boundary is structural even when its
operand does not supply the oct type; the operand remains a maskable value.
Python literal prefixes are syntax too. Formatted/template literal bodies are
private values, replaced whole while their literal delimiters remain intact.
Outside those bodies, structure is never touched: real quotes, commas, semicolons, brackets and
document markers stay where they are, and an escaped quote or encoded line break is kept only where
no value run is in progress (between members), so JSON stays JSON. A masked
run inside a string is replaced by the bare marker; outside one, by a quoted
marker, so a masked JSON number or literal becomes a string. Objects the JSON
scan decided are cut out of every block.

Anchors. Every alias (``*name``) in a block, or in a document with an oct
trigger and a possible YAML merge key/tag, names anchors to mask: from each
definition of that name anywhere in the text (``&name``) to the end of its YAML
document (the next ``---`` or ``...`` line at column 0, or the end of the text),
every value is masked the same way, keys and structure kept. No node grammar is
read, so no layout of the anchored node (a quoted scalar continued at column 0,
a flow collection, a block collection starting on the next line, a comment or a
stray bracket inside it) can end the mask early, and a nested anchor is inside
its parent's span. Aliases inside those spans are followed in turn, for a few
rounds. An alias whose name has no definition, or a chain still growing after
the last round, masks from every anchor in the text to the end of its document.
An anchor on a comment-only line, or one at the very end of its document, is
not a definition.

Over-redaction is accepted and expected. Every value near a ``kty`` of ``oct``
is masked, not only ``k``: ``kid``, ``alg``, comments, a sibling sequence item,
the rest of a log line, a neighbouring child mapping, values across blank lines,
a commented-out ``kty``'s call, a function name before a call's bracket on a
line with no bracket around the ``kty``. A matching anchor used by an ordinary
alias elsewhere in the block is masked too, and an unknown alias masks every
anchored value in the text. An aliased anchor's mask runs to the end of its
document, so the values after the anchored node in that document go too: this
happens only in a document holding the anchor when the text has an oct block
with an alias, and other documents keep their text. This also covers YAML
merge consumers outside the original block: their source anchor precedes the
consumer, so its document-end mask covers every downstream merge chain. An
unrelated alias in that same merge-bearing document can also expand masking
to its source; no YAML node grammar is read to distinguish those aliases. Tag
directives are conservative merge evidence even before a document marker.
Ordinary unpacking/multiplication in source code alone does not expand lookup
beyond the original block.

Limits (not masked):

* A value more than ``WINDOW_LINES`` significant lines after the ``kty`` name,
  including the tail of a block scalar that runs past the window. (An aliased
  anchored node is masked to the end of its document, however long.)
* A ``kty`` written by implicit string concatenation (``'oc' 't'``), held in a
  variable or computed by an expression (``kty=g()``, an f-string), or whose
  ``oct`` sits more than ``OCT_LOOKAHEAD`` significant lines below it, opens no
  block. Nor does a longer name that ends in kty (``JWK_KTY``, ``mykty``,
  ``foo.kty``): deliberate, so ordinary identifiers do not open blocks.
* A ``kty`` inside an object the JSON scan decided opens no block: there only
  that scan's private members are masked. For the same reason an anchor inside
  such an object is not masked when an alias in a block names it. (An unquoted
  ``oct`` flow object nested there is typed by the JSON scan itself, which
  masks its ``k``, task #2200.)
* This rule reads only ``oct`` types. RSA, EC and OKP keys are the JSON scan's
  (object literals and YAML flow mappings, quoted or plain types) and
  ``yaml_jwk``'s (YAML block mappings); neither changes what an oct block
  masks, and a value they mask inside an oct block is reported once.
* Brackets are matched lexically, ignoring quotes. A closing bracket inside a
  string argument can end a call's block early; the rest of the ``kty`` line and
  the deeper lines below it are still masked.
* The text of a double-quoted string followed by ``:`` is a key (JSON, YAML)
  and is kept whole, so a JSON object keeps its keys.
* A padded base64 piece that starts a continuation line of a multi-line quoted
  value and ends in ``='`` reads as a keyword argument name.

Cost. Every step is a forward pass over the text (including comment-aware
quoted extents, computed before masking windows), a pass over its lines
(significant-line links), a constant number of passes over document markers
(for trigger scopes and, when aliases need them, O(1) cursor lookup per anchor),
or a walk of at most
``WINDOW_LINES`` significant lines per trigger line along those links, with
per-line results cached. A lookahead records its
answer at every line end it crosses before a significant line, so a run of
blank or comment lines is walked once however many kty lines precede it. URI
properties use one boundary index during trigger scanning. A run of properties
(tags, anchors, URIs, malformed or unfinished tags) is walked one property at a
time, and a walk records its end at every property it crossed, so triggers that
share the run never rescan its suffix (task #2203). No step rescans a
suffix. Memory is the text's line offsets and per-line facts (arrays), one
bracket stack, the temporary URI offsets/tails, and the masked spans.
"""

from __future__ import annotations

import ast
import bisect
import re
import warnings
from array import array
from collections.abc import Iterator
from heapq import merge
from typing import NamedTuple

WINDOW_LINES = 16
OCT_LOOKAHEAD = 3
LONG_PROPERTY = 256


class Masked(NamedTuple):
    start: int
    end: int
    inline: bool  # inside a string: the bare marker, not a quoted one
    name: str  # the member name the value follows, or "value"


def _letter(char: str) -> str:
    """One letter as itself in any case, or as a \\u, \\x or \\U escape."""
    lower, upper = char.lower(), char.upper()
    codes = "|".join(f"(?i:{code:02x})" for code in sorted({ord(lower), ord(upper)}))
    return rf"(?:[{lower}{upper}]|\\+(?:u00|x|U000000)(?:{codes}))"


# A separator, literal or escaped (\u003a, \x3d, at any depth); or, for an
# explicit YAML key (``? kty``), a line end: lookahead below crosses separation
# lines with the same memoized walk used for a value on a later line.
_LINE_CHARS = r"\r\n\v\f\x1c-\x1e\x85\u2028\u2029"
# Match str.splitlines(), also used by audit line numbers and suppression.
# CRLF is one physical break. Encoded escapes remain value characters.
_LINE_BREAK = rf"(?:\r\n|[{_LINE_CHARS}])"
# YAML removes an escaped physical break and following indentation inside a
# double-quoted scalar. A fixed three-letter spelling has only two gaps; each
# gap is consumed once, including repeated empty continuation lines.
_CONTINUATION = rf"(?:\\{_LINE_BREAK}[ \t]*+)*+"
_CONTINUATION_RUN = re.compile(rf"(?:\\{_LINE_BREAK}[ \t]*+)++")


def _yaml_spelled(word: str) -> str:
    return _CONTINUATION.join(_letter(char) for char in word)


_COLON, _EQUALS, _GREATER = _letter(":"), _letter("="), _letter(">")
_SEPARATOR = (
    rf"(?:{_EQUALS}{_GREATER}|{_COLON}(?!:)|{_EQUALS}(?![=])"
    rf"|(?=[{_LINE_CHARS}#]))"
)
# The kty name, not inside a longer name (mykty, foo.kty, C:\kty, JWK_KTY),
# though it may follow an encoded line break (\nkty); then its closing quote and
# separator. YAML can split the quoted name itself across physical lines.
_KTY_BOUNDARY = r"(?:(?<![\w.$\\])|(?<=\\[nrt]))"
_KTY_LEFT_BOUNDARY = re.compile(_KTY_BOUNDARY)
_KTY_RE = re.compile(
    _KTY_BOUNDARY
    + _yaml_spelled("kty")
    + _CONTINUATION
    + r"(?![\w$])(?:\\*+[\"'`])?[ \t]*+"
    + _SEPARATOR
)


def _escape_forms(code: int) -> str:
    """Every \\x, \\u and \\U spelling of one code point, hex in any case."""
    forms = [f"U(?i:{code:08x})"]
    if code < 0x10000:
        forms.append(f"u(?i:{code:04x})")
    if code < 0x100:
        forms.append(f"x(?i:{code:02x})")
    return "|".join(forms)


# Any escape that decodes to whitespace, at any escaping depth. Derived from
# str.isspace, whose code points all lie below U+3001.
_ESCAPED_SPACE = (
    r"\\++(?:"
    + "|".join(_escape_forms(code) for code in range(0x3001) if chr(code).isspace())
    + ")"
)
_PROPERTY_CHARS = r"[^\s,\[\]{}()]"
# What may stand between a separator and its value.
_SKIP_PLAIN = (
    r"(?:[ \t]++"
    r"|\\*+[\"'`]"
    r"|[(\[{]"
    r"|\\++[nrtfv]"
    rf"|\\{_LINE_BREAK}"
    rf"|{_ESCAPED_SPACE}"
    r"|(?<![\w])[bBuUrRfF]{1,2}(?=\\*+[\"'])"
    r"|[>|][+-]?[0-9]?(?!\S)"
    r"|-(?=[ \t])"
    r")"
)
_PROPERTY_SKIP = rf"[&!]{_PROPERTY_CHARS}{{0,{LONG_PROPERTY - 1}}}+(?!{_PROPERTY_CHARS})"
# _VALUE_HEAD is the reference rule. _ValueHeads walks the same repetition one
# property at a time with the same alternative priority, so triggers that share
# a run of properties (with or without URI tags) do not each rescan its suffix.
_SKIP = rf"(?:{_SKIP_PLAIN}|!<[^>\s]*+>|{_PROPERTY_SKIP})*+"
_VALUE_KIND = (
    rf"(?:(?P<oct>{_yaml_spelled('oct')}{_CONTINUATION}(?![\w$]))"
    + r"|(?P<alias>\*)"
    + rf"|(?P<long>[&!]{_PROPERTY_CHARS}{{{LONG_PROPERTY}}})"
    + r"|(?P<comment>\#)"
    + rf"|(?P<eol>[{_LINE_CHARS}]|\Z))?"
)
_VALUE_HEAD = re.compile(_SKIP + _VALUE_KIND)
_VALUE_TAIL = re.compile(_VALUE_KIND)
_PLAIN_SKIP = re.compile(_SKIP_PLAIN + "*+")
_PROPERTY_AT = re.compile(_PROPERTY_SKIP)
_URI_BOUNDARY = re.compile(r"!<|[>\s]")


class _ValueHeads:
    """URI boundaries and shared property tails, measured once per trigger scan.

    ``skip`` walks ``_SKIP`` one property (or URI) at a time. Each step starts
    at a *stop*: the walk's start or the end of a property or URI. The walk's
    end depends only on the stop it starts from, so a walk of more than one
    step records its end at every stop it crossed (task #2203). Many triggers
    can share one run of properties: in ``kty: !<kty: !<...`` the blank ends
    every URI, ``!<kty:`` is read as a tag, and each trigger's separator end is
    a stop of the walk before it. Without the record, each trigger walked the
    rest of the run again, and n triggers cost n squared.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.has_uri = "!<" in text
        self.uri_ends: array | None = None
        # One slot per position: 0 is unknown, else the walk's end plus one.
        # Allocated on the first walk of more than one step.
        self.tails: array | None = None
        self.comment_from = self.comment_eol = -1

    def _index_uris(self) -> array:
        # A failed URI can be arbitrarily long too. Zero means no '>' before
        # whitespace/EOF. Each nested opening shares its first lexical boundary;
        # this deliberately keeps the old malformed-tag interpretation.
        ends = array("q", [0]) * len(self.text)
        pending = array("q")
        for boundary in _URI_BOUNDARY.finditer(self.text):
            if boundary.group() == "!<":
                pending.append(boundary.start())
            else:
                if boundary.group() == ">":
                    for start in pending:
                        ends[start] = boundary.end()
                del pending[:]
        self.uri_ends = ends
        return ends

    def _step(self, stop: int) -> tuple[int, int]:
        """Blanks and the like from ``stop``, then the next stop (or -1 at the end).

        The alternatives of ``_SKIP_PLAIN`` and the properties start with
        disjoint characters, so this is ``_SKIP``'s possessive repetition with
        its alternative priority kept: a URI before a ``!`` tag.
        """
        plain = _PLAIN_SKIP.match(self.text, stop).end()
        if self.has_uri and self.text.startswith("!<", plain):
            ends = self.uri_ends if self.uri_ends is not None else self._index_uris()
            end = ends[plain]
            if end:
                return plain, end
        property_match = _PROPERTY_AT.match(self.text, plain)
        return plain, -1 if property_match is None else property_match.end()

    def _walk(self, stop: int) -> tuple[int, int]:
        """The walk's end from ``stop`` and how many stops it crossed to get there."""
        tails = self.tails
        steps = 0
        while True:
            if tails is not None and tails[stop]:
                return tails[stop] - 1, steps
            plain, following = self._step(stop)
            if following < 0:
                return plain, steps
            stop = following
            steps += 1

    def skip(self, position: int) -> int:
        """``_SKIP``'s end from ``position``, each stop of a shared run walked once."""
        end, steps = self._walk(position)
        if steps > 1:
            # A second pass records the end at every stop of this walk, up to
            # the first one already known. Each stop is recorded once in all,
            # and no list of stops is held while walking.
            if self.tails is None:
                self.tails = array("q", [0]) * (len(self.text) + 1)
            tails = self.tails
            stop = position
            while stop >= 0 and not tails[stop]:
                tails[stop] = end + 1
                stop = self._step(stop)[1]
        return end

    def match(self, position: int) -> re.Match[str]:
        return _VALUE_TAIL.match(self.text, self.skip(position))

    def key_scalar(self, position: int) -> tuple[int, bool]:
        """Scalar-anchor head with the original multiline/comment separation."""
        if not self.has_uri:
            head = _KEY_SCALAR_HEAD.match(self.text, position)
            return head.end(), head.lastgroup == "kty"
        while True:
            skipped = self.skip(position)
            line_end = skipped
            if self.text.startswith("#", skipped):
                if not self.comment_from <= skipped < self.comment_eol:
                    self.comment_from = skipped
                    self.comment_eol = _TO_EOL.match(self.text, skipped).end()
                line_end = self.comment_eol
            line_break = _BREAK.match(self.text, line_end)
            if line_break is None:
                # A comment without its required line break was lookahead,
                # not consumed by the original repeated separation group.
                position = skipped
                break
            position = line_break.end()
        head = _KEY_SCALAR_TAIL.match(self.text, position)
        return head.end(), head.lastgroup == "kty"


_BLANK_LINE = re.compile(rf"[ \t]*+(?:[{_LINE_CHARS}]|\Z)")
_COMMENT_LINE = re.compile(r"[ \t]*+\#")
_TO_EOL = re.compile(rf"[^{_LINE_CHARS}]*+")
_BREAK = re.compile(_LINE_BREAK)
_LINE_HEAD = re.compile(
    r"(?P<raw>[ \t]*+)(?P<marks>(?:[-?][ \t]++)*+)"
    rf"(?:(?P<blank>[{_LINE_CHARS}]|\Z)|(?P<comment>\#)"
    rf"|(?P<doc>(?:---|\.\.\.)(?=[ \t{_LINE_CHARS}]|\Z)))?"
)
_KEY_PROPERTY = (
    r"(?:!<[^>\s]*+>"
    rf"|[&!]{_PROPERTY_CHARS}++"
    r"|[>|](?:[1-9][+-]?|[+-]?[1-9]?)(?!\S))"
)
_KEY_PROPERTIES = rf"(?:[ \t]*+{_KEY_PROPERTY})*+[ \t]*+(?:\#[^{_LINE_CHARS}]*+)?"
_STANDALONE_KEY = re.compile(rf"[ \t]*+(?:-[ \t]++)*+\?{_KEY_PROPERTIES}")
_PROPERTY_LINE = re.compile(rf"[ \t]*+{_KEY_PROPERTY}{_KEY_PROPERTIES}")
_BRACKET = re.compile(r"[()\[\]{}]")
_BRACKET_TYPE = {"(": 0, "[": 1, "{": 2, ")": 0, "]": 1, "}": 2}

# Escapes are kept whole except at a real physical break: an odd backslash
# run with the character it escapes
# (and a \u, \U or \x code), or an even run with an inner-level escape letter.
# An even run never takes a quote: that quote is real.
_HEX_CODE = r"u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|x[0-9A-Fa-f]{2}"
_LEX = re.compile(
    rf"(?P<esc>(?:\\\\)*+\\(?:{_HEX_CODE}|[^{_LINE_CHARS}])?|(?:\\\\)++(?:{_HEX_CODE}|[nrtbf/])?)"
    rf"|(?P<nl>{_LINE_BREAK})"
    r"|(?P<quote>[\"'`])"
    r"|(?P<open>[(\[{])"
    r"|(?P<close>[)\]}])"
    r"|(?P<comma>[,;])"
    r"|(?P<space>[ \t]++)"
    r"|(?P<sep>=>|:(?!:)|=(?![=]))"
    rf"|(?P<word>[^\\{_LINE_CHARS}\"'`()\[\]{{}},; \t:=]++|[:=]++)"
)
_SPACES = re.compile(r"[ \t]*+")
_SEP_AT = re.compile(r"=>|:(?!:)|=(?![=])")
_ENCODED_BREAK_AT = re.compile(r"\\++(?:[nr]|u000[aAdD])")
_QUOTE_AT = re.compile(r"\\*+[\"'`]")
_KEY_COLON_AT = re.compile(r"[ \t]*+:(?!:)")
_BREAK_ESCAPE = re.compile(r"\\++(?:[nr\r\n]|\r\n|u000[aAdD])")
_ANCHOR_NAME = r"([^\s,\[\]{}()\"'`&*\\]++)"
_ALIAS_RE = re.compile(r"(?<![\w*&\\])\*" + _ANCHOR_NAME)
_ANCHOR_RE = re.compile(r"(?<![\w*&\\])&" + _ANCHOR_NAME)
# Scalar anchors used as mapping keys have no ':' after their definition.
# Resolve their names before considering alias occurrences as kty candidates.
_KEY_SCALAR_KIND = rf"(?:(?P<kty>{_yaml_spelled('kty')}{_CONTINUATION})(?![\w$]))?"
_KEY_SCALAR_TAIL = re.compile(_KEY_SCALAR_KIND)
_KEY_SCALAR_HEAD = re.compile(
    rf"(?:{_SKIP}(?:\#[^{_LINE_CHARS}]*+)?{_LINE_BREAK})*+" + _SKIP + _KEY_SCALAR_KIND
)
_KEY_ALIAS_SEPARATOR = re.compile(r"[ \t]*+" + _SEPARATOR)


# YAML merge evidence requires a merge key, not JavaScript unary operators.
def _tag_spelling(word: str) -> str:
    return "".join(rf"(?:{re.escape(char)}|(?i:%{ord(char):02x}))" for char in word)


_CORE_MERGE_TAG = rf"(?:!!{_tag_spelling('merge')}|!<{_tag_spelling('tag:yaml.org,2002:merge')}>)"
_MERGE_KEY_TAG = re.compile(
    rf"(?:\A|(?<=[{_LINE_CHARS}{{\[,]))[ \t]*+"
    rf"(?:-[ \t]++)*+(?P<explicit>\?[ \t]++)?"
    rf"(?:&[^\s,\[\]{{}}]++[ \t]++)?{_CORE_MERGE_TAG}"
    rf"(?=[ \t{_LINE_CHARS}]|\Z)"
)
_PLAIN_MERGE_KEY = re.compile(rf"<<[ \t]*+(?::|(?=[{_LINE_CHARS}#]))")
# An inline merge's value must start a mapping/sequence/alias, or continue
# on another line. Unary JavaScript expressions followed by another operand,
# a semicolon, or a conditional's ordinary scalar branch do not qualify.
_MERGE_VALUE = re.compile(r":[ \t]*+(?:\*|\[|\{|(?=\#|\Z))")


def _has_merge_hint(text: str, start: int, end: int) -> bool:
    if _PLAIN_MERGE_KEY.search(text, start, end):
        return True
    owner = False
    line_start = start
    breaks = iter(_BREAK.finditer(text, start, end))
    while line_start <= end:
        boundary = next(breaks, None)
        line_end = boundary.start() if boundary else end
        # These disjoint line scans run once per physical line. Avoid an
        # unbounded suffix lookahead for every potential tag on that line.
        last_value_colon = -1
        for separator in _MERGE_VALUE.finditer(text, line_start, line_end):
            last_value_colon = separator.start()
        for tag in _MERGE_KEY_TAG.finditer(text, line_start, line_end):
            if owner or tag.group("explicit") or tag.end() <= last_value_colon:
                return True
        if not (
            _BLANK_LINE.fullmatch(text, line_start, line_end)
            or _COMMENT_LINE.match(text, line_start, line_end)
        ):
            owner = bool(
                _STANDALONE_KEY.fullmatch(text, line_start, line_end)
                or (owner and _PROPERTY_LINE.fullmatch(text, line_start, line_end))
            )
        if boundary is None:
            break
        line_start = boundary.end()
    return False


_TAG_DIRECTIVE = re.compile(rf"(?:\A|(?<=[{_LINE_CHARS}]))%TAG[ \t]")
_ALIAS_ROUNDS = 4

_BLANK, _COMMENT, _DOC, _CODE = range(4)


class _Lines:
    """Line offsets of the text, with per-line facts computed once on demand."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.starts = array("q", [0])
        self.starts.extend(match.end() for match in _BREAK.finditer(text))
        self.count = len(self.starts)
        # Per-line facts, filled on demand: 9 bytes a line, not a dict entry.
        self._kind = array("b", [-1]) * self.count
        self._raw = array("i", [0]) * self.count
        self._indent = array("i", [0]) * self.count
        self._window: dict[int | tuple[int, int], tuple[int, int]] = {}
        self._deeper: dict[int, int] = {}
        self._next: array | None = None  # the next significant line after each line
        self._previous: array | None = None  # the previous one, or -1

    def line_of(self, position: int) -> int:
        return bisect.bisect_right(self.starts, position) - 1

    def end(self, line: int) -> int:
        """Where the line's content ends, before its line break."""
        if line + 1 >= self.count:
            return len(self.text)
        following = self.starts[line + 1]
        return following - 2 if self.text.startswith("\r\n", following - 2) else following - 1

    def info(self, line: int) -> tuple[int, int, int]:
        """(kind, raw indent, indent counting ``- `` and ``? `` markers)."""
        if self._kind[line] < 0:
            head = _LINE_HEAD.match(self.text, self.starts[line])
            raw, marks = len(head.group("raw")), len(head.group("marks"))
            if head.group("blank") is not None and not marks:
                kind = _BLANK
            elif head.group("comment") is not None and not marks:
                kind = _COMMENT
            elif head.group("doc") is not None and not raw and not marks:
                kind = _DOC
            else:
                kind = _CODE
            self._kind[line], self._raw[line], self._indent[line] = kind, raw, raw + marks
        return self._kind[line], self._raw[line], self._indent[line]

    def _link(self) -> None:
        """Significant-line links (code and document-marker lines), so a run of
        blank or comment lines is crossed in one step, however many walks cross
        it."""
        following = array("q", [self.count]) * self.count
        previous = array("q", [-1]) * self.count
        last = self.count
        for index in range(self.count - 1, -1, -1):
            following[index] = last
            if self.info(index)[0] not in (_BLANK, _COMMENT):
                last = index
        last = -1
        for index in range(self.count):
            previous[index] = last
            if self.info(index)[0] not in (_BLANK, _COMMENT):
                last = index
        self._next, self._previous = following, previous

    def window(self, line: int, through: int | None = None) -> tuple[int, int]:
        """The line window around a trigger line: (first line, last line)."""
        through = line if through is None else through
        key = line if through == line else (line, through)
        cached = self._window.get(key)
        if cached is not None:
            return cached
        # The trigger line's own indentation, markers not counted, so the lines of
        # an explicit key (``? kty``) or a sequence item stay in its block.
        indent = self.info(line)[1]
        if self._next is None:
            self._link()
        parent = self._previous[line]
        for _ in range(WINDOW_LINES):
            if parent < 0:
                break
            if _STANDALONE_KEY.fullmatch(self.text, self.starts[parent], self.end(parent)):
                # Tags, anchors and block-scalar headers can decorate the
                # standalone marker or precede the scalar on later lines.
                # The value indicator and siblings return to the '?' level.
                indent = min(indent, self.info(parent)[1])
                break
            if not _PROPERTY_LINE.fullmatch(self.text, self.starts[parent], self.end(parent)):
                break
            parent = self._previous[parent]
        top, seen, index = line, 0, self._previous[line]
        while index >= 0 and seen < WINDOW_LINES:
            kind, _raw, level = self.info(index)
            if kind == _DOC or level < indent:
                break
            top, seen, index = index, seen + 1, self._previous[index]
        # Physical continuation lines inside the key are one logical token.
        # The value's bounded window starts after that token, at its own end.
        bottom, seen, index = through, 0, self._next[through]
        while index < self.count and seen < WINDOW_LINES:
            kind, _raw, level = self.info(index)
            if kind == _DOC or level < indent:
                break
            bottom, seen, index = index, seen + 1, self._next[index]
        cached = self._window[key] = (top, bottom)
        return cached

    def deeper_end(self, line: int) -> int:
        """The last line of the run below ``line`` indented deeper than it, within
        ``WINDOW_LINES`` significant lines."""
        cached = self._deeper.get(line)
        if cached is not None:
            return cached
        indent = self.info(line)[2]
        if self._next is None:
            self._link()
        bottom, seen, index = line, 0, self._next[line]
        while index < self.count and seen < WINDOW_LINES:
            kind, _raw, level = self.info(index)
            if kind == _DOC or level <= indent:
                break
            bottom, seen, index = index, seen + 1, self._next[index]
        self._deeper[line] = bottom
        return bottom


class _Spans:
    """Sorted, disjoint spans with a membership test."""

    def __init__(self, spans: list[tuple[int, int]]) -> None:
        self.spans = _merge(spans)
        self.starts = [start for start, _end in self.spans]

    def contains(self, position: int) -> bool:
        index = bisect.bisect_right(self.starts, position) - 1
        return index >= 0 and position < self.spans[index][1]


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def _subtract(spans: list[tuple[int, int]], cut: _Spans) -> list[tuple[int, int]]:
    """``spans`` (sorted, disjoint) minus the spans in ``cut``."""
    pieces: list[tuple[int, int]] = []
    index = 0
    holes = cut.spans
    for start, end in spans:
        while index < len(holes) and holes[index][1] <= start:
            index += 1
        position, probe = start, index
        while probe < len(holes) and holes[probe][0] < end:
            if holes[probe][0] > position:
                pieces.append((position, holes[probe][0]))
            position = max(position, holes[probe][1])
            probe += 1
        if position < end:
            pieces.append((position, end))
    return pieces


def _kty_candidates(text: str) -> Iterator[tuple[int, int]]:
    """Name candidates with the left boundary checked before removed continuations.

    Walk continuation runs alongside the name matches. A logical prefix split
    from the candidate is still an identifier prefix; never walk backward over
    the same continuation suffix for each candidate or build another offset map.
    A leading continuation after an opening quote keeps that quote's line as
    the trigger, so the original key indentation bounds its block.
    """
    runs = iter(_CONTINUATION_RUN.finditer(text))
    following = next(runs, None)
    previous = None
    for match in _KTY_RE.finditer(text):
        start = match.start()
        while following is not None and following.end() <= start:
            previous = following
            following = next(runs, None)
        if previous is not None and previous.end() == start:
            if _KTY_LEFT_BOUNDARY.match(text, previous.start()) is None:
                continue
            if previous.start() and text[previous.start() - 1] == '"':
                start = previous.start() - 1
        yield start, match.end()


def _triggers(text: str, decided: _Spans) -> list[int]:
    """Positions of every kty whose value is oct (or an alias)."""
    found: list[int] = []
    comment_from = comment_eol = -1  # a comment already measured to its line end
    below: dict[int, bool] = {}  # lookahead results, by line end
    heads = _ValueHeads(text)
    key_anchors: set[str] = set()
    measured_to, key_scalar = -1, False
    for anchor in _ANCHOR_RE.finditer(text):
        if anchor.start() >= measured_to:
            measured_to, key_scalar = heads.key_scalar(anchor.end())
            # _SKIP may cross a run of properties. Anchors inside that same
            # run share the answer; never rescan its suffix for every anchor.
        if key_scalar:
            key_anchors.add(anchor.group(1))
    aliases = []
    if key_anchors:
        for alias in _ALIAS_RE.finditer(text):
            if alias.group(1) in key_anchors:
                separator = _KEY_ALIAS_SEPARATOR.match(text, alias.end())
                if separator:
                    aliases.append((alias.start(), separator.end()))
    candidates = merge(_kty_candidates(text), aliases)
    for start, end in candidates:
        if decided.contains(start):
            continue
        head = heads.match(end)
        kind = head.lastgroup
        if kind in ("oct", "alias", "long"):
            found.append(start)
            continue
        if kind == "comment":
            comment_start = head.start("comment")
            if not comment_from <= comment_start < comment_eol:
                comment_from = comment_start
                comment_eol = _TO_EOL.match(text, comment_start).end()
            if _oct_below(text, comment_eol, below, heads):
                found.append(start)
        elif kind == "eol" and _oct_below(text, head.start("eol"), below, heads):
            found.append(start)
    return found


def _oct_below(text: str, position: int, memo: dict[int, bool], heads: _ValueHeads) -> bool:
    """Whether the value read from the lines after ``position`` (a line end) is oct.

    Every line end crossed before the first significant line leads to the same
    lines, so all of them get this answer in ``memo``: a run of blank or comment
    lines is walked once, however many kty lines precede it.
    """
    cached = memo.get(position)
    if cached is not None:
        return cached
    shared = [position]
    significant = 0
    result = False
    while significant < OCT_LOOKAHEAD:
        line_break = _BREAK.match(text, position)
        if line_break is None:
            break
        position = line_break.end()
        if _BLANK_LINE.match(text, position) or _COMMENT_LINE.match(text, position):
            position = _TO_EOL.match(text, position).end()
            if not significant:
                known = memo.get(position)
                if known is not None:
                    result = known
                    break
                shared.append(position)
            continue
        # An explicit YAML key's value indicator belongs to the next
        # significant line, after arbitrarily many blank/comment-only lines.
        indicator = _KEY_COLON_AT.match(text, position)
        head = heads.match(indicator.end() if indicator else position)
        kind = head.lastgroup
        if kind in ("oct", "alias", "long"):
            result = True
            break
        significant += 1
        if kind == "eol":
            position = head.start("eol")
        elif kind == "comment":
            position = _TO_EOL.match(text, head.start("comment")).end()
        else:
            break
    for crossed in shared:
        memo[crossed] = result
    return result


def _brackets(text: str, triggers: list[int]) -> tuple[list[int], list[int], dict[int, int]]:
    """For each trigger, its innermost enclosing open bracket and the one around
    that (-1 for none), and where each of those brackets closes.

    One forward pass with a typed stack: a closing bracket pops to the nearest
    open bracket of its type (one with no such open bracket is ignored), so a
    stray ``)`` cannot close a ``{``.
    """
    stack = array("q")
    counts = [0, 0, 0]
    inner = [-1] * len(triggers)
    outer = [-1] * len(triggers)
    closes: dict[int, int] = {}
    wanted: set[int] = set()
    index = 0

    def assign(upto: int) -> None:
        nonlocal index
        while index < len(triggers) and triggers[index] < upto:
            if stack:
                inner[index] = stack[-1] >> 2
                wanted.add(inner[index])
                if len(stack) > 1:
                    outer[index] = stack[-2] >> 2
                    wanted.add(outer[index])
            index += 1

    for match in _BRACKET.finditer(text):
        position = match.start()
        assign(position)
        if index == len(triggers) and not wanted:
            break
        char = text[position]
        kind = _BRACKET_TYPE[char]
        if char in "([{":
            stack.append(position << 2 | kind)
            counts[kind] += 1
            continue
        if not counts[kind]:
            continue
        while True:
            entry = stack.pop()
            counts[entry & 3] -= 1
            opened = entry >> 2
            if (entry & 3) == kind:
                if opened in wanted:
                    closes[opened] = match.end()
                    wanted.discard(opened)
                break
            wanted.discard(opened)  # closed by a mismatched bracket: unknown
    assign(len(text) + 1)
    return inner, outer, closes


def _blocks(text: str, lines: _Lines, triggers: list[int]) -> list[tuple[int, int]]:
    inner, outer, closes = _brackets(text, triggers)
    blocks: list[tuple[int, int]] = []
    windows_added: set[int | tuple[int, int]] = set()
    for trigger, bracket, around in zip(triggers, inner, outer, strict=True):
        line = lines.line_of(trigger)
        name_start = trigger
        if text[trigger] == '"':
            leading = _CONTINUATION_RUN.match(text, trigger + 1)
            if leading is not None:
                name_start = leading.end()
        # Candidate spellings do not overlap. Rereading each accepted name
        # once here has linear total cost and needs no second offset table.
        name = _KTY_RE.match(text, name_start)
        through = lines.line_of(name.end() - 1) if name is not None else line
        last_line = min(lines.count - 1, through + WINDOW_LINES)
        window_end = lines.end(last_line)
        closed = closes.get(bracket, -1) if bracket >= 0 else -1
        bracket_line = lines.line_of(bracket) if bracket >= 0 else -1
        if bracket >= 0 and bracket_line >= line - WINDOW_LINES:
            if 0 <= closed <= window_end:
                deeper = lines.deeper_end(line)
                end = closed if deeper == line else max(closed, lines.end(deeper))
                around_close = closes.get(around, -1) if around >= 0 else -1
                if 0 <= around_close <= window_end:
                    end = max(end, around_close)
                else:
                    end = max(end, lines.end(line))
                blocks.append((bracket, end))
                if bracket_line == line:
                    continue
            else:
                blocks.append((bracket, window_end))
        key = line if through == line else (line, through)
        if key not in windows_added:
            windows_added.add(key)
            top, bottom = lines.window(line, through)
            blocks.append((lines.starts[top], lines.end(bottom)))
    return _merge(blocks)


# A YAML document marker line: "---" or "..." at column 0.
_DOCUMENT_MARKER = re.compile(
    rf"(?:\A|(?<=[{_LINE_CHARS}]))(?:---|\.\.\.)(?=[ \t{_LINE_CHARS}]|\Z)"
)


class _DocumentEnds:
    """Where the YAML document holding each anchor ends: before the next
    document marker line, or at the end of the text. The markers are found in
    one pass; anchors are looked up in text order with a cursor, so each lookup
    is O(1) amortized."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.markers = [match.start() for match in _DOCUMENT_MARKER.finditer(text)]
        self._index = 0
        self._last = -1

    def after(self, position: int) -> int:
        if position < self._last:
            self._index = 0  # a new pass over the anchors starts from the top
        self._last = position
        while self._index < len(self.markers) and self.markers[self._index] <= position:
            self._index += 1
        if self._index == len(self.markers):
            return len(self.text)
        return self.markers[self._index]


def _trigger_documents(text: str, triggers: list[int]) -> list[tuple[int, int]]:
    """Documents with an oct trigger, in one forward pass over their markers.

    Aliases outside a trigger's indentation window can inherit its anchored
    mapping through YAML merges. Seed anchor lookup with every alias in those
    documents: masking a referenced definition through its document end also
    covers its later consumers, including merge chains of arbitrary depth.
    """
    markers = _DocumentEnds(text).markers
    ranges: list[tuple[int, int]] = []
    index, start = 0, 0
    for trigger in triggers:
        while index < len(markers) and markers[index] <= trigger:
            start = markers[index]
            index += 1
        end = markers[index] if index < len(markers) else len(text)
        if not ranges or ranges[-1][1] != end:
            ranges.append((start, end))
    # A %TAG directive precedes its '---', so it can sit outside the range
    # whose custom tag resolves to merge. Treat directives conservatively in
    # one whole-text pass, rather than rescanning prefixes for every document.
    if _TAG_DIRECTIVE.search(text):
        return ranges
    return [(start, end) for start, end in ranges if _has_merge_hint(text, start, end)]


def _anchor_region(match: re.Match[str], documents: _DocumentEnds) -> tuple[int, int]:
    """An aliased anchor masks every value from its definition to the end of its
    document. No node grammar is read, so no layout can end it early."""
    return match.end(), documents.after(match.end())


def _alias_names(text: str, pieces: list[tuple[int, int]]) -> set[str]:
    names: set[str] = set()
    for start, end in pieces:
        names.update(match.group(1) for match in _ALIAS_RE.finditer(text, start, end))
    return names


def _anchors(
    text: str, lines: _Lines, blocks: list[tuple[int, int]], decided: _Spans
) -> list[tuple[int, int]]:
    """The anchored values aliases in the blocks name, followed a few rounds."""
    pending = _alias_names(text, _subtract(blocks, decided))
    if not pending:
        return []
    documents = _DocumentEnds(text)
    regions: list[tuple[int, int]] = []
    asked: set[str] = set()
    defined: set[str] = set()
    everything = False
    for _round in range(_ALIAS_ROUNDS):
        found: list[tuple[int, int]] = []
        for match in _ANCHOR_RE.finditer(text):
            if match.group(1) in pending:
                start, end = _anchor_region(match, documents)
                # A commented-out anchor, or one at the very end of its
                # document, defines nothing: its alias then counts as unknown.
                if start < end and lines.info(lines.line_of(match.start()))[0] != _COMMENT:
                    defined.add(match.group(1))
                if found and start <= found[-1][1]:
                    found[-1] = (found[-1][0], max(found[-1][1], end))
                else:
                    found.append((start, end))
        asked |= pending
        regions.extend(found)
        if asked - defined:
            everything = True  # an alias with no definition: mask every anchor
            break
        pending = _alias_names(text, _subtract(_merge(found), decided)) - asked
        if not pending:
            break
    else:
        everything = True  # still growing after the last round
    if everything:
        regions = []
        for match in _ANCHOR_RE.finditer(text):
            start, end = _anchor_region(match, documents)
            if regions and start <= regions[-1][1]:
                regions[-1] = (regions[-1][0], max(regions[-1][1], end))
            else:
                regions.append((start, end))
    return regions


def _is_member_name(
    text: str, start: int, end: int, quote: str | None, limit: int | None = None
) -> bool:
    """Whether the word at [start, end) is a member name: a separator follows."""
    if quote is not None and text.startswith(quote, end):
        position = _SPACES.match(text, end + len(quote)).end()
        if _SEP_AT.match(text, position, len(text) if limit is None else limit):
            return True
    position = _SPACES.match(text, end).end()
    separator = _SEP_AT.match(text, position, len(text) if limit is None else limit)
    if separator is None:
        return False
    if separator.group() != "=":
        return True
    # "=" also pads base64 ("AbC=", "AbC=='"): a name needs a value after it on
    # its line, and inside a string, not a closing quote.
    after = _SPACES.match(text, separator.end()).end()
    if after >= len(text) or _BREAK.match(text, after) or _ENCODED_BREAK_AT.match(text, after):
        return False
    return quote is None or not _QUOTE_AT.match(text, after)


def _clip(text: str, start: int, end: int, low: int, high: int) -> tuple[int, int] | None:
    """A value run cut to its piece, without leading blanks; None when nothing
    is left, or when the run is the word oct itself."""
    start, end = max(start, low), min(end, high)
    if start < end:
        start = _SPACES.match(text, start, end).end()
    if start >= end or (end - start == 3 and text[start:end].lower() == "oct"):
        return None
    return start, end


# Python 3.14 string-prefix grammar; a candidate is at most two characters.
_PYTHON_PREFIXES = frozenset({"b", "r", "u", "f", "br", "rb", "fr", "rf", "t", "tr", "rt"})
_SINGLE_QUOTE_OPEN = r"(?:(?<!\w)|(?<=\b[bBuUrRfFtT])|(?<=\b[rR][bBfFtT])|(?<=\b[bBfFtT][rR]))'"
_PYTHON_PREFIX = r"(?:[bBuUfFtT]|[rR][bBfFtT]?|[bBfFtT][rR])"
_TRIPLE_CONTEXT = "|".join(
    rf"{quote}(?:\\[\s\S]|\\\Z|(?!{quote})[^\\])*+(?:{quote}|\Z)" for quote in ('"' * 3, "'" * 3)
)
_STRING_LITERALS = (
    rf"(?P<prefix>(?<![\w.$]){_PYTHON_PREFIX})?"
    rf"(?:(?P<triple>{_TRIPLE_CONTEXT})"
    r'|(?P<double>"(?:\\[\s\S]|\\\Z|[^"\\])*+(?:"|\Z))'
    rf"|(?P<single>{_SINGLE_QUOTE_OPEN}(?:\\[\s\S]|\\\Z|[^'\\])*+(?:'|\Z)))"
)
_STRING_CONTEXT = re.compile(
    # A comment wins before any quote in it. Complete strings (and an
    # unfinished final string) consume their whole extent once, including
    # physical breaks, so windows never have to rescan a prefix for context.
    rf"(?P<comment>(?<![^\s,\[\]{{}}()])\#[^{_LINE_CHARS}]*+)|{_STRING_LITERALS}"
)


def _formatted_end(text: str, start: int, delimiter: str) -> tuple[int, bool]:
    """Skip one f/t literal, including nested expressions, in a forward pass.

    Expression quotes may reuse the outer delimiter. Format specifiers are
    literal text again, with their own nested replacement fields. No expression
    is evaluated or parsed, and the explicit stack also bounds Python call depth.
    """
    frames: list[str | tuple[str, bool]] = [(delimiter, True)]
    position = start + len(delimiter)
    while position < len(text):
        frame = frames[-1]
        if isinstance(frame, tuple):
            kind, (closing, formatted) = "literal", frame
        else:
            kind = frame
            closing, formatted = "}" if frame in ("field", "format") else frame, False
        char = text[position]
        if kind == "literal":
            if text.startswith(closing, position):
                position += len(closing)
                frames.pop()
                if not frames:
                    return position, True
                continue
            if char == "\\":
                position += (
                    1 if formatted and text[position + 1 : position + 2] in ("{", "}") else 2
                )
                continue
            if formatted and char in "{}":
                if text.startswith(char * 2, position):
                    position += 2
                    continue
                if char == "{":
                    frames.append("field")
        elif kind == "format":
            if char == "}":
                frames.pop()
            elif char == "{":
                frames.append("field")
        elif char in "\"'":
            quote = char * (3 if text.startswith(char * 3, position) else 1)
            before = position
            while before > max(0, position - 2) and text[before - 1] in "bBuUrRfFtT":
                before -= 1
            prefix = text[before:position].lower()
            nested_formatted = prefix in _PYTHON_PREFIXES and ("f" in prefix or "t" in prefix)
            frames.append((quote, nested_formatted))
            position += len(quote)
            continue
        elif char == "#":
            newline = _BREAK.search(text, position)
            position = len(text) if newline is None else newline.end()
            continue
        elif char in "([{":
            frames.append({"(": ")", "[": "]", "{": "}"}[char])
        elif char == closing:
            frames.pop()
        elif char == ":" and kind == "field":
            frames[-1] = "format"
        position += 1
    return len(text), False


def _quote_context(text: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]], bytearray]:
    """Quoted extents, double-quoted keys and formatted flags, in one forward pass.

    Only a quote at a member boundary can open a key. A quote in a comment,
    an apostrophe, or the trailing quote in a plain YAML key cannot turn
    following records into a key and exempt their values from masking.
    """
    strings, keys = [], []
    formatted = bytearray()
    position = 0
    while token := _STRING_CONTEXT.search(text, position):
        position = token.end()
        if token.lastgroup == "comment":
            continue
        prefix = token.group("prefix") or ""
        start, end = token.start() + len(prefix), token.end()
        delimiter = text[start : start + (3 if text.startswith(text[start] * 3, start) else 1)]
        is_formatted = "f" in prefix.lower() or "t" in prefix.lower()
        if is_formatted:
            end, closed = _formatted_end(text, start, delimiter)
            position = end
        else:
            closed = end >= start + 2 * len(delimiter) and text.endswith(delimiter, start, end)
        strings.append((start + len(delimiter), end - len(delimiter) * closed))
        formatted.append(is_formatted)
        if token.lastgroup != "double" or not closed or not _KEY_COLON_AT.match(text, end):
            continue
        before = start - 1
        while before >= 0 and text[before] in " \t":
            before -= 1
        if before < 0 or _BREAK.fullmatch(text, before, before + 1) or text[before] in "{[,;(?":
            keys.append((start, end))
    return strings, keys, formatted


_EXPRESSION_SPACE = re.compile(r"\s*+")
_VALUE_PREFIX = re.compile(rf"(?:\s++|\#[^{_LINE_CHARS}]*+)*+")
_COMPOUND_STRUCTURE = re.compile(
    rf"(?P<comment>\#[^{_LINE_CHARS}]*+)|[()\[\]{{}},;]|{_LINE_BREAK}"
    r"|(?P<lambda>\blambda\b)|(?P<colon>:)"
    r"|(?P<for>\b(?:async\s++)?for\b)|(?P<in>\bin\b)"
    r"|(?<![=!<>:])=(?![=>])"
)
_COMPOUND_OPERATOR = re.compile(r"[+*/%@&|^~<>=!?:-]|\b(?:if|else|and|or|not|in|is)\b")
_SUBTRACTED_OPERAND_AT = re.compile(r"[(\[{]|\blambda\b")
_COMPOUND_ATTRIBUTE = re.compile(r"\.\s*+[\w$]++")
_COMPOUND_PART = re.compile(
    rf"(?P<comment>\#[^{_LINE_CHARS}]*+)|{_COMPOUND_ATTRIBUTE.pattern}"
    r"|[\w.$]++|[^\s\w.$+*/%@&|^~<>=!?:\#-]++"
)
_EXPRESSION_KEYWORDS = frozenset(("if", "else", "and", "or", "not", "in", "is"))
_TARGET_OPEN = re.compile(r"(?:async\s++)?for\s*+")
_COMPUTED_TARGET = re.compile(
    r"[\w\])]\s*[\[(]|['\"`]|[^\w\s.,*()\[\]]"
    r"|\b(?:True|False|None)\b|\b\d|\.\.\."
)


def _binding_structure(
    text: str, start: int, end: int, in_start: int, value_regions: list[tuple[int, int]] | None
) -> list[tuple[int, int]]:
    """Binding syntax excludes computed subscript/object values and call arguments."""
    opening = _TARGET_OPEN.match(text, start)
    assert opening is not None
    low = opening.end()
    target = text[low:in_start]
    if not _COMPUTED_TARGET.search(target):
        return [(start, end)]
    # Parse only complex assignment targets, without executing source. The
    # enclosing parentheses allow the original comprehension's physical layout.
    source = "for (" + target + ") in ():\n pass"
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            warnings.simplefilter("ignore", DeprecationWarning)
            binding = ast.parse(source).body[0].target
    except (SyntaxError, ValueError, RecursionError):
        return []  # malformed/unsupported targets remain private
    raw = source.encode("utf-8")
    line_starts = [0] + [match.end() for match in re.finditer(rb"\r\n|[\r\n]", raw)]
    chars = array("I")
    for index, char in enumerate(source):
        chars.extend([index] * len(char.encode("utf-8")))
    chars.append(len(source))
    holes: list[tuple[int, int]] = []
    for node in ast.walk(binding):
        values = []
        if isinstance(node, ast.Subscript):
            if isinstance(node.slice, ast.Tuple):
                values.extend(
                    value for value in node.slice.elts if not isinstance(value, ast.Slice)
                )
            elif not isinstance(node.slice, ast.Slice):
                values.append(node.slice)
        elif isinstance(node, ast.Slice):
            values.extend(
                value for value in (node.lower, node.upper, node.step) if value is not None
            )
        if isinstance(node, (ast.Subscript, ast.Attribute)) and not isinstance(
            node.value, (ast.Name, ast.Attribute)
        ):
            values.append(node.value)
        for value in values:
            before = chars[line_starts[value.lineno - 1] + value.col_offset]
            through = chars[line_starts[value.end_lineno - 1] + value.end_col_offset]
            holes.append((low + before - 5, low + through - 5))
    if value_regions is not None:
        value_regions.extend(holes)
    return _subtract([(start, end)], _Spans(holes))


def _literal_extent(text: str, body: tuple[int, int]) -> tuple[int, int, int] | None:
    """Prefix start, opening quote and end of a complete context literal."""
    low, high = body
    width = 3 if text[low - 3 : low] in ('"' * 3, "'" * 3) else 1
    opening = low - width
    delimiter = text[opening:low]
    if not delimiter or not text.startswith(delimiter, high):
        return None
    start = opening
    for size in (2, 1):
        candidate = opening - size
        if (
            candidate >= 0
            and text[candidate:opening].lower() in _PYTHON_PREFIXES
            and (candidate == 0 or not re.match(r"[\w.$]", text[candidate - 1]))
        ):
            start = candidate
            break
    return start, opening, high + width


def _native_expressions(
    text: str,
    strings: list[tuple[int, int]],
    pieces: list[tuple[int, int]],
    compounds: list[Masked],
    *,
    bindings: bool,
    value_regions: list[tuple[int, int]] | None = None,
) -> tuple[list[Masked], list[tuple[int, int]], list[tuple[int, int]]]:
    """Assignment ownership, slice values and optional bindings in one forward pass."""
    starts = [-1]
    headers = [0]
    targets = [-1]
    bounds = [-1]
    parameters: dict[int, list[list[int]]] = {}
    clauses: list[tuple[int, int]] = []
    selected: list[Masked | None] = [None]
    owners: list[int] = []
    expanded: list[Masked] = []
    divided: list[tuple[int, int]] = []
    index = string_index = position = 0

    def structure(start: int, end: int) -> None:
        # Dense parameters without defaults form one syntax span, rather
        # than allocating one private-value-sized record for every name.
        if clauses and clauses[-1][1] == start:
            clauses[-1] = (clauses[-1][0], end)
        else:
            clauses.append((start, end))

    def value_region(start: int, end: int) -> None:
        if value_regions is None or start < 0:
            return
        start = _VALUE_PREFIX.match(text, start, end).end()
        while end > start and text[end - 1].isspace():
            end -= 1
        if start < end:
            value_regions.append((start, end))

    def parameter(end: int, *, final: bool) -> None:
        states = parameters[len(starts) - 1]
        before, default = states[-1]
        if before >= 0:
            structure(before, position)
        value_region(default, end)
        if final:
            # A default's final colon separates the body; it must not merge
            # into the private run on either side of the header delimiter.
            structure(end, position)
            states.pop()
            if not states:
                del parameters[len(starts) - 1]
        else:
            states[-1] = [position, -1]

    def finish(end: int) -> None:
        start, member = starts[-1], selected[-1]
        if member is not None:
            piece = bisect.bisect_right(pieces, (start, len(text))) - 1
            if piece >= 0 and pieces[piece][0] <= start < end <= pieces[piece][1]:
                expanded.append(Masked(start, end, False, member.name))
            elif len(starts) > 1:
                divided.append((start, end))
        if start >= 0:
            owners.pop()
        starts[-1], selected[-1] = -1, None
        headers[-1] = 0
        parameters.pop(len(starts) - 1, None)

    while token := _COMPOUND_STRUCTURE.search(text, position):
        at = token.start()
        position = token.end()
        while index < len(compounds) and compounds[index].start <= at:
            if owners:
                selected[owners[-1]] = compounds[index]
            index += 1
        while string_index < len(strings) and strings[string_index][1] <= at:
            string_index += 1
        if string_index < len(strings):
            literal = _literal_extent(text, strings[string_index])
            if literal is not None and literal[0] <= at < literal[2]:
                position = literal[2]
                continue
        if token.lastgroup == "comment":
            continue
        atom = token.group()
        if atom == "=":
            if bindings and len(starts) - 1 in parameters:
                state = parameters[len(starts) - 1][-1]
                if state[0] >= 0:
                    structure(state[0], position)
                state[:] = [-1, position]
            if starts[-1] < 0 and not (headers[-1] and owners):
                # A lambda default shares its header's depth with the owning
                # keyword. It cannot replace that existing value owner.
                starts[-1] = _EXPRESSION_SPACE.match(text, position).end()
                owners.append(len(starts) - 1)
        elif token.lastgroup == "lambda":
            headers[-1] += 1
            if bindings and owners:
                parameters.setdefault(len(starts) - 1, []).append([at, -1])
        elif token.lastgroup == "colon":
            if headers[-1]:
                if bindings and len(starts) - 1 in parameters:
                    parameter(at, final=True)
                headers[-1] -= 1
            elif bounds[-1] >= 0:
                value_region(bounds[-1], at)
                bounds[-1] = position
        elif bindings and token.lastgroup == "for" and owners and owners[-1] < len(starts) - 1:
            targets[-1] = at
        elif bindings and token.lastgroup == "in" and targets[-1] >= 0:
            clauses.extend(_binding_structure(text, targets[-1], position, at, value_regions))
            targets[-1] = -1
        elif atom in "([{":
            before = at - 1
            while before >= 0 and text[before].isspace():
                if text[before] in _LINE_CHARS and len(starts) == 1:
                    break
                before -= 1
            subscript = (
                bindings
                and atom == "["
                and before >= 0
                and (text[before].isalnum() or text[before] in "_$)]}'\"")
            )
            starts.append(-1)
            headers.append(0)
            targets.append(-1)
            bounds.append(position if subscript else -1)
            selected.append(None)
        elif atom in ")]}":
            value_region(bounds[-1], at)
            finish(at)
            if len(starts) > 1:
                starts.pop()
                headers.pop()
                targets.pop()
                bounds.pop()
                selected.pop()
        elif atom == "," and headers[-1]:
            if bindings and len(starts) - 1 in parameters:
                parameter(at, final=False)
        elif (atom in ",;" and not headers[-1]) or len(starts) == 1:
            if atom == "," and bounds[-1] >= 0:
                value_region(bounds[-1], at)
                bounds[-1] = position
            finish(at)
    while index < len(compounds):
        if owners:
            selected[owners[-1]] = compounds[index]
        index += 1
    while starts:
        finish(len(text))
        starts.pop()
        selected.pop()
    return expanded, divided, clauses


def _whole_compounds(
    text: str,
    strings: list[tuple[int, int]],
    pieces: list[tuple[int, int]],
    masked: list[Masked],
    compounds: list[Masked],
) -> list[Masked]:
    """Expand joined operands and bare tails to their owning keyword value.

    A call/index/group before the joined fragment has already emitted masks.
    Keeping that punctuation next to a new literal can be invalid Python.
    Track assignment ownership across brackets rather than searching backward
    from every candidate. Complete quoted bodies and comments remain opaque, and
    an expansion may never cross an independently decided object or mask cut.
    """
    if not compounds:
        return masked
    expanded, divided, _ = _native_expressions(text, strings, pieces, compounds, bindings=False)
    # Nested keyword values can both be selected. The outer replacement owns
    # all of its children; keep one span and never copy overlapping values.
    merged = _merge([(member.start, member.end) for member in expanded])
    covered = _Spans(merged)
    names = {member.start: member.name for member in expanded}
    # A decided child object must keep its existing JSON output. Where that
    # cuts the owning value, retain expression operators between separately
    # masked operands instead of deleting the operator along with its literal.
    divided_spans = _Spans(_merge(divided))
    split = _merge(
        [
            (member.start, member.end)
            for member in compounds
            if not covered.contains(member.start) and divided_spans.contains(member.start)
        ]
    )
    split_spans = _Spans(split)
    replacements: list[Masked] = []
    string_index = 0

    def replace_part(part: re.Match[str]) -> None:
        if part.group() in _EXPRESSION_KEYWORDS:
            return
        comment = part.lastgroup == "comment"
        # Keep '#' structural. Attribute tails become a marker-shaped index,
        # which remains valid after a preserved call or indexed child object.
        replacements.append(
            Masked(
                part.start() + int(comment),
                part.end(),
                comment or part.group().startswith("."),
                "value",
            )
        )

    for low, high in split:
        position = low
        while string_index < len(strings):
            body = strings[string_index]
            literal = _literal_extent(text, body)
            if literal is None or literal[2] <= position:
                string_index += 1
                continue
            if literal[0] >= high:
                break
            for part in _COMPOUND_PART.finditer(text, position, literal[0]):
                replace_part(part)
            replacements.append(Masked(*body, True, "value"))
            position = literal[2]
            string_index += 1
        for part in _COMPOUND_PART.finditer(text, position, high):
            replace_part(part)
    result = sorted(
        [
            member
            for member in masked
            if not covered.contains(member.start) and not split_spans.contains(member.start)
        ]
        + [Masked(start, end, False, names[start]) for start, end in merged]
        + replacements,
        key=lambda member: member.start,
    )
    return result


def _mask(
    text: str,
    lines: _Lines,
    pieces: list[tuple[int, int]],
    triggers: list[int],
) -> list[Masked]:
    """Every value run inside ``pieces``, lexed from the start of each line."""
    if not pieces:
        return []
    strings, keys, formatted_strings = _quote_context(text)
    value_regions: list[tuple[int, int]] = []
    if "[" in text or re.search(r"\b(?:for|lambda)\b", text):
        _, _, clauses = _native_expressions(
            text, strings, pieces, [], bindings=True, value_regions=value_regions
        )
        # Lambda/comprehension bindings are removed before allocating masks. Large
        # unpacking targets otherwise consume memory proportional to names
        # that produce no private member in the returned cost model.
        pieces = _subtract(pieces, _Spans(_merge(clauses)))
    value_ends: dict[int, int] = {}
    for start, end in value_regions:
        value_ends[start] = min(value_ends.get(start, end), end)
    # A quoted slice bound is a value even though its closing quote is
    # followed by ':'. Actual mapping keys inside computed objects stay keys.
    keys = [span for span in keys if value_ends.get(span[0]) != span[1]]
    pieces = _subtract(pieces, _Spans(keys))
    string_index = 0
    # A quoted log snippet containing its own trigger has Python structure
    # within the string. Ordinary private strings in a surrounding block do
    # not: their leading ** must be masked with the rest of their value.
    structured_strings = bytearray(len(strings))
    trigger_index = 0
    for index, (low, high) in enumerate(strings):
        while trigger_index < len(triggers) and triggers[trigger_index] < low:
            trigger_index += 1
        structured_strings[index] = trigger_index < len(triggers) and triggers[trigger_index] < high

    def inside_string(position: int) -> bool:
        nonlocal string_index
        while string_index < len(strings) and strings[string_index][1] <= position:
            string_index += 1
        return string_index < len(strings) and strings[string_index][0] <= position

    def value_limit(start: int, high: int) -> int | None:
        end = value_ends.get(start)
        if inside_string(start):
            literal = _literal_extent(text, strings[string_index])
            if literal is not None and value_ends.get(literal[0]) == literal[2]:
                end = literal[2]
        return min(high, end) if end is not None else None

    masked: list[Masked] = []
    compounds: list[Masked] = []

    def bare_compound(member: Masked, high: int) -> None:
        if member.start == bare_tail:
            # A dot may be separated from its identifier by physical breaks.
            # Keep that complete attribute together even when the ordinary
            # value lexer emitted separate line-attributed masks.
            attribute = _COMPOUND_ATTRIBUTE.match(text, member.start, high)
            if attribute is not None:
                member = member._replace(end=max(member.end, attribute.end()))
            compounds.append(member)

    def join_previous_literal(start: int, end: int, low: int) -> tuple[int, int, bool]:
        # The quote cursor already points at the next string. If an unquoted
        # expression fragment follows a literal, replace both together: keeping
        # a bytes literal next to a new string marker would be invalid Python.
        if string_index == 0:
            return start, end, False
        previous = _literal_extent(text, strings[string_index - 1])
        if previous is None:
            return start, end, False
        before, _, through = previous
        if before < low or through > start or _EXPRESSION_SPACE.match(text, through).end() != start:
            return start, end, False
        start = before
        while masked and masked[-1].end > before:
            start = min(start, masked.pop().start)
        return start, end, True

    lexed_to = -1
    bare_tail = -1
    member_start = True
    # After a separator, up to the next ",", ";", bracket or real line break,
    # everything is the value: nothing there is a member name, and an escape
    # never ends a masked run (fail toward masking).
    in_value = False
    quote: str | None = None  # the quote token directly before, if any
    name = "value"
    for piece_start, piece_end in pieces:
        line_start = lines.starts[lines.line_of(piece_start)]
        if lexed_to < line_start:
            position, member_start, quote, name = line_start, True, None, "value"
            in_value = False
        else:
            position = lexed_to
        run_start = run_end = -1
        run_after_quote = False
        formatted_to = -1
        for token in _LEX.finditer(text, position, piece_end):
            kind = token.lastgroup
            start, end = token.span()
            if start < formatted_to:
                continue
            if inside_string(start) and formatted_strings[string_index]:
                low, high = strings[string_index]
                if piece_start <= low and high <= piece_end:
                    # Treat the entire formatted value as private. Replacing its
                    # body also removes expression operators together, preserving
                    # valid syntax without interpreting lambda, walrus or format
                    # grammar. Literal text cannot retain a prefix-shaped secret.
                    span = _clip(text, low, high, piece_start, piece_end)
                    if span is not None:
                        masked.append(Masked(*span, True, name))
                    run_start = run_end = -1
                    formatted_to = high
                    continue
            if (
                kind == "word"
                and member_start
                and not in_value
                and run_start < 0
                and quote is None
                and text.startswith("**", start)
                and (not inside_string(start) or structured_strings[string_index])
            ):
                # Every unpack operator is structure, independently of which
                # operand supplies the type. Its operand remains maskable.
                start += 2
                if start == end:
                    continue
            if kind == "word":
                if (
                    run_start < 0
                    and 1 <= end - start <= 2
                    and text[start:end].lower() in _PYTHON_PREFIXES
                    and (prefix_quote := _QUOTE_AT.match(text, end))
                    and text[prefix_quote.end() - 1] in "\"'"
                    and (not inside_string(start) or structured_strings[string_index])
                ):
                    # A prefix immediately before a literal is syntax. Prefix
                    # letters inside a surrounding private string stay values.
                    quote = None
                    continue
                if run_start >= 0:
                    run_end = end
                elif (
                    text[start] in "-."
                    and (start == 0 or _BREAK.fullmatch(text, start - 1, start))
                    and _DOCUMENT_MARKER.match(text, start)
                ):
                    # A document marker is structure, wherever a block reaches.
                    member_start, in_value = True, False
                elif in_value:
                    run_start, run_end = start, end
                    run_after_quote = quote is not None
                elif (
                    member_start
                    and end - start == 1
                    and text[start] in "-?"
                    and text.startswith((" ", "\t"), end)
                ):
                    pass  # a YAML sequence or complex-key marker
                elif (
                    member_start
                    and _is_member_name(text, start, end, quote, value_limit(start, piece_end))
                    and (not inside_string(start) or not formatted_strings[string_index])
                ):
                    name = text[start:end]
                    member_start = False
                else:
                    run_start, run_end = start, end
                    run_after_quote = quote is not None
                    member_start = False
                quote = None
                continue
            if kind == "sep":
                if (
                    token.group() == ":"
                    and inside_string(start)
                    and formatted_strings[string_index]
                ):
                    # Format specifiers and nested dicts need their colon while
                    # their expression's private characters remain maskable.
                    if run_start >= 0:
                        span = _clip(text, run_start, run_end, piece_start, piece_end)
                        if span is not None:
                            masked.append(Masked(*span, True, name))
                        run_start = run_end = -1
                    member_start, in_value, quote = False, True, None
                    continue
                if run_start >= 0:
                    run_end = end
                member_start, in_value = False, True
                quote = None
                continue
            if kind == "space":
                quote = None
                continue
            atom = token.group()
            quoted = kind == "quote" or (kind == "esc" and atom[-1] in "\"'`")
            if kind == "esc" and (run_start >= 0 or not (quoted or _BREAK_ESCAPE.fullmatch(atom))):
                # \xNN, \u00NN, \\: a value written as escapes is still a value,
                # and inside a value no escape, not even \" or \n, ends its run.
                if run_start < 0:
                    run_start = start
                    run_after_quote = quote is not None
                run_end = end
                member_start = False
                quote = None
                continue
            if run_start >= 0:
                span = _clip(text, run_start, run_end, piece_start, piece_end)
                if span is not None:
                    in_string = inside_string(span[0])
                    if not in_string:
                        low, high, joined = join_previous_literal(*span, piece_start)
                        if kind == "quote" and string_index < len(strings):
                            literal = _literal_extent(text, strings[string_index])
                            if (
                                literal is not None
                                and literal[1] == start
                                and literal[2] <= piece_end
                            ):
                                # A quoted operand cannot follow a bare inline
                                # marker. Mask the complete fragment and operand,
                                # with any preceding literal, as one expression.
                                member = Masked(low, literal[2], False, name)
                                masked.append(member)
                                compounds.append(member)
                                formatted_to = literal[2]
                                run_start = run_end = -1
                                member_start, in_value, quote = False, True, None
                                continue
                        member = Masked(
                            low, high, False if joined else run_after_quote or quoted, name
                        )
                        masked.append(member)
                        bare_compound(member, piece_end)
                        if kind in ("open", "nl") and _COMPOUND_OPERATOR.search(text, *span):
                            # An operator before a collection/call/group owns the
                            # following operand too. Keeping only the opening
                            # punctuation beside a quoted marker breaks syntax.
                            compounds.append(member)
                    else:
                        masked.append(Masked(*span, True, name))
                run_start = run_end = -1
            quote = atom if quoted else None
            if kind == "quote":
                member_start = not in_value
            elif kind == "esc":
                member_start = not in_value  # a quote or line break, outside a run
            elif kind == "nl":
                # Each physical line gets separate spans for audit attribution.
                # Quote context comes from the independent whole-text pass.
                member_start, in_value, name = True, False, "value"
                if 0 <= bare_tail < len(text) and text[bare_tail] == "#":
                    bare_tail = _EXPRESSION_SPACE.match(text, end).end()
            else:
                member_start, in_value = kind != "close", False
                if kind == "close" and not inside_string(start):
                    # Cache the next non-space position once per closing
                    # bracket. A following bare tail needs the same complete
                    # value ownership as a fragment adjoining a quoted operand.
                    bare_tail = _EXPRESSION_SPACE.match(text, end).end()
        if run_start >= 0:
            span = _clip(text, run_start, run_end, piece_start, piece_end)
            if span is not None:
                in_string = inside_string(span[0])
                if not in_string:
                    low, high, joined = join_previous_literal(*span, piece_start)
                    member = Masked(low, high, False if joined else run_after_quote, name)
                    masked.append(member)
                    bare_compound(member, piece_end)
                    if _SUBTRACTED_OPERAND_AT.match(text, piece_end) and _COMPOUND_OPERATOR.search(
                        text, *span
                    ):
                        # A decided child or lambda header was removed before
                        # lexing. Its opening operand still bounds this run:
                        # preserve the intervening operators just as when the
                        # ordinary lexer encounters an opening bracket itself.
                        compounds.append(member)
                else:
                    masked.append(Masked(*span, True, name))
        lexed_to = piece_end
    return _whole_compounds(text, strings, pieces, masked, compounds)


_UNPACKED_OBJECT = re.compile(
    rf"\*\*(?:[ \t{_LINE_CHARS}(]++|\\{_LINE_BREAK}|\#[^{_LINE_CHARS}]*+{_LINE_BREAK})*+(?=\{{)"
)

_UNPACK_CONTEXT = re.compile(
    _STRING_CONTEXT.pattern
    + rf"|(?P<continuation>\\{_LINE_BREAK})|(?P<operator>\*\*)"
    + r"|(?P<atom>[\w.$]++|[^\s])"
)


def _unpacked_objects(text: str, source_starts: set[int], cut: _Spans) -> set[int]:
    """Only argument/member unpacking can pass a child's type to its parent.

    Track the preceding token in a forward pass. Comments and continuations
    are whitespace; complete literals remain operands, including formatted
    literals with nested quotes. Exponentiation follows an operand instead of
    an opening argument/member boundary.
    """
    operators = set()
    position = 0
    previous = ""
    while token := _UNPACK_CONTEXT.search(text, position):
        position = token.end()
        kind = token.lastgroup
        if kind in ("comment", "continuation"):
            continue
        if kind in ("single", "double", "triple"):
            prefix = token.group("prefix") or ""
            start = token.start() + len(prefix)
            delimiter = text[start] * (3 if text.startswith(text[start] * 3, start) else 1)
            if "f" in prefix.lower() or "t" in prefix.lower():
                position, _ = _formatted_end(text, start, delimiter)
            previous = delimiter[-1]
            continue
        if kind == "operator" and (not previous or previous in "({,"):
            match = _UNPACKED_OBJECT.match(text, token.start())
            if match and match.end() in source_starts and not cut.contains(token.start()):
                operators.add(token.start())
        previous = text[position - 1]
    return operators


def oct_block_values(
    text: str,
    decided: list[tuple[int, int]],
    oct_sources: list[tuple[int, int]] | None = None,
    *,
    ignored_triggers: list[tuple[int, int]] | None = None,
    already_masked: list[tuple[int, int]] | None = None,
) -> list[Masked]:
    """Spans to mask outside ``decided``, with optional independently scanned strings.

    Such strings supply no triggers to this context, but remain maskable as
    values when this context has its own oct trigger.
    Already-covered replacements suppress duplicate masks only; their triggers
    and aliases still protect neighbouring values.
    """
    cut = _Spans(decided)
    # Every fallback region is inside the input; a full cut leaves nothing to mask.
    if cut.spans and cut.spans[0][0] <= 0 and cut.spans[0][1] >= len(text):
        return []
    # Decided objects keep their own masking, but an oct type inside an
    # anchored decided object can still be inherited by a later YAML merge.
    all_triggers = _triggers(text, _Spans([]))
    unpack_operators: set[int] = set()
    if oct_sources and "**" in text:
        # Only actual oct objects reported by the object scanner can supply a
        # call's type. A non-oct object's note mentioning kty is not evidence.
        source_starts = {start for start, _ in oct_sources}
        unpack_operators = _unpacked_objects(text, source_starts, cut)
        del source_starts
        all_triggers = sorted(all_triggers + list(unpack_operators))
    if ignored_triggers:
        ignored = _Spans(ignored_triggers)
        all_triggers = [position for position in all_triggers if not ignored.contains(position)]
    if not all_triggers:
        return []
    triggers = [position for position in all_triggers if not cut.contains(position)]
    lines = _Lines(text)
    blocks = _blocks(text, lines, triggers)
    # Alias consumers need not share the trigger's indentation or bounded
    # window. The document mask remains conditional on an actual alias.
    alias_scope = _merge(blocks + _trigger_documents(text, all_triggers))
    regions = _merge(blocks + _anchors(text, lines, alias_scope, cut))
    # Replacements already covering decoded values do not suppress lexical
    # triggers or aliases; only avoid reporting their private material twice.
    mask_cut = _Spans(decided + already_masked) if already_masked else cut
    return _mask(text, lines, _subtract(regions, mask_cut), triggers)
