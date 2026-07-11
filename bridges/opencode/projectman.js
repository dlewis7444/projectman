// projectman.js — ProjectMan status bridge for opencode (event → status file)
//
// Drop into ~/.config/opencode/plugins/ (install.sh does this idempotently).
// It is the opencode half of ProjectMan's *status contract*: it translates
// opencode's lifecycle events into the same per-cwd JSON schema that Claude
// Code's hook.js writes, so the sidebar status dots light up identically for
// either agent.
//
// Schema written (one file per cwd-slug under ~/.ProjectMan/status/):
//   { state: "working"|"waiting"|"done", event, cwd, ts, session }
// (idle/stopped are derived by ProjectMan from process/zellij liveness.)
//
// DEFENSIVE BY DESIGN: opencode's event-type names have drifted across
// versions (e.g. permission.asked vs permission.updated). The maps below
// react to every spelling we've seen; an unrecognised event is simply
// ignored. The plugin instance is per-directory — `input.directory`/
// `worktree` is the cwd ProjectMan spawned opencode in — so the cwd is
// taken from the closure, not parsed from events.

const fs = require("fs")
const path = require("path")
const os = require("os")

const STATUS_DIR = path.join(os.homedir(), ".ProjectMan", "status")

// Event classes (multiple spellings per class on purpose — version drift).
//
// STICKY DONE — TWO-LAYER DEFENSE keeping the dot on `done` after a turn ends.
// opencode emits post-idle noise on two distinct timescales, and a flat
// event→state map turned all of it back into `working` (knock-on: closing an
// idle project raised a false "Interrupt Active Work?"). Each layer kills one:
//
//   LAYER 1 — CLASS DEMOTION (any timescale). `session.updated` is session
//   METADATA, not turn activity. opencode 1.16.2 titles a NEW session in a
//   background LLM call after its first turn and emits a bare `session.updated`
//   on completion — round-3a traced it at +78s after `session.idle` (2378
//   samples; the session gained title "Alpha word response" at that frame),
//   pool-latency-dependent and once per session. No fixed time window can cover
//   a minute-scale event, so `session.updated` is simply NOT a working signal
//   at any offset — it falls through to ignored, no write, no file touch.
//
//   LAYER 2 — TIME WINDOW (ms-scale only). MESSAGE-class events
//   (`message.updated`, `message.part.updated`) DO mark real turns, but
//   opencode also dribbles one out as a post-idle bookkeeping tail —
//   `message.updated` at +20ms after `session.idle` in the round-2 trace
//   (the round-2 411-sample evidence also saw `session.updated` at +12ms,
//   now handled by layer 1). Idle-class events stamp `lastIdleAt`; message-
//   class events inside IDLE_STICKY_MS of that stamp are the tail and are
//   ignored. A genuine new turn arrives seconds-to-minutes later, well past
//   the window. Time-based, not "ignore the next N events", because the tail
//   shape varies between opencode versions (1.17.0 differs from 1.16.2).
//
// STRONG signals — a tool actually starting, a permission gate, `session.status`
// flipping to busy — bypass both layers and clear the stamp.
const IDLE_EVENTS = new Set(["session.idle", "session.compacted"])
// NOTE the deliberate ABSENCE of `session.updated` here: it is layer-1
// class-demoted (async title generation fires it ~+78s after idle on 1.16.2;
// round-3a trace). Only message-class events mark working.
const WEAK_WORKING_EVENTS = new Set([
  "message.updated",
  "message.part.updated",
])
const STRONG_WAITING_EVENTS = new Set(["permission.asked", "permission.updated"])
const IDLE_STICKY_MS = 2000

// `session.status` (emitted by 1.16.2, previously ignored) counts as
// idle-class when its properties indicate idle. The payload shape is not
// pinned across versions — parse defensively for the spellings we can
// anticipate; anything unrecognised stays ignored.
function isIdleStatus(props) {
  const s = props && props.status
  if (!s) return false
  if (typeof s === "string") return s === "idle"
  if (typeof s === "object") return s.type === "idle" || s.status === "idle"
  return false
}

// Symmetric with isIdleStatus: `session.status` flipping to busy is a STRONG
// working signal (an authoritative turn-start boundary). 1.16.2 emits status
// pairs (round-3a verdict); defensive parse — dead-but-harmless on versions
// that omit the busy half.
function isBusyStatus(props) {
  const s = props && props.status
  if (!s) return false
  if (typeof s === "string") return s === "busy"
  if (typeof s === "object") return s.type === "busy" || s.status === "busy"
  return false
}

