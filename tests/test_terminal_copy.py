from terminal_copy import Row, collapse_hard_wraps, join_rows, strip_soft_wrap_trailing


# ── strip_soft_wrap_trailing ─────────────────────────────────────────────────

def test_strip_removes_trailing_spaces():
    assert strip_soft_wrap_trailing('hello   ') == 'hello'


def test_strip_removes_trailing_tabs_and_spaces():
    assert strip_soft_wrap_trailing('hello \t \t') == 'hello'


def test_strip_preserves_leading_whitespace():
    assert strip_soft_wrap_trailing('    hello   ') == '    hello'


def test_strip_preserves_interior_whitespace():
    assert strip_soft_wrap_trailing('hi  there   ') == 'hi  there'


def test_strip_handles_empty():
    assert strip_soft_wrap_trailing('') == ''


def test_strip_handles_all_spaces():
    assert strip_soft_wrap_trailing('     ') == ''


def test_strip_does_not_touch_newline():
    # We never expect '\n' in row text, but guard the invariant.
    assert strip_soft_wrap_trailing('hi\n') == 'hi\n'


# ── join_rows ────────────────────────────────────────────────────────────────

def test_join_empty_list():
    assert join_rows([]) == ''


def test_single_row_no_newline():
    assert join_rows([Row('one line', False)]) == 'one line'


def test_single_row_preserves_trailing_space():
    assert join_rows([Row('trailing   ', False)]) == 'trailing   '


def test_all_hard_wrapped_preserves_trailing_space():
    rows = [Row('aaa   ', False), Row('bbb', False), Row('ccc  ', False)]
    assert join_rows(rows) == 'aaa   \nbbb\nccc  '


def test_all_soft_wrapped_strips_padding_and_joins():
    rows = [
        Row('first part   ', True),
        Row('second part   ', True),
        Row('third part   ', False),  # last row: no strip, no trailing newline
    ]
    assert join_rows(rows) == 'first partsecond partthird part   '


def test_mixed_wrap_types():
    rows = [
        Row('wrapped A   ', True),
        Row('wrapped B   ', False),  # hard — end of logical line
        Row('next logical line', False),
    ]
    assert join_rows(rows) == 'wrapped Awrapped B   \nnext logical line'


def test_continuation_margin_stripped_single_continuation():
    # When a row is a soft-wrap continuation, its leading whitespace is the
    # TUI's left margin and should be stripped so the logical line reunites.
    rows = [
        Row('def foo():   ', True),
        Row('    return 1', False),
    ]
    assert join_rows(rows) == 'def foo():return 1'


def test_continuation_margin_two_space_claude_style():
    # Claude-style 2-char gutter on every continuation row: strip it.
    rows = [
        Row('  first part of a long line   ', True),
        Row('  that wraps here', False),
    ]
    assert join_rows(rows) == '  first part of a long linethat wraps here'


def test_continuation_margin_common_prefix_across_multiple():
    # Common prefix across ALL continuation rows determines the strip.
    rows = [
        Row('  A long paragraph that   ', True),
        Row('    wraps with hanging   ', True),
        Row('    indent continues.', False),
    ]
    # Continuations are rows 2 and 3; both start with "    " → strip 4.
    assert join_rows(rows) == '  A long paragraph thatwraps with hangingindent continues.'


def test_continuation_margin_shortest_wins():
    # If continuations have differing indents, the common prefix (shorter) is stripped.
    rows = [
        Row('  start   ', True),
        Row('      over-indented wrap   ', True),
        Row('  back to margin', False),
    ]
    # Common prefix of "      " and "  " is "  " → strip 2 from each continuation.
    assert join_rows(rows) == '  start    over-indented wrapback to margin'


def test_continuation_margin_absent_when_no_common_prefix():
    # If any continuation has no leading whitespace, common prefix is empty.
    rows = [
        Row('aaa', True),
        Row('bbb', True),           # no leading whitespace → breaks common prefix
        Row('  ccc', False),
    ]
    assert join_rows(rows) == 'aaabbb  ccc'


def test_hard_wrapped_row_indent_unchanged():
    # A row that follows a HARD-wrapped predecessor keeps its full indent.
    rows = [
        Row('header', False),
        Row('    indented content', False),
    ]
    assert join_rows(rows) == 'header\n    indented content'


