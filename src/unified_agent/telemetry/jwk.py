"""Contextual JWK private-member detection for telemetry redaction.

Three rules. The JSON scan (``_json_members``) reads JWKs written as JSON,
Python or JavaScript object literals, and YAML flow mappings, and masks only
their private members. The oct-block rule (``oct_blocks``) covers symmetric
keys written without braces, as keyword arguments or YAML: near a ``kty`` of
``oct`` it masks every value. The block-mapping rule (``yaml_jwk``) covers RSA,
EC and OKP keys written as YAML block mappings and masks only the private
members of the typed mapping itself. Objects the JSON scan decides are left to
it, unchanged. ``oct_blocks`` and ``yaml_jwk`` document their rules, their
over-redaction and their limits.

An object whose type is an unquoted YAML plain scalar (``RSA``, ``EC``, ``OKP``
or ``oct`` ending at a flow separator, a closing bracket or a comment) has its
private members reported by the JSON scan (task #2200). The type is read by a
bounded reader after the separator that skips blanks, line breaks, comments and
node properties (tags, verbatim ``!<...>`` tags, anchors); an alias type, or a
property longer than 256 characters (an unfinished verbatim tag included),
stands for any asymmetric type. A quoted type after such properties (``!!str "RSA"``)
decides the object exactly as a bare quoted type does. A repeated ``kty``
(YAML keeps the last; parsers differ) types the object with the union of the
private sets of every key type it names (``_union_type``), and such an object
proves no member public. An interpolation (``${...}``) as the type is unknown,
selecting every private set. Such an object is not *decided*: it proves no
member public and cuts nothing out of an oct block, so the oct-block rule keeps
its own reading of the same text (an unquoted ``oct`` object still loses its
other values to that rule). A quoted type in the same object wins, as before.
JavaScript views and keyword proofs never read a plain type: they can run over
recovery text whose orphan quotes are blanked.

A private member whose value is a YAML alias (``d: *v``) also masks the node
its anchor defines (``yaml_jwk.alias_targets``). A JavaScript literal body is
cooked (escapes decoded, ``${...}`` opaque) before the block-mapping rule reads
it; a block JWK found there, or (in a template literal, not a regular
expression) an interpolated key beside a private-named key or a key-type
value, or an interpolated type beside a private-named key, in any form
(block, flow, compact JSON), masks the whole literal. ``_may_have_kty``
accepts a key ending in an interpolation (``k${part}y:``) as type evidence. A later rule reports only
bytes no earlier rule reported, so masked values never overlap.

A double-quoted type or member name is also read as a YAML double-quoted
scalar, which drops an escaped line break together with the next line's
indentation (``{kty: "oct\\<LF>    ", k: ...}`` is an oct key). That reading
is used only where it alone spells a key type or a member name the scan acts
on (``kty``, a private member): it adds evidence and never withdraws a decided
object's type (task #2126).
"""

from __future__ import annotations

import ast
import codecs
import json
import logging
import os
import re
import unicodedata
import warnings
from array import array
from bisect import bisect_right
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from io import StringIO

from . import jwk_fallback, oct_blocks, yaml_jwk

# Consume quoted strings as tokens so braces in their contents cannot move a
# member into another object. Accept an unfinished last string for log excerpts.
# An escape takes any next character, newline included, and a lone backslash may
# end the text: otherwise a string cut on a backslash matched from no quote and
# every later quote rescanned to the end (quadratic; PR #1146 review).
# A single quote right after a word character is an apostrophe (Here's,
# that's), not a string, unless the word is a Python string prefix (r'', b'',
# u'', f'', rb''): pairing apostrophes swallowed the prose between them.
_SINGLE_OPEN = oct_blocks._SINGLE_QUOTE_OPEN
_STRING = (
    r"""(?:"(?:\\[\s\S]|\\\Z|[^"\\])*+(?:"|\Z)"""
    rf"""|{_SINGLE_OPEN}(?:\\[\s\S]|\\\Z|[^'\\])*+(?:'|\Z))"""
)
# Tokens: a complete Python triple string (scanned in its own decoded context);
# a member name with
# its colon and, when it is one, its plain string value or the bracket of an
# array (an RSA multi-prime key's "oth" holds objects whose r, d and t are
# private and which carry no kty of their own, RFC 7518 6.3.2.7); any other
# string; or a brace. Member names are decoded before they are classified, so
# an escaped name ("\u006fth") keeps its meaning.
# An unquoted JavaScript member name ({kty: "oct", k: "..."}), matched only
# from the start of a word (round 4).
_BARE_NAME = r"(?<![\w$.])[A-Za-z_$][\w$]*+"
# Only the private alternate view decodes JS IdentifierName escapes. Runs of
# backslashes retain conservative evidence in malformed/encoded excerpts too.
_JS_IDENTIFIER_ESCAPE = re.compile(r"\\++u(?:\{[0-9a-fA-F]++\}|[0-9a-fA-F]{4})")
_JS_QUOTED_ESCAPE = re.compile(r"(?<!\\)(?P<slashes>\\++)u\{(?P<digits>[0-9a-fA-F]++)\}")
_JS_SALVAGE_ESCAPE = re.compile(r"(?<!\\)\\++(?=u\{|[\u2028\u2029])")
# A JS line continuation: an odd backslash run before a line terminator.
_JS_CONTINUED = re.compile(r"(?<!\\)(?:\\\\)*+\\(?:\r\n|[\r\n\u2028\u2029])")
_JS_BARE_NAME = (
    rf"(?<![\w$.\\])(?:[A-Za-z_$]|{_JS_IDENTIFIER_ESCAPE.pattern})"
    rf"(?:[\w$]|{_JS_IDENTIFIER_ESCAPE.pattern})*+"
)
# A Python string prefix before a value (u'oct', b'oct' in a Python 2 or bytes
# repr), so a prefixed kty is still read as one plain string.
_PREFIX = oct_blocks._PYTHON_PREFIX
_RAW_PREFIXES = frozenset(p for p in oct_blocks._PYTHON_PREFIXES if p == "b" or "r" in p)
_TRIPLE_STRING = "|".join(
    rf"{quote}(?:\\[\s\S]|\\\Z|(?!{quote})[^\\])*+(?:{quote}|\Z)" for quote in ('"' * 3, "'" * 3)
)
_TRIPLE_OPEN = re.compile(rf"(?P<prefix>{_PREFIX})?(?P<quote>\"{{3}}|'{{3}})")
_LITERAL_OPEN = re.compile(rf"(?P<prefix>{_PREFIX})?(?P<quote>\"{{3}}|'{{3}}|[\"'])")


def _member_tokens(
    separator: str, brackets: str, bare_name: str = _BARE_NAME, key_gap: str = r"\s*+"
) -> str:
    return (
        rf"(?P<triple>{_PREFIX}?(?:{_TRIPLE_STRING}))"
        rf"|(?P<key>{_STRING}|{bare_name}){key_gap}(?P<separator>{separator})\s*+"
        rf"(?:(?P<triple_value>{_PREFIX}?(?:{_TRIPLE_STRING}))"
        rf"|{_PREFIX}?(?P<value>{_STRING})|(?P<array>\[))?"
        rf"|(?P<string>{_STRING})|(?P<javascript>[`/])|[{brackets}]"
    )


# A block comment between a member name and its colon is legal JavaScript
# (``d/*gap*/:"<key>"``), and without this gap the member is never read, so a
# private value stays public on all four consumers (task #2165). This pattern
# only reads a block comment, and a block body here may not hold a quote, brace
# or colon: a literal hidden in comment prose must stay visible to the comment
# scan, so such a body is left unconsumed (task #2361). Every alternative needs
# at least one character, the body is capped at 1024 characters and all
# quantifiers are possessive, so a repeated ``a/*b `` cannot rescan the same
# characters: growth stays linear on text that is not a member at all.
#
# A ``//`` line comment is deliberately NOT part of this pattern, and its body
# is scanned as comment prose. It has no closing delimiter, so a colon inside
# its prose would be taken as the member's own separator and the ``kty``
# evidence in that prose would go with it: a JWK hidden in the prose then stops
# being detected (reproduced on PR #1417).
#
# A member read across a gap this pattern refuses (any ``//`` comment, a block
# body past the cap or holding a quote, brace or colon) comes from a second
# view instead (task #2366): ``_comment_gap_separator`` finds each comment run
# that ends in a colon, the run is blanked with offsets kept, and that view can
# only add members. The live scan still reads every body as prose. The 1024
# cap is therefore only this regex's work bound, not a detection limit.
_JS_TRIVIA_BODY = r"[^*\"'{}:]|\*(?!/)"
_JS_KEY_GAP = rf"\s*+(?:/\*(?:{_JS_TRIVIA_BODY}){{0,1024}}+\*/\s*+)*+"
_COMMENT_TOKEN = re.compile(_member_tokens(":", "{}", key_gap=_JS_KEY_GAP))
_TOKEN = re.compile(rf"(?P<comment>\#[^{oct_blocks._LINE_CHARS}]*+)|{_COMMENT_TOKEN.pattern}")
_JS_COMMENT_TOKEN = re.compile(_member_tokens(":", "{}", _JS_BARE_NAME, _JS_KEY_GAP))
_JS_TOKEN = re.compile(rf"(?P<comment>\#[^{oct_blocks._LINE_CHARS}]*+)|{_JS_COMMENT_TOKEN.pattern}")
_PROOF_TOKEN = re.compile(
    rf"(?P<comment>\#[^{oct_blocks._LINE_CHARS}]*+)|{_member_tokens(r':|=(?!=)', '{}()')}"
)
_COMMENT_GAP = re.compile(rf"[^{oct_blocks._LINE_CHARS}]++")
# Every character that ends a ``#`` comment token above (task #2169).
_COMMENT_END = re.compile(rf"[{oct_blocks._LINE_CHARS}]")
_COMMENT_CONTENT = re.compile(
    rf"(?P<hash>\#++)|{oct_blocks._STRING_LITERALS}|(?P<orphan_quote>['\"])"
)
_COMMENT_LITERAL_SCANS = tuple(
    re.compile(
        rf"(?P<prefix>(?<![\w.$]){_PREFIX})?"
        rf"(?:(?P<triple>{quote * 3}(?:\\[\s\S]|\\\Z|(?!{quote * 3})[^\\])*+(?:{quote * 3}|\Z))"
        rf"|(?P<plain>{_SINGLE_OPEN if quote == chr(39) else quote}(?:\\[\s\S]|\\\Z|[^{quote}\\])*+(?:{quote}|\Z)))"
    )
    for quote in ('"', "'")
)

# What may follow a plain string value: the next member, the end of its
# container, or the end of the text. Anything else ("a" "b", "a" + b) makes
# the value an expression.
_VALUE_TAIL = re.compile(rf"(?:\s++|\#[^{oct_blocks._LINE_CHARS}]*+)*+(?:[,}}\])]|\Z)")
# A string directly followed by a member name and its closing quote ("hi {"kty":)
# is mis-paired: its opening quote was unbalanced and its closing quote opens
# that member name. Valid JSON, Python and JS never put a word right after a
# closing quote.
_MISPAIRED = {q: re.compile(rf"[\w$]++{q}\s*+[:,}}\]]") for q in "\"'"}
_CLOSED_STRING = re.compile(r""""(?:\\[\s\S]|[^"\\])*+"|'(?:\\[\s\S]|[^'\\])*+'""")
# The structure an expression value is measured in: strings, brackets, commas.
_STRUCTURE = re.compile(
    rf"(?P<comment>\#[^{oct_blocks._LINE_CHARS}]*+)"
    rf"|{_PREFIX}?(?:{_TRIPLE_STRING})|{_STRING}|(?P<javascript>[`/])|[\[\](){{}},]"
)
_STRUCTURE_JS = re.compile(
    rf"(?P<comment>(?!))|{_PREFIX}?(?:{_TRIPLE_STRING})|{_STRING}"
    rf"|(?P<javascript>[`/])|[\[\](){{}},]"
)

# Keep the mapping scan's unconditional Python comment boundary, including x#.
# A string token starting earlier still consumes any # in its literal content.
_UNPACK_CONTEXT = re.compile(
    rf"(?P<line_comment>\#[^{oct_blocks._LINE_CHARS}]*+)|(?P<javascript>[`/])"
    rf"|{oct_blocks._UNPACK_CONTEXT.pattern}"
)
_GAP_SPACE = re.compile(r"\s*+")
_JAVASCRIPT_BREAK = re.compile(r"[\r\n\u2028\u2029]")
# Retain the existing conservative whitespace set and include JavaScript's BOM
# whitespace. Python does not accept the latter as whitespace; it must not add
# primary comment/type authority when used in an alternate boundary.
_JAVASCRIPT_TRIVIA = re.compile(r"[\s\ufeff]*+\Z")
# Other JavaScript operand words (typeof, void, ...) are legal Python variable
# names. Treating their following division as a regexp could un-own a real
# Python comment; only shared reserved words establish this lexical boundary.
_JAVASCRIPT_OPERAND_WORDS = frozenset({"return", "yield", "await", "in", "else"})
_JAVASCRIPT_ONLY_OPERANDS = frozenset(
    {"throw", "typeof", "void", "delete", "case", "instanceof", "do"}
)

# Find a possible identifier start, then consume its complete lexical extent
# with the same helper in both walks. Python's \w omits combining marks and
# joiners; ASCII-only starts also split Unicode and escaped transfer labels.
_JS_WORD = re.compile(r"[^\W\d]|[$\\]|[\u1885\u1886\u2118\u212e\u309b\u309c]")
# Preserve the original primary-label spelling set; new lexical spellings
# can retain alternate evidence without expanding primary authority.
_JS_PRIMARY_WORD = re.compile(r"[A-Za-z_$][\w$]*+")
_JS_IDENTIFIER_START = frozenset({"Lu", "Ll", "Lt", "Lm", "Lo", "Nl"})
_JS_IDENTIFIER_CONTINUE = _JS_IDENTIFIER_START | {"Mn", "Mc", "Nd", "Pc"}
_JS_OTHER_START = frozenset("\u1885\u1886\u2118\u212e\u309b\u309c")
_JS_OTHER_CONTINUE = _JS_OTHER_START | frozenset(
    "\u00b7\u0387\u1369\u136a\u136b\u136c\u136d\u136e\u136f\u1370\u1371\u19da\u200c\u200d"
)


def _javascript_identifier_part(char: str, *, start: bool = False) -> bool:
    return (
        char in "_$"
        or unicodedata.category(char)
        in (_JS_IDENTIFIER_START if start else _JS_IDENTIFIER_CONTINUE)
        or char in (_JS_OTHER_START if start else _JS_OTHER_CONTINUE)
    )


def _javascript_identifier_end(text: str, start: int) -> int:
    """Consume one identifier spelling without decoding or proving validity.

    Unicode ID_Start/ID_Continue categories, their Other_ID exceptions and
    joiners keep a label whole. Escape spellings stay opaque: they never become
    keywords or primary Python evidence. Even an incomplete escape advances
    only through local identifier text, stopping before trivia or other syntax.
    A braced escape's opaque body also retains non-hex identifier text in
    malformed excerpts; it cannot consume a nested brace, literal or separator.
    Uncertain spellings may retain alternate private evidence, never public
    proof. Each character belongs to one forward token, with no suffix retry.
    """
    position = start
    while position < len(text):
        char = text[position]
        if char == "\\":
            position += 1
            if text.startswith("u{", position):
                position += 2
                while position < len(text) and _javascript_identifier_part(text[position]):
                    position += 1
                if position < len(text) and text[position] == "}":
                    position += 1
        elif _javascript_identifier_part(char, start=position == start):
            position += 1
        else:
            break
    # The candidate pattern is deliberately conservative about Unicode number
    # categories. An uncertain start must still advance once and stay local.
    return max(start + 1, position)


def _javascript_identifier_token(text: str, start: int) -> tuple[int, int]:
    """An identifier or UNKNOWN spelling, plus its unfinished escape count.

    The ordinary extent helper still stops at punctuation. Non-trivia control,
    format and surrogate characters extend an UNKNOWN spelling, not a valid
    label or keyword. A consumed escape opener retains its own brace ownership
    until a close or source delimiter; its leftover close cannot close a JWK.
    """
    end = _javascript_identifier_end(text, start)
    while end < len(text):
        char = text[end]
        if not unicodedata.category(char).startswith("C") or (char.isspace() or char == "\ufeff"):
            break
        end += 1
        while end < len(text):
            if text[end] == "\\":
                end = _javascript_identifier_end(text, end)
            elif _javascript_identifier_part(text[end]):
                end += 1
            else:
                break
    raw = text[start:end]
    return end, raw.count("{") - raw.count("}")


_JS_HEADERS = frozenset({"if", "while", "for", "with", "switch", "catch"})
_JS_STATEMENTS = frozenset({"else", "do", "try", "finally"})
_JS_LINE_COMPLETIONS = frozenset({"debugger", "break", "continue"})
_JS_LABEL_TRANSFERS = frozenset({"break", "continue"})


@dataclass(slots=True)
class _JavascriptBoundary:
    """Lexical regexp boundaries shared by source and template walks.

    This tracks delimiters and token positions, never expression validity.
    Comments advance adjacent-token positions without erasing their boundary.
    Function headers await a body instead of making ordinary calls into regexp
    boundaries. Each delimiter has one entry and is discarded on its close.
    """

    word: str = ""
    word_end: int = -1
    prefix_end: int = -1
    statement_end: int = -1
    body_end: int = -1
    primary_body_end: int = -1
    completion_end: int = -1
    completion_break: bool = False
    completion_label: bool = False
    safe_statement: bool = False
    function: bool = False
    class_body: bool = False
    function_body: bool = False
    property_name: bool = False
    label: bool = False
    safe_label: bool = False
    identifier_escape: bool = False
    unknown_depth: int = -1
    unknown_completion: int = -1
    unknown_break: bool = False
    headers: dict[int, int] | None = None
    blocks: dict[int, bool] | None = None

    @staticmethod
    def adjacent(text: str, end: int, at: int, *, primary: bool = False) -> bool:
        if primary:
            return end >= 0 and not text[end:at].strip()
        return end >= 0 and _JAVASCRIPT_TRIVIA.fullmatch(text, end, at) is not None

    def statement(self, text: str, at: int, *, primary: bool = False) -> bool:
        return self.adjacent(text, self.statement_end, at, primary=primary)

    def regexp(self, text: str, at: int) -> bool:
        unknown_boundary = False
        if self.unknown_completion >= 0:
            self.unknown_break |= (
                _JAVASCRIPT_BREAK.search(text, self.unknown_completion, at) is not None
            )
            # Each uncertain gap is inspected once, even with many slashes.
            self.unknown_completion = at
            unknown_boundary = self.unknown_break
        return (
            unknown_boundary
            or self.statement(text, at)
            or self.adjacent(text, self.prefix_end, at)
            or (
                self.adjacent(text, self.completion_end, at)
                and (
                    self.completion_break
                    or _JAVASCRIPT_BREAK.search(text, self.completion_end, at) is not None
                )
            )
        )

    def line_trivia(self, text: str, at: int) -> bool:
        return (
            self.regexp(text, at)
            or (self.word in _JS_HEADERS and self.adjacent(text, self.word_end, at))
            or self.function
            or self.class_body
            or self.function_body
            or self.adjacent(text, self.body_end, at)
            or self.adjacent(text, self.completion_end, at)
        )

    def trivia(self, text: str, at: int, end: int) -> None:
        self.safe_statement &= self.statement(text, at, primary=True)
        self.safe_label &= self.adjacent(text, self.word_end, at, primary=True)
        self.primary_body_end = (
            end if self.adjacent(text, self.primary_body_end, at, primary=True) else -1
        )
        if self.adjacent(text, self.completion_end, at):
            self.completion_break |= (
                _JAVASCRIPT_BREAK.search(text, self.completion_end, end) is not None
            )
            self.completion_end = end
        else:
            self.completion_end, self.completion_break = -1, False
            self.completion_label = False
        for name in ("word_end", "prefix_end", "statement_end", "body_end"):
            previous = getattr(self, name)
            setattr(self, name, end if self.adjacent(text, previous, at) else -1)
        if self.word_end < 0:
            self.word = ""

    def value(self) -> None:
        self.word, self.word_end = "", -1
        self.prefix_end = self.statement_end = -1
        self.body_end = self.primary_body_end = -1
        self.completion_end, self.completion_break = -1, False
        self.completion_label = False
        self.safe_statement = False
        self.function = self.class_body = self.function_body = False
        self.property_name = self.label = self.safe_label = False
        self.identifier_escape = False

    def unknown_owner(self, end: int, depth: int) -> None:
        """Retain an UNKNOWN token's containing private candidate, not its type.

        Syntax delimiters still advance normally. Only private recovery may
        use this uncertainty; it cannot authorize a primary literal cache.
        """
        self.unknown_depth = depth if self.unknown_depth < 0 else min(self.unknown_depth, depth)
        if self.unknown_completion < 0:
            # The unknown token itself establishes local lexical uncertainty,
            # even when an opaque expression interrupted a transfer label.
            # Only private recovery uses its next-line regexp interpretation;
            # a real containing-source close still releases this evidence.
            self.unknown_completion = end
            self.unknown_break = False
        self.safe_label = self.safe_statement = False
        self.primary_body_end = -1

    def unknown_identifier_piece(self, text: str, raw: str, at: int, end: int) -> bool:
        """Advance an UNKNOWN escape body without assigning syntax authority.

        Punctuation remains a separate token. Only the escape's own close is
        withheld from the container stack. Trivia and real source delimiters
        end this local uncertainty, so later fields/literals keep their fences.
        """
        if not self.identifier_escape:
            return False
        if self.word_end != at:
            self.identifier_escape = False
            return False
        if raw[0].isspace() or raw[0] in "\ufeff\"'`/()[]{:;=":
            self.identifier_escape = False
            return False
        self.safe_label = self.safe_statement = False
        self.primary_body_end = self.prefix_end = self.statement_end = -1
        self.word = ""  # opaque fragments can never become keyword spellings
        self.function = self.class_body = self.function_body = self.property_name = False
        self.word_end = end
        if self.completion_end == at:
            self.completion_end = end
        else:
            self.completion_end = -1
            self.completion_label = False
        if raw == "}":
            self.identifier_escape = False
        return True

    def token(
        self, text: str, raw: str, at: int, end: int, depth: int, *, identifier: bool = False
    ) -> None:
        statement = self.statement(text, at)
        if identifier:
            # Property names are not keywords. In particular obj.catch(1)
            # and obj.switch(1) remain ordinary calls in either language.
            keyword = not self.property_name
            transfer_label = (
                keyword
                and self.completion_label
                and self.adjacent(text, self.completion_end, at)
                and not self.completion_break
                and _JAVASCRIPT_BREAK.search(text, self.completion_end, at) is None
            )
            header_modifier = (
                keyword
                and raw == "await"
                and self.word == "for"
                and self.adjacent(text, self.word_end, at)
            )
            self.label = statement
            self.safe_label = (
                self.safe_statement
                and self.statement(text, at, primary=True)
                and _JS_PRIMARY_WORD.fullmatch(raw) is not None
            )
            self.function = (keyword and raw == "function") or self.function
            self.class_body = (keyword and raw == "class") or self.class_body
            self.property_name = False
            if header_modifier:
                self.word_end = end
                return  # trivia and await preserve the pending for header
            self.word, self.word_end = raw, end
            # A restricted line completion survives trivia and, for a transfer,
            # one same-line label. It permits only alternate private evidence;
            # it never establishes primary statement/comment authority. Return
            # and yield already have operand boundaries, unlike these controls.
            completes = transfer_label or (keyword and raw in _JS_LINE_COMPLETIONS)
            self.completion_end = end if completes else -1
            self.completion_break = False
            self.completion_label = keyword and raw in _JS_LABEL_TRANSFERS and not transfer_label
            self.prefix_end = (
                end
                if keyword and raw in _JAVASCRIPT_OPERAND_WORDS | _JAVASCRIPT_ONLY_OPERANDS
                else -1
            )
            self.statement_end = end if keyword and raw in _JS_STATEMENTS else -1
            # `do` is a valid Python variable. Its alternate evidence cannot
            # change primary Python comment ownership.
            self.safe_statement = keyword and raw in _JS_STATEMENTS and raw != "do"
            if not keyword:
                self.word = ""
            self.function_body = False
            return
        header = raw == "(" and (
            self.function or (self.word in _JS_HEADERS and self.adjacent(text, self.word_end, at))
        )
        parameters = self.adjacent(text, self.body_end, at)
        named_block = (
            self.label
            and self.adjacent(text, self.word_end, at)
            and self.blocks is not None
            and depth - 1 in self.blocks
        )
        body = raw == "{" and (
            statement
            or self.class_body
            or self.function_body
            or parameters
            or named_block
            or (self.word == "catch" and self.adjacent(text, self.word_end, at))
        )
        # A parameter close followed by a body brace also covers method bodies.
        # Unlike a close followed by '/', this boundary is not Python division.
        safe_body = (
            (self.safe_statement and self.statement(text, at, primary=True))
            or self.class_body
            or self.function_body
            or self.adjacent(text, self.primary_body_end, at, primary=True)
        )
        if named_block:
            safe_body = safe_body or (
                self.safe_label and self.adjacent(text, self.word_end, at, primary=True)
            )
        # A brace directly after catch is an optional-binding statement body;
        # it cannot be an ordinary Python identifier followed by division.
        safe_body = safe_body or (
            body and self.word == "catch" and self.adjacent(text, self.word_end, at, primary=True)
        )
        if header:
            if self.headers is None:
                self.headers = {}
            # -1 awaits a function body; 0 is an ambiguous identifier call.
            # Even Python's reserved `if` can introduce a conditional expression
            # or comprehension filter. Primary ownership additionally requires
            # an established statement position in an unambiguous body.
            self.headers[depth] = (
                -1
                if self.function
                else int(
                    self.word not in {"switch", "catch"}
                    and self.label
                    and self.safe_label
                    and self.adjacent(text, self.word_end, at, primary=True)
                )
            )
        if body:
            if self.blocks is None:
                self.blocks = {}
            self.blocks[depth] = safe_body
        function, class_body = self.function, self.class_body
        label = self.label
        safe_label = self.safe_label and self.adjacent(text, self.word_end, at, primary=True)
        self.value()
        if raw == ".":
            self.property_name = True
        if raw == "=>":
            self.function_body = True
        if body:
            self.statement_end, self.safe_statement = end, safe_body
        elif raw == ";" and self.blocks is not None and depth - 1 in self.blocks:
            self.statement_end, self.safe_statement = end, self.blocks[depth - 1]
        elif raw == ":" and label:
            self.statement_end, self.safe_statement = end, safe_label
        if raw == "*" and function:
            self.function = True  # generator declaration's lexical marker
        if raw != "{" and class_body:
            self.class_body = True
        if raw in ":=([,!&|?{;+-*%^~<>/":
            self.prefix_end = end

    def close(self, depth: int, end: int, closing: str) -> None:
        if depth < self.unknown_depth:
            self.unknown_depth = self.unknown_completion = -1
            self.unknown_break = False
        header = self.headers.pop(depth, None) if self.headers is not None else None
        block = self.blocks.pop(depth, None) if self.blocks is not None else None
        self.value()
        if closing == ")":
            self.body_end = self.primary_body_end = end
        if header == -1:
            self.function_body = True
        elif header is not None or block is not None:
            self.statement_end = end
            self.safe_statement = bool(header) if header is not None else bool(block)


