"""Asymmetric JWKs written as YAML block mappings (task #2200).

``jwk.private_members`` reads a JWK as an object literal (the JSON scan), and
``oct_blocks`` masks every value near a symmetric (``oct``) type written without
braces. Neither typed an RSA, EC or OKP key written as a YAML block mapping,
so its private scalar ``d`` (and an RSA key's ``p``, ``q``, ``dp``, ``dq``,
``qi`` and ``oth``) passed every redactor.

Why a separate scanner. The oct-block rule is deliberately structure-blind: it
masks every value in a window of lines around the type, which suits a
symmetric key (where only ``kid``/``alg`` are lost). An asymmetric key's public
members (``n``, ``e``, ``x``, ``y``, ``crv``, ``kid``) are what readers of a log
need, and a window cannot tell a member of the key's own mapping from a member
of a child or sibling mapping. This module reads just enough YAML block
structure (indentation, ``- `` sequence items, document markers) to know which
mapping a member belongs to, and masks only private members of a typed
mapping. It shares no state with the oct-block rule, so that rule's output
stays byte for byte.

Mappings. A key line is ``name: value`` at some column, optionally after ``- ``
sequence markers and node properties; the name may be plain, single-quoted or
double-quoted (read as YAML, escapes and all), or an alias (``*n : value``)
resolved to the scalar its anchor names. An explicit key (``? name``) is read
as a node: properties (tags, verbatim ``!<...>`` tags, anchors) are skipped,
and the name may be quoted, an alias, or on the deeper line below the ``?``;
its value is the next key-level line, ``: value`` at the same column. Key lines at the same column
belong to one mapping until a line at a lower column, a new ``- `` item at or
before that column, or a document marker (``---``, ``...``) ends it. A mapping
is typed when one of its own keys is ``kty`` with the value ``RSA``, ``EC`` or
``OKP`` (plain, quoted, escaped, tagged, anchored, or on a later line, past
any lines holding only node properties), in any position: members before the
type count too. A type given as an alias types the mapping as any of them,
failing toward masking. Members of a child mapping, a parent, a sibling item
or another document are not members.

Values. A member's value runs from after its colon (or from the first line
below, when nothing but a comment follows the colon) through every following
line indented deeper than the key, a compact sequence (``oth:``, or ``oth:``
with only node properties, followed by ``- `` items at the key's own column),
and to the closing quote of a quoted scalar wherever that quote is (YAML lets
a quoted scalar continue at any indentation). Block scalars (``|``, ``>``) are
masked from their header. A plain or closed quoted value's trailing comment is
kept. Lines keep their breaks: a value is replaced in place, so audit line
numbers stay on the member's own lines.

Two readings. A stray quote can make a quoted value seem to swallow later lines,
and a quoted continuation at a low column can make a mapping seem to end. The
text is read twice, once skipping quoted continuations (only key, item and
document lines end mappings) and once not (any lower line ends one), and the
private members of both readings are masked. Each is one forward pass.

Anchors, aliases and merges. A private member whose value is an alias
(``d: *v``, block or flow, from any rule) masks the anchored node at its
definition, read as a quoted scalar to its closing quote, a flow collection to
its matching closing bracket (commas inside it at any depth included), or the
rest of its line and every deeper line (a plain or block scalar, a block
collection, a compact sequence). Text cannot always tell a real ``&v`` from one
in a comment, a quoted or block scalar or prose, so every ``&v`` before the
alias in its document is a candidate and every candidate's node is masked: a
fake anchor adds a masked node, it never replaces the real one. An alias key
names the scalar its candidates agree on; when they disagree, or a candidate
is not a scalar, the name is unknown and its value is masked as private.
Aliases inside a masked node are followed for a few rounds.
A merge key (``<<``) or an alias key can carry a type or a private member
between mappings, and that is not resolved. Instead, a document holding key-type
evidence (the word RSA, EC or OKP, or a double-quoted value with an escape) and
a merge key in key position (``<<`` plain or quoted, after node properties, at
a line start or after ``- ``, ``? `` or an explicit value's ``: ``, or right
after ``{``/``,`` and an optional ``? ``; followed by its ``:`` or, as an
explicit key, by the end of its line; in flow, a plain ``<<`` glued to a
collection, ``{<<:[*b]}``, or a quoted one glued to any value, ``{"<<":*b}``),
a merge tag (``!!merge``, ``tag:yaml.org,2002:merge``) on a key in the same key
position, a ``%TAG`` directive introducing that document,
or an alias key whose anchor is defined earlier in it is read conservatively.
There, every mapping that takes part in merging or aliasing has every
private-named member masked, block or flow, typed or not: one with a merge,
tagged, anchored, alias or type key at its own level, or the node of an
anchor (``priv: &p`` or a ``- &a`` item or a lone ``&a`` line, with the
mapping below; ``&p {...}``, also glued to a flow indicator, ``[&a {...}``,
or ending the line before the flow node). An explicit value holding a compact mapping
(``? k`` then ``: <<: *b`` with its siblings below) is read as that mapping. Plain mappings (a Python dict, a dataclass) are never read that
way, and public members stay. An alias token never starts with ``*`` nor holds
parentheses, so ``**kwargs)`` or ``f(*args)`` is code, not an alias.

Comments. One lexer, ``_YamlLex``, reads the text once as YAML (as PyYAML's
scanner does, checked against it by an oracle differential) and records its
comments, quoted scalars and real flow indicators; every comment decision
here asks it. A ``#`` begins a comment wherever a token could begin: at a
line start, after whitespace, and right after any token but a plain scalar
(a closed quoted scalar, a flow indicator, an anchor, an alias). A plain
scalar keeps a glued ``#`` (``x"#c``), a quoted scalar never holds a comment,
a quote opens one only where a token begins, and a tag is read whole
(``!t'x``). Flow mappings are walked over the lexer's flow indicators, so a
quote or ``#`` inside a scalar is never structure. A flow mapping YAML reads
as typed (its own ``kty`` holds a key type, an alias or an interpolation)
has its private members reported too, beside the object scan's reading of
the same braces as Python or JavaScript: the caller keeps them where no
other member overlaps, so a YAML-only reading cannot lose a member.

Interpolations. A key or type token built from word characters and JavaScript
template interpolations (``${kind}``, ``R${kind}A``, ``k${part}y``; ``jwk``
cooks a template's interpolations to ``${}``) is unknown: an unknown type
selects every private set, and an unknown name may be the type or a private
member, so its mapping is typed as any key and its value masked. A block key
whose value is a flow mapping (``"${1:3000}": {``, an editor snippet in a
pretty-printed JSON Schema) is neither, both being scalars (``oth`` a
sequence), so it is an ordinary key (task #2222).

Contexts. ``jwk`` runs this scanner on the raw text, on each decoded string
the JSON scan inspects and on each triple-quoted literal written with escapes
(a member there masks the whole string, as for decoded JSON), and on the
comment context (so a commented-out block key is masked).

Over-redaction accepted. A type spelled as the first line of a longer plain
scalar (the type name continued on the next line) still types its mapping; a
non-YAML line at a lower column inside a mapping (log noise) is read both as
ending it and as not; an indented comment under a value is part of that value.
In a conservative document every ``d``, ``p``, ``q``, ``dp``, ``dq``, ``qi`` and
``oth`` value of a merging, aliasing, typed or anchored mapping is masked, even
when that mapping is not a key. An anchored plain
scalar inside a flow collection is masked to the end of its line. An explicit
key's multi-line plain name is read from its first line. Every candidate
anchor's node is masked, and a mapping with an interpolated key or type is
typed as any key (a YAML config written with ``${VAR}`` placeholders too).

Limits (not masked):

* Merge keys and alias keys are not resolved; only the conservative reading
  above covers them, and only in a document with key-type evidence (a merged
  anchor is always in its own document, so its type is evidence there).
* An explicit key whose ``: value`` line is not the next key-level line at its
  column, or whose key is itself a collection, is not read as a member or a
  type (outside a conservative document).
* Anchor and alias tokens count only after whitespace, a flow indicator or the
  start of the text (as YAML requires), and only a value outside any string
  can be an alias: ``&x`` inside a JSON string is text.
* An alias with no earlier definition in its document names nothing (YAML
  cannot load it). A chain of aliases still growing after the last round masks
  each pending candidate definition to the end of its document. An alias key
  with more than eight candidate definitions is unknown (masked as private).
* A decoded string is read only to the JSON scan's depth bound (three levels
  of encoding), as for every rule.
* Text the JSON scan decided (an object with a quoted type) is never read by
  the block and conservative readings; a typed flow reading is kept there
  only where no other member overlaps it, and only where a divergent scalar
  lets YAML's reading differ from the object scan's: a plain scalar holding a
  character that starts one of that scan's swallowing lexemes (``#``, ``"``,
  ``'``, a backtick, ``/``), a quoted scalar that never closes, a single-quoted
  scalar ending in a backslash or holding a doubled quote, or a quoted scalar
  with a quote glued to either end. Inside a typed flow mapping with such a
  scalar, every private-named key in the mapping's text is also masked to
  its value's end (a textual sweep that fails toward masking where the two
  readings disagree about where a member starts), unless the mapping sits in
  an outermost flow collection that never closes (a runaway reading of code).
* A key line dedented to a column no open mapping has (malformed YAML) keeps
  the types of the mappings it closed. A block scalar's content indent
  follows PyYAML's rule: ``|N``/``>N`` fix it at the parent's indent plus N,
  otherwise the deepest leading-space run before its first content line.
* Merge and alias-key evidence counts only at a real token (a plain or quoted
  ``<<``, a merge tag, an alias) outside any outermost flow collection that
  never closes: text that is not YAML (code read as flow from a stray ``{``)
  makes no document conservative.
* Symmetric (``oct``) block keys are the oct-block rule's.

Deliberate input can make the scan do more work than ordinary text (long
runs of anchors, aliases, merges or properties): every step stays linear in
the input and is covered by growth tests, which is the accepted bound; a
redactor that gave up early would leak instead.

Cost. Each reading is one pass over the lines with a stack of open mappings and
a stack of open values, each pushed and popped once per key line. Value ends are
resolved lazily from the last content line, so deep nesting costs nothing per
level. A quoted value's closing quote is found by one forward search, reused
for every later quote opened before it, so quote searches never rescan.
Members covered by an enclosing member (deep ``oth`` chains) are dropped
before any value is sliced. Anchors are indexed once; a definition's line,
scalar and node are each read once (line starts are bisected, not rescanned),
and a definition inside a node already masked is not read again. Conservative
documents add one pass of a flow-key pattern whose alternatives are possessive
and whose properties are bounded (256 characters), with each private value's
extent scanned once; their evidence is one word search. Memory is the stacks, the
anchor index and the member spans.
"""