def test_margin_does_not_strip_logical_line_starts():
    # Only continuation rows lose the margin; first-of-logical-line rows keep it.
    rows = [
        Row('  line1 part1   ', True),
        Row('  line1 part2', False),       # continuation → strip "  "
        Row('  line2 all on one row', False),  # logical-line start → keep "  "
    ]
    assert join_rows(rows) == '  line1 part1line1 part2\n  line2 all on one row'


def test_whitespace_only_continuation_ignored_for_margin_detection():
    # An empty continuation row shouldn't drag the common prefix to "".
    rows = [
        Row('  start   ', True),
        Row('', True),               # whitespace-only, ignored for margin detection
        Row('  next', False),
    ]
    # Margin is "  " (from the non-empty continuation); stripped from both.
    assert join_rows(rows) == '  startnext'


def test_empty_row_hard_wrapped_becomes_blank_line():
    rows = [Row('above', False), Row('', False), Row('below', False)]
    assert join_rows(rows) == 'above\n\nbelow'


def test_empty_row_soft_wrapped_collapses():
    rows = [Row('a', True), Row('', True), Row('b', False)]
    assert join_rows(rows) == 'ab'


def test_hard_wrapped_trailing_spaces_never_stripped():
    # Regression guard: stripping happens only at soft-wrap boundaries.
    rows = [Row('line1   ', False), Row('line2', False)]
    assert join_rows(rows) == 'line1   \nline2'


def test_block_selection_shape_unchanged():
    rows = [Row('col1', False), Row('col2', False), Row('col3', False)]
    assert join_rows(rows) == 'col1\ncol2\ncol3'


def test_trailing_tab_padding_stripped_at_soft_wrap():
    rows = [Row('code\t\t', True), Row('more', False)]
    assert join_rows(rows) == 'codemore'


# ── collapse_hard_wraps (data-driven, no cols) ───────────────────────────────

def test_collapse_long_line_wrap_becomes_space():
    # max_len=90, threshold=75; preceding line (90 chars) ≥ 75 → collapse.
    text = 'X' * 90 + '\n  continuation here'
    assert collapse_hard_wraps(text) == 'X' * 90 + ' continuation here'


def test_collapse_short_preceding_line_preserved():
    # max_len=18 < 40 floor → noop, even though technically a 12-char line
    # precedes the '\n  '. Short selections are left alone.
    text = 'Short label:\n  python3 -c "..."'
    assert collapse_hard_wraps(text) == 'Short label:\n  python3 -c "..."'


def test_collapse_paragraph_break_preserved():
    text = 'X' * 90 + '.\n\n  Next paragraph starts here'
    assert collapse_hard_wraps(text) == 'X' * 90 + '.\n\n  Next paragraph starts here'


def test_collapse_four_space_indent_preserved():
    # 4-space indent (typical code indent) is left alone.
    text = 'X' * 90 + '\n    four-space indent'
    assert collapse_hard_wraps(text) == 'X' * 90 + '\n    four-space indent'


def test_collapse_three_space_wrap_also_collapses():
    # Claude Code emits '\n   ' (3 spaces) when the word-break space is
    # pushed onto the start of the continuation row. Collapse to a single
    # space to restore natural word spacing.
    text = 'X' * 90 + '\n   continuation with pushed space'
    assert collapse_hard_wraps(text) == 'X' * 90 + ' continuation with pushed space'


def test_collapse_five_plus_space_indent_preserved():
    text = 'X' * 90 + '\n     five-space indent'
    assert collapse_hard_wraps(text) == 'X' * 90 + '\n     five-space indent'


def test_collapse_three_row_wrap():
    text = 'X' * 90 + '\n  ' + 'Y' * 90 + '\n  ' + 'Z' * 10
    expected = 'X' * 90 + ' ' + 'Y' * 90 + ' ' + 'Z' * 10
    assert collapse_hard_wraps(text) == expected


def test_collapse_mixed_long_and_short():
    # max_len ~90 (first line); threshold = 75. First '\n  ' after 90-char
    # line collapses; second '\n  ' after a 34-char line stays.
    text = 'X' * 90 + '\n  continuation that is long enough' + '\n  short line'
    assert collapse_hard_wraps(text) == 'X' * 90 + ' continuation that is long enough' + '\n  short line'