def _javascript_regexp_prefix(text: str, at: int, *, javascript: bool = False) -> bool:
    before = at - 1
    while before >= 0 and (text[before].isspace() or (javascript and text[before] == "\ufeff")):
        before -= 1
    if before < 0 or text[before] in ":=([,!&|?{;+-*%^~<>":
        return True
    end = before + 1
    while before >= 0 and (text[before].isalnum() or text[before] in "_$"):
        before -= 1
    word = text[before + 1 : end]
    return word in _JAVASCRIPT_OPERAND_WORDS or (javascript and word in _JAVASCRIPT_ONLY_OPERANDS)


def _javascript_escape_followers(
    text: str, at: int, pending: int, literals: dict[int, int] | None = None
) -> tuple[int, int, bool, bool]:
    """Classify a local opaque-quote continuation before accepting a fence.

    A close owed to an escape cannot also close its interpolation. This run
    stops before a new template/regexp boundary. Real frames and quoted
    expressions retain their own closes; comments remain opaque trivia. The
    caller caches the inspected frontier, so no continuation is reprobed.
    This is lexical ownership, never a language-validity or keyword proof.
    """
    size = len(text)
    owned_end = -1
    source_closer = real_brace = False
    frames: list[str] = []
    opaque_quote_at = at
    while at < size:
        char = text[at]
        if text.startswith(":{", at):
            # This is the existing declaration/body source fence. Lookahead
            # must stop where the owning walk releases an unfinished label;
            # it cannot borrow closes or quoted extents from the new body.
            break
        if char.isspace() or char in "\ufeff,:;":
            at += 1
            continue
        if text.startswith("/*", at):
            end = text.find("*/", at + 2)
            at = size if end < 0 else end + 2
            continue
        if text.startswith("//", at):
            end = _JAVASCRIPT_BREAK.search(text, at + 2)
            at = size if end is None else end.end()
            continue
        if char == "}":
            while frames and frames[-1] not in {"}", "escape"}:
                frames.pop()
            if frames:
                if frames.pop() == "escape":
                    owned_end = at + 1
                    opaque_quote_at = at + 1
            elif pending:
                pending -= 1
                owned_end = at + 1
                opaque_quote_at = at + 1
            else:
                real_brace = True
        elif char in ")]":
            while frames and frames[-1] != char:
                frames.pop()
            if frames:
                frames.pop()
            else:
                # Only a closer outside the continuation's real frames can
                # prove a physical source fence and end ragged owners.
                return at + 1, owned_end, True, real_brace
        elif pending or frames:
            if char in "\"'":
                if at != opaque_quote_at:
                    literal = _CLOSED_STRING.match(text, at)
                    if literal is not None:
                        # A quote outside the pending token owns its contents
                        # after any lexical boundary, including punctuation or
                        # comments. Discovery reuses this private-only extent.
                        if literals is not None:
                            literals[at] = literal.end()
                        at = literal.end()
                        continue
                # Adjacent quotes remain part of the malformed token rather
                # than pairing with the real enclosing source delimiter.
                opaque_quote_at = at + 1
            if char in "`/#":
                break
            if char in "([{":
                frames.append({"(": ")", "[": "]", "{": "}"}[char])
            elif _JS_WORD.match(text, at) or unicodedata.category(char).startswith("C"):
                end, escape = _javascript_identifier_token(text, at)
                frames.extend("escape" for _ in range(escape))
                opaque_quote_at = end
                at = end
                continue
            # Other expression syntax is opaque, as in the owning walk. No
            # language-validity or keyword authority is inferred here.
        else:
            break
        at += 1
    return at, owned_end, source_closer, real_brace


def _javascript_literal(
    text: str, start: int, *, javascript: bool = False, statement_boundary: bool = False
) -> tuple[int, int, int] | None:
    """A regexp/template extent and its body, without evaluating JavaScript.

    A slash starts a regexp only at an expression boundary; Python division
    before a real # comment must retain its ordinary comment ownership.
    Unfinished literals stop at their language boundary, consuming each byte
    once even for malformed character classes. Templates use an explicit stack
    for interpolation and nested templates instead of recursive rescanning.
    """
    size = len(text)

    def regexp(
        at: int, javascript_words: bool = javascript, *, force: bool = False
    ) -> tuple[int, int, int] | None:
        if text.startswith("/*", at):
            closing = text.find("*/", at + 2)
            return (size, at + 2, size) if closing < 0 else (closing + 2, at + 2, closing)
        if not _javascript_regexp_prefix(text, at, javascript=javascript_words) and not (
            (at == start and statement_boundary) or force
        ):
            return None
        if text.startswith("//", at):
            return None
        position, in_class = at + 1, False
        while position < size and text[position] not in "\r\n\u2028\u2029":
            char = text[position]
            if char == "\\":
                position += 2
                continue
            if char == "/" and not in_class:
                end = position + 1
                while end < size and text[end].isascii() and text[end].isalpha():
                    end += 1
                return end, at + 1, position
            if char == "[":
                in_class = True
            elif char == "]":
                in_class = False
            position += 1
        return min(position, size), at + 1, min(position, size)

    if text[start] == "/":
        return regexp(start)
    frames = ["`"]
    escape_owners: list[int] = []
    contexts: list[tuple[int, _JavascriptBoundary]] = []
    interpolation_close_end = -1
    position = start + 1

    opaque_until = opaque_quote_at = -1
    opaque_literals: dict[int, int] = {}

    def physical_fence(at: int) -> bool:
        nonlocal opaque_until
        if not escape_owners:
            return True
        after, owned_end, source_closer, real_brace = _javascript_escape_followers(
            text, at + 1, len(escape_owners), opaque_literals
        )
        if source_closer:
            return True
        if owned_end >= 0:
            if real_brace and (after == size or text[after] in ".,;:(["):
                return True
            # The forward walk must keep these same owners through the gap,
            # rather than letting trivia cleanup preempt their actual closes.
            opaque_until = max(opaque_until, after, owned_end)
            return False
        # Trivia or punctuation alone never proves a synthetic close became
        # an interpolation close. Keep the inspected continuation until its
        # next literal/regexp boundary; the normal forward walk owns that
        # boundary. EOF remains a finite fence when no continuation is left.
        opaque_until = max(opaque_until, after)
        return after == size

    while position < size:
        char, closing = text[position], frames[-1]
        if closing == "`":
            if char == "\\":
                position += 2
                continue
            if char == "`":
                frames.pop()
                if not frames:
                    return position + 1, start + 1, position
                if contexts:
                    contexts[-1][1].value()
            elif text.startswith("${", position):
                frames.append("}")
                contexts.append((len(frames) - 1, _JavascriptBoundary(prefix_end=position + 2)))
                position += 2
                continue
            position += 1
            continue
        context = contexts[-1][1]
        if position == interpolation_close_end and (
            (char == "`" and physical_fence(position)) or text.startswith("${", position)
        ):
            # Only an interpolation-owned close can fence this template. A
            # synthetic close inside a real function/mapping leaves that real
            # frame and any enclosing escape alive across opaque punctuation.
            while len(frames) > contexts[-1][0]:
                frames.pop()
            contexts.pop()
            # The physical template fence also ends every pending escape in
            # this interpolation; none may enter a later source context.
            while escape_owners and escape_owners[-1] >= len(frames):
                escape_owners.pop()
            continue
        if position in opaque_literals:
            position = opaque_literals.pop(position)
            context.value()
            continue
        if (
            escape_owners
            and char in "\"'`"
            and context.word_end == position
            and opaque_quote_at == position
        ):
            # Keep escape-owned quotes opaque in this interpolation too; they
            # cannot pair with the real enclosing literal/template source fence.
            context.word_end = position + 1
            opaque_quote_at = position + 1
            position += 1
            continue
        if (
            escape_owners
            and position >= opaque_until
            and (char.isspace() or char in "\ufeff:;=" or (char in ")]" and frames[-1] != char))
        ):
            while escape_owners:
                owner = escape_owners.pop()
                while len(frames) > owner:
                    frames.pop()
                    context.close(len(frames), position, "")
            context.identifier_escape = False
            closing = frames[-1]
        if char == "}" and escape_owners:
            owner = escape_owners[-1]
            while len(frames) > owner + 1 and frames[-1] != "}":
                frames.pop()
                context.close(len(frames), position, "")
            if len(frames) == owner + 1:
                # A ragged interpolation may end with only unfinished escape
                # entries above its own close. Record that actual close event;
                # never infer it later from the raw preceding character. Any
                # intervening real frame still owns its boundary. The indices
                # are ordered, so this requires no per-close stack scan.
                depth = contexts[-1][0]
                if escape_owners[0] == depth + 1 and len(frames) - depth - 1 == len(escape_owners):
                    interpolation_close_end = position + 1
                frames.pop()
                escape_owners.pop()
                context.identifier_escape = bool(escape_owners)
                context.word_end = position + 1
                opaque_quote_at = position + 1
                position += 1
                continue
        identifier = bool(_JS_WORD.match(text, position)) or unicodedata.category(char).startswith(
            "C"
        )
        if not identifier and context.unknown_identifier_piece(text, char, position, position + 1):
            if char == "}":
                opaque_quote_at = position + 1
            position += 1
            continue
        if char.isspace() or char == "\ufeff":
            position += 1
            continue
        if text.startswith("//", position):
            end = _JAVASCRIPT_BREAK.search(text, position + 2)
            after = size if end is None else end.end()
            context.trivia(text, position, after)
            position = after
            continue
        if text.startswith("/*", position):
            end = text.find("*/", position + 2)
            after = size if end < 0 else end + 2
            context.trivia(text, position, after)
            position = after
            continue
        if char in "\"'":
            literal = _CLOSED_STRING.match(text, position)
            if literal is not None:
                position = literal.end()
                context.value()
                continue
        if char == "`":
            context.value()
            frames.append("`")
        elif text.startswith("=>", position):
            context.token(text, "=>", position, position + 2, len(frames))
            position += 2
            continue
        elif char == "/" and context.regexp(text, position):
            literal = regexp(position, True, force=True)
            if literal is not None:
                position = literal[0]
                context.value()
                continue
        elif identifier:
            end, escape = _javascript_identifier_token(text, position)
            raw = text[position:end]
            if not context.unknown_identifier_piece(text, raw, position, end):
                context.token(text, raw, position, end, len(frames), identifier=True)
            if escape:
                for _ in range(escape):
                    escape_owners.append(len(frames))
                    frames.append("escape")
            context.identifier_escape = bool(escape_owners)
            opaque_quote_at = end
            if escape or any(unicodedata.category(c).startswith("C") for c in raw):
                context.unknown_owner(end, contexts[-1][0])
            position = end
            continue
        elif char in "([{":
            context.token(text, char, position, position + 1, len(frames))
            frames.append({"(": ")", "[": "]", "{": "}"}[char])
        elif char == closing:
            frames.pop()
            if contexts[-1][0] == len(frames):
                contexts.pop()
            else:
                context.close(len(frames), position + 1, char)
        else:
            context.token(text, char, position, position + 1, len(frames))
        position += 1
    return size, start + 1, size


def _comment_gap_separator(text: str, start: int, block_close: int) -> tuple[int, int, int]:
    """The colon after the comment run at ``start``, or -1; where the walk ended.

    Line comments end where the comment scan's ``_COMMENT_END`` says, so the
    gap and that scan agree on every line break. ``block_close`` is the last
    ``*/`` found (``len(text)`` when there is none): callers pass it back and
    never start before the returned end, so each character is walked once, on
    a run of unclosed ``a/*b `` as on a ``a//b `` line.
    """
    size = len(text)
    position = start
    while True:
        if text.startswith("//", position):
            line_end = _COMMENT_END.search(text, position + 2)
            if line_end is None:
                return -1, size, block_close
            position = _GAP_SPACE.match(text, line_end.end()).end()
        elif text.startswith("/*", position):
            if block_close < position + 2:
                closing = text.find("*/", position + 2)
                block_close = size if closing < 0 else closing
            if block_close >= size:
                return -1, size, block_close
            position = _GAP_SPACE.match(text, block_close + 2).end()
        else:
            return (position if text.startswith(":", position) else -1), position, block_close


def _javascript_closed(text: str, start: int, literal: tuple[int, int, int]) -> bool:
    _, _, high = literal
    if high >= len(text):
        return False
    if text[start] == "`":
        return text[high] == "`"
    return text[high : high + 2] == "*/" if text.startswith("/*", start) else text[high] == "/"


# This view discovers conflicting lexical ownership, not JavaScript validity.
# A real Python comment cannot supply a slash opener; after a conflicting
# literal, only its owning mapping is scanned without Python comment rules.
_ALTERNATE_BOUNDARY = re.compile(
    rf"(?P<comment>\#[^{oct_blocks._LINE_CHARS}]*+)"
    rf"|{oct_blocks._STRING_LITERALS}|(?P<javascript>[`/])|(?P<word>{_JS_WORD.pattern})|=>|[{{}}\[\]():;,=+!*%&|^~<>?.-]|(?P<unknown>[^\s\ufeff])"
)

_ALTERNATE_ACTIVE = re.compile(
    rf"(?P<comment>(?!))|{oct_blocks._STRING_LITERALS}|(?P<javascript>[`/])"
    rf"|(?P<word>{_JS_WORD.pattern})|=>|[{{}}\[\]():;,=+!*%&|^~<>?.-]|(?P<unknown>[^\s\ufeff])"
)


def _alternate_intervals(
    text: str,
    statement_literals: dict[int, tuple[int, int, int]],
    alternate_literals: dict[int, tuple[int, int, int]],
    uncertain_punctuation: set[int] | None = None,
) -> list[tuple[int, int]]:
    """Source-fenced owners of JS lexical disagreements in one forward walk.

    This is deliberately conservative on malformed excerpts. Failed literal
    extents are walked structurally once, with inner slashes disabled, instead
    of retrying each opener against the suffix or demanding valid syntax.
    """
    if "/" not in text and "\\" not in text:
        return []
    stack: list[tuple[int, str, bool]] = []
    mappings: list[int] = []
    selected: list[tuple[int, int]] = []
    context = _JavascriptBoundary()
    position = failed_until = 0
    escape_owners: list[int] = []
    opaque_until = opaque_probe_end = -1
    opaque_literals: dict[int, int] = {}

    def release_escape() -> None:
        escape_owner = escape_owners.pop()
        # These are local malformed-token delimiters, never the containing
        # mapping. Every frame is discarded once; no suffix is rescanned.
        while len(stack) > escape_owner:
            start, closing, chosen = stack.pop()
            if closing == "}":
                mappings.pop()
            context.close(len(stack), position, "")
            if chosen:
                selected.append((start, position))
        context.identifier_escape = bool(escape_owners)
        context.word_end = position

    while position < len(text):
        if escape_owners and text[position] in "\"'`":
            if position in opaque_literals:
                position = opaque_literals.pop(position)
                context.value()
                continue
            # This private view retains punctuation in a contiguous unfinished
            # escape. Handle its opener before matching any literal extent, so
            # paired quotes cannot swallow real braces or rescan suffixes.
            if position >= opaque_probe_end:
                # Source discovery can retain a Python division view of //.
                # Do not reprobe backticks inside a comment already traversed
                # by this local classifier, even under that conflicting view.
                opaque_probe_end, owned_end, source_closer, real_brace = (
                    _javascript_escape_followers(
                        text, position + 1, len(escape_owners), opaque_literals
                    )
                )
                continuation = (
                    opaque_probe_end
                    if opaque_probe_end < len(text) and not source_closer and not real_brace
                    else -1
                )
                opaque_until = max(opaque_until, owned_end, continuation)
            context.word_end = position + 1
            if uncertain_punctuation is not None:
                uncertain_punctuation.add(position)
            position += 1
            continue
        recovery = position < failed_until
        tokens = _ALTERNATE_ACTIVE if recovery else _ALTERNATE_BOUNDARY
        token = tokens.search(text, position, failed_until if recovery else len(text))
        if token is None:
            if recovery:
                position = failed_until
                continue
            break
        previous = position
        at, position = token.start(), token.end()
        if (
            escape_owners
            and at >= opaque_until
            and (
                text[previous:at]
                or token.lastgroup in {"single", "double", "triple", "comment"}
                or (token.group() == ":" and text[position : position + 1] == "{")
            )
        ):
            while escape_owners:
                release_escape()
        escape = 0
        identifier = token.lastgroup == "word" or (
            token.lastgroup == "unknown" and unicodedata.category(text[at]).startswith("C")
        )
        if identifier:
            position, escape = _javascript_identifier_token(text, at)
        raw = text[at:position]
        unknown = identifier and (
            escape or any(unicodedata.category(c).startswith("C") for c in raw)
        )
        quoted_escape = (
            token.lastgroup in {"single", "double"}
            and "\\u{" in raw
            and _javascript_string(raw) != _string(raw)
        )
        if (unknown or (identifier and "\\" in raw) or quoted_escape) and mappings:
            owner = mappings[0]
            start, close, chosen = stack[owner]
            stack[owner] = (start, close, True)
        if raw == "}" and escape_owners:
            # Discard ragged non-brace frames only within this escape. A
            # nested brace still owns its close; only the next close may
            # release the escape. No containing JWK/source frame advances.
            while len(stack) > escape_owners[-1] + 1 and stack[-1][1] != "}":
                stack.pop()
                context.close(len(stack), position, "")
            if len(stack) == escape_owners[-1] + 1:
                release_escape()
                continue
        piece = context.unknown_identifier_piece(text, raw, at, position)
        # Even a continuation after opaque punctuation may contain several
        # unfinished openers. Register each before skipping the token body.
        if escape and mappings:
            for _ in range(escape):
                escape_owners.append(len(stack))
                stack.append((at, "escape", False))
        if piece:
            continue
        if token.lastgroup == "comment":
            context.value()
            continue
        if token.lastgroup == "javascript" and at >= failed_until:
            if text.startswith("/*", at):
                literal = _javascript_literal(text, at)
                assert literal is not None
                context.trivia(text, at, literal[0])
                position = literal[0]
                continue
            if text.startswith("//", at):
                if context.line_trivia(text, at):
                    end = _JAVASCRIPT_BREAK.search(text, at + 2)
                    after = len(text) if end is None else end.end()
                    context.trivia(text, at, after)
                    position = after
                else:
                    # A Python floor-division pair cannot start a regexp at
                    # its second slash or make the following # part of one.
                    position = at + 2
                    context.token(text, "//", at, position, len(stack))
                continue
        if token.lastgroup in {"single", "double", "triple"}:
            prefix = token.group("prefix") or ""
            if "f" in prefix.lower() or "t" in prefix.lower():
                start = at + len(prefix)
                quote = text[start] * (3 if text.startswith(text[start] * 3, start) else 1)
                position, _ = oct_blocks._formatted_end(text, start, quote)
            context.value()
            continue
        if token.lastgroup == "javascript":
            if at < failed_until:
                continue
            statement = (
                context.unknown_depth < 0
                and context.statement(text, at, primary=True)
                and context.safe_statement
            )
            literal = _javascript_literal(
                text, at, javascript=True, statement_boundary=context.regexp(text, at)
            )
            if literal is None:
                context.token(text, raw, at, position, len(stack))
                continue
            end, low, high = literal
            closed = _javascript_closed(text, at, literal)
            if statement and closed:
                # Only an unambiguous statement boundary may publish an extent
                # into primary ownership. Ambiguous calls remain private-only
                # alternates: their hash may start a real Python type comment.
                statement_literals[at] = literal
            elif raw == "/" and (not closed or not _javascript_regexp_prefix(text, at)):
                if closed:
                    alternate_literals[at] = literal
                # The owning value may be a nested function/body expression.
                # Preserve every containing mapping's independent role scan;
                # coalescing below means their shared bytes are visited once.
                if mappings:
                    owner = mappings[0]
                    start, close, chosen = stack[owner]
                    if not chosen:
                        stack[owner] = (start, close, True)
            if closed:
                position = end
            else:
                # Keep baseline private evidence in incomplete literal text.
                # No nested slash may re-scan this already inspected extent.
                failed_until = end
            context.value()
            continue
        if raw in "{[(":
            context.token(text, raw, at, position, len(stack))
            if raw == "{":
                mappings.append(len(stack))
            stack.append((at, {"{": "}", "[": "]", "(": ")"}[raw], False))
        elif raw in "}])":
            if context.unknown_depth >= 0 and raw != "}" and stack and stack[-1][1] != raw:
                context.token(text, raw, at, position, len(stack))
                continue
            # Ragged excerpts close each pending container once. An incomplete
            # selected owner never gets a fresh frame outside this source fence.
            while stack:
                start, closing, chosen = stack.pop()
                index = len(stack)
                if closing == "escape":
                    escape_owners.pop()
                context.close(index, position, closing if closing == raw else "")
                if closing == "}":
                    mappings.pop()
                if chosen:
                    selected.append((start, position))
                if closing == raw:
                    break
        else:
            context.token(text, raw, at, position, len(stack), identifier=identifier)
            context.identifier_escape = bool(escape)
            if unknown and mappings:
                context.unknown_owner(position, mappings[0] + 1)
    selected.extend((start, len(text)) for start, _, chosen in stack if chosen)
    result: list[tuple[int, int]] = []
    for low, high in sorted(selected):
        if result and low < result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], high))
        else:
            result.append((low, high))
    return result


def _comment_content(text: str) -> tuple[str, bool]:
    """One offset-preserving helper context, or explicit ambiguous ownership.

    Unquoted hashes are payload separators. Complete literals (including reused
    f/t delimiters) retain their bytes. An orphan or unfinished literal cannot
    establish helper ownership: the caller recovers potential private values
    only with actual type evidence, clipped to original lexical comment bodies.
    """
    result: StringIO | None = None
    copied = position = 0
    typed_literal = False
    while token := _COMMENT_CONTENT.search(text, position):
        position = token.end()
        if token.lastgroup == "hash":
            if result is None:
                result = StringIO()
            result.write(text[copied : token.start()])
            result.write(" " * (position - token.start()))
            copied = position
            continue
        if token.lastgroup == "orphan_quote":
            # After a literal carrying serialized type evidence, an ignored
            # closing apostrophe may actually close a value exposed by an
            # earlier premature quote match. Do not recover it independently.
            if typed_literal:
                return text, True
            continue
        prefix = token.group("prefix") or ""
        start = token.start() + len(prefix)
        delimiter = text[start] * (3 if token.lastgroup == "triple" else 1)
        if "f" in prefix.lower() or "t" in prefix.lower():
            position, closed = oct_blocks._formatted_end(text, start, delimiter)
            if not closed or position != token.end():
                # Reused delimiters can make the primary token end before the
                # helper's complete f/t extent. Do not let their owners differ.
                return text, True
        elif token.lastgroup == "triple":
            _, _, high = _literal_bounds(token.group())
            if high == len(token.group()):
                return text, True
        elif _MISPAIRED[text[start]].match(text, position) or (
            position == len(text) and not _CLOSED_STRING.fullmatch(text[start:position])
        ):
            return text, True
        candidate = _COMMENT_TOKEN.match(text, position - 1)
        if candidate is not None and candidate.group("key") is not None:
            # The quote just consumed as a closer can actually open a key,
            # including escaped names that the simple mispair hint cannot read.
            return text, True
        raw = text[start:position]
        typed_literal = typed_literal or (_may_have_kty(raw) and _string(raw) != "kty")
    if result is None:
        return text, False
    result.write(text[copied:])
    return result.getvalue(), False


@dataclass(slots=True)
class _CommentFrame:
    parent: int
    start: int
    opening: str
    expression: bool
    mask: int
    members: list[tuple[str, int, int, str | None]]
    end: int


@dataclass(slots=True)
class _PrivateEffectFrame:
    """Private-only entry state; none of these fields belong to a legacy frame."""

    start: int
    closing: str
    phase: str = "entry"
    key: int = 0  # kty=1, proved other=0, possible=-1
    key_braced: bool = False
    entry: int = -1
    accessor: str = ""
    value: int = 0
    value_braced: bool = False
    value_seen: bool = False
    child: int = -1
    spread: str = ""
    last: int | None = None  # ABSENT is distinct from UNKNOWN/NONJWK (zero).
    genuine: bool = False
    getter_kty: bool = False
    getter_other: bool = False
    constant: int = 0  # constant-key emitted prefix length, or -1=mismatch
    constant_next: bool = True
    constant_valid: bool = True
    computed_braced: bool = False
    private_name: str = ""  # identity/braced private key awaiting its value