from __future__ import annotations

import re
from array import array
from bisect import bisect_right
from collections.abc import Callable, Iterator
from heapq import merge

from . import oct_blocks

_RSA_ONLY = frozenset({"p", "q", "dp", "dq", "qi", "oth"})
_TYPE_BITS = {"RSA": 1, "EC": 2, "OKP": 2, "*": 3}
# "*" names an alias key that does not resolve to a scalar: private in any key.
_PRIVATE_NAMES = _RSA_ONLY | {"d", "*"}
_NAMES = _PRIVATE_NAMES | {"kty"}
_ALIAS_ROUNDS = 4
# More candidate definitions of one anchor name than this leaves a name unknown.
_AMBIGUOUS_CANDIDATES = 8
# A JavaScript template interpolation (``jwk._template_cooked`` keeps ``${}``):
# a name or type holding one is unknown, so it may be the type or a private name.
_INTERPOLATION = "${"
# Only a token made of word characters and interpolations is one (``${kind}``,
# ``R${kind}A``, ``k${part}y``): code mentioning "${" is not a key or a type.
_INTERPOLATED_TOKEN = re.compile(r"[\w.-]*+(?:\$\{[^{}\s]{0,256}+\}[\w.-]*+)++")


def _interpolated(value: str | None) -> bool:
    return bool(
        value and _INTERPOLATION in value and _INTERPOLATED_TOKEN.fullmatch(value.strip(" \t\"'"))
    )


_LINE_CHARS = oct_blocks._LINE_CHARS
_BREAK = re.compile(oct_blocks._LINE_BREAK)
_HEAD = re.compile(r"(?P<indent> *+)(?P<marks>(?:-(?:[ \t]++|$))*+)")
_DOCUMENT = re.compile(r"(?:---|\.\.\.)(?:[ \t]|$)")
_PROPERTIES = re.compile(r"(?:[&!][^ \t]*+(?:[ \t]++|$))*+")
_ENTRY = re.compile(
    r"(?:[&!][^ \t]*+[ \t]++)*+"
    r"(?P<key>\"(?:\\.|[^\"\\])*+\"|'(?:''|[^'])*+'"
    # An alias used as a key (*n : value), resolved through the anchor table.
    # Its name never starts with '*' or holds call syntax: **kwargs) is code.
    r"|\*[^ \t,\[\]{}:()*&'\"`]++(?::[^ \t,\[\]{}:()*&'\"`]++)*+"
    # Plain words separated by blanks, matched possessively: a lazy name
    # would retry the blank run before every character (quadratic).
    r"|(?![-?:](?:[ \t]|$))[^ \t#'\"\[\]{},&!*|>%@`:][^ \t:]*+(?:[ \t]++[^ \t:]++)*+)"
    r"[ \t]*+:(?:[ \t]++|$)"
)
# An explicit key (``? name``); its value is a ``: value`` line at its column.
_EXPLICIT = re.compile(r"\?(?:[ \t]++|$)")
_EXPLICIT_VALUE = re.compile(r":(?:[ \t]++|$)")
# YAML's comment rule, as PyYAML's scanner applies it (task #2200, review r7).
# A '#' begins a comment wherever a token could begin: at a line start, after
# whitespace, and right after any token that is not a plain scalar (a quoted
# scalar that really closed, a flow indicator, an anchor, an alias). A plain
# scalar absorbs a glued '#' ('x"#c' is one scalar); a quoted scalar never
# holds a comment; a quote opens one only where a token begins. Every comment
# decision in this module asks one lexer, ``_YamlLex``, built once per text.
_YAML_BREAKS = "\r\n\x85  "
_YAML_WHITE = " \t\r\n\x85  "
_YAML_BREAK = re.compile(r"\r\n|[\r\n\x85  ]")
_YAML_TO_BREAK = re.compile(r"[^\r\n\x85  ]*+")
_YAML_SPACES = re.compile(r"[ \t]*+")
_YAML_DOUBLE = re.compile(r'(?:\\[^\r\n\x85  ]|\\(?:\r\n|[\r\n\x85  ])|[^"\\])*+"')
_YAML_SINGLE = re.compile(r"(?:''|[^'])*+'")
_YAML_ANCHOR = re.compile(r"[0-9A-Za-z_-]*+")
_YAML_NON_SPACE = re.compile(r"[^ \t\r\n\x85  ]*+")
_YAML_VERBATIM = re.compile(r"!<[^>\s]*+>?")
# A plain chunk, as PyYAML scans one: up to whitespace or ': ' (in flow also
# ':' before a flow indicator, and any of ',?[]{}').
_YAML_BLOCK_CHUNK = re.compile(r"(?:[^ \t\r\n\x85  :]|:(?![ \t\r\n\x85  ]|\Z))++")
_YAML_FLOW_CHUNK = re.compile(r"(?:[^ \t\r\n\x85  :,?\[\]{}]|:(?![ \t\r\n\x85  ,\[\]{}]|\Z))++")
_YAML_BLOCK_HEADER = re.compile(r"[|>](?:[+-](?P<after>[1-9])?|(?P<before>[1-9])[+-]?)?")
_YAML_MARKER = re.compile(r"(?:---|\.\.\.)(?=[ \t\r\n\x85  ]|\Z)")
_YAML_DIRECTIVE_COMMENT = re.compile(r"[ \t]#")
# Every character with which the object scan (Python, JavaScript, JSON)
# starts a lexeme that can swallow what follows: a comment ('#', '//', '/*'),
# a string or template ('"', "'", '`', prefixes and triple quotes start with
# one) or a regular expression ('/'). Inside a YAML plain scalar any of them
# lets that scan's reading of the text part from YAML's.
_YAML_DIVERGENT = re.compile(r"[#\"'`/]")
# Characters glued to a quoted scalar that make the object scan pair its
# quotes differently: a third quote opens a triple-quoted string there.
_QUOTE_GLUE = frozenset("\"'`")


