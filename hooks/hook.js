#!/usr/bin/env node
// hook.js — Claude Code hook handler (per-project status files)
// Reads hook event JSON from stdin, writes/deletes ~/.claude/projectman/status/<slug>.json
const fs = require('fs')
const path = require('path')
const os = require('os')

const STATUS_DIR = path.join(os.homedir(), '.claude', 'projectman', 'status')

const STATE = {
  SessionStart: 'done',      Stop: 'done',
  UserPromptSubmit: 'working', PreToolUse: 'working',
  PostToolUse: 'working',    PostToolUseFailure: 'working',
  Notification: 'waiting',   PermissionRequest: 'waiting',
}

// Safety timeout: exit after 1 second if no stdin
const timeout = setTimeout(() => process.exit(0), 1000)

let input = ''
process.stdin.setEncoding('utf8')
process.stdin.on('data', chunk => { input += chunk })
process.stdin.on('end', () => {
  clearTimeout(timeout)
  try {
    const event = JSON.parse(input)
    const eventName = event.hook_event_name || ''
    const cwd = event.cwd || ''
    if (!cwd) return

    fs.mkdirSync(STATUS_DIR, { recursive: true })

    const slug = cwd.replace(/[\/\.]/g, '-').replace(/^-+/, '')
    const slugPath = path.join(STATUS_DIR, slug + '.json')

    if (eventName === 'SessionEnd') {
      try { fs.unlinkSync(slugPath) } catch (e) { if (e.code !== 'ENOENT') throw e }
      return
    }

    const state = STATE[eventName]
    if (!state) return

    const status = {
      state,
      event: eventName,
      cwd,
      ts: Math.floor(Date.now() / 1000),
      session: event.session_id || '',
    }
    if (event.tool_name) status.tool = event.tool_name

    fs.writeFileSync(slugPath, JSON.stringify(status))
  } catch (_) {
    // Silently ignore parse errors and write failures
  }
})
