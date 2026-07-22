from __future__ import annotations

import re
from collections.abc import Iterator

_FENCE_OPEN = re.compile(r"^(?: {0,3})(`{3,}|~{3,})")
_MATH = re.compile(r"\$\$(.+?)\$\$|(?<!\$)\$([^$\n]+?)\$(?!\$)", re.S)


def canonicalize_math_delimiters(source: str) -> str:
    """Convert TeX ``\\(...\\)``/``\\[...\\]`` delimiters to Markdown math.

    CommonMark treats the backslashes before parentheses and brackets as ordinary
    escapes.  Canonicalizing before Markdown tokenization prevents those delimiters
    from disappearing.  Fenced and inline code are deliberately left untouched.
    """

    if "\\(" not in source and "\\[" not in source:
        return source

    output: list[str] = []
    prose: list[str] = []
    fence: tuple[str, int] | None = None

    def flush_prose() -> None:
        if prose:
            output.append(_canonicalize_prose("".join(prose)))
            prose.clear()

    for line in source.splitlines(keepends=True):
        match = _FENCE_OPEN.match(line)
        if fence is not None:
            output.append(line)
            if match:
                marker = match.group(1)
                if marker[0] == fence[0] and len(marker) >= fence[1]:
                    fence = None
            continue
        if match:
            flush_prose()
            marker = match.group(1)
            fence = (marker[0], len(marker))
            output.append(line)
            continue
        prose.append(line)
    flush_prose()
    return "".join(output)


def math_fragments(value: str) -> Iterator[tuple[str, str]]:
    """Yield ``text``, ``inline`` and ``display`` fragments from Markdown math."""

    value = canonicalize_math_delimiters(value)
    cursor = 0
    for match in _MATH.finditer(value):
        if match.start() > cursor:
            yield "text", value[cursor : match.start()]
        display, inline = match.groups()
        yield ("display", display.strip()) if display is not None else ("inline", inline)
        cursor = match.end()
    if cursor < len(value):
        yield "text", value[cursor:]


def _canonicalize_prose(value: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(value):
        if value[index] == "`":
            run_end = index + 1
            while run_end < len(value) and value[run_end] == "`":
                run_end += 1
            delimiter = value[index:run_end]
            closing = _find_code_close(value, delimiter, run_end)
            if closing is not None:
                end = closing + len(delimiter)
                output.append(value[index:end])
                index = end
                continue

        opener = None
        closer = None
        replacement = None
        if value.startswith(r"\(", index) and not _is_escaped(value, index):
            opener, closer, replacement = r"\(", r"\)", "$"
        elif value.startswith(r"\[", index) and not _is_escaped(value, index):
            opener, closer, replacement = r"\[", r"\]", "$$"
        if opener and closer and replacement:
            closing = _find_unescaped(value, closer, index + len(opener))
            if closing is not None:
                body = value[index + len(opener) : closing]
                if opener == r"\(" and "\n" in body:
                    output.append(value[index])
                    index += 1
                    continue
                output.extend((replacement, body, replacement))
                index = closing + len(closer)
                continue

        output.append(value[index])
        index += 1
    return "".join(output)


def _find_code_close(value: str, delimiter: str, start: int) -> int | None:
    cursor = start
    while True:
        position = value.find(delimiter, cursor)
        if position < 0:
            return None
        before = position > 0 and value[position - 1] == "`"
        after = position + len(delimiter) < len(value) and value[position + len(delimiter)] == "`"
        if not before and not after:
            return position
        cursor = position + len(delimiter)


def _find_unescaped(value: str, token: str, start: int) -> int | None:
    cursor = start
    while True:
        position = value.find(token, cursor)
        if position < 0:
            return None
        if not _is_escaped(value, position):
            return position
        cursor = position + len(token)


def _is_escaped(value: str, position: int) -> bool:
    backslashes = 0
    cursor = position - 1
    while cursor >= 0 and value[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1