def _javascript_private_effects(
    text: str,
    unpacks: dict[int, tuple[int, int, int]],
    *,
    recovery: bool = False,
    statement_literals: dict[int, tuple[int, int, int]] | None = None,
    physical_comments: tuple[array, int] | None = None,
    evidence: str = "braced",
    private_fields: list[tuple[str, int, int]] | None = None,
) -> dict[int, int]:
    """One additional private-only member-entry projection and fixed-state fold.

    ``evidence`` names what makes a type member genuine JS: ``braced`` (a
    ``\\u{..}`` escape), ``continued`` (also a line continuation, which only
    JS reads in a type key or value spelling) or ``any`` (comment prose, where
    no Python source meaning applies; tasks #2227, #2239). ``private_fields``
    collects each quoted private key spelled with escapes as (name, value
    start, owning object start), for ``_identity_members``.

    This pass adds no public/decided interval, old type/mask, or unpack edge.
    Each accepted value/literal is consumed once; computed string pieces retain
    only their comparison against kty. Copy summaries fold on the owning entry's
    delimiter, never by reparsing a child's source. The old ** proof producer
    supplies original-operator bundles selected by the caller's accepted view.
    """
    nodes: dict[int, tuple[int | None, bool, bool]] = {}
    opening_order: list[int] = []
    relations: dict[int, int] = {}
    frames: list[_PrivateEffectFrame] = []
    proofs = _recovery_field_proofs(text) if recovery else None
    position = rescanned = 0
    size = len(text)

    def live(at: int) -> bool:
        if physical_comments is None:
            return True
        boundaries, offset = physical_comments
        return bisect_right(boundaries, offset + at) % 2 == 0

    def complete(frame: _PrivateEffectFrame) -> None:
        if frame.closing != "}":
            return
        if frame.spread:
            child = frame.child
            accepted = True
            if frame.spread == "**":
                bundle = unpacks.get(frame.entry)
                # An absent operator can be opaque in the accepted view. It
                # cannot acquire a projected mutation or a guessed offset.
                accepted = bundle is not None and bundle[0] == frame.start
                if accepted:
                    child = bundle[1]
            if accepted:
                summary = nodes.get(child)
                if child >= 0 and summary is not None:
                    state, genuine, hazard = summary
                    relations[child] = frame.start
                    frame.genuine |= genuine
                    if hazard:
                        frame.last = 0
                        frame.getter_other = True
                    elif state is not None:
                        frame.last = state
                else:
                    frame.last = 0
                    frame.getter_other = True  # Unknown copy can export accessors.
            # An ignored event still finishes this entry. Shared cleanup below
            # prevents its transient spread/offset from leaking into the next
            # field, without supplying an event, type, or genuine evidence.
        elif frame.key:
            frame.last = frame.value if frame.phase == "value" else 0
            if frame.accessor == "get":
                frame.getter_kty = True
            elif frame.phase == "value" and frame.key == 1:
                frame.getter_kty = False  # This exact descriptor is replaced.
            if frame.key < 0:
                frame.last = 0
                frame.getter_other = True
            if frame.last and (frame.key_braced or frame.value_braced):
                frame.genuine = True
        elif frame.accessor == "get":
            frame.getter_other = True
        frame.phase = "entry"
        frame.key = 0
        frame.key_braced = frame.value_braced = frame.value_seen = False
        frame.entry = frame.child = -1
        frame.accessor = frame.spread = frame.private_name = ""
        frame.value = 0

    def genuine_spelling(raw: str) -> bool:
        if evidence == "any":
            return True
        if _javascript_braced(raw):
            return True
        return evidence == "continued" and _JS_CONTINUED.search(raw) is not None

    def literal_word(raw: str, *, quoted: bool) -> tuple[str | None, bool]:
        if not quoted:
            # IdentifierName decoding is single-layer and never scans braces
            # from an escape as a new mapping delimiter.
            if any(m.group().split("u", 1)[0] != "\\" for m in _JS_IDENTIFIER_ESCAPE.finditer(raw)):
                return None, False
            return _javascript_member_name(raw), genuine_spelling(raw)
        prefix, low, high = _literal_bounds(raw)
        if prefix or low != 1 or high >= len(raw):
            return None, False
        return _javascript_logical_string(raw), genuine_spelling(raw)

    while position < size:
        char = text[position]
        if char.isspace() or char == "\ufeff":
            position += 1
            continue
        frame = frames[-1] if frames else None
        at = position
        if char == "#":
            if recovery:
                position += 1  # Payload separator, not new raw source authority.
            else:
                end = _PHYSICAL_BREAK.search(text, position + 1)
                position = size if end is None else end.end()
            continue
        if text.startswith("//", position):
            if recovery:
                # A comment payload separator, like # above (task #2224): the
                # rest of the physical line is the payload being recovered.
                position += 2
                continue
            end = _JAVASCRIPT_BREAK.search(text, position + 2)
            position = size if end is None else end.end()
            continue
        if text.startswith("/*", position):
            end = text.find("*/", position + 2)
            position = size if end < 0 else end + 2
            continue
        if statement_literals and position in statement_literals:
            position = statement_literals[position][0]
            if frame is not None and frame.phase == "value":
                frame.value_seen = True
                frame.value = 0
                frame.child = -1
            continue
        opening = _LITERAL_OPEN.match(text, position)
        indexed_name = recovery and (frame is None or frame.phase in ("entry", "name"))
        literal = None
        if opening is not None and not indexed_name:
            # _LITERAL_OPEN only locates a possible delimiter. The accepted
            # lexer also checks attached-prefix and apostrophe boundaries.
            # Rejection returns to ordinary identifier/punctuation progress;
            # it cannot consume or normalize a previously unowned suffix.
            literal = _RECOVERY_LITERAL.match(text, position)
            if literal is None:
                opening = None
        quoted = opening is not None
        raw = ""
        if quoted:
            assert opening is not None
            prefix = opening["prefix"] or ""
            quote = position + len(prefix)
            if indexed_name:
                # Adjacent same-delimiter intervals are indexed once. An
                # orphan must not trigger a suffix literal scan at each quote.
                proof = proofs.get(quote) if proofs is not None else None
                if proof is None:
                    position = opening.end()
                    continue
                position = proof[0] + 1
            else:
                assert literal is not None
                position = literal.end()
                if "f" in prefix.lower() or "t" in prefix.lower():
                    delimiter = opening["quote"]
                    position, _ = oct_blocks._formatted_end(text, quote, delimiter)
            raw = text[at:position]
            if not recovery and frame is None and at >= rescanned:
                if _MISPAIRED[text[quote]].match(text, position) or (
                    position == size and not _CLOSED_STRING.fullmatch(raw)
                ):
                    position, rescanned = opening.end(), position
                    continue
        elif _JS_WORD.match(text, position):
            position, _ = _javascript_identifier_token(text, position)
            raw = text[at:position]
        elif text.startswith("...", position) or text.startswith("**", position):
            position += 3 if text.startswith("...", position) else 2
            raw = text[at:position]
        else:
            position += 1
            raw = char

        if frame is not None and raw == frame.closing:
            if frame.closing == "]" and frame.phase == "computed":
                frames.pop()
                if frames:
                    parent = frames[-1]
                    parent.key = (
                        (1 if frame.constant == 3 else 0)
                        if frame.constant_valid and not frame.constant_next
                        else -1
                    )
                    parent.key_braced = frame.computed_braced
                    parent.phase = "name"
                continue
            complete(frame)
            frames.pop()
            if frame.closing == "}":
                nodes[frame.start] = (
                    frame.last,
                    frame.genuine,
                    frame.getter_kty or frame.getter_other,
                )
            if frames:
                parent = frames[-1]
                if parent.phase == "value" and parent.child == frame.start:
                    if frame.closing != "}":
                        parent.child = -1
            continue
        if frame is not None and raw == "," and frame.closing == "}":
            complete(frame)
            continue
        if frame is not None and frame.phase == "computed":
            if quoted and frame.constant_next:
                word, genuine = literal_word(raw, quoted=True)
                frame.computed_braced |= genuine
                if word is None:
                    frame.constant_valid = False
                elif frame.constant >= 0:
                    for letter in word:
                        if frame.constant < 3 and letter == "kty"[frame.constant]:
                            frame.constant += 1
                        else:
                            frame.constant = -1
                            break
                frame.constant_next = False
            elif raw == "+" and not frame.constant_next:
                frame.constant_next = True
            else:
                frame.constant_valid = False
            # Unknown nested expression delimiters still have exactly one
            # structural owner; their interior cannot become another key.
            if not quoted and raw in "([{":
                frames.append(
                    _PrivateEffectFrame(at, {"(": ")", "[": "]", "{": "}"}[raw], "opaque")
                )
            continue
        if frame is not None and frame.closing == "}" and frame.phase == "entry":
            frame.entry = at
            if raw in ("...", "**"):
                frame.phase, frame.spread = "value", raw
                continue
            if raw == "[":
                frames.append(_PrivateEffectFrame(at, "]", "computed"))
                frame.phase = "computed_wait"
                continue
            if quoted or _JS_WORD.match(text, at):
                word, genuine = literal_word(raw, quoted=quoted)
                frame.key = 1 if word == "kty" else -1 if word is None else 0
                frame.key_braced = genuine and live(at)
                frame.accessor = word if not quoted and word in ("get", "set") else ""
                frame.phase = "name"
                if private_fields is not None and quoted:
                    frame.private_name = _escaped_private_name(text, at, raw) or ""
                continue
            frame.key, frame.phase = (0 if raw[0].isdigit() else -1), "malformed"
        elif frame is not None and frame.closing == "}" and frame.phase == "name":
            if raw == ":":
                frame.phase = "value"
                continue
            if frame.accessor and raw == "[":
                frames.append(_PrivateEffectFrame(at, "]", "computed"))
                frame.phase = "computed_wait"
                continue
            if frame.accessor and (quoted or _JS_WORD.match(text, at)):
                word, genuine = literal_word(raw, quoted=quoted)
                frame.key = 1 if word == "kty" else -1 if word is None else 0
                frame.key_braced = genuine and live(at)
                frame.phase = "method"
                continue
            frame.phase = "method" if raw == "(" else "malformed"
        elif frame is not None and frame.phase == "value":
            if not frame.value_seen:
                frame.value_seen = True
                if private_fields is not None and frame.private_name and not frame.spread:
                    private_fields.append((frame.private_name, at, frame.start))
                if quoted and frame.key == 1 and not frame.spread and live(at):
                    word, genuine = literal_word(raw, quoted=True)
                    frame.value = _TYPE_MASK.get(word, 0)
                    frame.value_braced = genuine
                if raw == "{" and frame.spread:
                    frame.child = at
            else:
                frame.value = 0
                frame.value_braced = False
                frame.child = -1
        if not quoted and raw in "([{":
            if raw == "{":
                opening_order.append(at)
            frames.append(
                _PrivateEffectFrame(
                    at, {"(": ")", "[": "]", "{": "}"}[raw], "entry" if raw == "{" else "opaque"
                )
            )
    while frames:
        frame = frames.pop()
        complete(frame)
        if frame.closing == "}":
            nodes[frame.start] = (frame.last, frame.genuine, frame.getter_kty or frame.getter_other)
    # Literal-copy parents precede their children in local source order. A
    # reverse eligibility fold and forward role fold visit each edge once.
    eligibility = {start: genuine for start, (_, genuine, _) in nodes.items()}
    for child in reversed(opening_order):
        parent = relations.get(child)
        if parent is not None:
            eligibility[parent] = eligibility.get(parent, False) or eligibility.get(child, False)
    masks: dict[int, int] = {}
    for start in opening_order:
        parent = relations.get(start)
        eligible = eligibility.get(start, False) or (
            parent is not None and eligibility.get(parent, False)
        )
        eligibility[start] = eligible
        role = (nodes[start][0] or 0) | (masks.get(parent, 0) if parent is not None else 0)
        masks[start] = role if eligible else 0
    return masks


def _merged_intervals(first: array, second: array) -> array:
    """Two sorted, mutually disjoint interval arrays as one, in one pass."""
    merged = array("q")
    left = right = 0
    while left < len(first) or right < len(second):
        if right >= len(second) or (left < len(first) and first[left] <= second[right]):
            merged.extend((first[left], first[left + 1]))
            left += 2
        else:
            merged.extend((second[right], second[right + 1]))
            right += 2
    return merged


def _continued_comments(text: str, comments: array) -> array:
    """Comment bodies extended over JS line continuations (task #2169).

    A Python comment ends at the physical break, but a commented-out JS
    string ending in an unescaped backslash continues on the next line. The
    comment view also reads that line, so a key such as ``"d\\<break>p"``
    keeps its whole spelling. Live code is still scanned as before; this view
    only adds candidates. Each character is visited a bounded number of times.
    Every break that ends a comment token (JS, Python and YAML breaks,
    including NEL) also continues it, so no grammar's escaped break truncates
    the body.
    """
    bodies = array("q")
    reach = -1
    for index in range(0, len(comments), 2):
        low, high = comments[index], comments[index + 1]
        if low < reach:
            continue  # already inside an earlier body's continued lines
        segment = low
        while high < len(text) and _COMMENT_END.match(text, high):
            slash = high
            while slash > segment and text[slash - 1] == "\\":
                slash -= 1
            if (high - slash) % 2 == 0:
                break
            segment = high + (2 if text.startswith("\r\n", high) else 1)
            following = _COMMENT_END.search(text, segment)
            high = following.start() if following is not None else len(text)
        bodies.extend((low, high))
        reach = high
    return bodies


def _comment_recovery_members(
    text: str,
    comments: array,
    depth: int = 0,
    source: _SourceMap | None = None,
    snippets: list[_Snippet] | None = None,
) -> list[PrivateMember]:
    """Recover typed private fields with local, offset-preserving ownership.

    Ambiguous prose quotes cannot own syntax. Actual field values still own
    their complete literals, and brackets retain the enclosing object's type.
    Only literal unpack/grouping edges transfer types between frames. The two
    frame walks visit every edge once, including reverse-order members.
    """
    nodes = [_CommentFrame(-1, -1, "", False, 0, [], len(text))]
    stack = [0]
    position, property_work = 0, 0
    properties = oct_blocks._ValueHeads(text)
    blanked = array("q")
    javascript = _javascript_braced(text)
    tokens = _RECOVERY_TOKEN if javascript else _RECOVERY_BASE_TOKEN
    legacy_masks: dict[int, int] | None = {} if javascript else None
    literal_fields: dict[int, int] | None = {} if javascript else None
    proof_index = _recovery_field_proofs(text) if javascript else None
    value_proofs: dict[int, tuple[int, bool, int] | None] | None = {} if javascript else None
    proof_opaque_until = -1
    supplement_members: list[tuple[str, int, int]] | None = [] if javascript else None
    while token := tokens.search(text, position):
        name = token.lastgroup
        javascript_name = False
        actual_type_name = None
        if name == "javascript_field":
            raw_name = token["javascript_field"]
            prefix, _, _ = _literal_bounds(raw_name)
            quote_start = token.start("javascript_field") + len(prefix)
            if not prefix and (extra_name := _javascript_private_name(text, quote_start, raw_name)):
                assert supplement_members is not None
                supplement_members.append((extra_name, token.end(), stack[-1]))
            logical = _javascript_string(raw_name)
            actual_type_name = (
                not prefix
                and logical == "kty"
                and _javascript_type_name(text, quote_start, raw_name)
                and text[token.end("javascript_field") : token.end()].strip() == ":"
            )
            if (
                _javascript_braced(raw_name)
                and not _python_raw_literal(text, quote_start)
                and logical in _JAVASCRIPT_RECOVERY_NAMES
            ):
                name = logical
                javascript_name = True
            else:
                # An unknown/non-JS/raw field consumes exactly the old token,
                # including its old quote/hash handling and opacity rules.
                token = _RECOVERY_BASE_TOKEN.match(text, token.start())
                assert token is not None
                name = token.lastgroup
        position = token.end()
        if name == "unpack":
            continue
        if name in ("quote", "hash"):
            if name == "quote" and nodes[stack[-1]].expression:
                literal = _RECOVERY_LITERAL.match(text, token.start())
                if literal is not None:
                    position = literal.end()
                    prefix = literal.group("prefix") or ""
                    if "f" in prefix.lower() or "t" in prefix.lower():
                        start = literal.start() + len(prefix)
                        delimiter = text[start] * (3 if literal.lastgroup == "triple" else 1)
                        position, _ = oct_blocks._formatted_end(text, start, delimiter)
                    continue
            if name == "quote" and proof_index is not None:
                quote = token.end() - 1
                candidate = proof_index.pop(quote, None)
                if (
                    candidate is not None
                    and quote >= proof_opaque_until
                    and _recovery_nonprivate_name(text, token.start(), quote, candidate[0])
                ):
                    assert literal_fields is not None and value_proofs is not None
                    low = candidate[1]
                    literal_fields[low] = stack[-1]
                    if low not in value_proofs:
                        value_proofs[low] = _recovery_value_extent(text, low)
                    extent = value_proofs[low]
                    if extent is not None:
                        # Proof opacity only: BASE consumes its exact old
                        # tokens. Interior value fields cannot rescan this
                        # literal/formatted owner or acquire another proof.
                        proof_opaque_until = extent[0]
            blanked.extend((token.start(), token.end()))
            continue
        if name == "bracket":
            char = token.group()
            if char in "([{":
                parent = stack[-1]
                nodes.append(
                    _CommentFrame(
                        parent, token.start(), char, nodes[parent].expression, 0, [], len(text)
                    )
                )
                stack.append(len(nodes) - 1)
            elif (
                len(stack) > 1 and nodes[stack[-1]].opening == {"}": "{", ")": "(", "]": "["}[char]
            ):
                node = nodes[stack.pop()]
                node.end = token.start()
            continue
        frame = nodes[stack[-1]]
        low = token.end()
        if name == "kty":
            if actual_type_name is None:
                actual_type_name = (
                    _javascript_type_name(text, token.start("kty"), token["kty"])
                    and text[token.end("kty") : low].strip() == ":"
                )
            value_low = low
            if javascript and (value_token := _JAVASCRIPT_RECOVERY_TYPE.match(text, low)):
                raw_value = value_token["literal"]
                prefix, _, _ = _literal_bounds(raw_value)
                value = _javascript_string(raw_value)
                if (
                    _javascript_braced(raw_value)
                    and not _python_raw_literal(text, low + len(prefix))
                    and value in _KEY_TYPES
                ):
                    frame.mask |= _TYPE_MASK[value]
                    position = value_token.end()
                    continue
            if _RECOVERY_TYPE_OPEN.match(text, low):
                heads = _RECOVERY_QUOTED_TYPES
            else:
                prefix = _RECOVERY_TYPE_START.match(text, low)
                value_low = properties.skip(prefix.end())
                if text.find("!<", low, value_low) >= 0 or text.startswith("!<", value_low):
                    property_work += len(text) - low
                    if property_work > 2 * len(text):
                        # Uncertain long property runs protect private families
                        # in this frame, never an unrelated object or live code.
                        frame.mask = 7
                        if legacy_masks is not None and not javascript_name:
                            legacy_masks[stack[-1]] = 7
                        continue
                heads = _RECOVERY_TYPES
            for kind, head in heads.items():
                match = head.match(text, value_low)
                if match is not None:
                    frame.mask |= _TYPE_MASK[kind]
                    if legacy_masks is not None and not javascript_name:
                        index = stack[-1]
                        legacy_masks[index] = legacy_masks.get(index, 0) | _TYPE_MASK[kind]
                    position = max(position, match.end())
            continue
        high, value = len(text), None
        literal = _RECOVERY_LITERAL.match(text, low)
        if literal is not None:
            prefix = literal.group("prefix") or ""
            start, end = literal.start() + len(prefix), literal.end()
            closed = True
            if "f" in prefix.lower() or "t" in prefix.lower():
                delimiter = text[start] * (3 if literal.lastgroup == "triple" else 1)
                end, closed = oct_blocks._formatted_end(text, start, delimiter)
            if closed and _RECOVERY_TAIL.match(text, end):
                high = end
                raw = text[low:high]
                if "f" in prefix.lower() or "t" in prefix.lower():
                    value = raw
                elif literal.lastgroup == "triple":
                    value = _triple_value(raw)
                elif "b" in prefix.lower():
                    try:
                        value = ast.parse(raw, mode="eval").body.value.decode("utf-8", "replace")
                    except (SyntaxError, ValueError, AttributeError):
                        value = raw
                else:
                    value = _string(raw)
                position = end
        frame.members.append((name, low, high, value))
        frame.expression = frame.expression or value is None
    # URI offsets are only needed for the type pass, not the normal scanner
    # below; release them before another oct-block scan builds its own index.
    del properties
    projection = StringIO()
    code_view = StringIO()
    recovered_quotes = array("q")
    copied = 0
    for index in range(0, len(blanked), 2):
        low, high = blanked[index], blanked[index + 1]
        projection.write(text[copied:low])
        projection.write(" " * (high - low))
        code_view.write(text[copied:low])
        if text[low:high].startswith("#"):
            code_view.write(text[low:high])
        else:
            code_view.write(" " * (high - low))
            recovered_quotes.append(low)
        copied = high
    projection.write(text[copied:])
    code_view.write(text[copied:])
    public_members: list[PrivateMember] = []
    normal_edges: dict[int, int] = {}
    normal_uncertain: set[int] = set()
    normal_unsafe = array("q")
    normal_private_unpacks: dict[int, tuple[int, int, int]] = {}
    normal_private_opaque: list[tuple[int, int]] = []
    normal_children: list[_Snippet] = []
    normal, _ = _json_members_in_context(
        code_view.getvalue(),
        depth,
        oct_sources=None,
        snippets=normal_children,
        decoded_spans=[],
        audit_source=source,
        isolate_comments=False,
        public_members=public_members,
        recovered_quotes=recovered_quotes,
        unpack_evidence=(normal_edges, normal_uncertain, normal_unsafe),
        keyword_members=True,
        private_unpacks=normal_private_unpacks if javascript else None,
        private_opaque=normal_private_opaque if javascript else None,
    )
    projected_private_unpacks: dict[int, tuple[int, int, int]] = {}
    uncertain: set[int] = set()
    calls: dict[int, int] = {}
    computed_calls: dict[int, int] = {}
    inherited = _unpacked_parents(
        projection.getvalue(),
        uncertain_parents=uncertain,
        call_parents=calls,
        computed_calls=computed_calls,
        private_effects=projected_private_unpacks if javascript else None,
    )
    inherited.update(calls)
    uncertain.update(computed_calls)
    # Actual inline comments are trivia after a complete literal operand. The
    # payload view still discovers independent private constructions in notes.
    by_start = {node.start: index for index, node in enumerate(nodes)}
    proven_parents = set()
    for parent in normal_edges.values():
        node_index = by_start.get(parent)
        if node_index is None:
            continue
        quote_index = bisect_right(normal_unsafe, parent - 1)
        if quote_index == len(normal_unsafe) or normal_unsafe[quote_index] >= nodes[node_index].end:
            proven_parents.add(parent)
    uncertain.difference_update(proven_parents - normal_uncertain)
    uncertain.update(normal_uncertain)
    inherited.update(normal_edges)
    accepted_private_unpacks = dict(normal_private_unpacks)
    blocked_private_owners: set[int] = set()
    if javascript:
        opaque = sorted(normal_private_opaque)
        opaque_starts = [low for low, _ in opaque]
        for at, event in projected_private_unpacks.items():
            if at in normal_private_unpacks:
                continue  # The same original operator's normal proof wins.
            owner = event[0]
            if owner in proven_parents:
                interval = bisect_right(opaque_starts, at) - 1
                if interval >= 0 and at < opaque[interval][1]:
                    continue  # This normal view owns the operator opaquely.
                blocked_private_owners.add(owner)  # No guessed provenance.
            else:
                accepted_private_unpacks[at] = event
    supplement_masks = (
        _javascript_private_effects(text, accepted_private_unpacks, recovery=True)
        if javascript
        else {}
    )
    for owner in blocked_private_owners:
        supplement_masks.pop(owner, None)
    hints = [bool(node.mask) for node in nodes]
    legacy_hints = set(legacy_masks) if legacy_masks is not None else None
    for index in range(len(nodes) - 1, 0, -1):
        node = nodes[index]
        hints[node.parent] = hints[node.parent] or hints[index]
        if legacy_hints is not None and index in legacy_hints:
            legacy_hints.add(node.parent)
        parent = by_start.get(inherited.get(node.start))
        if parent is not None:
            nodes[parent].mask |= node.mask
            if legacy_masks is not None and (mask := legacy_masks.get(index, 0)):
                legacy_masks[parent] = legacy_masks.get(parent, 0) | mask
    for index, node in enumerate(nodes):
        if node.start in uncertain and hints[index]:
            node.mask = 7
            if legacy_masks is not None and index in legacy_hints:
                legacy_masks[index] = 7
        parent = by_start.get(inherited.get(node.start))
        if parent is not None:
            node.mask |= nodes[parent].mask
            if legacy_masks is not None and (mask := legacy_masks.get(parent, 0)):
                legacy_masks[index] = legacy_masks.get(index, 0) | mask
    if literal_fields:
        assert legacy_masks is not None and value_proofs is not None

        def literal_coarse_member(
            name: str,
            low: int,
            high: int,
            fields=literal_fields,
            owners=nodes,
            masks=legacy_masks,
            values=value_proofs,
        ) -> bool:
            role = 1 if name in _RSA_ONLY else 4 if name == "k" else 7
            for shift in range(4):
                original = low - shift
                index = fields.get(original)
                if index is None or owners[index].end >= len(text):
                    continue
                if masks.get(index, 0) & role:
                    continue
                extent = values[original]
                if extent is None:
                    continue
                end, closed, content = extent
                if (
                    closed
                    and low in (original, content)
                    and end == high
                    and end <= owners[index].end
                    and _RECOVERY_TAIL.match(text, end)
                ):
                    return True
            return False

        # Both publication paths can manufacture a private suffix after quote
        # removal. Withhold only the exact proven outer literal; independent
        # name/value snippets, siblings and legacy private roles stay intact.
        normal = [m for m in normal if not literal_coarse_member(m.name, m.start, m.end)]
    public = {(member.name, member.start) for member in public_members}
    selected = [
        (name, low, min(high, node.end), value)
        for node in nodes
        for name, low, high, value in node.members
        if (name, low) not in public
        and (not literal_fields or not literal_coarse_member(name, low, min(high, node.end)))
        and (
            node.mask & 1
            if name in _RSA_ONLY
            else node.mask & 4
            if name == "k"
            else node.mask
            if name in _PRIVATE
            else 0
        )
    ]
    if supplement_members:
        private_candidates = [
            (name, low, index)
            for name, low, index in supplement_members
            if supplement_masks.get(nodes[index].start, 0)
            & (1 if name in _RSA_ONLY else 4 if name == "k" else 7)
        ]
        if private_candidates:
            ends = _expression_ends(
                text, sorted({low for _, low, _ in private_candidates}), javascript=True
            )
            selected.extend(
                (name, low, min(ends[low], nodes[index].end), None)
                for name, low, index in private_candidates
            )
    selected.sort(key=lambda member: (member[1], -member[2]))
    # Structural ownership has been consumed. Release it before materializing
    # private values and decoded candidates; retaining both exceeded the budget.
    if literal_fields:
        del literal_coarse_member
    del legacy_masks, literal_fields, proof_index, value_proofs, legacy_hints
    del supplement_members, supplement_masks, accepted_private_unpacks, blocked_private_owners
    del normal_private_unpacks, normal_private_opaque, projected_private_unpacks
    del nodes, stack, by_start, hints, projection, code_view, blanked, recovered_quotes
    del uncertain, calls, computed_calls, inherited, normal_edges, normal_uncertain, normal_unsafe
    found = []
    index, covered = 0, -1
    for name, low, high, value in selected:
        if low < covered:
            low, value = covered, None
        if high <= low:
            continue
        covered = high
        while index < len(comments) and comments[index + 1] <= low:
            index += 2
        scan = index
        while scan < len(comments) and comments[scan] < high:
            begin, end = max(low, comments[scan]), min(high, comments[scan + 1])
            if end > begin:
                whole = begin == low and end == high and value is not None
                found.append(
                    PrivateMember(
                        name, value if whole else text[begin:end], begin, end, inline=not whole
                    )
                )
            scan += 2
    merged = {(member.name, member.start, member.end): member for member in found}
    for member in _comment_fenced(normal, comments, text):
        key = member.name, member.start, member.end
        prior = merged.get(key)
        if prior is None:
            merged[key] = member
        elif prior.audit_span is None and member.audit_span is not None:
            merged[key] = replace(prior, audit_span=member.audit_span)
    found = list(merged.values())
    if snippets is not None:
        originals = _comment_literal_snippets(text, comments, source, depth)
        original_spans = {child.span for child in originals}
        snippets.extend(originals)
        snippets.extend(
            replace(
                child,
                owns_source=False,
                regions=comments,
                depth=depth if child.depth is None else child.depth,
            )
            for child in normal_children
            if child.span not in original_spans
        )
    return found


