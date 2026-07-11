#!/usr/bin/env python3
# compat_toml.py — the idempotent TOML edit install.sh's grok step applies to
# ~/.grok/config.toml: set `[compat.claude] hooks = false` (F4).
#
# WHY (load-bearing, F4): grok reads ~/.claude/settings.json hooks BY DEFAULT,
# so Claude's hook.js would ALSO fire on grok events and fight ProjectMan's grok
# bridge for the status dot. Disabling compat.claude.hooks makes the grok bridge
# the SOLE status writer for grok sessions.
#
# Pure function (`merge_compat_claude_hooks_false`) so it is unit-testable
# headless (tests/test_grok_install_toml.py) and reused by both install.sh and
# any GUI path. It must PRESERVE every existing user key/section — install.sh
# edits a file the user owns. python3 stdlib only (tomllib read is 3.11+, so the
# WRITE side is a hand-rolled, surgical line edit — no `tomllib`/`toml` dep, and
# no full reserialize that would reorder/strip the user's file).
#
# Strategy: locate an existing `[compat.claude]` section and, within it, an
# existing `hooks =` line; rewrite that line to `hooks = false` if present,
# else insert it at the top of the section. If there is no `[compat.claude]`
# section, append one. Comments, ordering, and unrelated sections are left
# untouched. Idempotent: re-running on already-correct content is a no-op.
import re


_SECTION_RE = re.compile(r'^\s*\[([^\]]+)\]\s*(#.*)?$')
_HOOKS_RE = re.compile(r'^(\s*)hooks(\s*)=(\s*).*$')


def merge_compat_claude_hooks_false(text):
    """Return ``text`` with ``[compat.claude] hooks = false`` ensured.

    Preserves all other keys/sections/comments. Creates the file content from
    scratch when ``text`` is empty/None. Idempotent.
    """
    if not text:
        return "[compat.claude]\nhooks = false\n"

    lines = text.split("\n")
    # Find the [compat.claude] section span: from its header line to the next
    # section header (or EOF).
    section_start = None
    section_end = None  # exclusive
    for i, line in enumerate(lines):
        m = _SECTION_RE.match(line)
        if not m:
            continue
        name = m.group(1).strip()
        if section_start is None and name == "compat.claude":
            section_start = i
            continue
        if section_start is not None:
            # First section header AFTER [compat.claude] ends the span.
            section_end = i
            break
    if section_start is not None and section_end is None:
        section_end = len(lines)

    if section_start is None:
        # No [compat.claude] — append a fresh section. Ensure a clean blank-line
        # boundary so we don't glue onto a trailing key.
        out = list(lines)
        # Trim a single trailing empty element from split() if the text ended
        # with a newline, so we control the join cleanly.
        if out and out[-1] == "":
            out.pop()
        if out:  # separate from prior content with one blank line
            out.append("")
        out.append("[compat.claude]")
        out.append("hooks = false")
        out.append("")  # trailing newline
        return "\n".join(out)

    # Section exists — look for an existing `hooks =` line within it.
    hooks_idx = None
    for i in range(section_start + 1, section_end):
        if _HOOKS_RE.match(lines[i]):
            hooks_idx = i
            break

    out = list(lines)
    if hooks_idx is not None:
        m = _HOOKS_RE.match(out[hooks_idx])
        indent = m.group(1)
        out[hooks_idx] = f"{indent}hooks = false"
    else:
        # Insert `hooks = false` right after the section header.
        out.insert(section_start + 1, "hooks = false")
    return "\n".join(out)


def main():
    """CLI entry: read a config path, write it back with the edit applied.

    Usage: compat_toml.py <path-to-config.toml>
    Creates the file (and parents) when absent. Prints one of
    ``installed`` | ``already`` to stdout for install.sh's summary.
    """
    import os
    import sys
    if len(sys.argv) != 2:
        print("usage: compat_toml.py <config.toml>", file=sys.stderr)
        return 2
    path = sys.argv[1]
    try:
        with open(path) as f:
            existing = f.read()
    except FileNotFoundError:
        existing = ""
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    new = merge_compat_claude_hooks_false(existing)
    if new == existing:
        print("already")
        return 0
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(new)
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print("installed")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