class _YamlLex:
    """One forward pass over a text as YAML: comments, quotes, flow structure.

    Records comment spans (``#`` to its line end), quoted-scalar spans, every
    real flow indicator (``[ ] { } ,`` outside scalars and comments) and the
    extent of each outermost flow collection. Token-level, following PyYAML's
    scanner: plain scalars by chunk with block continuation by indentation,
    block scalar content skipped whole, anchors, tags, directives, document
    markers. A quoted scalar that never closes ends at its own line (text that
    is not YAML must not lose everything after a stray quote). Checked against
    PyYAML's own token marks by an oracle differential.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        code = "i" if len(text) < 2**31 else "q"  # positions: 4 bytes each
        self.comment_starts = array(code)
        self.comment_ends = array(code)
        self.quoted_starts = array(code)
        self.quoted_ends = array(code)
        self.flow_positions = array(code)
        self.flow_chars = bytearray()
        # For each flow indicator, the index of its matching one ('[' and ']',
        # '{' and '}'; -1 for ',' or an unmatched bracket): one stack pass.
        self.flow_match = array(code)
        self.flow_opens = array(code)
        self.flow_closes = array(code)
        # Where YAML and a Python/JavaScript reading of the same text part
        # ways: a plain scalar holding a character that starts one of that
        # reading's swallowing lexemes, a quoted scalar that never closes, or
        # a single-quoted scalar ending in a backslash.
        self.divergent = array(code)
        # Where alias, tag and plain-scalar tokens begin: a merge key or an
        # alias key is only ever one of these, never text inside a scalar.
        self.alias_starts = array(code)
        self.tag_starts = array(code)
        self.plain_starts = array(code)  # only those starting '<' (merge keys)
        # Where an outermost flow collection opens that never closes: text that
        # is not YAML (code, a broken excerpt) read as flow from there on.
        self.unterminated_from = len(text)
        self._lex()

    # -- queries -------------------------------------------------------------

    def comment_in(self, low: int, high: int) -> int:
        """The first comment start in ``[low, high)``, or -1."""
        index = bisect_right(self.comment_starts, low - 1)
        if index < len(self.comment_starts) and self.comment_starts[index] < high:
            return self.comment_starts[index]
        return -1

    def comment_around(self, position: int) -> tuple[int, int] | None:
        """The (start, end) of the comment holding ``position``, if any."""
        index = bisect_right(self.comment_starts, position) - 1
        if index >= 0 and position < self.comment_ends[index]:
            return self.comment_starts[index], self.comment_ends[index]
        return None

    def quoted_around(self, position: int) -> tuple[int, int] | None:
        """The (start, end) of the quoted scalar holding ``position``, if any."""
        index = bisect_right(self.quoted_starts, position) - 1
        if index >= 0 and position < self.quoted_ends[index]:
            return self.quoted_starts[index], self.quoted_ends[index]
        return None

    def flow_index(self, position: int) -> int:
        """The index of the first flow indicator at or after ``position``."""
        return bisect_right(self.flow_positions, position - 1)

    def is_flow(self, position: int) -> bool:
        """Whether a real flow indicator stands at ``position``."""
        index = self.flow_index(position)
        return index < len(self.flow_positions) and self.flow_positions[index] == position

    @staticmethod
    def _has(starts: array, position: int) -> bool:
        index = bisect_right(starts, position) - 1
        return index >= 0 and starts[index] == position

    def alias_at(self, position: int) -> bool:
        return self._has(self.alias_starts, position)

    def tag_at(self, position: int) -> bool:
        return self._has(self.tag_starts, position)

    def plain_at(self, position: int) -> bool:
        return self._has(self.plain_starts, position)

    def divergent_in(self, low: int, high: int) -> bool:
        """Whether a divergent scalar starts in ``[low, high)``."""
        index = bisect_right(self.divergent, low - 1)
        return index < len(self.divergent) and self.divergent[index] < high

    def in_flow(self, position: int) -> bool:
        """Whether ``position`` lies inside a flow collection."""
        index = bisect_right(self.flow_opens, position) - 1
        return index >= 0 and position < self.flow_closes[index]

    # -- the lexer -----------------------------------------------------------

    def _lex(self) -> None:
        text = self.text
        n = len(text)
        pos = 1 if text.startswith("﻿") else 0
        line_start = 0
        depth = 0
        parent = -1  # the block indentation the next node belongs to
        key_col = key_line = -1  # where the current line's node began
        key_open = True  # the next token starts a node (after an indicator)
        block_scalar = -2  # minimum indent of a pending block scalar's content
        block_increment = 0  # its indentation indicator (|2, >-3), 0 when absent
        while pos < n:
            pos = _YAML_SPACES.match(text, pos).end()
            if pos >= n:
                break
            char = text[pos]
            if char == "#":
                end = _YAML_TO_BREAK.match(text, pos).end()
                self.comment_starts.append(pos)
                self.comment_ends.append(end)
                pos = end
                continue
            if char in _YAML_BREAKS:
                pos = line_start = _YAML_BREAK.match(text, pos).end()
                if block_scalar > -2:
                    pos = line_start = self._block_content(pos, block_scalar, block_increment)
                    block_scalar = -2
                continue
            column = pos - line_start
            if column == 0 and _YAML_MARKER.match(text, pos):
                if depth:
                    self.flow_closes.append(pos)
                pos += 3
                depth, parent = 0, -1
                continue
            if column == 0 and char == "%" and not depth:
                pos = self._directive(pos)
                continue
            following = text[pos + 1] if pos + 1 < n else ""
            if char in "[{,":
                key_open = True
            if char in "[{":
                if not depth:
                    self.flow_opens.append(pos)
                depth += 1
                self._flow(pos, char)
                pos += 1
                continue
            if char in "]}":
                if depth == 1:
                    self.flow_closes.append(pos + 1)
                if depth:
                    depth -= 1
                    self._flow(pos, char)
                pos += 1  # a token at block level too (PyYAML's), closing nothing
                continue
            if char == ",":
                if depth:
                    self._flow(pos, char)
                pos += 1
                continue
            if char in "?:" and (depth or following in _YAML_WHITE):
                if not depth:
                    if char == "?":
                        parent = column
                    else:
                        parent = key_col if key_line == line_start else column
                key_open = True
                pos += 1
                continue
            if char == "-" and following in _YAML_WHITE:
                if not depth:
                    parent = column
                key_open = True
                pos += 1
                continue
            if key_line != line_start or key_open:
                key_col, key_line, key_open = column, line_start, False
            if char in "&*":
                if char == "*":
                    self.alias_starts.append(pos)
                pos = _YAML_ANCHOR.match(text, pos + 1).end()
                continue
            if char == "!":
                self.tag_starts.append(pos)
                pattern = _YAML_VERBATIM if following == "<" else _YAML_NON_SPACE
                pos = max(pos + 1, pattern.match(text, pos).end())
                continue
            if char in "\"'":
                close = (_YAML_DOUBLE if char == '"' else _YAML_SINGLE).match(text, pos + 1)
                end = close.end() if close is not None else _YAML_TO_BREAK.match(text, pos).end()
                if (
                    close is None
                    or (
                        char == "'"
                        and (text.startswith("\\'", end - 2) or "''" in text[pos + 1 : end - 1])
                    )
                    or text[end : end + 1] in _QUOTE_GLUE
                    or text[pos - 1 : pos] in _QUOTE_GLUE
                ):
                    # An unclosed quote ends at its line here; the object scan
                    # runs it to the text's end. A backslash before a single
                    # quote's close escapes it for that scan, a doubled quote
                    # inside one is two strings to it, and a quote glued to
                    # either end opens a triple-quoted string or pairs the
                    # quotes differently.
                    self.divergent.append(pos)
                self.quoted_starts.append(pos)
                self.quoted_ends.append(end)
                pos = end
                continue
            if char in "|>" and not depth:
                header = _YAML_BLOCK_HEADER.match(text, pos)
                pos = header.end()
                block_scalar = parent + 1
                block_increment = int(header["after"] or header["before"] or 0)
                continue
            start = pos
            if char == "<":
                self.plain_starts.append(pos)
            pos, line_start = self._plain(pos, depth, parent + 1, line_start)
            if _YAML_DIVERGENT.search(text, start, pos):
                self.divergent.append(start)
        if depth:
            self.flow_closes.append(n)
            self.unterminated_from = self.flow_opens[-1]
        self._match()

    def _match(self) -> None:
        match = self.flow_match
        # Open brackets awaiting their close, as 4-byte positions: a list of int
        # objects cost ~36 bytes each, a transient that put a deep nest of flow
        # mappings over its peak budget once nothing cached the lexer (#2218).
        stack = array(match.typecode)
        chars = self.flow_chars
        for index in range(len(chars)):
            match.append(-1)
            char = chars[index]
            if char in b"[{":
                stack.append(index)
            elif char in b"]}" and stack:
                opener = stack.pop()
                match[opener] = index
                match[index] = opener

    def _flow(self, position: int, char: str) -> None:
        self.flow_positions.append(position)
        self.flow_chars.append(ord(char))

    def _plain(self, pos: int, depth: int, indent: int, line_start: int) -> tuple[int, int]:
        """Past one plain scalar: PyYAML's chunks, spaces and continuations."""
        text = self.text
        n = len(text)
        chunk = _YAML_FLOW_CHUNK if depth else _YAML_BLOCK_CHUNK
        start = pos
        while True:
            if pos < n and text[pos] == "#":
                return pos, line_start
            match = chunk.match(text, pos)
            if match is None or match.end() == pos:
                # Nothing a plain scalar may hold: step over one character, so
                # text that is not YAML always advances.
                return (pos if pos > start else pos + 1), line_start
            pos = match.end()
            after = pos
            while after < n and text[after] == " ":
                after += 1
            if after < n and text[after] in _YAML_BREAKS:
                probe = _YAML_BREAK.match(text, after).end()
                while True:
                    if _YAML_MARKER.match(text, probe):
                        return pos, line_start
                    spaces = probe
                    while spaces < n and text[spaces] == " ":
                        spaces += 1
                    if spaces < n and text[spaces] in _YAML_BREAKS:
                        probe = _YAML_BREAK.match(text, spaces).end()
                        continue
                    break
                if spaces >= n or text[spaces] == "#":
                    return pos, line_start
                if not depth and spaces - probe < indent:
                    return pos, line_start
                pos, line_start = spaces, probe
                continue
            if after == pos or after >= n or text[after] == "#":
                return pos, line_start
            pos = after

    def _block_content(self, pos: int, min_indent: int, increment: int) -> int:
        """Past a block scalar's content, from the line after its header.

        PyYAML's rule: with an indentation indicator the content indent is
        ``min_indent + N - 1``; without one it is the larger of ``min_indent``
        and the deepest run of leading spaces before the first content (blank
        lines included). Lines reaching that indent are content, blank lines
        too; the first shallower line ends the scalar.
        """
        text = self.text
        n = len(text)
        min_indent = max(min_indent, 1)
        if increment:
            indent = min_indent + increment - 1
        else:
            deepest = 0
            probe = pos
            while True:
                spaces = probe
                while spaces < n and text[spaces] == " ":
                    spaces += 1
                deepest = max(deepest, spaces - probe)
                if spaces < n and text[spaces] in _YAML_BREAKS:
                    probe = _YAML_BREAK.match(text, spaces).end()
                    continue
                break
            indent = max(min_indent, deepest)
        while pos < n:
            spaces = pos
            while spaces < n and text[spaces] == " " and spaces - pos < indent:
                spaces += 1
            if spaces >= n:
                return n
            if text[spaces] in _YAML_BREAKS:
                pos = _YAML_BREAK.match(text, spaces).end()
                continue
            if spaces - pos < indent:
                return pos
            line_end = _YAML_TO_BREAK.match(text, spaces).end()
            if line_end >= n:
                return n
            pos = _YAML_BREAK.match(text, line_end).end()
        return pos

    def _directive(self, pos: int) -> int:
        end = _YAML_TO_BREAK.match(self.text, pos).end()
        found = _YAML_DIRECTIVE_COMMENT.search(self.text, pos, end)
        if found is not None:
            self.comment_starts.append(found.start() + 1)
            self.comment_ends.append(end)
        return end


_LEX_CACHE: tuple[str, _YamlLex] | None = None


def _lex_for(text: str) -> _YamlLex:
    """The lexer for ``text``, built once per text (the last one is kept)."""
    global _LEX_CACHE
    cached = _LEX_CACHE
    if cached is not None and cached[0] is text:
        return cached[1]
    lexed = _YamlLex(text)
    _LEX_CACHE = (text, lexed)
    return lexed


