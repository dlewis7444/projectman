# grok session-list fixtures

These pin the Grok harness session parser (`harnesses.parse_grok_session_list`).
Shapes match live `grok sessions list` output (column layout from grok 0.2.x).

| File | What it models | Source |
|---|---|---|
| `sessions_list.txt` | `grok sessions list` output (two sessions) | real CLI column layout |
| `sessions_list_many.txt` | same column shape, 9 rows | extends the real shape with synthetic rows (distinct UPDATED dates, a summary with embedded spaces, > 7 rows) so order-newest-first and cap-7 are exercised |

## Column shape

```
SESSION ID                            CREATED     UPDATED     STATUS      SUMMARY
019eb297-fa74-7741-863e-d8aa822ac7bf  2026-06-10  2026-06-10  local  Reply with exactly: hook-test-ok
```

- `SESSION ID` = UUIDv7 (36-char hyphenated; `SessionRef.id` is this verbatim — resume is `grok -r <id>`).
- `CREATED` / `UPDATED` = **date only** (`YYYY-MM-DD`, no time).
- `STATUS` = e.g. `local`.
- `SUMMARY` = free text, may contain spaces (the trailing column).
- No `--json` flag; the text columns are the machine route, so the parser splits the four fixed leading tokens and keeps the remainder as the title.

## Ordering

The CLI emits **newest-first by UPDATED**. Because UPDATED is date-only it can't
break same-day ties, but a UUIDv7 id is millisecond time-ordered, so the parser
**preserves the CLI's emission order** rather than re-sorting by the coarse date.
`last_active` is the UPDATED date parsed to an epoch for display only; it does
not drive ordering. Cap is 7 (parity with the other expanders).