// Same slug rule as Claude's hook.js so a cwd maps to one stable filename.
// KNOWN COLLISION (M-P3.4, deferred): mapping both '.' and '/' to '-' conflates
// `/p/a.b` with `/p/a/b` (same filename → sibling projects clobber each other's
// dot). A collision-safe scheme is a cross-writer contract change (this bridge +
// hook.js + a future grok hook + StatusWatcher must move in lockstep), so it has
// its own design round; until then this MUST stay byte-identical to hook.js's
// rule (the two share the status dir).
function slugFor(cwd) {
  return cwd.replace(/[\/\.]/g, "-").replace(/^-+/, "")
}

export const ProjectManStatus = async ({ directory, worktree, project }) => {
  // The cwd this opencode instance runs in. `directory` FIRST: ProjectMan
  // always spawns the agent at the project root, so `directory` IS the project
  // path StatusWatcher maps dots by. `worktree` is NOT preferred — the P2 VM
  // gate found opencode reports worktree="/" for NON-GIT project dirs (on both
  // 1.16.2 and 1.17.0), and worktree-first turned cwd into "/" → an empty slug
  // → a ".json" status file no project could ever map to (dots frozen).
  const cwd = directory || ((worktree && worktree !== "/") ? worktree : process.cwd())
  const slugPath = path.join(STATUS_DIR, slugFor(cwd) + ".json")
  // A stable per-session id if opencode hands us one (project.id is per-cwd);
  // events also carry sessionID where available (preferred when present).
  const projectSession = (project && project.id) || ""
  // When the last idle-class event landed (epoch ms; 0 = never). Weak
  // working-class events within IDLE_STICKY_MS of this are bookkeeping tail,
  // not a new turn — see the sticky-done comment above the event classes.
  let lastIdleAt = 0

  function write(state, event, session) {
    // Hard guard (same gate finding): an empty slug means cwd degenerated to
    // "/" or "" — a bare ".json" would be unmappable garbage. Never write it.
    if (!slugFor(cwd)) return
    try {
      fs.mkdirSync(STATUS_DIR, { recursive: true })
      fs.writeFileSync(
        slugPath,
        JSON.stringify({
          state,
          event,
          cwd,
          ts: Math.floor(Date.now() / 1000),
          session: session || projectSession,
        })
      )
    } catch (_) {
      // Never throw into opencode's event loop.
    }
  }

  function remove() {
    if (!slugFor(cwd)) return // same empty-slug guard as write()
    try {
      fs.unlinkSync(slugPath)
    } catch (e) {
      if (e && e.code !== "ENOENT") {
        /* swallow — best-effort cleanup */
      }
    }
  }

  return {
    // The lifecycle bus: the primary signal source.
    event: async ({ event }) => {
      if (!event || !event.type) return
      const type = event.type
      if (type === "session.deleted") {
        remove()
        return
      }
      const props = event.properties || {}
      const session = props.sessionID || (props.info && props.info.id) || ""

      // Idle-class → done, and stamp the sticky window.
      if (IDLE_EVENTS.has(type) || (type === "session.status" && isIdleStatus(props))) {
        lastIdleAt = Date.now()
        write("done", type, session)
        return
      }
      // Strong working — `session.status` flips to busy (authoritative
      // turn-start). Bypasses the sticky window + clears the stamp.
      if (type === "session.status" && isBusyStatus(props)) {
        lastIdleAt = 0
        write("working", type, session)
        return
      }
      // Strong waiting (permission gate via the bus) → bypass + clear stamp.
      if (STRONG_WAITING_EVENTS.has(type)) {
        lastIdleAt = 0
        write("waiting", type, session)
        return
      }
      // Weak working-class (message-class only) → layer-2 time window: ignored
      // inside the post-idle bookkeeping tail (`message.updated` +20ms after
      // `session.idle` in the round-2 trace), otherwise a genuine turn signal.
      // `session.updated` is NOT here — it is layer-1 class-demoted above and
      // never reaches this branch (async title-gen +78s; round-3a trace).
      if (WEAK_WORKING_EVENTS.has(type)) {
        if (Date.now() - lastIdleAt < IDLE_STICKY_MS) return
        write("working", type, session)
        return
      }
      // Anything else (incl. bare `session.updated`): unrecognised — ignored,
      // no write, no file touch.
    },
    // STRONG working signal — a tool starting means a turn is genuinely in
    // flight (often before message.updated lands). Typed hook,
    // version-stable; bypasses done-stickiness and clears the stamp.
    "tool.execute.before": async (input) => {
      lastIdleAt = 0
      write("working", "tool.execute.before", (input && input.sessionID) || "")
    },
    // STRONG waiting — some builds surface the permission gate only via the
    // typed hook. Bypasses stickiness and clears the stamp.
    "permission.ask": async (input) => {
      lastIdleAt = 0
      write("waiting", "permission.ask", (input && input.sessionID) || "")
    },
  }
}