# YAML anchor and alias tokens: after whitespace, a flow indicator or the
# start of the text, never inside a word or right after a quote ("&x" in a JSON
# string is text, not an anchor).
_ANCHOR = re.compile(r"(?:(?<=[\s\[{,])|\A)&([^\s,\[\]{}]++)")
_ALIAS = re.compile(r"(?:(?<=[\s\[{,:])|\A)\*([^\s,\[\]{}]++)")
_ALIAS_AT = re.compile(r"\*(?P<name>[^\s,\[\]{}()\"'`&*\\]++)")
# Evidence that a document needs the conservative reading: an alias used as a
# key (*n : value, or ? *n). Merge keys use the oct-block rule's merge hint.
_ALIAS_KEY_HINT = re.compile(
    rf"(?:(?<=[{{,])|(?:\A|(?<=[{_LINE_CHARS}]))[ \t]*+(?:[-?][ \t]++)*+)[ \t]*+"
    r"\*(?P<name>[^\s,\[\]{}()*&:'\"`]++)[ \t]*+:(?:[\s,\]}]|\Z)"
    r"|\?[ \t]++\*(?P<explicit>[^\s,\[\]{}()*&:'\"`]++)"
)
# Key-type evidence a conservative reading needs: the word RSA, EC, OKP or oct
# (a merge can carry an oct type into a mapping inside a decided object, where
# no oct-block reading is left), or a double-quoted value with an escape
# ("R\x53A"). One pass with no rescans; the hazard checks below keep ordinary
# code out of conservative documents.
_TYPE_VALUE = re.compile(r"(?<![\w.-])(?:RSA|EC|OKP|oct)(?![\w.-])|\"[^\"\\\r\n]{0,8}\\")
# A merge key candidate: '<<', plain or quoted (libyaml and Psych merge on a
# quoted one). ``_merge_key_in`` checks its key position and its ':' with the
# lexer, so a '<<:' inside a string, a comment or an expression is not one.
_MERGE_CANDIDATE = re.compile(
    r"(?P<quote>[\"']?)<<(?P=quote)|(?P<tag>!!merge(?![\w-])|!<tag:yaml\.org,2002:merge>)"
)
# Node properties: a verbatim tag (bounded) or any other tag or anchor,
# possessive (it stops at whitespace, a comma or a bracket).
_PROPERTY_AT = re.compile(r"!<[^>\s]{0,1024}+>|[&!][^\s,\[\]{}]*+")
# A flow key's name and ':' (after its gap and properties): quoted, alias or a
# one-word plain name.
_FLOW_KEY_NAME = re.compile(
    r"(?P<key>\"(?:\\.|[^\"\\])*+\"|'(?:''|[^'])*+'|\*[^\s,\[\]{}()*&:'\"`]++"
    r"|[^\s,\[\]{}#'\"&!*:?][^\s,\[\]{}:]*+)"
    rf"[ \t{_LINE_CHARS}]*+:"
)
_SPACES = re.compile(r"[ \t]*+")
_BLANKS = re.compile(rf"[ \t{_LINE_CHARS}]*+")
_CLOSE = {
    '"': re.compile(r"(?:\\[\s\S]|[^\"\\])*+\""),
    "'": re.compile(r"(?:''|[^'])*+'"),
}


class _Quotes:
    """Closing quotes for quoted scalars opened in increasing order.

    A quote opened before the last close found shares that close: the earlier
    search consumed its region from an aligned token boundary, so a fresh search
    would end at the same place or earlier. Reusing it never ends a mask early.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.last: dict[str, tuple[int, int]] = {'"': (-1, -1), "'": (-1, -1)}

    def close(self, opening: int) -> int:
        """Offset of the closing quote, or -1 when the scalar never closes."""
        quote = self.text[opening]
        low, high = self.last[quote]
        if low < opening < high:
            return high if high < len(self.text) else -1
        found = _CLOSE[quote].match(self.text, opening + 1)
        end = found.end() - 1 if found is not None else len(self.text)
        self.last[quote] = (opening, end)
        return end if found is not None else -1


def _lines(text: str) -> Iterator[tuple[int, int]]:
    """(start, content end) of every physical line, splitlines()-compatible."""
    start = 0
    for line_break in _BREAK.finditer(text):
        yield start, line_break.start()
        start = line_break.end()
    if start < len(text):
        yield start, len(text)


def _single_quoted(raw: str) -> str:
    """A single-quoted YAML scalar's value, with its line folding."""
    lines = _BREAK.split(raw[1:-1].replace("''", "'"))
    parts = [lines[0] if len(lines) == 1 else lines[0].rstrip(" \t")]
    newlines = 0
    for index, line in enumerate(lines[1:], 2):
        last = index == len(lines)
        line = line.lstrip(" \t") if last else line.strip(" \t")
        if not line and not last:
            newlines += 1
            continue
        parts.append("\n" * newlines if newlines else " ")
        parts.append(line)
        newlines = 0
    return "".join(parts)


_LINE_BREAK_CHARS = frozenset("\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029")


def _flow_value_end(lex: _YamlLex, position: int, limit: int) -> int:
    """End of a flow value: the next real ',' or closing bracket at its depth.

    Nested collections are jumped over by the lexer's bracket matching, so a
    value costs its own siblings, never its nested content again. An
    unclosed bracket runs to ``limit`` (failing toward masking).
    """
    positions, chars, match = lex.flow_positions, lex.flow_chars, lex.flow_match
    index = lex.flow_index(position)
    while index < len(positions):
        at = positions[index]
        if at >= limit:
            break
        char = chars[index]
        if char in b"[{":
            if match[index] < 0:
                return limit
            index = match[index] + 1
            continue
        return at  # a ',' or a closing bracket at this depth
    return limit


def _collection_end(lex: _YamlLex, position: int, limit: int) -> int:
    """Just past the bracket closing the flow collection opened at ``position``.

    Commas at any depth belong to the collection. An unterminated collection
    runs to ``limit`` (the end of its document).
    """
    index = lex.flow_index(position)
    if index >= len(lex.flow_positions) or lex.flow_positions[index] != position:
        return limit
    closing = lex.flow_match[index]
    if closing < 0:
        return limit
    return min(limit, lex.flow_positions[closing] + 1)


class _Anchors:
    """Anchor definitions (``&name``) by name, read lazily, one pass to index.

    A scalar anchor resolves an alias key (``*n : value``) to its name. A node
    anchor gives the extent masked when a private member's value is its alias.
    """

    def __init__(self, text: str, double_quoted: Callable[[str], str | None]) -> None:
        self.text = text
        self.double_quoted = double_quoted
        self._index: dict[str, list[int]] | None = None
        self._nodes: dict[int, tuple[int, int]] = {}
        self._scalars: dict[int, str | None] = {}
        self._documents: list[int] | None = None
        self._starts: list[int] | None = None  # line starts, for O(log n) lookups
        self._ends: list[int] | None = None  # each line's content end

    def line(self, position: int) -> tuple[int, int]:
        """(start, content end) of the physical line holding ``position``."""
        if self._starts is None:
            self._starts, self._ends = [0], []
            for line_break in _BREAK.finditer(self.text):
                self._ends.append(line_break.start())
                self._starts.append(line_break.end())
            self._ends.append(len(self.text))
        assert self._ends is not None
        index = bisect_right(self._starts, position) - 1
        return self._starts[index], self._ends[index]

    def index(self) -> dict[str, list[int]]:
        if self._index is None:
            self._index = {}
            for anchor in _ANCHOR.finditer(self.text):
                self._index.setdefault(anchor.group(1), []).append(anchor.end())
        return self._index

    def definition(self, name: str, at: int) -> int:
        """The end of the latest ``&name`` before ``at``; -1 when there is none."""
        ends = self.index().get(name)
        if not ends:
            return -1
        found = bisect_right(ends, at) - 1
        return ends[found] if found >= 0 else -1

    def _markers(self) -> list[int]:
        if self._documents is None:
            self._documents = [
                marker.start() for marker in oct_blocks._DOCUMENT_MARKER.finditer(self.text)
            ]
        return self._documents

    def document_end(self, position: int) -> int:
        markers = self._markers()
        found = bisect_right(markers, position)
        return markers[found] if found < len(markers) else len(self.text)

    def document_start(self, position: int) -> int:
        markers = self._markers()
        found = bisect_right(markers, position) - 1
        return markers[found] if found >= 0 else 0

    def candidate_range(self, name: str, at: int) -> tuple[list[int], int, int]:
        """``name``'s definition list and the bounds of its candidates before ``at``.

        Every ``&name`` before ``at`` in its document is a candidate. Text cannot
        always tell a real anchor from ``&name`` in a comment, a quoted or block
        scalar or plain prose, and YAML uses the latest real one, so every
        candidate counts: a fake anchor can only add a masked node, never take
        the real one's place (task #2200, review r2).
        """
        ends = self.index().get(name)
        if not ends:
            return [], 0, 0
        low = bisect_right(ends, self.document_start(at) - 1)
        return ends, low, bisect_right(ends, at)

    def scalar(self, name: str, at: int) -> str | None:
        """The scalar an alias at ``at`` names, or None when unknown or ambiguous.

        Every candidate definition must read the same scalar; a mix (a fake
        ``&n x`` after the real ``&n d``), a node that is not a scalar, or more
        candidates than are worth reading leaves the name unknown, which the
        callers mask as a private member.
        """
        index, low, high = self.candidate_range(name, at)
        if high <= low or high - low > _AMBIGUOUS_CANDIDATES:
            return None
        values = set()
        for end in index[low:high]:
            if end not in self._scalars:
                self._scalars[end] = self._read_scalar(end)
            values.add(self._scalars[end])
        return values.pop() if len(values) == 1 else None

    def _read_scalar(self, end: int) -> str | None:
        text = self.text
        line_end = self.line(end)[1]
        position = _PROPERTIES.match(text, _SPACES.match(text, end, line_end).end(), line_end).end()
        if position >= line_end:
            return None
        char = text[position]
        if char in "\"'":
            close = _CLOSE[char].match(text, position + 1)
            if close is None:
                return None
            raw = text[position : close.end()]
            return self.double_quoted(raw) if char == '"' else _single_quoted(raw)
        if char in "[{*|>#":
            return None
        stop = position
        while (
            stop < line_end
            and text[stop] not in ",[]{}#"
            and not (text[stop] == ":" and (stop + 1 == line_end or text[stop + 1] in " \t"))
        ):
            stop += 1
        return text[position:stop].rstrip(" \t") or None

    def node(self, end: int) -> tuple[int, int]:
        """The extent of the node anchored by the definition ending at ``end``."""
        known = self._nodes.get(end)
        if known is None:
            known = self._nodes[end] = self._read_node(end)
        return known

    def _read_node(self, end: int) -> tuple[int, int]:
        text = self.text
        limit = self.document_end(end)
        start_of_line, line_end = self.line(end)
        line_end = min(line_end, limit)
        indent = 0
        while text.startswith(" ", start_of_line + indent):
            indent += 1
        dash_line = text.startswith("-", start_of_line + indent)
        position = _PROPERTIES.match(text, _SPACES.match(text, end, line_end).end(), line_end).end()
        char = text[position] if position < line_end else "#"
        if char in "\"'":
            quoted = _lex_for(text).quoted_around(position)
            if quoted is not None and quoted[0] == position:
                return position, min(limit, quoted[1])
            close = _CLOSE[char].match(text, position + 1, limit)
            return position, close.end() if close is not None else limit
        lex = _lex_for(text)
        if char in "[{" and lex.is_flow(position):
            return position, _collection_end(lex, position, limit)
        if char != "#" and position < lex.unterminated_from and lex.in_flow(position):
            # A plain scalar or an alias inside a flow collection ends at its
            # next real ',' or closing bracket, not at the line's end.
            stop = _flow_value_end(lex, position, limit)
            while stop > position and (
                text[stop - 1] in " \t" or text[stop - 1] in _LINE_BREAK_CHARS
            ):
                stop -= 1
            return (position, stop) if stop > position else (end, end)
        # A plain or block scalar, a block collection below, or an alias: the
        # rest of the line, then every deeper line (and, under a key, its
        # compact sequence). Comment lines are kept only inside the extent.
        first = position if char != "#" else -1
        last = line_end if char != "#" else -1
        cursor = line_end
        for line in _BREAK.finditer(text, line_end, limit):
            if line.start() != cursor:
                break
            begin = line.end()
            content = begin
            while content < limit and text[content] == " ":
                content += 1
            stop = content
            while stop < limit and text[stop] not in _LINE_BREAK_CHARS:
                stop += 1
            cursor = stop
            if content >= stop or text[content] == "#":
                continue
            deeper = content - begin > indent
            compact = (
                content - begin == indent
                and not dash_line
                and text.startswith("-", content)
                and (content + 1 == stop or text[content + 1] in " \t")
            )
            if not (deeper or compact):
                break
            if first < 0:
                first = content
            last = stop
        if first < 0:
            return end, end
        while last > first and text[last - 1] in " \t":
            last -= 1
        return first, last