def _unpacked_parents(
    text: str,
    call_operands: set[int] | None = None,
    *,
    uncertain_parents: set[int] | None = None,
    computed_calls: dict[int, int] | None = None,
    call_parents: dict[int, int] | None = None,
    statement_literals: dict[int, tuple[int, int, int]] | None = None,
    private_effects: dict[int, tuple[int, int, int]] | None = None,
    private_opaque: list[tuple[int, int]] | None = None,
) -> dict[int, int]:
    """Complete literal operand brace -> its owner, in one forward pass.

    Both sides of the operand matter: binary power is not unpacking, and a
    literal followed by an operator, attribute, index or call is only a prefix
    of a computed operand. Such an unpack cannot establish a definite type;
    its owner must conservatively protect every potentially private member.
    """
    if "**" not in text:
        return {}
    parents: dict[int, int] = {}
    # Candidate: literal start, owner start, owner stack depth, owner kind.
    candidates: dict[int, tuple[int, int, int, int]] = {}
    awaiting: tuple[int, int, int, int] | None = None
    stack = array("q")
    counts = [0, 0, 0]
    parameters: set[int] = set()
    lambda_headers = array("q")  # nested headers may share a bracket depth
    definition_depth = definition_stage = -1
    private_offsets: dict[int, int] | None = {} if private_effects is not None else None

    def complete(candidate: tuple[int, int, int, int]) -> None:
        literal, owner, _, kind = candidate
        if private_effects is not None:
            assert private_offsets is not None
            private_effects[private_offsets.pop(id(candidate))] = (owner, literal, kind)
        if kind == 2:
            parents[literal] = owner
        elif kind == 0:
            if call_operands is not None:
                call_operands.add(literal)
            if call_parents is not None:
                call_parents[literal] = owner

    def uncertain(candidate: tuple[int, int, int, int]) -> None:
        literal, owner, _, kind = candidate
        if private_effects is not None:
            assert private_offsets is not None
            private_effects[private_offsets.pop(id(candidate))] = (owner, -1, kind)
        if kind == 2 and uncertain_parents is not None:
            uncertain_parents.add(owner)
        elif kind == 0 and computed_calls is not None:
            computed_calls[owner] = len(text)

    position = 0
    previous = ""
    while token := _UNPACK_CONTEXT.search(text, position):
        position = token.end()
        kind = token.lastgroup
        if kind in ("line_comment", "comment", "continuation"):
            if private_opaque is not None and kind != "continuation":
                private_opaque.append(token.span())
            continue
        raw = token.group()
        javascript = (
            (statement_literals or {}).get(token.start())
            or _javascript_literal(text, token.start())
            if kind == "javascript"
            else None
        )
        if javascript is not None:
            position = javascript[0]
        if raw == "lambda" and kind == "atom":
            lambda_headers.append(len(stack))
        elif raw == ":" and lambda_headers and lambda_headers[-1] == len(stack):
            lambda_headers.pop()
        if raw == "def" and kind == "atom":
            definition_depth, definition_stage = len(stack), 0
        elif definition_stage == 0:
            definition_stage = 1 if kind == "atom" and raw.isidentifier() else -1
        elif definition_stage == 1:
            if raw == "(" and len(stack) == definition_depth:
                parameters.add(token.start())
                definition_stage = -1
            elif raw == "[" and len(stack) == definition_depth:
                definition_stage = 2  # Python's optional type-parameter list
            else:
                definition_stage = -1
        elif definition_stage == 2 and raw == "]" and len(stack) == definition_depth + 1:
            definition_stage = 1
        if awaiting is not None:
            if len(stack) > awaiting[2] and raw == ")" and stack[-1] & 3 == 0:
                pass  # only the original operand's wrapping parentheses remain
            elif len(stack) == awaiting[2] and raw in (",", "}" if awaiting[3] == 2 else ")"):
                complete(awaiting)
                awaiting = None
            else:
                uncertain(awaiting)
                awaiting = None
        if javascript is not None:
            if private_opaque is not None:
                private_opaque.append((token.start(), position))
            previous = '"'
            continue
        if kind in ("single", "double", "triple"):
            prefix = token.group("prefix") or ""
            start = token.start() + len(prefix)
            delimiter = text[start] * (3 if text.startswith(text[start] * 3, start) else 1)
            if "f" in prefix.lower() or "t" in prefix.lower():
                position, _ = oct_blocks._formatted_end(text, start, delimiter)
            if private_opaque is not None:
                private_opaque.append((token.start(), position))
            previous = delimiter[-1]
            continue
        if kind == "operator" and (not previous or previous in "({,"):
            match = oct_blocks._UNPACKED_OBJECT.match(text, token.start())
            if (
                stack
                and stack[-1] & 3 in (0, 2)
                and stack[-1] >> 2 not in parameters
                and not (lambda_headers and lambda_headers[-1] == len(stack))
            ):
                owner = stack[-1] >> 2
                candidate = (match.end() if match else -1, owner, len(stack), stack[-1] & 3)
                if private_offsets is not None:
                    private_offsets[id(candidate)] = token.start()
                if match:
                    candidates[match.end()] = candidate
                else:
                    uncertain(candidate)
        raw = text[position - 1]
        previous = raw
        if raw not in oct_blocks._BRACKET_TYPE:
            continue
        kind = oct_blocks._BRACKET_TYPE[raw]
        if raw in "([{":
            stack.append(token.start() << 2 | kind)
            counts[kind] += 1
        elif counts[kind]:
            while True:
                entry = stack.pop()
                counts[entry & 3] -= 1
                if computed_calls is not None and entry >> 2 in computed_calls:
                    computed_calls[entry >> 2] = token.start()
                parameters.discard(entry >> 2)
                while lambda_headers and lambda_headers[-1] > len(stack):
                    lambda_headers.pop()
                candidate = candidates.pop(entry >> 2, None)
                if candidate is not None:
                    if entry & 3 == kind:
                        awaiting = candidate
                    else:
                        uncertain(candidate)
                if entry & 3 == kind:
                    break
    if awaiting is not None:
        if len(stack) == awaiting[2]:
            complete(awaiting)  # an excerpt may end after a complete operand
        else:
            uncertain(awaiting)
    for candidate in candidates.values():
        uncertain(candidate)
    return parents


def _call_value_spans(
    text: str,
    calls: list[tuple[int, int]],
    decided: list[tuple[int, int]],
    statement_literals: dict[int, tuple[int, int, int]] | None = None,
) -> list[tuple[int, int]]:
    """Mask complete uncertain argument values, retaining call/unpack syntax."""
    objects = dict(decided)
    result: list[tuple[int, int]] = []

    def literal_object(low: int, high: int) -> bool:
        position, wrappers, seen = low, 0, False
        while token := _UNPACK_CONTEXT.search(text, position, high):
            position = token.end()
            if token.lastgroup in ("line_comment", "comment", "continuation"):
                continue
            raw = token.group()
            if not seen and raw == "(":
                wrappers += 1
            elif not seen and raw == "{" and token.start() in objects:
                position, seen = objects[token.start()], True
                if position > high:
                    return False
            elif not seen and raw == "{":
                while following := _UNPACK_CONTEXT.search(text, position, high):
                    position = following.end()
                    if following.lastgroup not in ("line_comment", "comment", "continuation"):
                        break
                if following is None or following.group() != "}":
                    return False
                seen = True
            elif seen and raw == ")" and wrappers:
                wrappers -= 1
            else:
                return False
        return seen and not wrappers

    def plain_type(low: int, high: int) -> bool:
        token = _UNPACK_CONTEXT.match(text, low, high)
        if token is None or token.end() != high or token.lastgroup not in ("single", "double"):
            return False
        prefix = token.group("prefix") or ""
        return (
            not any(char in prefix.lower() for char in "ft")
            and _string(text[low + len(prefix) : high]) in _KEY_TYPES
        )

    for call_low, call_high in calls:
        position, depth = call_low, 0
        headers = array("q")
        low = high = -1
        keyword = candidate = None

        def finish(low: int, high: int, keyword: str | None) -> None:
            if (
                low >= 0
                and high > low
                and not ((keyword == "kty" and plain_type(low, high)) or literal_object(low, high))
            ):
                result.append((low, high))

        while token := _UNPACK_CONTEXT.search(text, position, call_high):
            position = token.end()
            kind, raw = token.lastgroup, token.group()
            if kind in ("line_comment", "comment", "continuation"):
                continue
            if raw == "," and not depth and not headers:
                finish(low, high, keyword)
                low = high = -1
                keyword = candidate = None
                continue
            if low < 0:
                if raw in ("*", "**"):
                    continue
                low = token.start()
                candidate = raw if kind == "atom" and raw.isidentifier() else None
            elif candidate is not None:
                if not depth and raw == "=" and not text.startswith("==", token.start()):
                    keyword, candidate = candidate, None
                    low = high = -1
                    continue
                candidate = None
            if kind == "javascript" and (
                literal := (statement_literals or {}).get(token.start())
                or _javascript_literal(text, token.start())
            ):
                position = literal[0]
            elif kind in ("single", "double", "triple"):
                prefix = token.group("prefix") or ""
                start = token.start() + len(prefix)
                delimiter = text[start] * (3 if text.startswith(text[start] * 3, start) else 1)
                if "f" in prefix.lower() or "t" in prefix.lower():
                    position, _ = oct_blocks._formatted_end(text, start, delimiter)
            elif raw == "lambda":
                headers.append(depth)
            elif raw == ":" and headers and headers[-1] == depth:
                headers.pop()
            elif raw in "([{":
                depth += 1
            elif raw in ")]}":
                depth = max(0, depth - 1)
                while headers and headers[-1] > depth:
                    headers.pop()
            high = position
        finish(low, high, keyword)
    return result


_PRIVATE = frozenset({"d", "p", "q", "dp", "dq", "qi", "k"})
_KEY_TYPES = frozenset({"RSA", "EC", "OKP", "oct"})
_TYPE_MASK = {"RSA": 1, "EC": 2, "OKP": 2, "oct": 4, "*": 7}
# Several different types in one object (YAML keeps the last; JSON parsers
# differ): the union of their private sets (task #2200). "*" is every set.
_ANY_TYPE = "*"


def _union_type(previous: str | None, current: str) -> str:
    """The type whose private set covers both readings of a repeated ``kty``."""
    if previous is None or previous == current:
        return current
    known = {kind for kind in (previous, current) if kind in _KEY_TYPES or kind == _ANY_TYPE}
    if not known:
        return current  # neither spelling is a key type: nothing is private
    if len(known) == 1:
        return known.pop()
    if _ANY_TYPE in known or "oct" in known:
        return _ANY_TYPE
    return "RSA" if "RSA" in known else "EC"  # EC and OKP share one private set


# Private only in an RSA key (RFC 7518 6.3.2): elsewhere they are ordinary names.
_RSA_ONLY = frozenset({"p", "q", "dp", "dq", "qi", "oth"})
# Member names a YAML reading may supply (task #2126).
_YAML_NAMES = _PRIVATE | {"kty", "oth"}
# An "oth" entry's own private members (RFC 7518 6.3.2.7): r, d and t. Any other
# name in an entry is not key material (task #2317). Every member used to count,
# so an "oth" array cut off in an excerpt (it never closes) made each later
# object an entry: the fields of every log record after it (`type`, `id`,
# `turn_id`) were reported as private. Other private names stay masked there.
_OTH_ENTRY_PRIVATE = frozenset({"r", "d", "t"})
_OTH_ENTRY_NAMES = _YAML_NAMES | _OTH_ENTRY_PRIVATE
# A YAML flow type read after the separator (task #2200): first blanks, line
# breaks, comments (a '#' after a blank) and node properties: verbatim tags
# (!<tag:yaml.org,2002:str>, commas included), tags and anchors. Each step
# consumes characters; properties are bounded (a longer one, or an unfinished
# verbatim tag, is read as a type: failing toward masking), and a comment
# already skipped for an earlier type in the same text is jumped, not rescanned.
_TYPE_STEP = re.compile(
    rf"[ \t{oct_blocks._LINE_CHARS}]++|!<[^>\s]{{0,256}}+>|[&!][^\s,\[\]{{}}<]{{0,256}}+(?![^\s,\[\]{{}}])"
)
_LONG_PROPERTY = re.compile(r"[&!]")
_TO_LINE_END = re.compile(rf"[^{oct_blocks._LINE_CHARS}]*+")
_PLAIN_TYPE = re.compile(
    r"(?P<type>RSA|EC|OKP|oct)"
    rf"(?=(?:[ \t{oct_blocks._LINE_CHARS}]++"
    rf"|(?<=[ \t{oct_blocks._LINE_CHARS}])\#[^{oct_blocks._LINE_CHARS}]*+)*+"
    r"(?:[,}\]]|\Z))"
)
_QUOTED_TYPE = re.compile(_STRING)


def _type_gap(text: str, position: int, comments: list[int]) -> int:
    """The end of the blanks, comments and properties before a flow type.

    ``comments`` holds the last comment skipped in this text (start, end), so
    types inside one comment never rescan it: each comment is read once.
    """
    while True:
        step = _TYPE_STEP.match(text, position)
        if step is not None and step.end() > position:
            position = step.end()
            continue
        if text.startswith("#", position) and (
            position == 0 or text[position - 1] in " \t" or text[position - 1] in _LINE_BREAKS
        ):
            if comments[0] <= position < comments[1]:
                position = comments[1]
            else:
                end = _TO_LINE_END.match(text, position).end()
                comments[0], comments[1] = position, end
                position = end
            continue
        return position


_LINE_BREAKS = frozenset("\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029")


def _flow_type(
    text: str, position: int, comments: list[int] | None = None
) -> tuple[str | None, str | None] | None:
    """The type a YAML flow value spells: (plain type, None) or (None, quoted raw).

    An alias (``*t``) stands for any key type: it reads as RSA, whose private
    set covers every asymmetric private member (an alias to an ``oct`` value
    is the oct-block rule's). A quoted type after properties, comments or
    breaks is returned raw, to be read and decided like any quoted type.
    """
    position = _type_gap(text, position, comments if comments is not None else [-1, -1])
    if text.startswith("*", position) or _LONG_PROPERTY.match(text, position):
        # An alias, or a property too long to read: any asymmetric type.
        return "RSA", None
    if text.startswith("${", position):
        # A JavaScript template interpolation: the type is unknown.
        return _ANY_TYPE, None
    if text.startswith(('"', "'"), position):
        quoted = _QUOTED_TYPE.match(text, position)
        if quoted is not None and _VALUE_TAIL.match(text, quoted.end()):
            return None, quoted.group()
        return None
    plain = _PLAIN_TYPE.match(text, position)
    return (plain["type"], None) if plain is not None else None


def _recovery_spelling(word: str) -> str:
    letters = []
    for char in word:
        named = "|".join(re.escape(unicodedata.name(c)) for c in (char.lower(), char.upper()))
        octal = "|".join(format(ord(c), "o") for c in (char.lower(), char.upper()))
        ordinary = oct_blocks._letter(char).replace(r"\+", r"\++")
        letters.append(rf"(?:{ordinary}|\\++(?:{octal}|N\{{(?i:{named})\}}))")
    return oct_blocks._CONTINUATION.join(letters)


# A line continuation may sit at any position of a quoted key or type value,
# including directly after its opening or before its closing quote (#2169).
# A leading run is read only after an actual opening quote: a bare name cannot
# start with a continuation, and an unanchored leading run would let a search
# rescan the rest of a long run from every one of its lines (quadratic).
_RECOVERY_FIELD = re.compile(
    r"(?<![\w.$\\])(?:\\*+['\"]"
    + oct_blocks._CONTINUATION
    + ")?(?:"
    + "|".join(
        rf"(?P<{name}>{_recovery_spelling(name)})"
        for name in sorted(_PRIVATE | {"kty", "oth", "r", "t"}, key=lambda name: (-len(name), name))
    )
    + rf"){oct_blocks._CONTINUATION}(?:\\*+['\"])?{oct_blocks._CONTINUATION}\s*+"
    + rf"(?:{oct_blocks._COLON}|{oct_blocks._EQUALS}(?!=))\s*+"
)
_RECOVERY_BASE_TOKEN = re.compile(
    _RECOVERY_FIELD.pattern
    + rf"|(?P<bracket>[{{}}()\[\]])|(?P<unpack>\*\*)|(?P<hash>\#++)|(?P<quote>(?:{oct_blocks._PYTHON_PREFIX})?[\"'])"
)
_RECOVERY_TYPE_START = re.compile(rf"(?:{oct_blocks._PYTHON_PREFIX}(?=\\*+['\"]))?")
_RECOVERY_TYPES = {
    kind: re.compile(rf"{_recovery_spelling(kind)}{oct_blocks._CONTINUATION}(?![\w$])")
    for kind in _KEY_TYPES
}

_RECOVERY_TYPE_OPEN = re.compile(
    rf"(?:{oct_blocks._PYTHON_PREFIX})?(?P<quote>\\*+(?:\"{{3}}|'{{3}}|['\"]))"
)
_RECOVERY_VALUE_SEPARATOR = r"[,#}\])]"
_RECOVERY_QUOTED_TYPES = {
    kind: re.compile(
        rf"{_RECOVERY_TYPE_OPEN.pattern}{oct_blocks._CONTINUATION}{_recovery_spelling(kind)}"
        rf"{oct_blocks._CONTINUATION}(?P=quote)"
        rf"{oct_blocks._CONTINUATION}(?=\s|{_RECOVERY_VALUE_SEPARATOR}|\Z)"
    )
    for kind in _KEY_TYPES
}
_RECOVERY_LITERAL = re.compile(oct_blocks._STRING_LITERALS)
_RECOVERY_TAIL = re.compile(rf"\s*+(?:{_RECOVERY_VALUE_SEPARATOR}|\Z)")
_RECOVERY_SEPARATOR = re.compile(
    rf"{oct_blocks._CONTINUATION}\s*+(?:{oct_blocks._COLON}|{oct_blocks._EQUALS}(?!=))\s*+"
)
_RECOVERY_QUOTES = re.compile(r"""\\++|['"]""")


def _recovery_field_proofs(text: str) -> dict[int, tuple[int, int]]:
    """Index adjacent physical quote intervals; escaped quotes never reopen.

    Each maximal slash run/quote is visited once. Name intervals are disjoint
    for each delimiter (two fixed views), even when BASE later blanks quotes.
    This index confers no field, type, public or unpack authority.
    """
    previous: dict[str, int | None] = {"'": None, '"': None}
    slash_end, odd = -1, False
    result = {}
    for token in _RECOVERY_QUOTES.finditer(text):
        raw = token.group()
        if raw[0] == "\\":
            slash_end, odd = token.end(), len(raw) % 2 == 1
            continue
        at = token.start()
        if at == slash_end and odd:
            continue
        adjacent = (at > 0 and text[at - 1] == raw) or (at + 1 < len(text) and text[at + 1] == raw)
        opening = previous[raw]
        if not adjacent and opening is not None:
            separator = _RECOVERY_SEPARATOR.match(text, at + 1)
            if separator is not None:
                result[opening] = at, separator.end()
        previous[raw] = at if not adjacent and (at == 0 or text[at - 1] != "\\") else None
    return result


def _recovery_nonprivate_name(text: str, start: int, quote: int, end: int) -> bool:
    """Require constant local name semantics in both lexical interpretations."""
    if start and (text[start - 1] in ".\\" or _javascript_identifier_part(text[start - 1])):
        return False
    prefix = text[start:quote].lower()
    if prefix and prefix not in oct_blocks._PYTHON_PREFIXES:
        return False
    if "f" in prefix or "t" in prefix:
        return False  # a dynamically formatted name is not a constant proof
    raw = text[start : end + 1]
    if "b" in prefix or "r" in prefix:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                warnings.simplefilter("ignore", DeprecationWarning)
                node = ast.parse(raw, mode="eval").body
            if not isinstance(node, ast.Constant) or not isinstance(node.value, (str, bytes)):
                return False
            value = (
                node.value.decode("utf-8", "replace")
                if isinstance(node.value, bytes)
                else node.value
            )
        except (SyntaxError, ValueError):
            return False
        return value not in _JAVASCRIPT_RECOVERY_NAMES
    # This extra JS view only vetoes unsafe withholding; ordinary token
    # interpretation still requires its own genuine braced evidence.
    return (
        _string(raw) not in _JAVASCRIPT_RECOVERY_NAMES
        and _javascript_logical_string(raw) not in _JAVASCRIPT_RECOVERY_NAMES
    )


def _recovery_value_extent(text: str, low: int) -> tuple[int, bool, int] | None:
    """Read one original value owner once, never per publication/ancestor."""
    literal = _RECOVERY_LITERAL.match(text, low)
    if literal is None:
        return None
    prefix = (literal["prefix"] or "").lower()
    quote = low + len(prefix)
    delimiter = text[quote] * (3 if literal.lastgroup == "triple" else 1)
    if "f" in prefix or "t" in prefix:
        end, closed = oct_blocks._formatted_end(text, quote, delimiter)
    else:
        end = literal.end()
        _, _, high = _literal_bounds(text[low:end])
        closed = high < end - low
    return end, closed, quote + len(delimiter)


def _javascript_recovery_spelling(word: str) -> str:
    # A bounded logical-word PREFIX prefilter, never a scan for the closing
    # quote of an arbitrary string. The actual token decoder validates parity
    # and case. Each word has at most three letters and two continuation gaps.
    letters = []
    for char in word:
        codes = "|".join(format(ord(c), "x") for c in (char.lower(), char.upper()))
        identity = rf"|\\++{re.escape(char)}" if char not in "bfnrtvxu" else ""
        letters.append(rf"(?:{_recovery_spelling(char)}|\\++u\{{0*+(?i:{codes})\}}{identity})")
    return oct_blocks._CONTINUATION.join(letters)


