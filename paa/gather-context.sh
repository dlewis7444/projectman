#!/usr/bin/env bash
# Generates project-snapshot.md for the Projects Admin Agent.
# Lives in .project-admin-agent/.system/; called by ProjectMan before each PAA launch.

SYSTEM_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECTS_DIR="$(cd "$SYSTEM_DIR/../.." && pwd)"
ARCHIVE_DIR="$PROJECTS_DIR/.archive"
OUT="$SYSTEM_DIR/project-snapshot.md"
STATUS_DIR="${HOME}/.claude/projectman/status"
HISTORY_FILE="${HOME}/.claude/history.jsonl"

{
  echo "# Project Snapshot"
  echo ""
  echo "Generated: $(date -Iseconds)"
  echo ""
  echo "## Active Projects"
  echo ""

  for proj_path in "$PROJECTS_DIR"/*/; do
    [ -d "$proj_path" ] || continue
    name="$(basename "$proj_path")"
    [[ "$name" == .* ]] && continue

    proj_clean="${proj_path%/}"
    tags=""

    # Git branch + dirty state
    if [ -d "$proj_path/.git" ]; then
      branch=$(git -C "$proj_path" rev-parse --abbrev-ref HEAD 2>/dev/null)
      dirty=$(git -C "$proj_path" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
      if [ "${dirty:-0}" -gt 0 ]; then
        tags="$tags [git:$branch +${dirty}]"
      else
        tags="$tags [git:$branch]"
      fi
    fi

    # Missing CLAUDE.md flag
    [ ! -f "$proj_path/CLAUDE.md" ] && tags="$tags [NO CLAUDE.md]"

    # Last Claude session date (history.jsonl timestamp is in ms)
    if [ -f "$HISTORY_FILE" ]; then
      last_date=$(grep -F "\"project\":\"$proj_clean\"" "$HISTORY_FILE" 2>/dev/null | \
        python3 -c "
import sys, json, datetime
lines = sys.stdin.readlines()
ts = max((json.loads(l).get('timestamp', 0) for l in lines if l.strip()), default=0)
if ts:
    print(datetime.datetime.fromtimestamp(ts / 1000).strftime('%Y-%m-%d'))
" 2>/dev/null)
      [ -n "$last_date" ] && tags="$tags [last:$last_date]"
    fi

    # Active status (working/waiting are notable; done/idle are not)
    if [ -d "$STATUS_DIR" ]; then
      match=$(grep -rl "\"cwd\":\"$proj_clean\"" "$STATUS_DIR" 2>/dev/null | head -1)
      if [ -n "$match" ]; then
        state=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('state',''))" "$match" 2>/dev/null)
        case "$state" in
          working|waiting) tags="$tags [ACTIVE:$state]" ;;
        esac
      fi
    fi

    echo "- **$name**$tags"
  done

  echo ""
  echo "## Archived Projects"
  echo ""
  archived_found=0
  for arch_path in "$ARCHIVE_DIR"/*/; do
    [ -d "$arch_path" ] || continue
    arch_name="$(basename "$arch_path")"
    [[ "$arch_name" == .* ]] && continue
    echo "- $arch_name"
    archived_found=1
  done
  [ "$archived_found" -eq 0 ] && echo "(none)"
  echo ""

  echo "## Summary"
  echo ""
  ACTIVE=$(find "$PROJECTS_DIR" -maxdepth 1 -mindepth 1 -type d ! -name '.*' | wc -l)
  ARCHIVED=$(find "$ARCHIVE_DIR" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l)
  echo "- Active: $ACTIVE"
  echo "- Archived: $ARCHIVED"
} > "$OUT"