def _type_bits(value: str | None) -> int:
    """The private-set bits a type value selects; an interpolated one, all."""
    if _interpolated(value):
        return 3
    return _TYPE_BITS.get(value or "", 0)


class _Mapping:
    __slots__ = ("column", "bits", "members", "start", "eligible")

    def __init__(self, column: int, start: int, eligible: bool = False) -> None:
        self.column = column
        self.start = start
        self.bits = 0
        self.members: list[tuple[str, int, int]] = []
        # Takes part in YAML merging or aliasing: a merge, tagged, alias or type
        # key at its own level, or the node of an anchor. Only such a mapping is
        # read conservatively; a plain mapping (or a Python dict) never is.
        self.eligible = eligible


class _Entry:
    __slots__ = (
        "column",
        "mapping",
        "name",
        "start",
        "end",
        "plain",
        "awaiting",
        "empty",
        "anchored",
    )

    def __init__(self, column: int, mapping: _Mapping, name: str) -> None:
        self.column = column
        self.mapping = mapping
        self.name = name
        self.start = -1
        self.end = -1
        self.plain = True
        self.awaiting = True  # nothing after the colon yet: the value is below
        self.empty = True  # nothing after the colon at all (a compact sequence may follow)
        self.anchored = False  # only properties with an anchor after the colon


def _reading(
    text: str,
    cut: oct_blocks._Spans,
    double_quoted: Callable[[str], str | None],
    *,
    skip_quotes: bool,
    out: list[tuple[str, int, int]],
    anchors: _Anchors,
    conservative: oct_blocks._Spans,
    lex: _YamlLex,
) -> None:
    quotes = _Quotes(text)
    mappings: list[_Mapping] = []
    entries: list[_Entry] = []
    last_end = last_plain_end = -1  # the latest content line's end, and before its comment
    skip_until = -1
    # An explicit key awaiting its ': value' line: (column, raw name, its offset).
    # A name of None is still to come, on the next deeper line.
    explicit: tuple[int, str | None, int] | None = None

    def key_name(raw: str, at: int) -> str | None:
        """A key node's name: properties skipped, quoted or alias resolved."""
        raw = raw[_PROPERTIES.match(raw).end() :]
        if _interpolated(raw):
            return _INTERPOLATION  # built at runtime: maybe the type, maybe private
        if raw[:1] == '"':
            return double_quoted(raw)
        if raw[:1] == "'":
            return raw[1:-1].replace("''", "'") if raw.endswith("'") and len(raw) > 1 else None
        if raw[:1] == "*":
            alias = _ALIAS_AT.match(raw)
            resolved = anchors.scalar(alias["name"], at) if alias is not None else None
            return resolved if resolved is not None else "*"
        return raw.rstrip(" \t")

    def scalar(position: int, high: int) -> str | None:
        """The scalar starting at ``position``: a key type candidate."""
        position = _PROPERTIES.match(text, position, high).end()
        if position >= high:
            return None
        char = text[position]
        if char == "*":
            return "*"
        if char in "\"'":
            close = quotes.close(position)
            if close < 0:
                return None
            raw = text[position : close + 1]
            return double_quoted(raw) if char == '"' else _single_quoted(raw)
        comment = lex.comment_in(position, high)
        return text[position : comment if comment >= 0 else high].rstrip(" \t")

    def close_entries(column: int, *, keep_compact: bool = False) -> None:
        while entries and entries[-1].column >= column:
            entry = entries[-1]
            if keep_compact and entry.column == column and entry.empty:
                break
            entries.pop()
            if entry.start < 0:
                continue
            end = max(entry.end, last_plain_end if entry.plain else last_end)
            if end > entry.start and entry.name != "kty":
                entry.mapping.members.append((entry.name, entry.start, end))

    def close_mappings(column: int) -> int:
        """Close mappings deeper than ``column``; their type bits, combined."""
        bits = 0
        while mappings and mappings[-1].column > column:
            closed = mappings.pop()
            finish(closed)
            bits |= closed.bits
        return bits

    def finish(mapping: _Mapping) -> None:
        if mapping.eligible and conservative.spans and conservative.contains(mapping.start):
            # A document using merges or alias keys: any mapping may hold a
            # key's private members, or inherit its type (fail toward masking).
            mapping.bits = 3
        if not mapping.bits:
            return
        for name, start, end in mapping.members:
            if name in _RSA_ONLY and not mapping.bits & 1:
                continue
            out.append((name, start, end))

    def trimmed(start: int, end: int) -> int:
        while end > start and text[end - 1] in " \t":
            end -= 1
        return end

    def extend(start: int, high: int) -> None:
        nonlocal last_end, last_plain_end
        last_end = trimmed(start, high)
        comment = lex.comment_in(start, high)
        last_plain_end = trimmed(start, comment) if comment >= 0 else last_end

    def enter(start: int, position: int, raw: str, value: int, key_line: bool, key_at: int) -> None:
        """One key at ``position``: its mapping, name, eligibility and value."""
        nonlocal explicit, skip_until, last_end, last_plain_end, dedent_bits
        column = position - start
        if not mappings or mappings[-1].column != column:
            # The mapping below an anchored, otherwise empty value is that
            # anchor's node (priv: &p, then its keys on the deeper lines).
            anchored = bool(entries and entries[-1].anchored and entries[-1].column < column) or (
                0 <= pending_anchor <= column
            )
            mappings.append(_Mapping(column, position, anchored))
            # A key dedented to a column no open mapping has is malformed in
            # every reading (' kty: RSA' then 'd: x'): its mapping takes the
            # type of the mappings that line closed, failing toward masking.
            mappings[-1].bits |= dedent_bits
        mapping = mappings[-1]
        name = key_name(raw, position) if raw else None
        if (
            name in ("<<", "kty")
            or raw[:1] == "*"
            or (value >= 0 and "!" in text[position:value] and not key_line)
            or "&" in text[position:key_at]
        ):
            mapping.eligible = True
        anchored_value = False
        flow_mapping = False
        if value >= 0 and not key_line:
            properties_end = _PROPERTIES.match(text, value, high).end()
            anchored_value = (
                properties_end > value
                and "&" in text[value:properties_end]
                and (properties_end >= high or text.startswith("#", properties_end))
            )
            flow_mapping = text.startswith("{", properties_end)
        if name == _INTERPOLATION and flow_mapping:
            # A mapping value (`"${1:3000}": {`, an editor snippet in a JSON
            # schema): the key is neither a type nor a private member, both
            # of which are scalars (`oth` a sequence) (#2222).
            name = None
        elif name == _INTERPOLATION:
            # An unknown name may be the type (of any kind) or a private member.
            mapping.bits |= 3
            name = "*"
        extend(position, high)
        if key_line:
            explicit = (column, raw or None, position)
            name = None
        if name not in _NAMES:
            # Not a member we act on, but its value still ends at a lower line.
            entries.append(_Entry(column, mapping, ""))
            entries[-1].awaiting = entries[-1].empty = False
            entries[-1].anchored = anchored_value
            return
        current = _Entry(column, mapping, name)
        current.anchored = anchored_value
        entries.append(current)
        properties = _PROPERTIES.match(text, value, high).end()
        only_properties = properties >= high or text.startswith("#", properties)
        if only_properties and (name == "kty" or properties == value):
            return  # the value (or the type) is on the lines below
        current.awaiting = False
        # Only node properties after the colon (d: !!seq, d: &a): the node is
        # below, and may be a compact sequence at the key's own column.
        current.empty = only_properties
        current.start = value
        char = text[properties] if properties < high else "#"
        if name == "kty":
            mapping.bits |= _type_bits(scalar(value, high))
            current.start = -1
            return
        if char in "\"'":
            close = quotes.close(properties)
            # A closed quoted scalar keeps a trailing comment, as a plain one
            # does; one that never closes runs to its last line's end.
            current.plain = close >= 0
            current.end = close + 1 if close >= 0 else last_end
            if close >= 0 and skip_quotes and close >= high:
                skip_until = close
                last_end = last_plain_end = close + 1
        elif char in "|>":
            current.plain = False
            current.end = last_end
        else:
            current.end = last_plain_end

    pending_anchor = -1  # the least column of a mapping a properties line anchors
    dedent_bits = 0  # type bits of mappings this line closed (a plain key line)
    for start, high in _lines(text):
        if start <= skip_until:
            continue
        dedent_bits = 0
        head = _HEAD.match(text, start, high)
        position = head.end()
        indent = len(head["indent"])
        if position >= high and not head["marks"]:
            continue  # blank
        if text.startswith("#", position) and not head["marks"]:
            # Comments never end a value or a mapping; one deeper than every
            # open value may be block-scalar content.
            if entries and position - start > entries[-1].column:
                extend(position, high)
            continue
        if not indent and not head["marks"] and _DOCUMENT.match(text, position, high):
            close_entries(0)
            close_mappings(-1)
            pending_anchor = -1
            continue
        raw = None
        value = -1
        key_line = False  # an explicit key: its value comes on a later line
        key_at = position  # where the key's name starts, after its properties
        inner = None  # an explicit value holding a compact mapping's first key
        if position < high and not cut.contains(position):
            entry = _ENTRY.match(text, position, high)
            if entry is not None:
                raw, value = entry["key"], entry.end()
                key_at = entry.start("key")
            elif (question := _EXPLICIT.match(text, position, high)) is not None:
                name_at = _PROPERTIES.match(text, question.end(), high).end()
                comment = lex.comment_in(name_at, high)
                raw = text[name_at : trimmed(name_at, comment if comment >= 0 else high)]
                key_line = True
            elif (
                explicit is not None
                and explicit[0] == position - start
                and (colon := _EXPLICIT_VALUE.match(text, position, high)) is not None
            ):
                raw, value = explicit[1] or "", colon.end()
                inner_at = _SPACES.match(text, value, high).end()
                if inner_at < high and not cut.contains(inner_at):
                    inner = _ENTRY.match(text, inner_at, high)
                    if inner is not None:
                        value = high  # the outer value is the mapping below
            elif (
                explicit is not None
                and explicit[1] is None
                and not head["marks"]
                and position - start > explicit[0]
            ):
                # An explicit key's name on the deeper line below its '?'.
                name_at = _PROPERTIES.match(text, position, high).end()
                comment = lex.comment_in(name_at, high)
                name_raw = text[name_at : trimmed(name_at, comment if comment >= 0 else high)]
                if name_raw:
                    explicit = (explicit[0], name_raw, name_at)
        if raw is not None or head["marks"]:
            explicit = None
        if head["marks"]:
            # An item: it ends deeper values and mappings, and starts its own.
            # A compact sequence (``oth:`` then items at its column) is a value.
            close_entries(indent, keep_compact=True)
            close_mappings(indent)
        elif raw is not None:
            close_entries(indent)
            dedent_bits = close_mappings(indent)
        else:
            close_entries(indent)
            if not skip_quotes:
                close_mappings(indent)
        # A line of node properties alone ('!!str', '&a', '- &a'), maybe commented.
        properties_end = position
        if raw is None and position < high and not cut.contains(position):
            properties_end = _PROPERTIES.match(text, position, high).end()
        properties_line = properties_end > position and (
            properties_end >= high or text.startswith("#", properties_end)
        )
        if (
            entries
            and entries[-1].awaiting
            and entries[-1].start < 0
            # 'kty:' then a properties line: the type scalar is still below.
            and not (entries[-1].name == "kty" and properties_line and not head["marks"])
        ):
            awaiting = entries[-1]
            awaiting.awaiting = False
            if raw is None and awaiting.name == "kty" and not head["marks"]:
                awaiting.mapping.bits |= _type_bits(scalar(position, high))
            awaiting.start = head.start("marks") if head["marks"] else position
            awaiting.plain = False
        if raw is None:
            # A line of node properties holding an anchor ('- &a', or '&a'
            # alone): the next mapping at or below it is that anchor's node.
            pending_anchor = -1
            if properties_line and "&" in text[position:properties_end]:
                pending_anchor = indent + 1 if head["marks"] else position - start
            if position < high:
                extend(position, high)
            continue
        enter(start, position, raw, value, key_line, key_at)
        pending_anchor = -1
        if inner is not None:
            # An explicit value that is itself a compact mapping (: <<: *b, its
            # siblings below): the outer key's value starts at the inner key.
            outer = entries[-1] if entries else None
            if outer is not None and outer.awaiting and outer.start < 0:
                outer.awaiting = outer.empty = False
                outer.start, outer.plain = inner.start(), False
            enter(start, inner.start(), inner["key"], inner.end(), False, inner.start("key"))
    close_entries(0)
    close_mappings(-1)