_JAVASCRIPT_RECOVERY_NAMES = _PRIVATE | {"kty", "oth", "r", "t"}
_JAVASCRIPT_RECOVERY_FIELD = (
    rf"(?<![\w.$\\])(?P<javascript_field>(?:{_PREFIX})?(?P<jsquote>['\"])"
    + oct_blocks._CONTINUATION
    + "(?:"
    + "|".join(
        _javascript_recovery_spelling(name)
        for name in sorted(_JAVASCRIPT_RECOVERY_NAMES, key=lambda name: (-len(name), name))
    )
    + rf"){oct_blocks._CONTINUATION}(?P=jsquote)){oct_blocks._CONTINUATION}\s*+"
    + rf"(?:{oct_blocks._COLON}|{oct_blocks._EQUALS}(?!=))\s*+"
)
_RECOVERY_TOKEN = re.compile(_JAVASCRIPT_RECOVERY_FIELD + "|" + _RECOVERY_BASE_TOKEN.pattern)
_JAVASCRIPT_RECOVERY_TYPE = re.compile(
    rf"(?P<literal>(?:{_PREFIX})?(?P<delimiter>['\"]){oct_blocks._CONTINUATION}(?:"
    + "|".join(_javascript_recovery_spelling(kind) for kind in sorted(_KEY_TYPES))
    + rf"){oct_blocks._CONTINUATION}(?P=delimiter)){oct_blocks._CONTINUATION}"
    + rf"(?=\s|{_RECOVERY_VALUE_SEPARATOR}|\Z)"
)
_JAVASCRIPT_BARE_TYPE = re.compile(
    rf"(?<![\w.$\\'\"])(?P<type_name>{_javascript_recovery_spelling('kty')})\s*+:\s*+"
    + _JAVASCRIPT_RECOVERY_TYPE.pattern
)
_JAVASCRIPT_PRIVATE_STRING = re.compile(
    rf"(?P<private_quote>['\"]){oct_blocks._CONTINUATION}(?:"
    + "|".join(_javascript_recovery_spelling(name) for name in sorted(_PRIVATE | {"oth"}))
    + rf"){oct_blocks._CONTINUATION}(?P=private_quote)\Z"
)

# A quoted private key name in any JS spelling (identity or braced escapes,
# continuations), followed by its separator. Only a prefilter: the effects pass
# decides ownership and roles (tasks #2227, #2239, #2245).
_ESCAPED_PRIVATE_FIELD = re.compile(
    rf"(?<![\w.$\\])(?P<quote>['\"]){oct_blocks._CONTINUATION}(?:"
    + "|".join(_javascript_recovery_spelling(name) for name in sorted(_PRIVATE | {"oth"}))
    + rf"){oct_blocks._CONTINUATION}(?P=quote){oct_blocks._CONTINUATION}\s*+:"
)


def _escaped_private_name(text: str, start: int, raw: str) -> str | None:
    """A quoted, unprefixed key that only escape decoding makes private."""
    if raw[:1] not in ("'", '"') or "\\" not in raw or not _javascript_unprefixed(text, start):
        return None
    if _JAVASCRIPT_PRIVATE_STRING.fullmatch(raw) is None:
        return None
    logical = _javascript_logical_string(raw)
    return logical if logical in _PRIVATE or logical == "oth" else None


def _identity_members(
    text: str, regions: array | None, *, evidence: str, recovery: bool
) -> list[PrivateMember]:
    """Private members whose key is private only under JS escape rules.

    ``"\\d\\p"`` is dp in JS (identity escapes) but stays ``\\d\\p`` in Python.
    The ordinary scans keep the Python meaning unless the key's own type has
    braced evidence. This narrow pass applies the JS meaning where Python
    source meaning cannot hold (tasks #2227, #2239, #2245): comment prose
    (``evidence="any"``, read with orphan-quote recovery) and live source whose
    type member is spelled across a line continuation (``"continued"``). The
    object's last discriminator, spreads and accessors decide the role exactly
    as for braced evidence, so a name private only for RSA stays public in an
    EC key. One linear effects pass plus one expression-end pass; nothing runs
    unless an escaped private key is present. Members are fenced to
    ``regions`` (comment bodies) when given.
    """
    if not any("\\" in match.group() for match in _ESCAPED_PRIVATE_FIELD.finditer(text)):
        return []
    fields: list[tuple[str, int, int]] = []
    masks = _javascript_private_effects(
        text, {}, recovery=recovery, evidence=evidence, private_fields=fields
    )
    chosen = [
        (name, at)
        for name, at, owner in fields
        if masks.get(owner, 0) & (1 if name in _RSA_ONLY else 4 if name == "k" else 7)
    ]
    if not chosen:
        return []
    # Spans first, values last: a nested private value holds its inner private
    # members, so copying each one would copy the shared tail once per level.
    spans: list[tuple[int, int, str, str | None]] = []
    pending = []
    for name, at in chosen:
        literal = _RECOVERY_LITERAL.match(text, at)
        if literal is not None and not (literal["prefix"] or ""):
            raw = literal.group()
            _, low, high = _literal_bounds(raw)
            if high < len(raw) and _RECOVERY_TAIL.match(text, literal.end()):
                spans.append((at, literal.end(), name, raw))
                continue
        pending.append((name, at))
    if pending:
        ends = _expression_ends(text, sorted({at for _, at in pending}), javascript=True)
        trimmed = _trimmed_ends(text, ends.values())
        for name, at in pending:
            end = max(at, trimmed[ends[at]])
            if end > at:
                spans.append((at, end, name, None))
    spans.sort(key=lambda span: (span[0], -span[1]))
    members = []
    covered = -1
    for at, end, name, raw in spans:
        if end <= covered:
            continue  # inside a kept value, which already replaces it whole
        covered = end
        value = text[at:end] if raw is None else _javascript_logical_string(raw)
        members.append(PrivateMember(name, value, at, end))
    return _comment_fenced(members, regions, text) if regions is not None else members


def _trimmed_ends(text: str, ends: Iterable[int]) -> dict[int, int]:
    """Each end with its trailing whitespace dropped, in one backward pass.

    Ends are visited in order and a walk stops at the previous end, reusing its
    result, so many values ending in one whitespace run scan it once.
    """
    trimmed: dict[int, int] = {}
    previous = -1
    for end in sorted(set(ends)):
        low = end
        while low > previous and low > 0 and text[low - 1].isspace():
            low -= 1
        trimmed[end] = trimmed[previous] if low == previous else low
        previous = end
    return trimmed


_HINT_SPELLED = oct_blocks._yaml_spelled("kty").replace(r"\+", r"\++")
_KTY_HINT = re.compile(rf"(?i:kty)|(?<!\\){_HINT_SPELLED}")
_HINT_ESCAPE = re.compile(
    r"(?<!\\)\\++(?:(?P<hex>u\{[0-9a-fA-F]++\}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|x[0-9a-fA-F]{2})"
    r"|(?P<oct>[0-7]{1,3})|N\{(?P<named>[^{}\r\n]{1,128}+)\}"
    r"|(?P<simple>[abfnrtv])|(?P<line>\r\n|[\r\n\u2028\u2029])[ \t]*+"
    r"|(?P<identity>[\s\S]))"
)
_HINT_SIMPLE = dict(zip("abfnrtv", "\a\b\f\n\r\t\v", strict=True))


def _hint_escape(match: re.Match[str]) -> str:
    try:
        if match["hex"] is not None:
            digits = match["hex"][1:].strip("{}").lstrip("0") or "0"
            value = chr(int(digits, 16)) if len(digits) <= 8 else match.group()
            if value in "\r\n\u2028\u2029" and match.group().startswith("\\\\"):
                return "\\" + value
            return value
        if match["oct"] is not None:
            return chr(int(match["oct"], 8))
        if match["named"] is not None:
            return unicodedata.lookup(match["named"])
        if match["simple"] is not None:
            value = _HINT_SIMPLE[match["simple"]]
            if value in "\r\n" and match.group().startswith("\\\\"):
                return "\\" + value
            return value
        if match["identity"] is not None:
            return match["identity"]
        return ""  # escaped physical continuation
    except (ValueError, KeyError):
        return match.group()


# A JavaScript template interpolation ending a key token: k${part}y: or ${name}:
_INTERPOLATED_NAME = re.compile(r"\$\{[^{}`]{0,256}\}[\w$]*+[\"']?[ \t]*+:")


def _may_have_kty(text: str) -> bool:
    """Conservative word evidence, without recursively scanning every escape.

    Canonical escaped/continued spellings are recognized directly. A bounded
    normalization also handles encoded backslashes, octal and named Unicode
    escapes. An unresolved deeper encoding stays eligible, never rejected.
    Each pass is linear; no JSON, source-map or quote scan is launched here.
    """
    if "${" in text and _INTERPOLATED_NAME.search(text):
        return True  # a template key built at runtime may spell the type
    for _ in range(8):
        if _KTY_HINT.search(text):
            return True
        if "\\" not in text:
            return False
        decoded = _HINT_ESCAPE.sub(_hint_escape, text)
        if decoded == text:
            return False
        text = decoded
    return True


def _escaped_name(word: str) -> str:
    return "".join(oct_blocks._letter(char) for char in word)


# A member name past the decode bound (task #2124, review at 79723aa6): the
# name in any case or escape form, its closing quote at any escaping depth,
# then a separator (literal or escaped) or a line break (real or encoded at any
# depth, or a comment), which ends an explicit YAML key (``? "kty"`` / ``: oct``).
_UNDECODED_END = (
    rf"(?![\w$])(?:\\*+[\"'`])?[ \t]*+"
    rf"(?:{oct_blocks._COLON}|{oct_blocks._EQUALS}|[{oct_blocks._LINE_CHARS}#]"
    r"|\\++(?:[nr]|u000[aAdD]|x0[aAdD]))"
)
_UNDECODED_TYPE = re.compile(oct_blocks._KTY_BOUNDARY + _escaped_name("kty") + _UNDECODED_END)
_UNDECODED_PRIVATE = re.compile(
    oct_blocks._KTY_BOUNDARY
    + "(?P<name>"
    + "|".join(_escaped_name(name) for name in ("oth", "dp", "dq", "qi", "k", "d", "p", "q"))
    + ")"
    + _UNDECODED_END
)


def _string_regions(
    matches: Iterator[re.Match[str]], quoted: list[tuple[int, int]]
) -> Iterator[tuple[int, re.Match[str]]]:
    """Each name match with the top-level string body it belongs to (-1: none).

    A name whose body ends inside the match (its separator lies outside the
    closing quote) is a key of the enclosing text, as in ``_child_strings``.
    Matches and bodies are both in order: one cursor, a linear walk.
    """
    index = 0
    for match in matches:
        start, end = match.span()
        while index < len(quoted) and quoted[index][1] <= start:
            index += 1
        inside = index < len(quoted) and quoted[index][0] <= start
        yield (index if inside and quoted[index][1] >= end else -1), match


