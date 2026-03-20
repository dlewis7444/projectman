#!/usr/bin/env bash
# Generates project-snapshot.md for the Projects Admin Agent.
# Called by ProjectMan before each PAA launch.

PROJECTS_DIR="$(dirname "$0")/.."
OUT="$(dirname "$0")/project-snapshot.md"

{
  echo "# Project Snapshot"
  echo ""
  echo "Generated: $(date -Iseconds)"
  echo ""
  echo "## Project Listing"
  echo ""
  echo '```'
  ls -1 "$PROJECTS_DIR" | grep -v '^\.'
  echo '```'
  echo ""
  echo "## Active Project Count"
  echo ""
  ACTIVE=$(ls -1 "$PROJECTS_DIR" | grep -v '^\.' | wc -l)
  ARCHIVED=0
  if [ -d "$PROJECTS_DIR/.archive" ]; then
    ARCHIVED=$(ls -1 "$PROJECTS_DIR/.archive" 2>/dev/null | wc -l)
  fi
  echo "- Active: $ACTIVE"
  echo "- Archived: $ARCHIVED"
} > "$OUT"
