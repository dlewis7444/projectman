"""Soft-wrap-aware clipboard joining for terminal selections.

Pure module, no GTK/Vte imports. The GTK layer (terminal.py) is responsible
for extracting rows and their wrap status from VTE; this module just joins
them into the final clipboard string.

The problem this solves: TUIs often pad each visual row with trailing spaces
to style the background. When VTE soft-wraps those rows, the padding ends
up embedded mid-string when the selection is copied. We strip the padding
only at soft-wrap boundaries so wrapped logical lines paste as one
continuous line, while leaving hard-terminated rows untouched.
"""

import os.path
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Row:
    text: str
    soft_wrapped: bool


def strip_soft_wrap_trailing(text: str) -> str:
    return text.rstrip(' \t')


def _leading_ws(text: str) -> str:
    stripped = text.lstrip(' \t')
    return text[:len(text) - len(stripped)]


def _continuation_margin(rows: list[Row]) -> str:
    """Longest common leading-whitespace prefix across continuation rows.

    A continuation row is one whose immediate predecessor is soft-wrapped —
    i.e., a row that continues a logical line from the row above. TUIs like
    Claude render a left gutter (e.g., 2 spaces) on every row including
    continuations, so this common prefix is almost certainly the gutter.
    Stripping it on the join reunites the logical line without injecting
    spaces at wrap boundaries.

    Whitespace-only continuation rows are skipped (they carry no signal).
    """
    indents = []
    for i in range(1, len(rows)):
        if not rows[i - 1].soft_wrapped:
            continue
        text = rows[i].text
        if not text.strip(' \t'):
            continue
        indents.append(_leading_ws(text))
    if not indents:
        return ''
    return os.path.commonprefix(indents)


def join_rows(rows: list[Row]) -> str:
    if not rows:
        return ''
    margin = _continuation_margin(rows)
    parts = []
    last = len(rows) - 1
    prev_soft = False
    for i, row in enumerate(rows):
        text = row.text
        if prev_soft and margin and text.startswith(margin):
            text = text[len(margin):]
        if i == last:
            parts.append(text)
        elif row.soft_wrapped:
            parts.append(strip_soft_wrap_trailing(text))
        else:
            parts.append(text)
            parts.append('\n')
        prev_soft = row.soft_wrapped
    return ''.join(parts)


# Matches '\n' + 2 or 3 leading spaces (not 4+), not preceded by another
# '\n' (which would be a paragraph break). Claude Code's CLI hard-wraps with
# either:
#   - `\n  ` (2 spaces = margin; word-break space ate the row's trailing slot
#     and got stripped by VTE)
#   - `\n   ` (3 spaces = margin + absorbed word-break space that didn't fit
#     on the previous row, pushed to the start of the continuation)
# Both get replaced with a single space to restore natural word spacing.
# 4+ leading spaces are left alone (code indents, nested list continuations).
_HARD_WRAP_RE = re.compile(r'(?<!\n)\n {2,3}(?! )')


def collapse_hard_wraps(text: str) -> str:
    """Collapse `\\n  ` wrap artifacts emitted by TUIs that hard-wrap at a
    2-space hanging indent (Claude Code, etc.).

    Approach is data-driven — the "wrap width" is inferred from the maximum
    line length in the selection itself rather than from the outer terminal's
    column count. This is important because a multiplexer (zellij, tmux) can
    make the VTE width substantially larger than the effective wrap width
    seen by the inner program.

    A `\\n  ` is collapsed to a single space only when the preceding line's
    length is close to the observed max (within 15 chars of word-break slack).
    Paragraph breaks (`\\n\\n  ...`), 3+ space indents, and tab indents are
    all left untouched by construction.
    """
    lens = [len(line) for line in text.split('\n') if line.strip(' \t')]
    if not lens:
        return text
    max_len = max(lens)
    if max_len < 40:
        # Selection is too short for meaningful wrap detection; leave alone.
        return text
    threshold = max_len - 15

    def replace(match: re.Match) -> str:
        line_start = text.rfind('\n', 0, match.start()) + 1
        line_len = match.start() - line_start
        if line_len >= threshold:
            return ' '
        return match.group(0)

    return _HARD_WRAP_RE.sub(replace, text)