def test_collapse_handles_empty():
    assert collapse_hard_wraps('') == ''


def test_collapse_handles_no_wrap_pattern():
    assert collapse_hard_wraps('hello world') == 'hello world'


def test_collapse_short_selection_is_noop():
    # max_len < 40 → no collapse, even if a '\n  ' is present.
    text = 'hi\n  there'
    assert collapse_hard_wraps(text) == 'hi\n  there'


def test_collapse_real_world_claude_example():
    text = (
        'Tests still 213 green. Before I guess at another heuristic, I want to see the actual data — there are a few things'
        '\n  that could be going wrong and they need different fixes.'
        '\n\n  Two things would help, in order of usefulness:'
        '\n\n  1. Paste the raw result into a repr() so I can see the exact bytes:'
        '\n  python3 -c "import sys; print(repr(sys.stdin.read()))"'
        '\n  then paste → Ctrl+D.'
    )
    assert collapse_hard_wraps(text) == (
        'Tests still 213 green. Before I guess at another heuristic, I want to see the actual data — there are a few things'
        ' that could be going wrong and they need different fixes.'
        '\n\n  Two things would help, in order of usefulness:'
        '\n\n  1. Paste the raw result into a repr() so I can see the exact bytes:'
        '\n  python3 -c "import sys; print(repr(sys.stdin.read()))"'
        '\n  then paste → Ctrl+D.'
    )


def test_collapse_narrative_paragraph_under_multiplexer():
    # Mirrors the user's latest repr under zellij — three wraps in a row,
    # all near the same length. VTE's cols is irrelevant; max_len wins.
    text = (
        "  Restart ProjectMan and try again. A good test selection is a multi-line wrapped paragraph from Claude's narrative"
        '\n  output. If you still see 2-space indents at wrap points, share a fresh repr — there are two more knobs (the 15-char'
        '\n  threshold, the exactly-2-spaces match) I can tune based on your terminal width, which the debug log will now print as'
        '\n  cols=....'
    )
    assert collapse_hard_wraps(text) == (
        "  Restart ProjectMan and try again. A good test selection is a multi-line wrapped paragraph from Claude's narrative"
        ' output. If you still see 2-space indents at wrap points, share a fresh repr — there are two more knobs (the 15-char'
        ' threshold, the exactly-2-spaces match) I can tune based on your terminal width, which the debug log will now print as'
        ' cols=....'
    )


def test_collapse_mixed_2_and_3_space_wraps_with_paragraph_breaks():
    # Exactly the shape from the user's latest repr: a 3-space wrap (space
    # pushed to continuation) and two 2-space wraps (space stripped by VTE),
    # with \n\n paragraph breaks between blocks.
    text = (
        '225 green, including the new test that mirrors your exact repr (4 near-full-width lines under zellij).'
        '\n\n  What changed: collapse_hard_wraps no longer takes cols. Instead it reads the max line length from the selection itself'
        '\n   as the "effective wrap width" proxy — that\'s robust to zellij/tmux eating width off the inner program\'s view.'
        '\n  Threshold is max_len - 15 with a max_len >= 40 floor to skip short selections entirely.'
        '\n\n  Restart ProjectMan and try the same wrapped paragraph. If it still misbehaves I\'ll need the new debug log ([DBG]'
        '\n  smart-copy ...) to see what rows made it into the selection and what the final result_head was.'
    )
    assert collapse_hard_wraps(text) == (
        '225 green, including the new test that mirrors your exact repr (4 near-full-width lines under zellij).'
        '\n\n  What changed: collapse_hard_wraps no longer takes cols. Instead it reads the max line length from the selection itself'
        ' as the "effective wrap width" proxy — that\'s robust to zellij/tmux eating width off the inner program\'s view.'
        ' Threshold is max_len - 15 with a max_len >= 40 floor to skip short selections entirely.'
        '\n\n  Restart ProjectMan and try the same wrapped paragraph. If it still misbehaves I\'ll need the new debug log ([DBG]'
        ' smart-copy ...) to see what rows made it into the selection and what the final result_head was.'
    )