def _merge_key_in(text: str, lex: _YamlLex, low: int, high: int) -> bool:
    """Whether a real merge key stands in ``text[low:high]``.

    A '<<' (plain, or a whole quoted scalar), or a merge tag, outside comments
    and other scalars, in key position: at a line start (after indentation, '- ', '? '
    or an explicit value's ': ' markers and node properties) or right after a
    real '{' or ','; then its ':' before whitespace, a line end or a flow
    indicator, or in flow glued to a collection (or, quoted, to an alias), or
    as an explicit key nothing but a comment or the line end after it.
    """
    for candidate in _MERGE_CANDIDATE.finditer(text, low, high):
        start, end = candidate.start(), candidate.end()
        if lex.comment_around(start) is not None or start >= lex.unterminated_from:
            continue  # a comment, or structure only a runaway flow reading made
        quoted = lex.quoted_around(start)
        if candidate["tag"]:
            # A merge tag on a key (!!merge <<, !!merge "<<", ? !!merge k):
            # the same key position as '<<', whatever follows the tag.
            if not lex.tag_at(start):
                continue
        elif candidate["quote"]:
            if quoted != (start, end):
                continue
        elif not lex.plain_at(start):
            continue  # inside another scalar, a comment or a tag

        def back_blanks(position: int) -> int:
            while position > low and text[position - 1] in " \t":
                position -= 1
            return position

        position = back_blanks(start)
        for _ in range(64):  # node properties before the key: &m !!merge <<
            token = position
            while token > low and not text[token - 1].isspace() and text[token - 1] not in "[{,":
                token -= 1
            if token < position and text[token] in "&!":
                position = back_blanks(token)
                continue
            break
        keyed = False
        while True:
            if position <= low or text[position - 1] in _LINE_BREAK_CHARS:
                keyed = True
                break
            previous = text[position - 1]
            if previous in "{," and lex.is_flow(position - 1):
                keyed = True
                break
            if previous in "-?:" and text[position] in " \t":
                position = back_blanks(position - 1)
                continue
            break
        if not keyed:
            continue
        if candidate["tag"]:
            return True
        after = end
        while after < high and text[after] in " \t":
            after += 1
        if after >= high or text[after] in _LINE_BREAK_CHARS or text[after] == "#":
            return True  # an explicit key: '? <<', its value below
        if text[after] != ":":
            continue
        following = text[after + 1] if after + 1 < len(text) else ""
        if not following or following in " \t,]}" or following in _LINE_BREAK_CHARS:
            return True
        if lex.in_flow(start) and (following in "[{" or (candidate["quote"] and following == "*")):
            return True
    return False


def _conservative_documents(
    text: str, kty_hint: Callable[[str], bool] | None, anchors: _Anchors, lex: _YamlLex
) -> list[tuple[int, int]]:
    """Documents with key-type evidence that also use a merge key or an alias key.

    Their structure is not resolved: a merge can carry a type or a private
    member between mappings, and an alias key can name one. In such a document
    every private-named member is masked, in block and flow mappings alike.
    """
    if "<<" not in text and "*" not in text and "!" not in text:
        return []
    markers = [marker.start() for marker in oct_blocks._DOCUMENT_MARKER.finditer(text)]
    bounds = [0, *markers, len(text)]
    # A %TAG directive belongs to the document its following '---' starts (or,
    # with no marker after it, to its own range); it is not global.
    directed = set()
    for directive in oct_blocks._TAG_DIRECTIVE.finditer(text):
        following = bisect_right(markers, directive.start())
        if following < len(markers):
            directed.add(markers[following])
        else:
            directed.add(bounds[bisect_right(bounds, directive.start()) - 1])
    documents = []
    for low, high in zip(bounds, bounds[1:], strict=False):
        if low >= high:
            continue
        if _TYPE_VALUE.search(text, low, high) is None:
            continue
        hazard = low in directed or _merge_key_in(text, lex, low, high)
        if not hazard:
            # An alias key counts only where its anchor is defined before it
            # in this document: ordinary code (f(a, *args: int)) defines none.
            for key in _ALIAS_KEY_HINT.finditer(text, low, high):
                at = (key.start("name") if key["name"] else key.start("explicit")) - 1
                if not lex.alias_at(at) or at >= lex.unterminated_from:
                    continue  # inside a scalar or a comment, or a runaway flow
                name = key["name"] or key["explicit"]
                if anchors.definition(name, key.start()) >= low:
                    hazard = True
                    break
        if hazard and (kty_hint is None or kty_hint(text[low:high])):
            documents.append((low, high))
    return documents


