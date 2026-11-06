"""Minimal S-expression reader/writer for KiCad files.

Parsed nodes are plain Python lists. Quoted strings become `QStr` (a str
subclass) so they can be written back quoted; bare atoms stay `str`.
Top-level child spans are recorded so library files can be rewritten while
keeping untouched entries byte-for-byte.
"""
from __future__ import annotations

from typing import Iterator, List, Optional, Tuple, Union


class QStr(str):
    """A string that was quoted in the source."""


Node = Union[str, "SList"]


class SList(list):
    """A list node that remembers its source span (start, end) offsets."""

    span: Tuple[int, int] = (0, 0)

    @property
    def head(self) -> Optional[str]:
        return self[0] if self and isinstance(self[0], str) else None


def _tokenize(text: str) -> Iterator[Tuple[str, object, int]]:
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "(":
            yield ("(", None, i)
            i += 1
        elif c == ")":
            yield (")", None, i)
            i += 1
        elif c == '"':
            j = i + 1
            buf = []
            while j < n:
                ch = text[j]
                if ch == "\\" and j + 1 < n:
                    nxt = text[j + 1]
                    buf.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
                    j += 2
                    continue
                if ch == '"':
                    break
                buf.append(ch)
                j += 1
            yield ("s", QStr("".join(buf)), i)
            i = j + 1
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n()"':
                j += 1
            yield ("a", text[i:j], i)
            i = j


def parse(text: str) -> SList:
    """Parse the first top-level expression in `text`."""
    stack: List[SList] = []
    root: Optional[SList] = None
    for kind, val, pos in _tokenize(text):
        if kind == "(":
            node = SList()
            node.span = (pos, -1)
            if stack:
                stack[-1].append(node)
            stack.append(node)
        elif kind == ")":
            if not stack:
                raise ValueError(f"unbalanced ')' at {pos}")
            node = stack.pop()
            node.span = (node.span[0], pos + 1)
            if not stack:
                root = node
                break
        else:
            if not stack:
                continue
            stack[-1].append(val)
    if root is None:
        raise ValueError("no complete expression found")
    return root


def find(node: SList, name: str) -> Optional[SList]:
    for ch in node:
        if isinstance(ch, list) and ch and ch[0] == name:
            return ch  # type: ignore[return-value]
    return None


def findall(node: SList, name: str) -> List[SList]:
    return [ch for ch in node if isinstance(ch, list) and ch and ch[0] == name]  # type: ignore[misc]


def walk(node: SList, name: str) -> Iterator[SList]:
    """Yield all descendant lists whose head is `name` (depth first)."""
    for ch in node:
        if isinstance(ch, list):
            if ch and ch[0] == name:
                yield ch  # type: ignore[misc]
            yield from walk(ch, name)  # type: ignore[arg-type]


def value(node: Optional[SList], idx: int = 1, default=None):
    if node is None or len(node) <= idx:
        return default
    return node[idx]


def prop_map(node: SList) -> dict:
    """Return {name: value} of `(property "name" "value" ...)` children."""
    out = {}
    for p in findall(node, "property"):
        if len(p) >= 3:
            out[str(p[1])] = str(p[2])
    return out


def q(s: object) -> str:
    """Quote a value as a KiCad string."""
    s = str(s)
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{s}"'


def num(x: float) -> str:
    """Format a number the way KiCad does (no trailing zeros, max 6 decimals)."""
    if isinstance(x, int):
        return str(x)
    s = f"{x:.6f}".rstrip("0").rstrip(".")
    if s in ("-0", ""):
        s = "0"
    return s


def dumps(node: Node, indent: int = 0) -> str:
    """Serialise a parsed tree back to text (used for small fragments)."""
    if isinstance(node, QStr):
        return q(node)
    if isinstance(node, str):
        return node
    if isinstance(node, (int, float)):
        return num(node)
    parts = [dumps(c, indent + 1) for c in node]
    simple = all(not isinstance(c, list) for c in node)
    if simple:
        return "(" + " ".join(parts) + ")"
    pad = "\t" * (indent + 1)
    out = "(" + parts[0]
    for c, p in zip(node[1:], parts[1:]):
        if isinstance(c, list):
            out += "\n" + pad + p
        else:
            out += " " + p
    return out + "\n" + "\t" * indent + ")"