def _owned_quote_regions(text: str, quoted: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Only complete, validated quoted text can prove child ownership.

    Quote-context spans include unfinished and mispaired excerpts. Validate
    each disjoint quoted-text reading once, without evaluating expressions or
    decoding another document level. Formatted extents keep the streaming
    quote walker's ownership (no expression ASTs). If the reading of any other
    literal fails, ownership stays unproved. Total inspected source is bounded
    by the context length.
    """
    owned = []
    for body in quoted:
        extent = oct_blocks._literal_extent(text, body)
        if extent is None:
            continue
        start, opening, end = extent
        if _MISPAIRED[text[opening]].match(text, end):
            continue
        raw = text[start:end]
        prefix = text[start:opening].lower()
        if "f" in prefix or "t" in prefix:
            # Formatted extents come from the streaming quote walker, which
            # already tracks replacement fields (PEP 701 quote reuse,
            # multi-line expressions, nested literals). An ordinary-literal
            # re-reading rejects valid ones and so lets a child's type name
            # retype its public parent (review r1 at 7e6e4eb2), so they keep
            # the walker's ownership, as on main. The walker does not prove
            # a formatted literal valid (#2248).
            owned.append(body)
            continue
        if oct_blocks._STRING_CONTEXT.fullmatch(raw) is None:
            continue  # no expression ASTs
        if raw.startswith('"'):
            try:
                json.loads(raw)
            except ValueError:
                pass
            else:
                owned.append(body)
                continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                warnings.simplefilter("ignore", DeprecationWarning)
                node = ast.parse(raw, mode="eval").body
        except (SyntaxError, ValueError, RecursionError):
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            owned.append(body)
    return owned


# Escape layers past the decode bound, flattened without decoding JSON
# (reviews at 875394f0, 5a5b916b and 454a2038): any run of backslashes is
# transparent; a following \u, \u{...} (up to 16 digits, leading zeroes
# included), \U, \x, octal or \N{NAME} code is replaced by its character; a
# break escape (\r\n, \r or \n, each possibly behind its own run) by one line
# break; \b, \f, \v and \a by a blank, \/ by a slash; an escaped physical break
# and its indentation (a YAML continuation) by nothing; a quote by nothing. A
# decoded backslash (\u005c, \x5c, \134) starts the next round. Any other
# escape is uncertain. Documents holding one, or still holding a backslash
# after the last round, fail closed. Complete field literals can distinguish
# an ordinary data backslash from an invalid character code/name below.
_FLAT_ESCAPE = re.compile(
    rf"\\++(?:(?P<line>(?:\r\n|[{oct_blocks._LINE_CHARS}])[ \t]*+)"
    r"|u\{(?P<braced>[0-9a-fA-F]{1,16})\}|u(?P<u>[0-9a-fA-F]{4})"
    r"|U(?P<big>[0-9a-fA-F]{8})|x(?P<x>[0-9a-fA-F]{2})"
    r"|N\{(?P<named>[A-Za-z0-9 \-]{1,88})\}"
    r"|(?P<octal>[0-7]{1,3})|(?P<brk>r(?:\\++n)?|n)|(?P<tab>t)|(?P<blank>[bfva])"
    r"|(?P<slash>/)|(?P<other>[\s\S]))?"
    r"|[\"'`]"
)
_FLAT_ROUNDS = 8
_FLAT_BREAKS = re.compile(rf"\r\n?|[{oct_blocks._LINE_CHARS}]")
# A name in the flattened text: letters in any case, perhaps split by line
# breaks and indentation (a continuation whose backslash was itself escaped),
# followed by a separator, a comment, or a line end (an explicit YAML key).
_FLAT_GAP = r"(?:\n[ \t]*+)*+"
_FLAT_END = r"(?![\w$])[ \t]*+(?:[:=\#\n])"


def _flat_name(word: str) -> str:
    return _FLAT_GAP.join(re.escape(char) for char in word)


_FLAT_TYPE = re.compile(r"(?i)(?<![\w$.])" + _flat_name("kty") + _FLAT_END)
_FLAT_PRIVATE = re.compile(
    r"(?i)(?<![\w$.])(?P<name>"
    + "|".join(_flat_name(name) for name in ("oth", "dp", "dq", "qi", "k", "d", "p", "q"))
    + ")"
    + _FLAT_END
)
# The backstop (review at 5a5b916b): whole tokens anywhere, no separator.
_TOKEN_TYPE = re.compile(r"(?<![A-Za-z0-9_])kty(?![A-Za-z0-9_])")
_TOKEN_PRIVATE = re.compile(r"(?<![A-Za-z0-9_])(?P<name>oth|dp|dq|qi|k|d|p|q)(?![A-Za-z0-9_])")


# Returned for an escape the flattening pass cannot decode (an unknown
# letter, a too-long braced code, an unknown \N name, a code point past
# U+10FFFF, a trailing backslash run). A noncharacter, never kept as text:
# past the decode bound its presence alone fails the string closed.
_UNDECODABLE = "\uffff"
# An ordinary unrecognized backslash, distinct from an invalid character code
# or named sequence. A successfully read field literal may contain this as data.
_LITERAL_ESCAPE = "\ufffe"
# The marker is a name character on both sides of the boundary. Otherwise a
# failed name could restart at every marker in a dense run (quadratic).
_SCALAR_NAME = rf"(?<![\w$.{_LITERAL_ESCAPE}])[A-Za-z_${_LITERAL_ESCAPE}][\w${_LITERAL_ESCAPE}]*+"
_SCALAR_CONTINUATION = re.compile(r"\n[ \t]*+")
# The existing member-token grammar, with the uncertainty marker admitted in
# names. No type-name prefilter: an unresolved name of any kind stays a document.
_SCALAR_TOKEN = re.compile(_member_tokens(r":|=(?!=)", "{}()", _SCALAR_NAME))


def _flat_char(number: int) -> str:
    if number > 0x10FFFF:
        return _UNDECODABLE
    char = chr(number)
    return "" if char in "\"'`" else char


def _flat_escape(match: re.Match[str]) -> str:
    if match.group(0)[0] != "\\" or match.group("line") is not None:
        return ""  # a quote, or a continuation
    if match.group("brk") is not None:
        return "\n"
    if match.group("tab") is not None:
        return "\t"
    if match.group("blank") is not None:
        return " "
    if match.group("slash") is not None:
        return "/"
    code = match.group("braced") or match.group("u") or match.group("big") or match.group("x")
    if code is not None:
        return _flat_char(int(code, 16))
    if match.group("octal") is not None:
        return _flat_char(int(match.group("octal"), 8))
    named = match.group("named")
    if named is not None:
        try:
            decoded = unicodedata.lookup(named)
        except KeyError:
            return _UNDECODABLE
        # Unicode named sequences may contain several codepoints. They are
        # not Python named-character escapes; retain uncertainty without ord().
        return _flat_char(ord(decoded)) if len(decoded) == 1 else _UNDECODABLE
    other = match.group("other")
    if other is not None and other in "\"'`":
        return ""  # an escaped quote
    if other in ("u", "N") and match.string[match.end() : match.end() + 1] == "{":
        return _UNDECODABLE  # oversized/invalid character-code syntax
    return _LITERAL_ESCAPE


def _flattened(value: str) -> tuple[str, bool]:
    """``value`` flattened (``_FLAT_ESCAPE``), and whether flattening finished.

    Each round is one forward pass over a string that never grows; at most
    ``_FLAT_ROUNDS`` rounds. Finished means no backslash is left and every
    escape was decoded (neither uncertainty marker is in the result).
    """
    for _ in range(_FLAT_ROUNDS):
        value = _FLAT_ESCAPE.sub(_flat_escape, value)
        if _UNDECODABLE in value or _LITERAL_ESCAPE in value:
            return value, False
        if "\\" not in value:
            return _FLAT_BREAKS.sub("\n", value), True
    return value, False


def _flat_private(flat: str) -> str | None:
    """The private name a flattened string pairs with a key-type name, if any.

    The separator search (case-insensitive, names split by continuations),
    then the whole-token backstop (review at 5a5b916b): ``kty`` and a private
    name anywhere, with no separator required.
    """
    if _FLAT_TYPE.search(flat) is not None:
        private = _FLAT_PRIVATE.search(flat)
        if private is not None:
            return re.sub(r"[\n \t]", "", private["name"]).lower()
    if _TOKEN_TYPE.search(flat) is not None:
        token = _TOKEN_PRIVATE.search(flat)
        if token is not None:
            return token["name"]
    return None


def _terminal_field_text(value: str, flat: str) -> bool:
    """Whether uncertainty belongs only to literal data, not a child document.

    The caller proved a complete field literal with the actual string reader.
    Reuse the scanner's member tokens on original and normalized text to reject
    escaped/uncertain names, object members and string-valued child fields.
    Quotes, brackets and control characters within scalar
    data are not themselves evidence of another encoding layer. Two ordered
    token passes; no parsing or recursive decoding beyond the existing budget.
    """
    for text in (value, _SCALAR_CONTINUATION.sub("", flat)):
        depth = 0
        for token in _SCALAR_TOKEN.finditer(text):
            name = token.group("key")
            if name is not None:
                start = token.start("key")
                if (
                    depth
                    or "\\" in name
                    or _LITERAL_ESCAPE in name
                    or (start > 0 and text[start - 1] == "\\")
                    or any(
                        token.group(group) is not None
                        for group in ("value", "triple_value", "array")
                    )
                ):
                    return False
            elif token.group() in ("{", "("):
                depth += 1
            elif token.group() in ("}", ")"):
                depth = max(0, depth - 1)
    return True


def _undecoded_private(value: str, *, scalar: bool = False) -> str | None:
    """A private member name when a string left undecoded may hold a JWK.

    Past the decode bound a string is not read as JSON or YAML again. It fails
    closed instead (task #2124, reviews at 79723aa6 and 875394f0): when it
    holds both a key-type name and a private member name, each followed by a
    separator, a comment or an explicit key's line break, the caller masks the
    whole string.

    A string with no backslash is read as it is, by region: its own text or
    one of its top-level quoted bodies. A type named only inside a child
    literal (``note="{'kty': 'oct'}"``) supplies no type to the text around
    it, as on the decoded path. Any other string is flattened first (escape
    layers, quotes and YAML continuations, ``_flattened``) and read whole: no
    escape spelling is enumerated, and since its quotes can no longer be told
    apart there is no region condition (over-masking is accepted past the
    bound). When flattening does not finish (the round cap, or an escape it
    cannot decode), the string is masked with no type evidence at all
    (review at 454a2038). A complete field scalar already decoded by the
    current document can retain ordinary literal backslashes when the member
    lexer proves they belong to data rather than an encoded name or child
    document. Invalid character codes/names and unfinished rounds never gain
    this exception. No private/type token pair may be present in either view.

    Every step is a forward regex pass (at most ``_FLAT_ROUNDS`` substitution
    rounds) or one quote-context pass with a cursor walk: linear.
    """
    if "\\" in value:
        # No type-hint gate here (review at 454a2038): a string still encoded
        # after the round cap, or holding an escape the pass cannot decode,
        # fails closed without type evidence. Only an already-decoded terminal
        # scalar can retain a literal unknown escape, under the checks below.
        flat, finished = _flattened(value)
        if not finished:
            if (
                scalar
                and "\\" not in flat
                and _UNDECODABLE not in flat
                and _flat_private(value) is None
                and _flat_private(flat) is None
                and _terminal_field_text(value, flat)
            ):
                # The current document already decoded this complete field
                # scalar (e.g. a Windows path). Its literal backslash is not
                # another document layer. No type-hint filter is involved;
                # embedded syntax and unresolved rounds still fail closed.
                return None
            if (
                "\\" not in flat
                and _FLAT_TYPE.search(flat) is None
                and _FLAT_PRIVATE.search(flat) is None
            ):
                # Only an uncertainty marker stopped flattening and no layer is
                # left. With neither a key-type nor a private member name in
                # member position, the string is a label or note (\u{a:b},
                # \N{UNKNOWN}), not a JWK: main never masked these.
                return None
            return "value"
        name = _flat_private(flat)
        if name is None:
            return None
        return name if name in _PRIVATE or name == "oth" else "value"
    if _UNDECODED_TYPE.search(value) is None or _UNDECODED_PRIVATE.search(value) is None:
        return None
    # Malformed quote context cannot prove that type and private evidence are
    # unrelated. Use the same ownership proof as the decoded trigger path.
    quoted = _owned_quote_regions(value, oct_blocks._quote_context(value)[0])
    typed = {region for region, _ in _string_regions(_UNDECODED_TYPE.finditer(value), quoted)}
    for region, private in _string_regions(_UNDECODED_PRIVATE.finditer(value), quoted):
        if region in typed:
            name = private["name"]
            return name if name in _PRIVATE or name == "oth" else "value"
    return None


def is_private_member(kty: object, name: object) -> bool:
    """Whether ``name`` is a private member of a JWK whose ``kty`` is ``kty``.

    ``k`` is private only in a symmetric (``oct``) key; ``p``, ``q``, ``dp``,
    ``dq``, ``qi`` and ``oth`` (the other primes, each with private r, d and t)
    only in an RSA key.
    """
    if not isinstance(kty, str) or not isinstance(name, str):
        return False
    if kty == _ANY_TYPE:
        return name in _PRIVATE or name in _RSA_ONLY
    if name in _RSA_ONLY:
        return kty == "RSA"
    return kty in _KEY_TYPES and name in _PRIVATE and (name != "k" or kty == "oct")


@dataclass(frozen=True, slots=True)
class PrivateMember:
    name: str
    value: str
    start: int
    end: int
    # Inside a string (an oct-block value): replaced by the bare marker, so the
    # string's own quotes stay. A JSON-scan member is replaced with quotes.
    inline: bool = False
    # Physical source extent for audit attribution, independently of replacement.
    audit_span: tuple[int, int] | None = None


_PLAIN_TYPED = -2


@dataclass(slots=True)
class _JwkFrame:
    start: int
    oth_of: _JwkFrame | None = None
    members: list[PrivateMember] | tuple[()] = ()
    oth: list[PrivateMember] | tuple[()] = ()
    in_oth: bool = False
    kty: str | None = None
    has_kty: bool = False
    # A token offset for an escaped type; -1 none; _PLAIN_TYPED when an unquoted
    # YAML plain scalar typed the frame (task #2200): its private members are
    # reported, but the object is not decided and proves nothing public. A
    # sentinel, not a field: frames are held per nesting level (peak budget).
    identifier_type: int = -1
    type_hint: bool = False
    uncertain_unpack: bool = False
    closing: str = "}"


def audit_span(member: PrivateMember, text: str) -> tuple[int, int]:
    """Source material covered by a member, excluding literal delimiters."""
    if member.audit_span is not None:
        return member.audit_span
    if member.inline:
        return member.start, member.end
    raw = text[member.start : member.end]
    _, low, high = _literal_bounds(raw)
    while low < high and raw[low].isspace():
        low += 1
    while high > low and raw[high - 1].isspace():
        high -= 1
    return member.start + low, member.start + high


def _literal_bounds(raw: str) -> tuple[str, int, int]:
    opening = _LITERAL_OPEN.match(raw)
    if opening is not None:
        prefix, quote, low = (opening["prefix"] or "").lower(), opening["quote"], opening.end()
    elif raw and raw[0] in "\"'":
        prefix, quote, low = "", raw[0], 1
    else:
        return "", 0, len(raw)
    high = len(raw)
    if high >= low + len(quote) and raw.endswith(quote):
        position = high - len(quote) - 1
        while position >= low and raw[position] == "\\":
            position -= 1
        if (high - len(quote) - 1 - position) % 2 == 0:
            high -= len(quote)
    return prefix, low, high


@dataclass(slots=True)
class _SourceMap:
    """Compact decoded runs -> original source runs; no per-character objects."""

    starts: array
    origins: array  # original start << 1 | one-to-one flag
    ends: array
    point: tuple[int, int] | None = None

    def bounds(self, low: int, high: int) -> tuple[int, int]:
        if self.point is not None:
            return self.point
        left = max(0, bisect_right(self.starts, low) - 1)
        right = max(0, bisect_right(self.starts, max(low, high - 1)) - 1)
        first, last = self.origins[left], self.origins[right]
        start = first >> 1
        end = self.ends[right]
        if first & 1:
            start += low - self.starts[left]
        if last & 1:
            end = min(end, (last >> 1) + high - self.starts[right])
        return start, max(start + 1, end)


def _slice_source(source: _SourceMap | None, low: int, high: int) -> _SourceMap:
    if source is None:
        return _SourceMap(array("q", [0]), array("q", [(low << 1) | 1]), array("q", [high]))
    if source.point is not None:
        return source
    starts, origins, ends = array("q"), array("q"), array("q")
    first = max(0, bisect_right(source.starts, low) - 1)
    last = bisect_right(source.starts, high - 1)
    for index in range(first, last):
        begin = max(low, source.starts[index])
        end = min(high, source.starts[index + 1] if index + 1 < len(source.starts) else high)
        origin = source.origins[index]
        starts.append(begin - low)
        origins.append(origin + ((begin - source.starts[index]) << 1) if origin & 1 else origin)
        ends.append(
            min(source.ends[index], (origin >> 1) + end - source.starts[index])
            if origin & 1
            else source.ends[index]
        )
    return _SourceMap(starts, origins, ends)


@dataclass(slots=True)
class _Snippet:
    value: str
    span: tuple[int, int]
    source: _SourceMap
    # Decoded strings replace their enclosing literal; comment bodies retain
    # their exact member offsets. Further # characters are ordinary comment text.
    decoded: bool = True
    comment: bool = False
    # Comment snippets and their decoded descendants cannot reserve live code.
    owns_source: bool = True
    # Root-source comment bodies fence replacements through decoded descendants.
    regions: array | None = None
    literals_only: bool = False
    depth: int | None = None
    # A JavaScript literal's source body is inspected once without treating
    # its nested backticks/slashes as additional encoded-string layers.
    javascript_body: bool = False
    # A template literal (not a regular expression): its ${...} interpolations
    # are runtime text, read as unknown names and types (task #2200).
    template: bool = False


_PHYSICAL_BREAK = re.compile(rf"[{oct_blocks._LINE_CHARS}]")
_SOURCE_ESCAPE = re.compile(
    r"\\u[dD][89aAbB][0-9a-fA-F]{2}\\u[dD][c-fC-F][0-9a-fA-F]{2}"
    r"|\\(?:\r\n|N\{[^}]*+(?:}|\Z)|x[0-9a-fA-F]{0,2}|u[0-9a-fA-F]{0,4}"
    r"|U[0-9a-fA-F]{0,8}|[0-7]{1,3}|[\s\S]|\Z)|\r\n|\r"
)


def _source_map(
    raw: str, value: str, span: tuple[int, int], parent: _SourceMap | None
) -> _SourceMap:
    if parent is not None and parent.point is not None:
        return parent
    if parent is None and not _PHYSICAL_BREAK.search(raw):
        return _SourceMap(
            array("q", [0]),
            array("q", [span[0] << 1]),
            array("q", [span[0] + 1]),
            (span[0], span[0] + 1),
        )
    prefix, low, high = _literal_bounds(raw)
    starts, origins, ends = array("q"), array("q"), array("q")
    position = 0
    valid = True
    byte_decoder = codecs.getincrementaldecoder("utf-8")("replace") if "b" in prefix else None
    pending_bytes: tuple[int, int] | None = None
    json_string = raw.startswith('"') and not raw.startswith('"' * 3)

    def emit(piece: str, begin: int, end: int, plain: bool) -> None:
        nonlocal position, valid, pending_bytes
        absolute_low, absolute_high = span[0] + begin, span[0] + end
        source_low, source_high = (
            parent.bounds(absolute_low, absolute_high)
            if parent is not None
            else (absolute_low, absolute_high)
        )
        normalized = piece.replace("\r\n", "\n").replace("\r", "\n")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            if "r" in prefix or plain:
                output = normalized
                data = (
                    output.encode("utf-8", errors="surrogatepass")
                    if byte_decoder is not None
                    else None
                )
            elif byte_decoder is not None:
                try:
                    data = codecs.escape_decode(
                        normalized.encode("utf-8", errors="surrogatepass"), "replace"
                    )[0]
                except ValueError:
                    # escape_decode refuses cut-off byte escapes even with a
                    # replacement handler. Retain a conservative source extent.
                    valid = False
                    data = normalized.encode("utf-8", errors="surrogatepass")
                output = ""
            elif json_string and len(normalized) == 12 and normalized.startswith(r"\u"):
                output = json.loads('"' + normalized + '"')
                data = None
            else:
                output = codecs.decode(
                    normalized.encode("raw_unicode_escape"), "unicode_escape", "backslashreplace"
                )
                data = None
        if byte_decoder is not None:
            output = byte_decoder.decode(data or b"")
            if pending_bytes is not None:
                source_low = min(source_low, pending_bytes[0])
                source_high = max(source_high, pending_bytes[1])
                plain = False
            pending_bytes = (source_low, source_high) if byte_decoder.getstate()[0] else None
        if not output:
            return
        valid = valid and value.startswith(output, position)
        affine = plain and output == piece and source_high - source_low == len(piece)
        starts.append(position)
        origins.append(source_low << 1 | int(affine))
        ends.append(source_high)
        position += len(output)

    def literal(begin: int, end: int) -> None:
        if begin >= end:
            return
        if byte_decoder is not None and byte_decoder.getstate()[0] and end - begin > 1:
            emit(raw[begin : begin + 1], begin, begin + 1, True)
            begin += 1
        cursor = begin
        if parent is not None:
            first = bisect_right(parent.starts, span[0] + begin)
            last = bisect_right(parent.starts, span[0] + end - 1)
            for index in range(first, last):
                right = parent.starts[index] - span[0]
                emit(raw[cursor:right], cursor, right, True)
                cursor = right
        emit(raw[cursor:end], cursor, end, True)

    cursor = low
    for token in _SOURCE_ESCAPE.finditer(raw, low, high):
        literal(cursor, token.start())
        # Raw escapes are ordinary source runs unless a physical CR needs
        # normalization; split them at parent mapping boundaries as well.
        if "r" in prefix and "\r" not in token.group():
            literal(token.start(), token.end())
        else:
            emit(token.group(), token.start(), token.end(), not token.group().startswith("\\"))
        cursor = token.end()
    literal(cursor, high)
    if byte_decoder is not None:
        final = byte_decoder.decode(b"", final=True)
        if final:
            valid = False  # fall back conservatively for a split invalid byte
    if not valid or position != len(value) or not starts:
        source_low, source_high = (
            parent.bounds(span[0] + low, span[0] + high)
            if parent is not None
            else (span[0] + low, span[0] + high)
        )
        return _SourceMap(
            array("q", [0]),
            array("q", [source_low << 1]),
            array("q", [max(source_low + 1, source_high)]),
        )
    return _SourceMap(starts, origins, ends)


def _attribute(
    members: list[PrivateMember], text: str, source: _SourceMap | None
) -> list[PrivateMember]:
    if source is None:
        return members
    return [
        member
        if member.audit_span is not None
        else replace(member, audit_span=source.bounds(*audit_span(member, text)))
        for member in members
    ]


def _read_string(raw: str) -> tuple[str, bool]:
    """The ordinary string reading and whether it actually decoded a literal."""
    try:
        if raw.startswith('"'):
            try:
                return json.loads(raw), True
            except ValueError:
                # Double-quoted Python literals also permit hex/octal escapes.
                # Read that same literal grammar before treating it as cut off.
                pass
        # A Python repr of a Windows path ('C:\\dir') is an invalid escape,
        # whose warning category varies by Python version; only inspect it.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            warnings.simplefilter("ignore", DeprecationWarning)
            # literal_eval builds recursive converter closures even for one
            # constant; on supported CPython builds those cycles accumulate
            # until cyclic GC runs. Read the parsed constant directly while
            # retaining literal_eval semantics for any other expression.
            node = ast.parse(raw.lstrip(" \t"), mode="eval").body
            value = node.value if isinstance(node, ast.Constant) else ast.literal_eval(node)
        return (value, True) if isinstance(value, str) else (raw[1:], False)
    except (ValueError, SyntaxError):
        return raw[1:], False  # an unfinished/invalid literal in a log excerpt


def _string(raw: str) -> str:
    return _read_string(raw)[0]


# YAML double-quoted scalars (task #2126). The break set is the oct-block rule's
# (a superset of YAML's), so both rules remove the same escaped continuations.
_YAML_BREAKS = re.compile(rf"[ \t]*+({oct_blocks._LINE_BREAK})")
_YAML_BLANKS = re.compile(r"[ \t]*+")
_YAML_RUN = re.compile(rf'[^\\"{oct_blocks._LINE_CHARS} \t]++|[ \t]++')
_YAML_ESCAPES = {
    "0": "\0",
    "a": "\a",
    "b": "\b",
    "t": "\t",
    "\t": "\t",
    "n": "\n",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    "e": "\x1b",
    " ": " ",
    '"': '"',
    "/": "/",
    "\\": "\\",
    "N": "\x85",
    "_": "\xa0",
    "L": "\u2028",
    "P": "\u2029",
}
_YAML_HEX_WIDTH = {"x": 2, "u": 4, "U": 8}
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _yaml_breaks(raw: str, position: int, out: list[str]) -> int:
    """Consume the blank lines after a break; drop the next line's indentation."""
    while (line := _YAML_BREAKS.match(raw, position)) is not None:
        out.append(line[1] if line[1] in "\u2028\u2029" else "\n")
        position = line.end()
    return _YAML_BLANKS.match(raw, position).end()


def _yaml_double_quoted(raw: str) -> str | None:
    """Read one double-quoted scalar as YAML does, or None when YAML cannot.

    Python and JSON keep the indentation after an escaped physical break; YAML
    removes the break and that indentation, and folds an unescaped break (with
    its surrounding blanks) to one space, or to the newlines of blank lines
    after it. The escape set is YAML's (``\\N \\_ \\L \\P \\e \\/``, a backslash
    before a blank, ``\\x``/``\\u``/``\\U`` hex). An invalid escape makes the
    scalar unreadable. One forward pass; a cut-off excerpt returns its prefix.
    """
    if raw[:1] != '"' or raw.startswith('"' * 3):
        return None
    out: list[str] = []
    position, high = 1, len(raw)
    while position < high:
        char = raw[position]
        if char == '"':
            break
        if char == "\\":
            position += 1
            if position == high:
                break
            escape = raw[position]
            if (line := _YAML_BREAKS.match(raw, position)) is not None and line.start(
                1
            ) == position:
                position = _yaml_breaks(raw, line.end(), out)
            elif escape in _YAML_ESCAPES:
                out.append(_YAML_ESCAPES[escape])
                position += 1
            elif width := _YAML_HEX_WIDTH.get(escape):
                digits = raw[position + 1 : position + 1 + width]
                if len(digits) != width or not _HEX_DIGITS.issuperset(digits):
                    return None
                code = int(digits, 16)
                if code > 0x10FFFF:
                    return None
                out.append(chr(code))
                position += 1 + width
            else:
                return None
            continue
        if (line := _YAML_BREAKS.match(raw, position)) is not None:
            breaks: list[str] = []
            position = _yaml_breaks(raw, line.end(), breaks)
            if line[1] in "\u2028\u2029":
                out.append(line[1])
            elif not breaks:
                out.append(" ")
            out.extend(breaks)
            continue
        run = _YAML_RUN.match(raw, position)
        if run is None:
            # Unreachable while _YAML_RUN covers every non-break character;
            # a drift must make the scalar unreadable, never raise (or be
            # stripped under -O and raise AttributeError) out of redaction.
            return None
        out.append(run.group())
        position = run.end()
    return "".join(out)


def _yaml_reading(raw: str, value: str, names: frozenset[str]) -> str:
    """``value``, or the YAML reading of ``raw`` when only that one is in ``names``.

    A double-quoted scalar written in YAML reads differently from Python only
    through escapes (folding alone always leaves a blank in the value), so a
    raw string without a backslash keeps its reading at no cost. The YAML
    reading can only add a key type or a private name, never withdraw one.
    """
    if value in names or "\\" not in raw:
        return value
    reading = _yaml_double_quoted(raw)
    return reading if reading in names else value


def _javascript_member_name(raw: str) -> str:
    """Decode only local Unicode escapes; never confer primary type authority."""

    def decode(match: re.Match[str]) -> str:
        digits = match.group().lstrip("\\")[1:].strip("{}").lstrip("0") or "0"
        if len(digits) > 6:
            return match.group()
        codepoint = int(digits, 16)
        return chr(codepoint) if codepoint <= 0x10FFFF else match.group()

    return _JS_IDENTIFIER_ESCAPE.sub(decode, raw)


def _python_raw_literal(text: str, quote_start: int) -> bool:
    """Recognize only a directly attached, source-local raw/bytes prefix."""
    for width in (2, 1):
        low = quote_start - width
        if low < 0 or text[low:quote_start].lower() not in _RAW_PREFIXES:
            continue
        if low and (text[low - 1] in ".\\" or _javascript_identifier_part(text[low - 1])):
            continue
        return True
    return False


def _javascript_unprefixed(text: str, quote_start: int) -> bool:
    """A supplemental JS field/type cannot borrow a Python-prefixed quote."""
    return quote_start == 0 or not (
        text[quote_start - 1] in ".\\" or _javascript_identifier_part(text[quote_start - 1])
    )


def _javascript_private_name(text: str, start: int, raw: str) -> str | None:
    """Additional private evidence only; the ordinary token stays unchanged."""
    if (
        raw[:1] not in ("'", '"')
        or "\\" not in raw
        or not _javascript_unprefixed(text, start)
        or _javascript_braced(raw)
        or _JAVASCRIPT_PRIVATE_STRING.fullmatch(raw) is None
    ):
        return None
    logical = _javascript_logical_string(raw)
    return logical if logical in _PRIVATE or logical == "oth" else None


def _javascript_braced_type_name(text: str, start: int, raw: str) -> bool:
    """Genuine independently usable braced discriminator-name provenance."""
    if not _javascript_braced(raw):
        return False
    return _javascript_type_name(text, start, raw)


def _javascript_type_name(text: str, start: int, raw: str) -> bool:
    if raw[:1] in ("'", '"'):
        return _javascript_unprefixed(text, start) and _javascript_string(raw) == "kty"
    return (
        _javascript_unprefixed(text, start)
        and (start == 0 or text[start - 1] not in "\"'")
        and _javascript_member_name(raw) == "kty"
        and all(
            match.group().split("u", 1)[0] == "\\" for match in _JS_IDENTIFIER_ESCAPE.finditer(raw)
        )
    )


def _javascript_braced(raw: str) -> bool:
    """Whether this token has actual, bounded braced Unicode evidence."""
    for match in _JS_QUOTED_ESCAPE.finditer(raw):
        if len(match["slashes"]) % 2 == 0:
            continue
        digits = match["digits"].lstrip("0") or "0"
        if len(digits) <= 6 and int(digits, 16) <= 0x10FFFF:
            return True
    return False


def _javascript_string(raw: str) -> str:
    """Read one braced-evidence JS token; unrelated tokens retain Python semantics."""
    if not _javascript_braced(raw):
        return _string(raw)
    return _javascript_logical_string(raw)


def _javascript_logical_string(raw: str) -> str:
    """Read exactly one logical JS string, without recursively decoding output."""
    _, position, high = _literal_bounds(raw)
    if position == 0:
        return _string(raw)
    result = StringIO()
    simple = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
    hexadecimal = "0123456789abcdefABCDEF"
    while position < high:
        slash = raw.find("\\", position, high)
        if slash < 0:
            result.write(raw[position:high])
            break
        result.write(raw[position:slash])
        position = slash + 1
        if position == high:
            result.write("\\")
            break
        escape = raw[position]
        position += 1
        if escape in "\r\n\u2028\u2029":
            if escape == "\r" and position < high and raw[position] == "\n":
                position += 1
        elif escape in simple:
            result.write(simple[escape])
        elif escape in "01234567":
            limit = 3 if escape < "4" else 2
            while position < high and position - slash <= limit and raw[position] in "01234567":
                position += 1
            result.write(chr(int(raw[slash + 1 : position], 8)))
        elif escape == "u" and position < high and raw[position] == "{":
            begin = position + 1
            position = begin
            while position < high and raw[position] in hexadecimal:
                position += 1
            if position > begin and position < high and raw[position] == "}":
                digits = raw[begin:position].lstrip("0") or "0"
                position += 1
                if len(digits) <= 6 and int(digits, 16) <= 0x10FFFF:
                    result.write(chr(int(digits, 16)))
                    continue
            result.write(raw[slash:position])
        elif escape in "ux":
            width = 4 if escape == "u" else 2
            end = position + width
            digits = raw[position:end]
            if end <= high and len(digits) == width and all(c in hexadecimal for c in digits):
                result.write(chr(int(digits, 16)))
                position = end
            else:
                result.write(raw[slash:position])
        else:
            # NonEscapeCharacter is an identity escape in JS. The emitted
            # character is never fed back into this decoder.
            result.write(escape)

    return result.getvalue()


def _triple_value(raw: str) -> str:
    """Decode a Python literal, retaining useful content in cut-off excerpts."""
    opening = _LITERAL_OPEN.match(raw)
    assert opening is not None
    prefix, quote = (opening["prefix"] or "").lower(), opening["quote"]
    width = len(quote)
    closed = len(raw) >= opening.end() + width and raw.endswith(quote)
    if closed:
        position = len(raw) - width - 1
        while position >= opening.end() and raw[position] == "\\":
            position -= 1
        closed = (len(raw) - width - 1 - position) % 2 == 0
    body = raw[opening.end() : -width if closed else len(raw)]
    if "f" not in prefix:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                warnings.simplefilter("ignore", DeprecationWarning)
                node = ast.parse(raw, mode="eval").body
            if isinstance(node, ast.Constant):
                if isinstance(node.value, str):
                    return node.value
                if isinstance(node.value, bytes):
                    return node.value.decode("utf-8", errors="replace")
        except (SyntaxError, ValueError):
            pass
    if "r" in prefix:
        return body
    # Every remaining nonraw route needs the SAME one salvage layer, including
    # skipped f-string AST and parsed nonconstant nodes. Successful constants
    # and raw prefixes have already returned. Even runs retain literal parity.
    body = _JS_SALVAGE_ESCAPE.sub(
        lambda match: match.group() + ("\\" if len(match.group()) % 2 else ""), body
    )
    # Salvage valid escapes before an unfinished escape or closing delimiter.
    # raw_unicode_escape preserves existing Unicode while unicode_escape follows
    # Python's string-escape rules; malformed tail bytes remain visible text.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return codecs.decode(
            body.encode("raw_unicode_escape"), "unicode_escape", "backslashreplace"
        )


def _expression_ends(
    text: str,
    starts: list[int],
    *,
    javascript: bool = False,
    statement_literals: dict[int, tuple[int, int, int]] | None = None,
) -> dict[int, int]:
    """Where each value expression starting at ``starts`` ends, in one pass.

    An expression ends at the first ',' at its own depth or at the bracket that
    closes its container; one never closed runs to the end of the text.
    """
    ends: dict[int, int] = {}
    pending: dict[int, list[int]] = {}
    depth = index = 0
    quoted, _, formatted = oct_blocks._quote_context(text)
    opaque = []
    for (low, high), flag in zip(quoted, formatted, strict=True):
        if flag:
            width = 3 if text[low - 3 : low] in ('"' * 3, "'" * 3) else 1
            opaque.append((low - width, min(len(text), high + width)))
    opaque_index = 0
    position, failed_until = starts[0], 0
    while position < len(text):
        recovery = javascript and position < failed_until
        token = (_STRUCTURE_JS if recovery else _STRUCTURE).search(
            text, position, failed_until if recovery else len(text)
        )
        if token is None:
            if recovery:
                position = failed_until
                continue
            break
        position = token.end()
        at = token.start()
        while index < len(starts) and starts[index] <= at:
            pending.setdefault(depth, []).append(starts[index])
            index += 1
        while opaque_index < len(opaque) and opaque[opaque_index][1] <= at:
            opaque_index += 1
        if opaque_index < len(opaque) and opaque[opaque_index][0] <= at:
            # Replacement-field punctuation is inside this literal, not the
            # enclosing member expression. Skip its closing quote as well so
            # it cannot be reparsed as an opener swallowing the container end.
            position = opaque[opaque_index][1]
            while index < len(starts) and starts[index] < position:
                ends[starts[index]] = position
                index += 1
            continue
        if token.group("comment") is not None:
            continue
        if token.group("javascript") is not None:
            if at < failed_until:
                continue
            literal = (statement_literals or {}).get(at) or _javascript_literal(
                text, at, javascript=javascript
            )
            if literal is not None:
                if javascript and not _javascript_closed(text, at, literal):
                    failed_until = literal[0]
                else:
                    position = literal[0]
            continue
        piece = token.group(0)
        if piece == ",":
            ends.update(dict.fromkeys(pending.pop(depth, ()), at))
        elif piece in ("(", "[", "{"):
            depth += 1
        elif piece in (")", "]", "}"):
            ends.update(dict.fromkeys(pending.pop(depth, ()), at))
            depth -= 1
    for start in starts:
        ends.setdefault(start, len(text))
    return ends


_LOG = logging.getLogger(__name__)
# Set to "1" (the test suite does) to raise instead of failing closed.
STRICT_ENV = "UA_REDACTION_STRICT"


def private_members(text: str, *, coarse: bool = True) -> list[PrivateMember]:
    """Private key values: the JSON scan's members, oct-block values, ``coarse`` runs (#2441).

    None overlap: oct blocks and coarse runs skip every object the JSON scan decided.

    An unexpected exception fails closed (task #2200, review r4): the whole
    text is one private member, so the log redactor masks all of it, the
    public-reply gate blocks it and the secrets audit reports every line
    (failing its gate loudly). Only the exception type and the text's length
    are logged, never the text. With ``UA_REDACTION_STRICT=1`` (set by the
    test suite and the fuzzers) the exception propagates, so no bug hides.
    """
    try:
        found = _private_members(text)
    except Exception as exc:  # noqa: BLE001 - fail closed; see the docstring
        if os.environ.get(STRICT_ENV) == "1":
            raise
        _LOG.warning(
            "JWK scan failed (%s) on %d characters; masking the whole text",
            type(exc).__name__,
            len(text),
        )
        return [PrivateMember("unparsed", text, 0, len(text))]
    return found if coarse else [m for m in found if m.name != jwk_fallback.NAME]


def _private_members(text: str) -> list[PrivateMember]:
    if not _may_have_kty(text):
        return jwk_fallback.members(text, [], [])
    oct_sources: list[tuple[int, int]] = []
    decoded_spans: list[tuple[int, int]] = []
    public: list[PrivateMember] = []  # private names the type made public (#2445)
    found, decided = _json_members(
        text, 0, oct_sources=oct_sources, _decoded_spans=decoded_spans, _public_members=public
    )
    found, spare = _by_document(text, found)
    objects = _object_spans(text, decided, decoded_spans)
    extra = [
        PrivateMember(span.name, text[span.start : span.end], span.start, span.end, span.inline)
        for span in oct_blocks.oct_block_values(
            text,
            objects,
            oct_sources,
            ignored_triggers=decoded_spans,
            already_masked=[(m.start, m.end) for m in found],
        )
    ]
    extra.extend(_block_members(text, objects, found + extra))
    extra.extend(_alias_members(text, objects, found + extra))
    if spare:
        extra.extend(_spare_members(spare, found + extra))
    extra.extend(jwk_fallback.members(text, decided, found + extra, decoded_spans, public))
    if not extra:
        return found
    return sorted(found + extra, key=lambda member: (member.start, -member.end))


def _by_document(
    text: str, found: list[PrivateMember]
) -> tuple[list[PrivateMember], list[PrivateMember]]:
    """Members clamped to the YAML documents their own source lies in.

    A decoded string's members take the whole literal's span. When that
    literal crosses a document marker (a Python ``'''`` that YAML reads as
    a single-quoted scalar), a member whose audit span lies in a later
    document would also cover an earlier document's private value, which is
    then never reported as its own finding (review r9). Each such member is
    clamped to the documents its audit span touches; the parts cut off are
    returned as spares, to be masked after every other rule has had its turn
    (so the masked bytes never shrink).
    """
    if "---" not in text and "..." not in text:
        return found, []
    markers = [marker.start() for marker in oct_blocks._DOCUMENT_MARKER.finditer(text)]
    if not markers:
        return found, []
    kept: list[PrivateMember] = []
    spare: list[PrivateMember] = []
    for member in found:
        crossed = bisect_right(markers, member.start)
        if (
            member.audit_span is None
            or member.inline
            or crossed >= len(markers)
            or markers[crossed] >= member.end
        ):
            kept.append(member)
            continue
        low, high = member.audit_span
        first = bisect_right(markers, low) - 1
        last = bisect_right(markers, max(low, high - 1))
        start = max(member.start, markers[first] if first >= 0 else 0)
        end = min(member.end, markers[last] if last < len(markers) else len(text))
        if start >= end:
            kept.append(member)  # its source lies outside it: keep it whole
            continue
        kept.append(replace(member, start=start, end=end))
        if member.start < start:
            spare.append(replace(member, end=start))
        if end < member.end:
            spare.append(replace(member, start=end))
    return kept, spare


def _spare_members(spare: list[PrivateMember], found: list[PrivateMember]) -> list[PrivateMember]:
    """The parts of ``_by_document`` spares no member found covers.

    They keep the attribution of the member they were cut from.
    """
    pieces = yaml_jwk.subtract_named(
        _disjoint_named([(str(index), m.start, m.end) for index, m in enumerate(spare)]),
        oct_blocks._Spans([(member.start, member.end) for member in found]),
    )
    return [
        replace(spare[int(index)], start=start, end=end, inline=False)
        for index, start, end in pieces
    ]


def _disjoint_named(pieces: list[tuple[str, int, int]]) -> list[tuple[str, int, int]]:
    pieces.sort(key=lambda piece: (piece[1], -piece[2]))
    disjoint: list[tuple[str, int, int]] = []
    cover = -1
    for name, start, end in pieces:
        if end <= cover:
            continue
        disjoint.append((name, max(start, cover), end))
        cover = end
    return disjoint


def _alias_members(
    text: str, decided: list[tuple[int, int]], found: list[PrivateMember]
) -> list[PrivateMember]:
    """Anchored nodes named by private members' alias values (``d: *v``)."""
    if not found or "*" not in text:
        return []
    pieces = yaml_jwk.alias_targets(
        text,
        # A value inside a string (an oct-block run) is never a YAML alias.
        [(member.name, member.start, member.end) for member in found if not member.inline],
        decided,
        _yaml_double_quoted,
    )
    return [
        PrivateMember(name, text[start:end], start, end)
        for name, start, end in _uncovered(pieces, found)
    ]


def _uncovered(
    pieces: list[tuple[str, int, int]], found: list[PrivateMember]
) -> list[tuple[str, int, int]]:
    """The parts of sorted, disjoint ``pieces`` outside every member found.

    A new rule never reports bytes another rule already masks: masked values
    stay disjoint, so no material is copied or reported twice.
    """
    covered = oct_blocks._Spans([(member.start, member.end) for member in found])
    return yaml_jwk.subtract_named(pieces, covered)


def _block_members(
    text: str,
    decided: list[tuple[int, int]],
    found: list[PrivateMember],
    audit_source: _SourceMap | None = None,
) -> list[PrivateMember]:
    """YAML members (``yaml_jwk``) no other rule's member already covers.

    Block and conservative members fill what found members leave. A flow
    mapping the YAML reading types (``yaml_jwk`` typed members) is kept only
    where no found member overlaps it: there the object scan, reading the
    text as Python or JavaScript, lost a member YAML still sees (a '#' glued
    inside a plain scalar, a quote inside one), even inside a decided object.
    """
    # The object scan's comment regions ('#' to the line end, Python's rule)
    # are not YAML's: a '#' glued inside a YAML plain scalar is text. Only
    # decided objects are cut from the YAML readings.
    objects = [(start, end) for start, end in decided if not text.startswith("#", start)]
    pieces, typed = yaml_jwk.yaml_members(text, objects, _yaml_double_quoted, _may_have_kty)
    if not pieces and not typed:
        return []
    pieces = _uncovered(pieces, found)
    if typed:
        covered = oct_blocks._Spans(
            [(member.start, member.end) for member in found] + [(s, e) for _, s, e in pieces]
        )
        for name, start, end in typed:
            index = bisect_right(covered.starts, end - 1) - 1
            if index >= 0 and covered.spans[index][1] > start:
                continue  # another rule already masks part of it
            pieces.append((name, start, end))
        pieces.sort(key=lambda piece: (piece[1], -piece[2]))
    members = []
    for name, start, end in pieces:
        members.append(
            PrivateMember(
                name,
                text[start:end],
                start,
                end,
                audit_span=audit_source.bounds(start, end) if audit_source else None,
            )
        )
    return members


def _object_spans(
    text: str, decided: list[tuple[int, int]], decoded_spans: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Only independently scanned source regions exclude outer block masks.

    A string containing a decided object supplies no outer lexical trigger, but
    remains a value of its enclosing oct block. Keep that distinction separate
    from the historical decided-span output of the JSON scan.
    """
    if not decided:
        return decided
    strings = set(decoded_spans)
    quoted, _, formatted = oct_blocks._quote_context(text)
    bodies = [span for span, flag in zip(quoted, formatted, strict=True) if flag]
    seen: set[int] = set()
    objects = []
    for span in decided:
        if span in strings:
            continue
        index = bisect_right(bodies, (span[0], len(text))) - 1
        if index >= 0 and bodies[index][0] <= span[0] and span[1] <= bodies[index][1]:
            # Reused quotes can expose expression objects to the raw token
            # scanner. Their source still belongs to the formatted value.
            if index not in seen:
                decoded_spans.append(bodies[index])
                seen.add(index)
            continue
        objects.append(span)
    return objects


def _json_members(
    text: str,
    _depth: int,
    *,
    oct_sources: list[tuple[int, int]] | None = None,
    _audit_source: _SourceMap | None = None,
    _decoded_spans: list[tuple[int, int]] | None = None,
    _javascript_context: bool = False,
    _javascript_literals: bool = True,
    _comment: bool = False,
    _identifier_types: dict[int, int] | None = None,
    _identifier_comments: tuple[array, int] | None = None,
    _alternate: bool = True,
    _comment_gaps: bool = True,
    _public_members: list[PrivateMember] | None = None,
) -> tuple[list[PrivateMember], list[tuple[int, int]]]:
    """Private key values inside their own RSA/EC/OKP/oct object.

    Extra nested metadata and JWKS key sets do not change the owning object.
    Once the object's ``kty`` makes a member private, its whole value is taken:
    a plain string whatever its alphabet, up to the closing quote or, in a
    cut-off excerpt, the end of the text; any other value (``("a" "b")``,
    ``r"..."``, an array) as the whole expression up to the next ',' or
    closing bracket at its depth. When unsure, redact. Every member of an RSA
    key's ``oth`` objects is taken too. Encoded JSON strings are inspected to a
    bounded depth; every private member found inside one is reported with the
    span of the enclosing string, avoiding decoded-to-source offset ambiguity.
    Bare d/k fields and the public parameters of a JWK are left alone.

    Also returns the spans this scan decided: each object with a plain-string
    ``kty`` (to the end of the text when it never closes), and each encoded
    string holding one. Lexical comment regions are included once inspected
    independently, so unfinished comment objects cannot reserve live code.
    ``_decoded_spans`` distinguishes encoded strings: their child types supply
    no outer trigger, but a real enclosing oct block still owns those strings
    as values. Only independently inspected source regions cut out masks.
    """
    snippets: list[_Snippet] = []
    if _decoded_spans is None:
        _decoded_spans = []
    found, decided = _json_members_in_context(
        text,
        _depth,
        oct_sources=oct_sources,
        snippets=snippets,
        audit_source=_audit_source,
        decoded_spans=_decoded_spans,
        javascript_context=_javascript_context,
        javascript_literals=_javascript_literals,
        comment=_comment,
        identifier_types=_identifier_types,
        identifier_comments=_identifier_comments,
        alternate_javascript=_alternate,
        comment_gaps=_comment_gaps,
        public_members=_public_members,
    )
    if not snippets:
        return found, decided
    # Scan decoded triple strings and the independent comment context. Decoded
    # offsets replace their outer literal; comments retain exact source offsets.
    # The worklist avoids recursion on snippet depth; ordinary encoded JSON still
    # observes the existing three-level bound.
    while snippets:
        snippet = snippets.pop()
        value, span, source = snippet.value, snippet.span, snippet.source
        children: list[_Snippet] = []
        sources: list[tuple[int, int]] = []
        call_operands: set[int] = set()
        decoded_children: list[tuple[int, int]] = []
        literal_members: list[PrivateMember] = []
        nested, nested_decided = _json_members_in_context(
            value,
            _depth if snippet.depth is None else snippet.depth,
            oct_sources=sources,
            snippets=children,
            call_operands=call_operands,
            audit_source=source,
            decoded_spans=decoded_children,
            comment=snippet.comment,
            literal_members=literal_members if snippet.literals_only else None,
            javascript_literals=not snippet.javascript_body,
            alternate_javascript=_alternate,
            comment_gaps=_comment_gaps,
        )
        if snippet.literals_only:
            nested = literal_members
        else:
            if snippet.javascript_body and ("\\" in value or (snippet.template and "${" in value)):
                # A JavaScript literal body is raw source: cook its escapes
                # (interpolations opaque) so a YAML block written with \n is
                # seen; any member masks the whole literal (task #2200).
                cooked = _template_cooked(value, interpolations=snippet.template)
                if yaml_jwk.block_members(cooked, [], _yaml_double_quoted, _may_have_kty) or (
                    snippet.template and yaml_jwk.interpolated_jwk(cooked)
                ):
                    nested.append(PrivateMember("d", value, 0, len(value)))
            elif not snippet.decoded or text.find("\\", span[0], span[1]) >= 0:
                # A commented-out YAML block JWK (source offsets), or one in a
                # literal whose line breaks are escapes (the whole literal is
                # masked). Real breaks in a literal are read in the raw text.
                nested.extend(
                    _block_members(value, _object_spans(value, nested_decided, []), nested, source)
                )
            nested.extend(
                PrivateMember(
                    piece.name,
                    value[piece.start : piece.end],
                    piece.start,
                    piece.end,
                    audit_span=source.bounds(piece.start, piece.end),
                )
                for piece in oct_blocks.oct_block_values(
                    value,
                    _object_spans(value, nested_decided, decoded_children),
                    sources,
                    ignored_triggers=[child.span for child in children if child.decoded]
                    + decoded_children,
                    already_masked=[(m.start, m.end) for m in nested],
                )
            )
        if snippet.decoded:
            replacements = [
                replace(member, start=span[0], end=span[1], inline=False) for member in nested
            ]
            found.extend(_comment_fenced(replacements, snippet.regions, text))
            if nested_decided and snippet.owns_source:
                decided.append(span)
                _decoded_spans.append(span)
            snippets.extend(
                replace(
                    child,
                    span=span,
                    decoded=True,
                    owns_source=snippet.owns_source and child.owns_source,
                    regions=snippet.regions,
                    depth=snippet.depth if child.depth is None else child.depth,
                )
                for child in children
            )
        else:
            # Comment source preserves the complete context's original offsets.
            # A real private expression may already cover a comment in its
            # entirety; do not retain another copy of the same private bytes.
            covered = oct_blocks._Spans([(member.start, member.end) for member in found])
            for member in _comment_fenced(nested, snippet.regions, text):
                index = bisect_right(covered.starts, member.start) - 1
                if index < 0 or member.end > covered.spans[index][1]:
                    found.append(member)
            # Only the lexical comment bodies belong to this scan. Decided
            # mappings or decoded strings can extend across blanked live code.
            snippets.extend(
                replace(
                    child,
                    owns_source=False,
                    regions=snippet.regions,
                    depth=snippet.depth if child.depth is None else child.depth,
                )
                for child in children
            )
    # The live, comment-view and recovery scans may each report one member
    # (task #2236). Only exact copies collapse, plus a copy with no audit span
    # when an otherwise identical member has one. Members that differ in value
    # (a placeholder and a real secret in one replaced literal), name, span,
    # inline mode or audit span all stay, so the result is order independent.
    unique = dict.fromkeys(found)
    attributed = {
        (member.name, member.value, member.start, member.end, member.inline)
        for member in unique
        if member.audit_span is not None
    }
    kept = [
        member
        for member in unique
        if member.audit_span is not None
        or (member.name, member.value, member.start, member.end, member.inline) not in attributed
    ]
    return sorted(kept, key=lambda member: (member.start, -member.end)), decided


_TEMPLATE_PIECE = re.compile(
    r"\\(?:u\{(?P<braced>[0-9A-Fa-f]{1,8})\}|u(?P<unicode>[0-9A-Fa-f]{4})"
    r"|x(?P<hex>[0-9A-Fa-f]{2})|(?P<char>\r\n|[\s\S]))|\$\{"
)
_TEMPLATE_SIMPLE = {"n": "\n", "r": "\r", "t": "\t", "v": "\v", "f": "\f", "b": "\b", "0": "\0"}
_TEMPLATE_CONTINUATIONS = frozenset({"\n", "\r", "\r\n", "\u2028", "\u2029"})


def _template_cooked(raw: str, *, interpolations: bool = True) -> str:
    """A JavaScript template (or string) body as the runtime reads it.

    Escapes decode; a backslash before a line terminator is removed. Each
    ``${...}`` interpolation is opaque: it becomes the marker ``${}``, to its
    balanced closing brace or, unbalanced, to the end of the body. The
    block-mapping rule reads a name or type holding that marker as unknown.
    A regular expression body has no interpolations: ``${`` stays text there. Used
    only to reveal line structure; its offsets are never reported. One pass.
    """
    out: list[str] = []
    position = 0
    while (piece := _TEMPLATE_PIECE.search(raw, position)) is not None:
        out.append(raw[position : piece.start()])
        position = piece.end()
        if piece.group() == "${":
            if not interpolations:
                out.append("${")  # a regular expression's text, not runtime text
                continue
            depth = 1
            while position < len(raw) and depth:
                depth += (raw[position] == "{") - (raw[position] == "}")
                position += 1
            out.append("${}")  # opaque: yaml_jwk reads it as an unknown name or type
            continue
        digits = piece["braced"] or piece["unicode"] or piece["hex"]
        if digits is not None:
            code = int(digits, 16)
            out.append(chr(code) if code <= 0x10FFFF else "?")
        else:
            char = piece["char"]
            out.append("" if char in _TEMPLATE_CONTINUATIONS else _TEMPLATE_SIMPLE.get(char, char))
    out.append(raw[position:])
    return "".join(out)


def _comment_literal_snippets(
    text: str, regions: array, source: _SourceMap, depth: int
) -> list[_Snippet]:
    """Read each quote family independently when prose ownership is ambiguous.

    A malformed single-quoted wrapper cannot swallow a complete double-quoted
    literal (or vice versa). Each family is one forward pass; ordinary encoded
    literals still spend the existing JSON decode depth. Python f/t/r wrappers
    retain their own literal semantics and physical-source mapping.
    """
    snippets = []
    for scanner in _COMMENT_LITERAL_SCANS:
        position = 0
        while token := scanner.search(text, position):
            position = token.end()
            raw = token.group()
            prefix, low, high = _literal_bounds(raw)
            if high == len(raw):
                continue
            if "f" in prefix or "t" in prefix:
                start = token.start() + low - (3 if token.lastgroup == "triple" else 1)
                delimiter = text[start] * (3 if token.lastgroup == "triple" else 1)
                position, closed = oct_blocks._formatted_end(text, start, delimiter)
                if not closed:
                    continue
                raw = text[token.start() : position]
            target_depth = depth + (0 if prefix or token.lastgroup == "triple" else 1)
            if target_depth > 3:
                continue
            value = _triple_value(raw)
            if not _may_have_kty(value):
                continue
            span = token.start(), position
            snippets.append(
                _Snippet(
                    value,
                    span,
                    _source_map(raw, value, span, source),
                    owns_source=False,
                    regions=regions,
                    depth=target_depth,
                )
            )
    return snippets


def _comment_fenced(
    members: list[PrivateMember], regions: array | None, text: str
) -> list[PrivateMember]:
    """Keep independently scanned comments from replacing intervening code."""
    if regions is None:
        return members
    found = []
    for member in members:
        index = max(0, bisect_right(regions, member.start) - 1)
        index -= index % 2
        while index < len(regions) and regions[index] < member.end:
            low, high = max(member.start, regions[index]), min(member.end, regions[index + 1])
            if high > low:
                if low == member.start and high == member.end:
                    found.append(member)
                else:
                    audit_low, audit_high = audit_span(member, text)
                    audit = max(low, audit_low), min(high, audit_high)
                    if audit[1] <= audit[0]:
                        audit = low, high
                    found.append(
                        replace(
                            member,
                            start=low,
                            end=high,
                            value=text[low:high],
                            inline=True,
                            audit_span=audit,
                        )
                    )
            index += 2
    return found


def _child_strings(value: str, quoted: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The quoted bodies of a decoded string that are child strings, not key names.

    A child string holds text of its own: a ``kty`` inside it is that text's
    business (the recursive decode reads it), so it supplies no trigger to the
    decoded string around it. A body that ends inside a ``kty`` name match,
    whose separator therefore lies outside the closing quote (a quoted mapping
    key, or a quoted explicit YAML key whose ``:`` value indicator starts the
    next line), is this string's own key name instead (task #2124). Raw text
    already reads those keys; inside an encoded log string the decoded copy is
    the only one whose line breaks are real, so ignoring it left the explicit
    key's secret.

    ``quoted`` comes from one forward pass, so it is sorted and disjoint, and
    the name candidates come in order too: one cursor walks the bodies
    alongside them, so the pass is linear in both.
    """
    # A lexical body alone is not proof of a child literal (orphan quotes in
    # raw log excerpts can span the whole explicit-key document).
    quoted = _owned_quote_regions(value, quoted)
    if not quoted:
        return quoted
    names: set[int] = set()
    index = 0
    for start, end in oct_blocks._kty_candidates(value):
        while index < len(quoted) and quoted[index][1] <= start:
            index += 1
        if index == len(quoted):
            break
        if quoted[index][0] <= start < quoted[index][1] < end:
            names.add(index)
    if not names:
        return quoted
    return [span for index, span in enumerate(quoted) if index not in names]


def _json_members_in_context(
    text: str,
    _depth: int,
    *,
    oct_sources: list[tuple[int, int]] | None,
    snippets: list[_Snippet],
    decoded_spans: list[tuple[int, int]],
    call_operands: set[int] | None = None,
    audit_source: _SourceMap | None = None,
    comment: bool = False,
    literal_members: list[PrivateMember] | None = None,
    isolate_comments: bool = True,
    public_members: list[PrivateMember] | None = None,
    recovered_quotes: array | None = None,
    unpack_evidence: tuple[dict[int, int], set[int], array] | None = None,
    keyword_members: bool = False,
    javascript_literals: bool = True,
    javascript_context: bool = False,
    alternate_javascript: bool = True,
    identifier_types: dict[int, int] | None = None,
    identifier_comments: tuple[array, int] | None = None,
    private_unpacks: dict[int, tuple[int, int, int]] | None = None,
    private_opaque: list[tuple[int, int]] | None = None,
    comment_gaps: bool = False,
) -> tuple[list[PrivateMember], list[tuple[int, int]]]:
    """Scan one context, queuing decoded strings and isolated comment source."""
    frames: list[_JwkFrame] = []
    found: list[PrivateMember] = []
    decided: list[tuple[int, int]] = []
    comments = array("q")
    # // line comment bodies (task #2224). They join only the isolated comment
    # view; live scanning still reads them as code, and they decide nothing.
    slash_comments = array("q")
    slash_checked = -1  # end of the last // body examined
    # Comment runs between a member name and its colon (task #2366), as
    # [run start, colon) pairs, for the additive view in publish_comment_gaps.
    gap_runs = array("q")
    gap_checked = -1  # where the last comment run's walk ended
    block_close = -1  # the last */ found by that walk
    comment_type = False
    expressions: list[int] = []  # starts of private values that are expressions
    type_positions: list[int] = []  # real, plain key-type members in this context
    type_comments = [-1, -1]  # the last comment a flow type read skipped (#2200)
    # Each frame's own last type token. A repeated kty in one object joins the
    # earlier one; a type copied from an unpacked child is replaced (spread
    # order is well defined), as before (#2200).
    own_types: dict[int, str] = {}
    uncertain_parents: set[int] = set()
    computed_calls: dict[int, int] = {}
    public_candidates: list[tuple[PrivateMember, int, int]] = []
    proof_calls: dict[int, int] = {}
    proof_frames: dict[int, _JwkFrame] = {}
    statement_literals: dict[int, tuple[int, int, int]] = {}
    alternate_literals: dict[int, tuple[int, int, int]] = {}
    uncertain_punctuation: set[int] = set()
    alternate_intervals = (
        _alternate_intervals(text, statement_literals, alternate_literals, uncertain_punctuation)
        if javascript_literals
        else [(0, len(text))]
        if alternate_javascript and "\\" in text
        else []
    )
    # A literal body already has one physical source fence. Its raw scan must
    # retain nested template contents and ordinary hashes in that same fence,
    # without reopening literals or consuming an encoded-string depth level.
    if javascript_context:
        statement_literals.update(alternate_literals)
    del alternate_literals
    track_public = public_members is not None or bool(alternate_intervals)
    primary_public: set[tuple[int, str]] = set()
    supplemental_unpacks = (
        private_unpacks
        if private_unpacks is not None
        else {}
        if javascript_context and _javascript_braced(text)
        else None
    )
    unpacked_parents = _unpacked_parents(
        text,
        call_operands,
        uncertain_parents=uncertain_parents,
        computed_calls=computed_calls,
        call_parents=proof_calls if unpack_evidence is not None else None,
        statement_literals=statement_literals,
        private_effects=supplemental_unpacks,
        private_opaque=private_opaque,
    )
    if unpack_evidence is not None:
        unpack_evidence[0].update(unpacked_parents)
        unpack_evidence[0].update(proof_calls)
        unpack_evidence[1].update(uncertain_parents | computed_calls.keys())
        if keyword_members:
            # The proof view has real call frames, so the same validated
            # literal operands can transfer types into those frames too.
            unpacked_parents.update(proof_calls)
            uncertain_parents.update(computed_calls)
    unpacked_children: dict[int, list[_JwkFrame]] = {}
    supplement = javascript_context and _javascript_braced(text)
    supplement_members: dict[int, list[tuple[PrivateMember, int]]] | None = (
        {} if supplement else None
    )
    supplement_masks = (
        _javascript_private_effects(
            text,
            supplemental_unpacks or {},
            statement_literals=statement_literals,
            physical_comments=identifier_comments,
        )
        if supplement
        else {}
    )

    def identifier_mask(frame: _JwkFrame) -> int:
        if frame.identifier_type < 0:
            return 0
        if identifier_comments is not None:
            boundaries, offset = identifier_comments
            if bisect_right(boundaries, offset + frame.identifier_type) % 2:
                return 0
        return _TYPE_MASK.get(frame.kty, 0)

    def record_identifier_members(members, mask: int, *, other_primes: bool = False) -> None:
        if identifier_types is None or not mask:
            return
        for member in members:
            role = 1 if other_primes or member.name in _RSA_ONLY else 4 if member.name == "k" else 7
            if private_mask := mask & role:
                identifier_types[member.start] = private_mask

    def supplement_mask(frame: _JwkFrame) -> int:
        return supplement_masks.get(frame.start, 0)

    def publish_supplement(frame: _JwkFrame, mask: int) -> None:
        assert supplement_members is not None
        for member, role in supplement_members.pop(frame.start, ()):
            if private_mask := mask & role:
                found.append(member)
                if identifier_types is not None:
                    identifier_types[member.start] = private_mask

    def finish(frame: _JwkFrame, end: int) -> None:
        proof_frames.pop(frame.start, None)
        computed = frame.uncertain_unpack or frame.start in uncertain_parents
        type_hint = frame.has_kty or frame.type_hint
        if frames and type_hint:
            frames[-1].type_hint = True
        if computed:
            frame.uncertain_unpack = True
        uncertain = computed and type_hint
        if frame.has_kty or uncertain:
            decided.append((frame.start, end))
            # Source offsets only: recursive decoded-string scans never receive
            # this output. A literal ** object supplies the enclosing call's
            # type even though this scan retains ownership of the object itself.
            if (
                oct_sources is not None
                and str(frame.kty).casefold() in ("oct", _ANY_TYPE)
                and (call_operands is None or frame.start in call_operands)
            ):
                oct_sources.append((frame.start, end))
        owner = frame.oth_of
        if owner is not None and frame.has_kty:
            # An "oth" entry carries no kty (RFC 7518 6.3.2.7). One with its own
            # is a key and is judged by that type first (PR #1378 review: after
            # an untyped owner's cut-off "oth" array, a later EC key lost its
            # type and its "d" stayed visible). Members its own type leaves
            # public still go to the owner, private there when it is RSA.
            own, rest = [], []
            for member in frame.members:
                (own if is_private_member(frame.kty, member.name) else rest).append(member)
            if rest:
                if not owner.oth:
                    owner.oth = []
                owner.oth.extend(rest)
            frame.members = own
            frame.oth_of = owner = None
        if owner is not None:
            # An "oth" entry: its private-named members (r, d, t and the
            # private names) are private if the owner is RSA.
            # Entries nested in entries report to the same owner directly, so no
            # member is copied twice (copying up each level was quadratic).
            pending = [frame]
            while pending:
                node = pending.pop()
                if node.members or node.oth:
                    if not owner.oth:
                        owner.oth = []
                    owner.oth.extend(node.members)
                    owner.oth.extend(node.oth)
                if supplement_members is not None:
                    if extra := supplement_members.pop(node.start, None):
                        supplement_members.setdefault(owner.start, []).extend(extra)
                pending.extend(unpacked_children.pop(node.start, ()))
            return
        kty = frame.kty
        copying = (
            proof_frames.get(unpacked_parents.get(frame.start))
            if keyword_members
            else frames[-1]
            if frames and unpacked_parents.get(frame.start) == frames[-1].start
            else None
        )
        if copying is not None:
            # Inherit types in source order, but defer copied values: a later
            # type in the parent can make an earlier child's k private.
            if frame.has_kty:
                copying.kty = kty
                copying.has_kty = True
                copying.identifier_type = frame.identifier_type
            if computed:
                copying.uncertain_unpack = True
            if (
                frame.members
                or frame.oth
                or frame.start in unpacked_children
                or (supplement_members is not None and frame.start in supplement_members)
            ):
                unpacked_children.setdefault(copying.start, []).append(frame)
            return
        if frame.start not in unpacked_children:
            if supplement:
                publish_supplement(frame, supplement_mask(frame))
            escaped_mask = identifier_mask(frame)
            record_identifier_members(frame.members, escaped_mask)
            record_identifier_members(frame.oth, escaped_mask, other_primes=True)
            if (
                track_public
                and kty in _KEY_TYPES
                and not uncertain
                and frame.identifier_type != _PLAIN_TYPED
            ):
                public_candidates.extend(
                    (m, frame.start, end)
                    for m in frame.members
                    if not is_private_member(kty, m.name) and (m.end > m.start or m.end == -1)
                )
            found.extend(m for m in frame.members if is_private_member(kty, m.name) or uncertain)
            if uncertain or is_private_member(kty, "oth"):
                found.extend(frame.oth)
            return
        # Walk the completed literal-unpack tree once. Keep each member in its
        # owning node instead of copying lists up every level (quadratic).
        # A value private in either its source or a copying ancestor is private.
        pending_nodes = [(frame, 0, 0)]
        while pending_nodes:
            node, inherited, inherited_identifiers = pending_nodes.pop()
            # Private roles accumulate through copying ancestors. Keep the
            # same union for live escaped-type provenance, with primary
            # comment-owned types filtered before propagation. Three role
            # bits suffice; no member copies or ancestor lists are required.
            escaped_mask = inherited_identifiers | identifier_mask(node)
            if supplement:
                supplementary_mask = supplement_mask(node)
                publish_supplement(node, supplementary_mask)
            record_identifier_members(node.members, escaped_mask)
            record_identifier_members(node.oth, escaped_mask, other_primes=True)
            mask = inherited | (
                7
                if node.uncertain_unpack and (inherited or node.has_kty or node.type_hint)
                else _TYPE_MASK.get(node.kty, 0)
            )
            found.extend(
                member
                for member in node.members
                if (
                    mask & 1
                    if member.name in _RSA_ONLY
                    else mask & 4
                    if member.name == "k"
                    else mask
                )
            )
            if track_public and mask and node.identifier_type != _PLAIN_TYPED:
                public_candidates.extend(
                    (member, frame.start, end)
                    for member in node.members
                    if (member.end > member.start or member.end == -1)
                    and not (
                        mask & 1
                        if member.name in _RSA_ONLY
                        else mask & 4
                        if member.name == "k"
                        else mask
                    )
                )
            if mask & 1:
                found.extend(node.oth)
            for child in unpacked_children.pop(node.start, ()):
                pending_nodes.append((child, mask, escaped_mask))

    position = javascript_failed_until = 0
    rescanned = 0  # text before this was already rescanned after a mis-pairing
    tokens = (
        (_JS_COMMENT_TOKEN if comment else _JS_TOKEN)
        if javascript_context
        else (_PROOF_TOKEN if keyword_members else _COMMENT_TOKEN if comment else _TOKEN)
    )
    while position < len(text):
        recovery = javascript_context and position < javascript_failed_until
        token = (_JS_COMMENT_TOKEN if recovery else tokens).search(
            text, position, javascript_failed_until if recovery else len(text)
        )
        if token is None:
            if recovery:
                position = javascript_failed_until
                continue
            break
        position = token.end()
        raw = token.group(0)
        if javascript_context and token.start() in uncertain_punctuation:
            # Reuse the same escape-owned punctuation as boundary discovery.
            # Primary scans keep their ordinary literal fences and authority.
            position = token.start() + 1
            continue
        if token.group("javascript") is not None:
            if (
                comment_gaps
                and token.start() >= gap_checked
                and text.startswith(("//", "/*"), token.start())
            ):
                separator, gap_checked, block_close = _comment_gap_separator(
                    text, token.start(), block_close
                )
                if separator >= 0:
                    gap_runs.extend((token.start(), separator))
            if (
                not comment
                and not recovery
                and text.startswith("//", token.start())
                and token.start() >= slash_checked
            ):
                # Python reads // as division, JS as a line comment. Its body
                # is also read as comment prose, so an orphan quote there gets
                # the same ownership recovery as in a # comment. In JS the
                # first // owns the rest of its line: later pairs on that line
                # are inside its body, examined once (no suffix rescans).
                low = token.start() + 2
                end = _COMMENT_END.search(text, low)
                high = len(text) if end is None else end.start()
                slash_checked = high
                # Every body joins the view, as every # body does (task
                # #2245): a type key split by a continuation has no hint on
                # its first line, and one object may span several // lines.
                # Only a hint makes the view worth building; the continued
                # bodies are checked again below.
                slash_comments.extend((low, high))
                comment_type = comment_type or _may_have_kty(text[low:high])
            if not javascript_literals or token.start() < javascript_failed_until:
                continue
            literal = statement_literals.get(token.start()) or _javascript_literal(
                text, token.start(), javascript=javascript_context
            )
            if (
                literal is not None
                and javascript_context
                and not _javascript_closed(text, token.start(), literal)
            ):
                javascript_failed_until = literal[0]
            elif literal is not None:
                position, low, high = literal
                value = text[low:high]
                if text.startswith("/*", token.start()) and _may_have_kty(value):
                    # A block comment is JS prose: "\\d\\p" is dp there, even
                    # after nested // or # prose with an orphan quote (#2245).
                    found.extend(
                        replace(member, start=low + member.start, end=low + member.end)
                        for member in _identity_members(value, None, evidence="any", recovery=True)
                    )
                if _may_have_kty(value):
                    physical = audit_source.bounds(low, high) if audit_source else (low, high)
                    source = _SourceMap(array("q"), array("q"), array("q"), physical)
                    snippets.append(
                        _Snippet(
                            value,
                            (token.start(), position),
                            source,
                            comment=True,
                            depth=_depth,
                            javascript_body=True,
                            template=raw == "`",
                        )
                    )
            continue
        if not comment and not recovery and token.group("comment") is not None:
            if slash_comments and slash_comments[-1] > token.start():
                slash_comments[-1] = token.start()  # the # comment owns the rest
            comments.extend((token.start() + 1, token.end()))
            comment_type = comment_type or _may_have_kty(raw[1:])
            continue
        if (
            token.group("string") is not None
            and token.start() >= rescanned
            and (
                _MISPAIRED[raw[0]].match(text, token.end())
                or (token.end() == len(text) and not _CLOSED_STRING.fullmatch(raw))
            )
        ):
            # A mis-paired string, or one never closed: its opening quote was
            # unbalanced, so drop it and read on from the next character (an
            # unclosed string's content is not decoded either way). Each stretch
            # is rescanned at most once, so the scan stays linear.
            position, rescanned = token.start() + 1, token.end()
            continue
        frame = frames[-1] if frames else None
        if raw == "{" or (keyword_members and raw == "("):
            oth_of = None
            if (
                raw == "{"
                and frame is not None
                and frame.in_oth
                and unpacked_parents.get(token.start()) != frame.start
            ):
                # A proved member unpack after an oth array belongs to the
                # mapping itself; it cannot be another entry of that array.
                oth_of = frame.oth_of or frame
            frames.append(
                _JwkFrame(
                    token.start(),
                    oth_of=oth_of,
                    closing="}" if raw == "{" else ")",
                )
            )
            if keyword_members:
                proof_frames[token.start()] = frames[-1]
            continue
        if raw == "}" or (keyword_members and raw == ")"):
            if keyword_members:
                # Malformed grouping cannot prove a field public. Each frame
                # is removed once, keeping ragged excerpts bounded.
                while frames and frames[-1].closing != raw:
                    frame = frames.pop()
                    frame.uncertain_unpack = True
                    finish(frame, token.end())
                    if frames:
                        frames[-1].uncertain_unpack = True
            if frames:
                finish(frames.pop(), token.end())
            continue
        if token.group("triple") is not None:
            value = _triple_value(raw)
            if _may_have_kty(value):
                snippets.append(
                    _Snippet(
                        value,
                        token.span(),
                        _source_map(raw, value, token.span(), audit_source),
                    )
                )
            continue
        value: str | None = None
        decoded_literal = False
        value_group = "triple_value" if token.group("triple_value") is not None else "value"
        if token.group("key") is not None:
            raw_name = token.group("key")
            if raw_name[0] in "\"'":
                javascript_name = javascript_context and not (
                    "\\u{" in raw_name and _python_raw_literal(text, token.start("key"))
                )
                name = _javascript_string(raw_name) if javascript_name else _string(raw_name)
                # A YAML double-quoted name (an explicit flow key may span
                # lines) can also spell the type or a private member (#2126).
                # A plain (non-?) flow key spanning lines is not valid YAML, and
                # this reading applies in JS/Python contexts too: both
                # deliberately over-mask ambiguous text.
                name = _yaml_reading(
                    raw_name,
                    name,
                    _OTH_ENTRY_NAMES
                    if frame is not None and frame.oth_of is not None
                    else _YAML_NAMES,
                )
            else:
                name = _javascript_member_name(raw_name) if javascript_context else raw_name
            if keyword_members and frame is not None:
                separator = token.group("separator")
                if (separator == "=") != (frame.closing == ")") or (
                    separator == "=" and raw_name[0] in "\"'"
                ):
                    frame.uncertain_unpack = True
            if frame is not None:
                frame.in_oth = name == "oth" and token.group("array") is not None
            plain = token.group(value_group) is not None and bool(
                _VALUE_TAIL.match(text, token.end())
            )
            if plain:
                if value_group == "triple_value":
                    value = _triple_value(token.group(value_group))
                else:
                    value, decoded_literal = _read_string(token.group(value_group))
            if frame is not None and name == "kty":
                if plain:
                    interpreted_type = False
                    if (
                        javascript_context
                        and value_group == "value"
                        and "\\u{" in token.group(value_group)
                        and not _python_raw_literal(text, token.start(value_group))
                    ):
                        javascript_value = _javascript_string(token.group(value_group))
                        interpreted_type = javascript_value != value
                        value = javascript_value
                    # YAML removes an escaped break and the next line's
                    # indentation; Python keeps the indentation (#2126).
                    if value_group == "value":
                        value = _yaml_reading(token.group(value_group), value, _KEY_TYPES)
                    frame.kty = _union_type(own_types.get(frame.start), value)
                    own_types[frame.start] = frame.kty
                    frame.has_kty = True
                    # A repeated, different type: the union's private set
                    # already covers every reading, so a member it leaves
                    # public is public in each; escaped-type provenance stays.
                    frame.identifier_type = (
                        token.start()
                        if javascript_context and ("\\" in raw_name or interpreted_type)
                        else -1
                        if frame.identifier_type != _PLAIN_TYPED
                        else _PLAIN_TYPED
                    )
                    if computed_calls and value in _KEY_TYPES:
                        type_positions.append(token.start())
                elif (
                    not keyword_members
                    and not javascript_context
                    and token.group(value_group) is None
                    and token.group("array") is None
                    and (flow_type := _flow_type(text, token.end(), type_comments)) is not None
                ):
                    # YAML reads an unquoted type as a plain scalar (task #2200).
                    # It types the frame's members without deciding the object:
                    # the oct-block rule keeps its own reading of the text. Not
                    # in JavaScript views or keyword proofs: those may run over
                    # recovery text whose orphan quotes are blanked, where a
                    # quoted (perhaps raw-prefixed) type would read as bare.
                    # A later type joins an earlier one (YAML keeps the last;
                    # the union of private sets covers every reading).
                    plain_kind, quoted_type = flow_type
                    if quoted_type is None:
                        frame.kty = _union_type(own_types.get(frame.start), plain_kind)
                        own_types[frame.start] = frame.kty
                        frame.identifier_type = _PLAIN_TYPED
                    else:
                        # Properties before a quoted type (!!str "RSA"): the
                        # object is decided exactly as for a bare quoted type.
                        # A local of its own: ``value`` is the token's own
                        # string, and this token has none (the quoted type
                        # follows properties), so later steps must see None.
                        read_type = _yaml_reading(quoted_type, _string(quoted_type), _KEY_TYPES)
                        previous = own_types.get(frame.start)
                        frame.kty = _union_type(previous, read_type)
                        own_types[frame.start] = frame.kty
                        frame.has_kty = True
                        if previous is not None and previous != read_type:
                            frame.identifier_type = _PLAIN_TYPED
                        if computed_calls and read_type in _KEY_TYPES:
                            type_positions.append(token.start())
            if (
                supplement
                and frame is not None
                and (extra_name := _javascript_private_name(text, token.start("key"), raw_name))
                and extra_name != name
            ):
                if plain:
                    member = PrivateMember(extra_name, value or "", *token.span(value_group))
                else:
                    begin = token.start(value_group) if token.group(value_group) else token.end()
                    if token.group("array") is not None:
                        begin = token.start("array")
                    expressions.append(begin)
                    member = PrivateMember(extra_name, "", begin, -1)
                role = (
                    1
                    if frame.oth_of is not None or extra_name in _RSA_ONLY
                    else 4
                    if extra_name == "k"
                    else 7
                )
                assert supplement_members is not None
                supplement_members.setdefault(frame.start, []).append((member, role))
            if (
                frame is not None
                and name != "kty"
                and (
                    name in _PRIVATE
                    or name == "oth"
                    or (frame.oth_of is not None and name in _OTH_ENTRY_PRIVATE)
                )
            ):
                if plain:
                    span = token.span(value_group)
                    if not frame.members:
                        frame.members = []
                    frame.members.append(PrivateMember(name, value or "", *span))
                elif not frame.in_oth:
                    # Not one plain string: take the whole value expression.
                    begin = token.start(value_group) if token.group(value_group) else token.end()
                    if token.group("array") is not None:
                        begin = token.start("array")
                    expressions.append(begin)
                    if not frame.members:
                        frame.members = []
                    frame.members.append(PrivateMember(name, "", begin, -1))
            if value is None and token.group(value_group) is not None:
                if value_group == "triple_value":
                    value = _triple_value(token.group(value_group))
                else:
                    value, decoded_literal = _read_string(token.group(value_group))
        else:
            if frame is not None:
                # Any other string ends an "oth" array of the current object.
                frame.in_oth = False
            value = _string(token.group("string"))
            value_group = "string"
        if value is not None and value_group == "triple_value":
            if _may_have_kty(value):
                span = token.span(value_group)
                snippets.append(
                    _Snippet(
                        value,
                        span,
                        _source_map(token.group(value_group), value, span, audit_source),
                    )
                )
        elif value is not None and _depth < 3 and _may_have_kty(value):
            sources: list[tuple[int, int]] | None = [] if "**" in value else None
            span = token.span(value_group)
            decoded_source = _source_map(token.group(value_group), value, span, audit_source)
            decoded_children: list[tuple[int, int]] = []
            nested, nested_decided = _json_members(
                value,
                _depth + 1,
                oct_sources=sources,
                _audit_source=decoded_source,
                _decoded_spans=decoded_children,
                _alternate=alternate_javascript,
                _comment_gaps=comment_gaps,
            )
            nested_objects = _object_spans(value, nested_decided, decoded_children)
            ignored: list[tuple[int, int]] | None = None
            if not sources and _may_have_kty(value):
                # A decoded snippet can supply its own unquoted type. Scan it
                # here, where prefixes/format syntax have their real context;
                # a child string cannot supply the surrounding snippet's type.
                # A quoted key name is not a child string (task #2124).
                quoted = _child_strings(value, oct_blocks._quote_context(value)[0])
                if oct_blocks._triggers(value, oct_blocks._Spans(nested_objects + quoted)):
                    ignored = [span for span in quoted if span[0] < span[1]]
                del quoted
            scan_blocks = bool(sources) or ignored is not None
            if scan_blocks:
                # These offsets belong only to the decoded value. Inspect its
                # unpacked calls locally, then mask the whole enclosing source
                # string as for decoded JSON members; never export its offsets.
                nested.extend(
                    PrivateMember(
                        piece.name,
                        value[piece.start : piece.end],
                        piece.start,
                        piece.end,
                        audit_span=decoded_source.bounds(piece.start, piece.end),
                    )
                    for piece in oct_blocks.oct_block_values(
                        value,
                        nested_objects,
                        sources,
                        ignored_triggers=(ignored or []) + decoded_children,
                        already_masked=[(m.start, m.end) for m in nested],
                    )
                )
            # An encoded YAML block JWK: its members mask the whole string too.
            nested.extend(_block_members(value, nested_objects, nested))
            replacements = [replace(m, start=span[0], end=span[1], inline=False) for m in nested]
            found.extend(replacements)
            if literal_members is not None:
                literal_members.extend(replacements)
            if nested_decided:
                decided.append(span)
                decoded_spans.append(span)
        elif (
            value is not None
            and _depth >= 3
            and (
                bound_name := _undecoded_private(
                    value, scalar=token.group("key") is not None and plain and decoded_literal
                )
            )
            is not None
        ):
            # The decode bound (task #2124): this string stays encoded. Fail
            # closed on it whole rather than decode deeper; the enclosing
            # strings take its span in turn, as for any decoded member.
            member = PrivateMember(bound_name, value, *token.span(value_group))
            found.append(member)
            if literal_members is not None:
                literal_members.append(member)
    lexical = comments
    if slash_comments and isolate_comments:
        lexical = _merged_intervals(comments, slash_comments)
    bodies = lexical
    if isolate_comments and lexical and identifier_comments is None:
        # A JS string continuation joins the next physical line to a comment
        # body (task #2169). Only the comment view grows; regions stay lexical.
        # Not in an alternate view: its recovery scan would re-enter this block
        # on the same continued body, one nesting level per continued line.
        bodies = _continued_comments(text, lexical)
        if not comment_type and bodies != lexical:
            comment_type = any(
                _may_have_kty(text[bodies[index] : bodies[index + 1]])
                for index in range(0, len(bodies), 2)
            )
    if comment_type and isolate_comments:
        # Comments share a separate source context: a genuine commented-out
        # mapping may span several lines, even with live code between them.
        # Blank the code while preserving character offsets and physical breaks;
        # comment braces and fields can never modify a real source frame.
        regions: list[tuple[int, int]] = []
        previous = 0
        for index in range(0, len(comments), 2):
            low, high = comments[index], comments[index + 1]
            if regions and not text[previous : low - 1].strip():
                regions[-1] = (regions[-1][0], high)
            else:
                regions.append((low - 1, high))
            previous = high
        body = StringIO()
        previous = 0
        for index in range(0, len(bodies), 2):
            low, high = bodies[index], bodies[index + 1]
            body.write(_COMMENT_GAP.sub(lambda match: " " * len(match[0]), text[previous:low]))
            body.write(text[low:high])
            previous = high
        body.write(_COMMENT_GAP.sub(lambda match: " " * len(match[0]), text[previous:]))
        source = audit_source or _SourceMap(
            array("q", [0]), array("q", [1]), array("q", [len(text)])
        )
        # The independent comment scan handles only these lexical bodies;
        # an unfinished comment object must never exclude later live calls.
        decided.extend(regions)
        original = body.getvalue()
        # Comment prose has no Python source meaning, so a key private only
        # under JS escapes ("\\d\\p") is read as JS (tasks #2227, #2239).
        found.extend(_identity_members(original, bodies, evidence="any", recovery=True))
        content, ambiguous = _comment_content(original)
        # Inner hashes are real inline comments for the enclosing excerpt,
        # while their payload may contain independently private constructions.
        # The normal/payload ownership pair must not depend on unrelated prose
        # having an orphan quote elsewhere in this context.
        separate_ownership = ambiguous or content != original
        # A Python comment cannot continue on the next physical line, so a
        # continued body is foreign source (task #2169): read it with the
        # recovery scan too, which accepts JS identity escapes ("\d\p").
        # A // body is JS comment prose (task #2224) and is read the same way.
        foreign = bodies != comments
        # Complete literals keep their ordinary recursive decoding even when
        # another comment has ambiguous quote ownership. Recovery only adds
        # source-fenced candidates; it must never replace that established scan.
        snippets.append(
            _Snippet(
                content,
                (0, len(text)),
                source,
                decoded=False,
                comment=True,
                owns_source=False,
                regions=bodies if separate_ownership else None,
                literals_only=separate_ownership,
            )
        )
        if separate_ownership or foreign:
            found.extend(_comment_recovery_members(original, bodies, _depth, source, snippets))
    if (
        not comment
        and isolate_comments
        and not javascript_context
        and "\\" in text
        and _JS_CONTINUED.search(text)
    ):
        # A type member spelled across a line continuation is JS evidence for
        # its object, like a braced escape, so "\\d\\p" there is dp (#2227).
        found.extend(_identity_members(text, None, evidence="continued", recovery=False))
    # Innermost first, so a cut-off "oth" entry reaches its owner.
    while frames:
        finish(frames.pop(), len(text))
    if supplement:
        assert supplement_members is not None
        supplement_members.clear()
        supplement_masks.clear()
    del supplemental_unpacks

    def publish_public(ends: dict[int, int]) -> None:
        if public_members is None and not public_candidates:
            return
        # Removed orphan quotes inside an actual parsed frame make ownership
        # uncertain. Quotes inside its real inline comments cannot affect it.
        unsafe = array(
            "q",
            (point for point in (recovered_quotes or ()) if bisect_right(comments, point) % 2 == 0),
        )
        if unpack_evidence is not None:
            unpack_evidence[2].extend(unsafe)
        for member, low, high in public_candidates:
            end = ends.get(member.start, member.end)
            if end <= member.start or end >= high:
                continue
            index = bisect_right(unsafe, low - 1)
            if index == len(unsafe) or unsafe[index] >= high:
                if alternate_intervals:
                    primary_public.add((member.start, member.name))
                if public_members is not None:
                    public_members.append(
                        replace(member, end=end, value=text[member.start : end])
                        if member.end == -1
                        else member
                    )

    def publish_alternates() -> None:
        # Raw text has no trustworthy language selector. A conflicting local
        # lexical view can add private evidence, including malformed snippets;
        # it cannot export public/decided/type/unpack ownership to this scan.
        if not alternate_javascript:
            return
        # Slash/hash-derived types cannot revoke a primary public-role proof,
        # including literal unpacking. The value start and member
        # name identify that same physical member in this source context.
        # A public expression's extent proves nothing about the roles of
        # independently typed descendants inside it.
        for start, end in alternate_intervals:
            escaped_types: dict[int, int] = {}
            extra, _ = _json_members(
                text[start:end],
                _depth,
                _audit_source=_slice_source(audit_source, start, end),
                _javascript_context=True,
                _javascript_literals=javascript_literals,
                _comment=comment,
                _identifier_types=escaped_types,
                _identifier_comments=(comments, start),
                _alternate=False,
                _comment_gaps=False,
            )
            for member in extra:
                low, high = start + member.start, start + member.end
                # The mask retains only live escaped types that independently
                # make this member private, including copying ancestors.
                live_identifier_type = bool(escaped_types.get(member.start, 0))
                if (low, member.name) in primary_public and not live_identifier_type:
                    continue
                found.append(replace(member, start=low, end=high))

    def publish_comment_gaps() -> None:
        # The same text with each comment run before a colon blanked, so its
        # member reads as ``d   :"value"`` (task #2366). Like the alternates it
        # only adds private evidence: the live scan above already read those
        # bodies as prose, and nothing here touches public or decided state.
        if not gap_runs:
            return
        view = StringIO()
        previous = 0
        for index in range(0, len(gap_runs), 2):
            low, high = gap_runs[index], gap_runs[index + 1]
            view.write(text[previous:low])
            view.write(_COMMENT_GAP.sub(lambda match: " " * len(match[0]), text[low:high]))
            previous = high
        view.write(text[previous:])
        extra, _ = _json_members(
            view.getvalue(),
            _depth,
            _audit_source=audit_source,
            _javascript_literals=javascript_literals,
            _comment=comment,
            _alternate=alternate_javascript,
            _comment_gaps=False,
        )
        # The view repeats every member the live scan found; report those once.
        covered = oct_blocks._Spans([(member.start, member.end) for member in found])
        for member in extra:
            index = bisect_right(covered.starts, member.start) - 1
            if index < 0 or member.end > covered.spans[index][1]:
                found.append(member)

    # Computed call unpacks can supply any key type. Select complete argument
    # values while keeping unpack operators, keywords, commas and callees. A
    # computed value cannot strand operators between decided child objects.
    call_spans: list[tuple[int, int]] = []
    covered = -1
    for start, end in sorted(computed_calls.items()):
        if start + 1 >= covered:
            call_spans.append((start + 1, end))
            covered = end
    del computed_calls
    # Unrelated evidence cannot turn ordinary **kwargs into a JWK. Only real
    # plain type members count; a formatter's kty=TYPE_NAME is not key material.
    typed_calls = []
    for low, high in call_spans:
        index = bisect_right(type_positions, low - 1)
        if index < len(type_positions) and type_positions[index] < high:
            typed_calls.append((low, high))
    call_spans = typed_calls
    call_values = (
        _call_value_spans(text, call_spans, decided, statement_literals) if call_spans else []
    )
    # These arguments are now classified here; a later oct-block pass must not
    # reinterpret their structural prefixes or their callee across line breaks.
    decided.extend(call_spans)
    call_starts = {start for start, _ in call_values}
    found.extend(PrivateMember("k", "", start, end) for start, end in call_values)
    if expressions or call_values:
        # Resolve expression ends; a member inside an expression already taken
        # whole is dropped, so no text is sliced twice (nested expressions
        # would otherwise copy the rest of the text once per level).
        ends = (
            _expression_ends(
                text,
                sorted(set(expressions)),
                javascript=javascript_context,
                statement_literals=statement_literals,
            )
            if expressions
            else {}
        )
        ordered = sorted(
            (
                (m, ends[m.start] if m.end == -1 else m.end, m.end == -1 or m.start in call_starts)
                for m in found
            ),
            key=lambda item: (item[0].start, -item[1]),
        )
        found = []
        cover = -1
        for member, end, whole in ordered:
            if member.start < cover:
                continue
            if whole:
                member = replace(member, end=end, value=text[member.start : end].strip())
                cover = end
            found.append(member)
        publish_public(ends)
        publish_alternates()
        publish_comment_gaps()
        return _attribute(
            sorted(found, key=lambda m: (m.start, -m.end)), text, audit_source
        ), decided
    publish_public({})
    publish_alternates()
    publish_comment_gaps()
    return _attribute(
        sorted(found, key=lambda member: (member.start, -member.end)), text, audit_source
    ), decided
