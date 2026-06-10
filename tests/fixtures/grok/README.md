# grok session-list fixtures

These pin the `GrokAdapter` session parser (`agents.parse_grok_session_list`).
Recorded from the **real** Part-0 bench probe of `grok sessions list`
(grok 0.2.39 on bench-vm, 2026-06-10) — not invented.

| File | What it models | Source |
|---|---|---|
| `sessions_list.txt` | `grok sessions list` output (two real sessions) | **verbatim** from the probe — `scripts-local/evidence/p3-grok-probe/probe.md` Q4 (the agent's verbatim transcription of the live CLI output; the two session UUIDs also appear in the hook captures `raw/hooks.log` and the probe's `summary.json` ids) |
| `sessions_list_many.txt` | same column shape, 9 rows | **extends the real shape** with synthetic rows (distinct UPDATED dates, a summary with embedded spaces, > 7 rows) so the order-newest-first and cap-7 invariants are exercised — the real capture had only two same-day sessions. The header + column layout are byte-for-byte the real probe shape; only the row values are synthetic. Marked here exactly as the opencode fixtures mark their invented rows. |

## Column shape (observed, probe Q4)

```
SESSION ID                            CREATED     UPDATED     STATUS      SUMMARY
019eb297-fa74-7741-863e-d8aa822ac7bf  2026-06-10  2026-06-10  local  Reply with exactly: hook-test-ok
```

- `SESSION ID` = UUIDv7 (36-char hyphenated; `SessionRef.id` is this verbatim — resume is `grok -r <id>`).
- `CREATED` / `UPDATED` = **date only** (`YYYY-MM-DD`, no time).
- `STATUS` = e.g. `local`.
- `SUMMARY` = free text, may contain spaces (the trailing column).
- No `--json` flag exists (probe `raw/grok-sessions-help.txt`); the text columns are the only machine route, so the parser splits the four fixed leading tokens and keeps the remainder as the title.

## Ordering

The CLI emits **newest-first by UPDATED** (probe-observed). Because UPDATED is
date-only it can't break same-day ties, but a UUIDv7 id is millisecond
time-ordered, so the parser **preserves the CLI's emission order** rather than
re-sorting by the coarse date (a date-key sort would scramble same-day rows).
`last_active` is the UPDATED date parsed to an epoch for display only; it does
not drive ordering. Cap is 7 (parity with the other expanders).
