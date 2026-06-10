#!/usr/bin/env node
// hook.js — Claude Code hook handler (per-project status files)
// Reads hook event JSON from stdin, writes/deletes ~/.ProjectMan/status/<slug>.json
// (the agent-neutral status dir, Decision 2). ProjectMan's StatusWatcher also
// reads the legacy ~/.claude/projectman/status/ dir during the deprecation
// window, so an un-updated install keeps working.
const fs = require('fs')
const path = require('path')
const os = require('os')

const STATUS_DIR = path.join(os.homedir(), '.ProjectMan', 'status')

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

    // KNOWN COLLISION (M-P3.4, deferred): this rule maps both '.' and '/' to
    // '-', so `/p/a.b` and `/p/a/b` produce the SAME status filename and two
    // such sibling projects clobber each other's dot. A collision-safe slug
    // (distinct separators or a hash) is a cross-writer contract change — every
    // status writer (this hook, the opencode bridge, a future grok hook) and
    // StatusWatcher's reader must move in lockstep — so it has its own design
    // round. Until then any slug change here MUST be mirrored in
    // bridges/*/slugFor and model.StatusWatcher.
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