def _anchored_before(text: str, opening: int, low: int, lex: _YamlLex) -> bool:
    """Whether node properties right before ``opening`` include an anchor.

    The properties may be glued to a flow indicator (``[&a {``, ``,&a {``),
    end the line before (``[&a`` then ``{`` below), or stand alone on that
    line (``&a`` then ``{`` below), with comments between: blanks, line breaks
    and a crossed line's trailing comment are skipped, and a property token
    ends at whitespace or a flow indicator. A token longer than 256 characters,
    or more than 64 crossed comment lines, counts as an anchor (failing toward
    masking: this only decides which mappings a conservative reading masks).
    """
    position = opening
    for _ in range(2):  # an anchor, or a tag after an anchor (&p !!map {)
        crossed = 0
        while True:
            while position > low and text[position - 1].isspace():
                position -= 1
            comment = lex.comment_around(position - 1) if position > low else None
            if comment is None:
                break
            crossed += 1
            if crossed > 64:
                return True
            position = comment[0]  # a comment is whitespace here
        end = position
        while (
            position > low and not text[position - 1].isspace() and text[position - 1] not in "[{,"
        ):
            if end - position >= 256:
                return True  # too long to read: fail toward masking
            position -= 1
        token = text[position:end]
        if token.startswith("&") and len(token) > 1:
            return True
        if not token.startswith("!"):
            return False
    return False


class _FlowFrame:
    __slots__ = ("mapping", "eligible", "anchored", "merging", "members", "bits", "start")

    def __init__(self, mapping: bool, anchored: bool, start: int) -> None:
        self.mapping = mapping
        self.anchored = anchored
        self.eligible = anchored
        # A merge key, a tagged key or an alias key at its own level, or an
        # anchored node: YAML naming the object scan never resolves, so its
        # members are kept even inside a decided object (review r9).
        self.merging = anchored
        self.members: list[tuple[str, int, int]] = []
        self.bits = 0  # the YAML reading's key types: RSA 1, EC/OKP 2, oct 4
        self.start = start


_FLOW_TYPE_BITS = {"RSA": 1, "EC": 2, "OKP": 2, "oct": 4, "*": 7}
# A private-named key anywhere in a typed mapping's text, for the sweep below:
# not part of a longer word, quoted or bare, then ':'.
_PRIVATE_KEY_SWEEP = re.compile(
    r"(?<![\w$.-])(?P<quote>[\"']?)(?P<name>dp|dq|qi|oth|d|p|q|k)(?P=quote)[ \t]*+:"
)
_SWEEP_VALUE_END = re.compile(rf"[,}}\]{_LINE_CHARS}]")
_FLOW_PRIVATE = _PRIVATE_NAMES | {"k"}


def _flow_type_bits(value: str | None) -> int:
    if _interpolated(value):
        return 7
    return _FLOW_TYPE_BITS.get(value or "", 0)


def _flow_members(
    text: str,
    lex: _YamlLex,
    anchors: _Anchors,
    double_quoted: Callable[[str], str | None],
    conservative: oct_blocks._Spans,
    out: list[tuple[str, int, int]],
    typed_out: list[tuple[str, int, int]],
    uncut_out: list[tuple[str, int, int]],
) -> None:
    """Private-named members of flow mappings, as YAML reads them.

    One pass over the lexer's real flow indicators (quoted scalars and
    comments are never structure). In a conservative document an eligible
    mapping (a merge, tagged, alias or type key at its own level, or an
    anchored node) has every private-named member masked (``out``). One that
    merges, tags or aliases a key, or is anchored, keeps them even inside an
    object the object scan decided (``uncut_out``): that scan reads JSON and
    never resolves a merge in a mapping nested in a quoted-type object, an
    alias key (``*n : x``) or a merged-in member. Anywhere,
    a mapping YAML reads as typed (its own ``kty`` holds a key type, an alias
    or an interpolation) has its private members masked (``typed_out``): the
    object scan reads Python and JavaScript, so a YAML-only reading of a flow
    mapping (a glued '#' inside a plain scalar, a quote inside one) would
    otherwise lose members it cannot see.
    """
    frames: list[_FlowFrame] = []
    high = len(text)
    sweeps: list[tuple[int, int, int]] = []  # typed mappings where the readings part

    def gap(position: int) -> int:
        while True:
            position = _BLANKS.match(text, position, high).end()
            comment = lex.comment_around(position) if position < high else None
            if comment is None or comment[0] != position:
                return position
            position = comment[1]

    def value_text(start: int, end: int) -> str | None:
        position = start
        while position < end:
            node = _PROPERTY_AT.match(text, position, end)
            if node is None or node.end() == position:
                break
            position = gap(node.end())
        if position >= end:
            return None
        char = text[position]
        if char == "*":
            return "*"
        if char in "\"'":
            quoted = lex.quoted_around(position)
            if quoted is None or quoted[0] != position:
                return None
            raw = text[position : quoted[1]]
            return double_quoted(raw) if char == '"' else _single_quoted(raw)
        comment = lex.comment_in(position, end)
        stop = comment if comment >= 0 else end
        line = _BREAK.search(text, position, stop)
        return text[position : line.start() if line else stop].strip(" \t")

    def key_at(at: int) -> None:
        frame = frames[-1]
        position = gap(at)
        has_tag = False
        while True:  # '? ' and node properties, in either order: ? !!merge <<
            if text.startswith("?", position) and text[position + 1 : position + 2].isspace():
                position = gap(position + 1)
                continue
            node = _PROPERTY_AT.match(text, position, high)
            if node is None or node.end() == position:
                break
            has_tag = has_tag or node.group().startswith("!")
            position = gap(node.end())
        key = _FLOW_KEY_NAME.match(text, position, high)
        if key is None:
            return
        raw = key["key"]
        if raw[0] == '"':
            name = double_quoted(raw)
        elif raw[0] == "'":
            name = raw[1:-1].replace("''", "'")
        elif raw[0] == "*":
            name = anchors.scalar(raw[1:], key.start("key"))
            name = "*" if name is None else name
            frame.eligible = frame.merging = True
        else:
            name = raw
        if name in ("<<", "kty") or has_tag:
            frame.eligible = True
        if name == "<<" or has_tag:
            frame.merging = True
        value = gap(key.end())
        end = _flow_value_end(lex, value, high)
        if _interpolated(name):
            # Built at runtime: it may be the type (when its value is a key
            # type, an alias or an interpolation) or a private member.
            frame.bits |= _flow_type_bits(value_text(value, end))
            name = "*"
        if name == "kty":
            frame.bits |= _flow_type_bits(value_text(value, end))
            return
        if name not in _FLOW_PRIVATE:
            return
        while end > value and (text[end - 1] in " \t" or text[end - 1] in _LINE_BREAK_CHARS):
            end -= 1
        if end > value:
            frame.members.append((name, value, end))

    def close(at: int) -> None:
        frame = frames.pop()
        if frame.eligible and conservative.spans and conservative.contains(frame.start):
            # A merging mapping may take any type, an oct key's too: its 'k'
            # counts (inside a decided object no oct-block reading is left).
            target, names = (uncut_out, _FLOW_PRIVATE) if frame.merging else (out, _PRIVATE_NAMES)
            target.extend(member for member in frame.members if member[0] in names)
        # Typed members only where the object scan's reading of these braces
        # can differ from YAML's (a divergent scalar inside them); elsewhere
        # that scan already reports the same members.
        if frame.bits and lex.divergent_in(frame.start, at):
            # Where the readings can part, YAML's own structure is not proof
            # either (a quote YAML closes on the next line may be an unclosed
            # one in an excerpt): the mapping's text is swept for every
            # private-named key too, after the walk, failing toward masking.
            # Not inside an outermost flow that never closes: that structure
            # is only a runaway reading (code read as flow from a stray '{').
            if frame.start < lex.unterminated_from:
                sweeps.append((frame.start, at, frame.bits))
            for name, start, end in frame.members:
                if name in _RSA_ONLY:
                    keep = frame.bits & 1
                elif name == "k":
                    keep = frame.bits & 4
                elif name == "d":
                    keep = frame.bits & 3
                else:
                    keep = True  # an unknown name in a typed mapping
                if keep:
                    typed_out.append((name, start, end))

    positions, chars = lex.flow_positions, lex.flow_chars
    for index in range(len(positions)):
        at = positions[index]
        char = chars[index]
        if char in b"[{":
            anchored = bool(frames and frames[-1].anchored) or _anchored_before(text, at, 0, lex)
            frames.append(_FlowFrame(char == ord("{"), anchored, at))
            if char == ord("{"):
                key_at(at + 1)
        elif char in b"]}":
            if frames:
                close(at)
        elif frames and frames[-1].mapping:
            key_at(at + 1)
    while frames:
        close(high)
    _sweep(text, sweeps, typed_out)


def _sweep(
    text: str, sweeps: list[tuple[int, int, int]], typed_out: list[tuple[str, int, int]]
) -> None:
    """Every private-named key in typed mappings whose readings can part.

    A mapping inside one already swept joins its group (their type bits
    combined), so each character is swept once however deep the nesting. A
    key inside a value already searched to its end ('d: x d: x ...') shares
    that end and lies inside the span masked: it is skipped, so no stretch of
    text is searched twice (review r9).
    """
    groups: list[list[int]] = []
    for start, end, bits in sorted(sweeps):
        if groups and start < groups[-1][1]:
            groups[-1][1] = max(groups[-1][1], end)
            groups[-1][2] |= bits
        else:
            groups.append([start, end, bits])
    for start, end, bits in groups:
        searched = -1  # the end of the latest value searched for
        for sweep in _PRIVATE_KEY_SWEEP.finditer(text, start, end):
            if sweep.start() < searched:
                continue  # inside a value already masked to the same end
            name = sweep["name"]
            if name in _RSA_ONLY:
                keep = bits & 1
            elif name == "k":
                keep = bits & 4
            else:
                keep = bits & 3
            if not keep:
                continue
            value = _SPACES.match(text, sweep.end(), end).end()
            stop = _SWEEP_VALUE_END.search(text, value, end)
            close = searched = stop.start() if stop else end
            while close > value and text[close - 1] in " \t":
                close -= 1
            if close > value:
                typed_out.append((name, value, close))


