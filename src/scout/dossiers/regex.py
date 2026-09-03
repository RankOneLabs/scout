"""The portable regex grammar and compiler for the dossier-source dossier contract.

Ported from the upstream producer implementation (pinned producer commit
44a70aa86d470e99c6315126ffdad5e1640d3f1c):
  packages/core/src/regex/portable-regex.ts -> parse_portable_regex
  packages/core/src/regex/compile.ts        -> compile_portable_regex / ascii_fold

See the producer's docs/regex-grammar.md for the authoritative grammar this
module implements. Re-exported by ``dossier_contract`` for backward-compatible
callers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# Portable regex grammar
# ---------------------------------------------------------------------------


class PortableRegexError(ValueError):
    """A pattern uses syntax outside the portable regex grammar."""

    def __init__(self, message: str, position: int) -> None:
        self.position = position
        super().__init__(f"{message} (at code point offset {position})")


# Characters that may be backslash-escaped to mean themselves. This is the
# entire escape vocabulary of the portable grammar.
_ESCAPABLE_PUNCTUATION: frozenset[str] = frozenset(
    "\\^$.|?*+()[]{}/"
)
# Additional characters meaningful only inside a character class that may
# also be escaped there to mean themselves literally.
_CLASS_ESCAPABLE_EXTRA: frozenset[str] = frozenset("-")


@dataclass(frozen=True, slots=True)
class LiteralNode:
    cp: int
    kind: Literal["literal"] = "literal"


@dataclass(frozen=True, slots=True)
class DotNode:
    kind: Literal["dot"] = "dot"


@dataclass(frozen=True, slots=True)
class AnchorNode:
    type: Literal["start", "end"]
    kind: Literal["anchor"] = "anchor"


@dataclass(frozen=True, slots=True)
class CharClassItem:
    cp: int
    type: Literal["char"] = "char"


@dataclass(frozen=True, slots=True)
class RangeClassItem:
    from_cp: int
    to_cp: int
    type: Literal["range"] = "range"


ClassItem = CharClassItem | RangeClassItem


@dataclass(frozen=True, slots=True)
class ClassNode:
    negate: bool
    items: tuple[ClassItem, ...]
    kind: Literal["class"] = "class"


@dataclass(frozen=True, slots=True)
class GroupNode:
    capturing: bool
    body: AlternationNode
    kind: Literal["group"] = "group"


@dataclass(frozen=True, slots=True)
class QuantNode:
    node: AstNode
    min: int
    max: int | None
    lazy: bool
    kind: Literal["quant"] = "quant"


@dataclass(frozen=True, slots=True)
class ConcatNode:
    parts: tuple[AstNode, ...]
    kind: Literal["concat"] = "concat"


@dataclass(frozen=True, slots=True)
class AlternationNode:
    options: tuple[ConcatNode, ...]
    kind: Literal["alt"] = "alt"


AstNode = (
    LiteralNode | DotNode | AnchorNode | ClassNode | GroupNode
    | QuantNode | ConcatNode | AlternationNode
)

_DIGIT_RE = re.compile(r"[0-9]")


class _Parser:
    """Recursive-descent parser for the portable regex grammar.

    Iterates by Unicode code point (Python ``str`` already does this) so
    astral literals are treated as single atoms, mirroring the TS parser's
    explicit ``Array.from(pattern)`` code-point iteration.
    """

    def __init__(self, pattern: str) -> None:
        self._cps: list[str] = list(pattern)
        self._pos = 0

    def parse(self) -> AlternationNode:
        node = self._parse_alternation()
        if self._pos != len(self._cps):
            raise PortableRegexError(f'Unexpected character "{self._peek()}"', self._pos)
        return node

    def _peek(self, offset: int = 0) -> str | None:
        idx = self._pos + offset
        return self._cps[idx] if 0 <= idx < len(self._cps) else None

    def _at_end(self) -> bool:
        return self._pos >= len(self._cps)

    def _advance(self) -> str:
        c = self._cps[self._pos]
        self._pos += 1
        return c

    def _expect(self, c: str) -> None:
        if self._peek() != c:
            found = self._peek() or "<end>"
            raise PortableRegexError(f'Expected "{c}" but found "{found}"', self._pos)
        self._pos += 1

    def _parse_alternation(self) -> AlternationNode:
        options = [self._parse_concat()]
        while self._peek() == "|":
            self._advance()
            options.append(self._parse_concat())
        return AlternationNode(options=tuple(options))

    def _parse_concat(self) -> ConcatNode:
        parts: list[AstNode] = []
        while not self._at_end() and self._peek() not in ("|", ")"):
            parts.append(self._parse_quantified())
        return ConcatNode(parts=tuple(parts))

    def _parse_quantified(self) -> AstNode:
        atom = self._parse_atom()
        quant = self._try_parse_quantifier_suffix()
        if quant is None:
            return atom

        if self._peek() in ("*", "+", "?"):
            raise PortableRegexError(
                "Quantifier cannot be repeated (possessive quantifiers are not supported)",
                self._pos,
            )

        return QuantNode(node=atom, min=quant[0], max=quant[1], lazy=quant[2])

    def _try_parse_quantifier_suffix(self) -> tuple[int, int | None, bool] | None:
        c = self._peek()
        min_: int
        max_: int | None

        if c == "*":
            self._advance()
            min_, max_ = 0, None
        elif c == "+":
            self._advance()
            min_, max_ = 1, None
        elif c == "?":
            self._advance()
            min_, max_ = 0, 1
        elif c == "{":
            saved = self._pos
            parsed = self._try_parse_brace_quantifier()
            if parsed is None:
                self._pos = saved
                return None
            min_, max_ = parsed
        else:
            return None

        lazy = False
        if self._peek() == "?":
            self._advance()
            lazy = True
        return (min_, max_, lazy)

    def _try_parse_brace_quantifier(self) -> tuple[int, int | None] | None:
        self._advance()  # consume '{'
        min_digits = self._read_digits()
        if min_digits == "":
            return None
        min_ = int(min_digits)

        if self._peek() == "}":
            self._advance()
            return (min_, min_)

        if self._peek() == ",":
            self._advance()
            max_digits = self._read_digits()
            if self._peek() != "}":
                return None
            self._advance()
            if max_digits == "":
                return (min_, None)
            max_ = int(max_digits)
            if max_ < min_:
                raise PortableRegexError(
                    f"Quantifier range {{{min_},{max_}}} has max < min", self._pos
                )
            return (min_, max_)

        return None

    def _read_digits(self) -> str:
        s = ""
        while not self._at_end() and self._peek() is not None and _DIGIT_RE.fullmatch(self._peek()):  # type: ignore[arg-type]
            s += self._advance()
        return s

    def _parse_atom(self) -> AstNode:
        if self._at_end():
            raise PortableRegexError("Unexpected end of pattern", self._pos)
        c = self._peek()
        assert c is not None

        if c == "(":
            return self._parse_group()
        if c == "[":
            return self._parse_class()
        if c == ".":
            self._advance()
            return DotNode()
        if c == "^":
            self._advance()
            return AnchorNode(type="start")
        if c == "$":
            self._advance()
            return AnchorNode(type="end")
        if c == "\\":
            return LiteralNode(cp=self._parse_escape(_ESCAPABLE_PUNCTUATION))
        if c in ("{", "}", "]", ")", "*", "+", "?"):
            raise PortableRegexError(f'"{c}" must be escaped to be used literally', self._pos)

        self._advance()
        return LiteralNode(cp=ord(c))

    def _parse_escape(self, allowed: frozenset[str]) -> int:
        backslash_pos = self._pos
        self._advance()  # consume '\'
        if self._at_end():
            raise PortableRegexError("Dangling escape at end of pattern", backslash_pos)
        next_c = self._advance()
        class_extra_ok = allowed is _ESCAPABLE_PUNCTUATION and next_c in _CLASS_ESCAPABLE_EXTRA
        if not (next_c in allowed or class_extra_ok):
            raise PortableRegexError(
                f'Unsupported escape "\\{next_c}" — only regex punctuation may be '
                "escaped in the portable grammar",
                backslash_pos,
            )
        return ord(next_c)

    def _parse_group(self) -> GroupNode:
        self._expect("(")
        capturing = True
        if self._peek() == "?":
            if self._peek(1) == ":":
                self._advance()
                self._advance()
                capturing = False
            else:
                raise PortableRegexError(
                    f'Unsupported group construct "(?{self._peek(1) or ""}" '
                    "— only (?:...) non-capturing groups are supported",
                    self._pos,
                )
        body = self._parse_alternation()
        self._expect(")")
        return GroupNode(capturing=capturing, body=body)

    def _parse_class(self) -> ClassNode:
        self._expect("[")
        negate = False
        if self._peek() == "^":
            self._advance()
            negate = True
        items: list[ClassItem] = []
        while True:
            if self._at_end():
                raise PortableRegexError("Unterminated character class", self._pos)
            if self._peek() == "]":
                self._advance()
                break
            cp = self._read_class_atom_code_point()
            if self._peek() == "-" and self._peek(1) not in (None, "]"):
                self._advance()  # consume '-'
                to_cp = self._read_class_atom_code_point()
                if to_cp < cp:
                    raise PortableRegexError(
                        f"Character class range out of order: {cp}-{to_cp}", self._pos
                    )
                items.append(RangeClassItem(from_cp=cp, to_cp=to_cp))
            else:
                items.append(CharClassItem(cp=cp))
        if not items:
            raise PortableRegexError("Character class must not be empty", self._pos)
        return ClassNode(negate=negate, items=tuple(items))

    def _read_class_atom_code_point(self) -> int:
        if self._peek() == "\\":
            allowed = _ESCAPABLE_PUNCTUATION | _CLASS_ESCAPABLE_EXTRA
            return self._parse_escape(allowed)
        return ord(self._advance())


def parse_portable_regex(pattern: str) -> AlternationNode:
    """Parse a portable regex pattern into an AST.

    Raises ``PortableRegexError`` on any construct outside the allowlisted
    grammar (see module docstring for the reference documentation).
    """
    return _Parser(pattern).parse()


# ---------------------------------------------------------------------------
# Portable regex compiler
# ---------------------------------------------------------------------------

_ASCII_UPPER_A = 0x41
_ASCII_UPPER_Z = 0x5A


def _is_ascii_upper(cp: int) -> bool:
    return _ASCII_UPPER_A <= cp <= _ASCII_UPPER_Z


def _to_ascii_lower(cp: int) -> int:
    return cp + 32 if _is_ascii_upper(cp) else cp


def ascii_fold(text: str) -> str:
    """ASCII-only lowercasing: folds A-Z to a-z, leaves every other code point untouched."""
    return "".join(chr(_to_ascii_lower(ord(ch))) for ch in text)


def _fold_class_item(item: ClassItem) -> ClassItem:
    if isinstance(item, CharClassItem):
        return CharClassItem(cp=_to_ascii_lower(item.cp))
    return RangeClassItem(from_cp=_to_ascii_lower(item.from_cp), to_cp=_to_ascii_lower(item.to_cp))


def _fold_node(node: AstNode) -> AstNode:
    if isinstance(node, LiteralNode):
        return LiteralNode(cp=_to_ascii_lower(node.cp))
    if isinstance(node, ClassNode):
        return ClassNode(negate=node.negate, items=tuple(_fold_class_item(i) for i in node.items))
    if isinstance(node, (DotNode, AnchorNode)):
        return node
    if isinstance(node, GroupNode):
        return GroupNode(capturing=node.capturing, body=_fold_alternation(node.body))
    if isinstance(node, QuantNode):
        return QuantNode(node=_fold_node(node.node), min=node.min, max=node.max, lazy=node.lazy)
    if isinstance(node, ConcatNode):
        return ConcatNode(parts=tuple(_fold_node(p) for p in node.parts))
    if isinstance(node, AlternationNode):
        return _fold_alternation(node)
    raise AssertionError(f"unreachable node kind: {node!r}")


def _fold_alternation(alt: AlternationNode) -> AlternationNode:
    return AlternationNode(
        options=tuple(ConcatNode(parts=tuple(_fold_node(p) for p in c.parts)) for c in alt.options)
    )


# Metacharacters in a Python `re` pattern outside a character class.
_PY_META: frozenset[str] = frozenset("\\^$.|?*+()[]{}")
# Metacharacters inside a Python `re` character class.
_PY_CLASS_META: frozenset[str] = frozenset("\\]^-")


def _emit_literal_char(cp: int, meta_set: frozenset[str]) -> str:
    ch = chr(cp)
    return f"\\{ch}" if ch in meta_set else ch


def _emit_class_item(item: ClassItem) -> str:
    if isinstance(item, CharClassItem):
        return _emit_literal_char(item.cp, _PY_CLASS_META)
    lo = _emit_literal_char(item.from_cp, _PY_CLASS_META)
    hi = _emit_literal_char(item.to_cp, _PY_CLASS_META)
    return f"{lo}-{hi}"


def _emit_quantifier(min_: int, max_: int | None) -> str:
    if min_ == 0 and max_ is None:
        return "*"
    if min_ == 1 and max_ is None:
        return "+"
    if min_ == 0 and max_ == 1:
        return "?"
    if max_ is None:
        return f"{{{min_},}}"
    if min_ == max_:
        return f"{{{min_}}}"
    return f"{{{min_},{max_}}}"


def _emit_node(node: AstNode, *, multiline: bool) -> str:
    if isinstance(node, LiteralNode):
        return _emit_literal_char(node.cp, _PY_META)
    if isinstance(node, DotNode):
        return "."
    if isinstance(node, AnchorNode):
        if node.type == "start":
            return "^"
        # Python's bare `$` also matches just before a trailing newline,
        # unlike JS's non-multiline `$` (absolute end of string only).
        # `\Z` reproduces JS's stricter semantics; in MULTILINE mode both
        # engines agree that `$` matches before every line break.
        return "$" if multiline else "\\Z"
    if isinstance(node, ClassNode):
        items = "".join(_emit_class_item(i) for i in node.items)
        return f"[{'^' if node.negate else ''}{items}]"
    if isinstance(node, GroupNode):
        inner = _emit_alternation(node.body, multiline=multiline)
        return f"({inner})" if node.capturing else f"(?:{inner})"
    if isinstance(node, QuantNode):
        inner = _emit_node(node.node, multiline=multiline)
        wrapped = f"(?:{inner})" if isinstance(node.node, (ConcatNode, AlternationNode)) else inner
        return f"{wrapped}{_emit_quantifier(node.min, node.max)}{'?' if node.lazy else ''}"
    if isinstance(node, ConcatNode):
        return "".join(_emit_node(p, multiline=multiline) for p in node.parts)
    if isinstance(node, AlternationNode):
        return _emit_alternation(node, multiline=multiline)
    raise AssertionError(f"unreachable node kind: {node!r}")


def _emit_alternation(alt: AlternationNode, *, multiline: bool) -> str:
    return "|".join(
        "".join(_emit_node(p, multiline=multiline) for p in c.parts) for c in alt.options
    )


@dataclass(frozen=True, slots=True)
class CompiledPortableRegex:
    pattern: re.Pattern[str]
    fold_subject: bool


def compile_portable_regex(
    pattern: str, *, i: bool = False, m: bool = False, s: bool = False
) -> CompiledPortableRegex:
    """Parse and compile a portable regex pattern into a native ``re.Pattern``.

    Authored ``m``/``s`` pass straight through as ``re.MULTILINE``/``re.DOTALL``.
    Authored ``i`` is never passed as a native flag (JS and Python disagree on
    Unicode case-insensitive equivalence tables); instead the AST's literal and
    class code points are ASCII-folded and the caller must ASCII-fold the
    subject the same way (``fold_subject`` signals this).
    """
    ast = parse_portable_regex(pattern)
    effective_ast = _fold_alternation(ast) if i else ast
    source = _emit_alternation(effective_ast, multiline=m)

    native_flags = re.UNICODE
    if m:
        native_flags |= re.MULTILINE
    if s:
        native_flags |= re.DOTALL

    return CompiledPortableRegex(pattern=re.compile(source, native_flags), fold_subject=i)


__all__ = [
    "AlternationNode",
    "AstNode",
    "CompiledPortableRegex",
    "PortableRegexError",
    "ascii_fold",
    "compile_portable_regex",
    "parse_portable_regex",
]