def yaml_members(
    text: str,
    decided: list[tuple[int, int]],
    double_quoted: Callable[[str], str | None],
    kty_hint: Callable[[str], bool] | None = None,
) -> tuple[list[tuple[str, int, int]], list[tuple[str, int, int]]]:
    """(block and conservative members, typed flow members) of ``text``.

    The first list holds private members of typed YAML block mappings and of
    the mappings a conservative document reads (sorted, disjoint, outside
    ``decided``). The second holds private members of flow mappings YAML
    reads as typed, outside nothing: the caller keeps those that no other
    rule's member overlaps. ``double_quoted`` reads a double-quoted scalar as
    YAML (``jwk._yaml_double_quoted``). ``kty_hint`` says whether a document
    has key-type evidence; without it, every document using merges or alias
    keys is read conservatively.
    """
    if ":" not in text:
        return [], []
    multi_line = _BREAK.search(text) is not None
    lex = _lex_for(text)
    anchors = _Anchors(text, double_quoted)
    documents = _conservative_documents(text, kty_hint, anchors, lex)
    cut = oct_blocks._Spans(decided)
    conservative = oct_blocks._Spans(documents)
    found: list[tuple[str, int, int]] = []
    typed: list[tuple[str, int, int]] = []
    uncut: list[tuple[str, int, int]] = []
    if multi_line:
        for skip_quotes in (True, False):
            _reading(
                text,
                cut,
                double_quoted,
                skip_quotes=skip_quotes,
                out=found,
                anchors=anchors,
                conservative=conservative,
                lex=lex,
            )
    if lex.flow_positions and (documents or lex.divergent):
        _flow_members(text, lex, anchors, double_quoted, conservative, found, typed, uncut)
    members = _disjoint(found, cut)
    if uncut:
        members = _disjoint(members + uncut, oct_blocks._Spans([]))
    return members, _disjoint(typed, oct_blocks._Spans([]))


def block_members(
    text: str,
    decided: list[tuple[int, int]],
    double_quoted: Callable[[str], str | None],
    kty_hint: Callable[[str], bool] | None = None,
) -> list[tuple[str, int, int]]:
    """Every private member ``yaml_members`` finds, as one sorted list."""
    members, typed = yaml_members(text, decided, double_quoted, kty_hint)
    return sorted(members + typed, key=lambda member: (member[1], -member[2]))


def _disjoint(
    found: list[tuple[str, int, int]], cut: oct_blocks._Spans
) -> list[tuple[str, int, int]]:
    if not found:
        return []
    found.sort(key=lambda member: (member[1], -member[2]))
    disjoint: list[tuple[str, int, int]] = []
    cover = -1
    for name, start, end in found:
        if end <= cover:
            continue
        disjoint.append((name, max(start, cover), end))
        cover = end
    return subtract_named(disjoint, cut)


def subtract_named(
    pieces: list[tuple[str, int, int]], cut: oct_blocks._Spans
) -> list[tuple[str, int, int]]:
    """Sorted, disjoint named ``pieces`` minus ``cut``, in one forward walk."""
    holes = cut.spans
    if not holes:
        return pieces
    result: list[tuple[str, int, int]] = []
    index = 0
    for name, start, end in pieces:
        while index < len(holes) and holes[index][1] <= start:
            index += 1
        position, probe = start, index
        while probe < len(holes) and holes[probe][0] < end:
            if holes[probe][0] > position:
                result.append((name, position, holes[probe][0]))
            position = max(position, holes[probe][1])
            probe += 1
        if position < end:
            result.append((name, position, end))
    return result


def alias_targets(
    text: str,
    members: list[tuple[str, int, int]],
    decided: list[tuple[int, int]],
    double_quoted: Callable[[str], str | None],
) -> list[tuple[str, int, int]]:
    """Anchored nodes that private members' alias values (``d: *v``) name.

    The alias masks only its own token; the private material is the anchored
    node at its definition. Every ``&v`` before the alias in its document is a
    candidate, and each candidate's node is masked: a fake ``&v`` in a comment
    or a scalar cannot take the real definition's place. Aliases
    inside a masked node are followed too, for a few rounds; a chain still
    growing after that masks from each pending definition to the end of its
    document. An alias with no definition names nothing YAML can load.

    Inside an object the object scan decided, a value counts as an alias
    only where the YAML lexer reads an alias token (a JSON string ``"*v"`` is
    text), and the nodes it names are masked there too: that scan never
    follows an alias value to an anchor defined in the same quoted-type
    object (review r9). Only a private-named member's alias reaches into a
    decided object: another value an oct block masks (a merge key's ``*b``)
    leaves a decided object it merges public, as the object scan read it.
    """
    if "*" not in text or "&" not in text:
        return []
    anchors = _Anchors(text, double_quoted)
    inside = oct_blocks._Spans(decided)
    lex: _YamlLex | None = None
    pending: list[tuple[str, str, int]] = []
    for name, start, end in members:
        position = _PROPERTIES.match(text, _BLANKS.match(text, start, end).end(), end).end()
        alias = _ALIAS_AT.match(text, position, end)
        if alias is None:
            continue
        if inside.spans and inside.contains(alias.start()):
            lex = lex or _lex_for(text)
            if not lex.alias_at(alias.start()):
                continue
        pending.append((name, alias["name"], alias.start()))
    found: list[tuple[str, int, int]] = []

    def groups(pending: list[tuple[str, str, int]]) -> dict[tuple[str, int], tuple[int, str]]:
        # Every candidate definition before the alias in its document: the
        # latest alias per name and document bounds them, so each definition
        # is listed once however many aliases name it.
        latest: dict[tuple[str, int], tuple[int, str]] = {}
        for member, alias, at in pending:
            key = (alias, anchors.document_start(at))
            if key not in latest or at > latest[key][0]:
                latest[key] = (at, member)
        return latest

    def streams(latest: dict[tuple[str, int], tuple[int, str]]) -> Iterator[tuple[int, str]]:
        # Each group is an ascending slice of one name's definitions; merge
        # them lazily, so a long run of candidates is never materialized.
        def stream(alias: str, at: int, member: str) -> Iterator[tuple[int, str]]:
            ends, low, high = anchors.candidate_range(alias, at)
            for index in range(low, high):
                yield ends[index], member

        return merge(*(stream(alias, at, member) for (alias, _), (at, member) in latest.items()))

    for _round in range(_ALIAS_ROUNDS):
        if not pending:
            break
        added: list[tuple[str, int, int]] = []
        cover = -1
        for end, member in streams(groups(pending)):
            if end <= cover:
                continue  # inside a node already masked: no second read
            low, high = anchors.node(end)
            if high > low:
                added.append((member, low, high))
                cover = max(cover, high)
        found.extend(added)
        pending = [
            (member, alias.group(1), alias.start())
            for member, low, high in added
            for alias in _ALIAS.finditer(text, low, high)
        ]
    else:
        # A chain still growing: each pending candidate to its document end.
        cover = -1
        for end, member in streams(groups(pending)):
            if end > cover:
                cover = anchors.document_end(end)
                found.append((member, end, cover))
    private = [piece for piece in found if piece[0] in _FLOW_PRIVATE]
    if len(private) == len(found):
        return _disjoint(found, oct_blocks._Spans([]))
    other = _disjoint([piece for piece in found if piece[0] not in _FLOW_PRIVATE], inside)
    return _disjoint(_disjoint(private, oct_blocks._Spans([])) + other, oct_blocks._Spans([]))


# Template text cooked by ``jwk._template_cooked``: an interpolation in a key
# token or in a type value, a private-named key, and a key-type value. After a
# quoted key's ':' may stand anything but another ':' (compact JSON:
# "d":"v"); after a plain key's, a blank or the end of the line (as in YAML).
_INTERPOLATED_KEY = re.compile(
    r"[\"'][^\s,\[\]{}:\"']{0,64}\$\{\}[^\s,\[\]{}:\"']{0,64}[\"'][ \t]*+:(?!:)"
    r"|[^\s,\[\]{}:\"']{0,64}\$\{\}[^\s,\[\]{}:\"']{0,64}[ \t]*+:(?=[ \t]|$)",
    re.MULTILINE,
)
_INTERPOLATED_TYPE = re.compile(r"kty[\"']?[ \t]*+:[ \t]*+[\"']?[^\s,\[\]{}\"']{0,64}\$\{\}")
_PRIVATE_KEY = re.compile(
    r"(?:^|(?<=[\s{,]))(?:[\"'](?:d|p|q|dp|dq|qi|oth|k)[\"'][ \t]*+:(?!:)"
    r"|(?:d|p|q|dp|dq|qi|oth|k)[ \t]*+:(?=[ \t]|$))",
    re.MULTILINE,
)
_KEY_TYPE_VALUE = re.compile(r":[ \t]*+[\"']?(?:RSA|EC|OKP|oct)(?![\w.-])")


def interpolated_jwk(cooked: str) -> bool:
    """Whether a cooked template may hold a JWK whose type or names are runtime.

    The whole literal is masked (failing toward masking) when an interpolation
    makes a key or type unknown and the literal could be a key with a private
    member: an interpolated key token (``k${part}y:``, ``"${n}":``) beside a
    private-named key or a key-type value, or an interpolated type value
    (``kty: ${kind}``, ``"kty":"R${x}A"``) beside a private-named key. Flow,
    compact JSON and block forms alike.
    """
    if "${}" not in cooked:
        return False
    if _INTERPOLATED_KEY.search(cooked) is not None:
        return bool(_PRIVATE_KEY.search(cooked) or _KEY_TYPE_VALUE.search(cooked))
    return bool(_INTERPOLATED_TYPE.search(cooked) and _PRIVATE_KEY.search(cooked))
