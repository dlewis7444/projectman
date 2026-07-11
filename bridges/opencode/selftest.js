// selftest.js — synthetic, deterministic harness for the opencode status
// bridge (projectman.js). Plain node, no opencode, no real sleeps, no network.
//
//   node bridges/opencode/selftest.js
//
// Exit 0 iff every check passes; nonzero on any failure. Prints a per-check
// tally. install.sh copies ONLY projectman.js into opencode's plugin dir
// (bridges/opencode/projectman.js → ~/.config/opencode/plugins/projectman.js),
// so this file never reaches the loader — it is a dev/CI artifact only.
//
// HOW IT LOADS THE PLUGIN: projectman.js mixes `require()` (fs/path/os) with an
// ESM `export const ProjectManStatus` — opencode's loader idiom. We do not
// touch that. Instead we (1) point process.env.HOME at a throwaway sandbox so
// the module's top-level STATUS_DIR resolves inside it, then (2) read the
// source, strip the leading `export ` so the body is plain CommonJS, and
// evaluate it in a node:vm context with `require`/`module`/`exports` injected.
// Date.now is stubbed per step so timed sequences (the +12ms/+20ms tail and the
// +78s title-gen flip) are exact and instant.

const fs = require("fs")
const os = require("os")
const path = require("path")
const vm = require("vm")
const { createRequire } = require("module")

// ── sandbox HOME (must be set BEFORE the plugin module body runs, because
// STATUS_DIR is computed at module top-level from os.homedir()) ──────────────
const SANDBOX = fs.mkdtempSync(path.join(os.tmpdir(), "pm-opencode-selftest-"))
process.env.HOME = SANDBOX
// Belt and suspenders: os.homedir() prefers $HOME on Linux, but pin it so a
// stray getpwuid fallback can never write into the real ~/.ProjectMan.
os.homedir = () => SANDBOX
const STATUS_DIR = path.join(SANDBOX, ".ProjectMan", "status")

process.on("exit", () => {
  try {
    fs.rmSync(SANDBOX, { recursive: true, force: true })
  } catch (_) {}
})

// ── load projectman.js as CommonJS via vm (strip the single ESM export) ──────
const PLUGIN_SRC_PATH = path.join(__dirname, "projectman.js")
function loadPlugin() {
  let src = fs.readFileSync(PLUGIN_SRC_PATH, "utf8")
  // The file has exactly one `export` (the named handler). Strip the keyword so
  // the assignment becomes a plain top-level const we then read off the sandbox.
  src = src.replace(/^export const ProjectManStatus/m, "const ProjectManStatus")
  src += "\nmodule.exports = { ProjectManStatus }\n"
  const moduleObj = { exports: {} }
  const sandboxRequire = createRequire(PLUGIN_SRC_PATH)
  const context = vm.createContext({
    require: sandboxRequire,
    module: moduleObj,
    exports: moduleObj.exports,
    __dirname: path.dirname(PLUGIN_SRC_PATH),
    __filename: PLUGIN_SRC_PATH,
    process, // shares our patched process.env.HOME
    console,
    Date, // so our Date.now stub (set on the real global) is observed
  })
  vm.runInContext(src, context, { filename: PLUGIN_SRC_PATH })
  return moduleObj.exports.ProjectManStatus
}
const ProjectManStatus = loadPlugin()

// ── deterministic clock ──────────────────────────────────────────────────────
// at(ms) sets the clock to BASE+ms. BASE is a realistic epoch so the plugin's
// "never idle" sentinel (lastIdleAt === 0) always reads as the distant past —
// i.e. a fresh closure's weak event is NOT inside IDLE_STICKY_MS of 0. (Using a
// literal 0 here would make `Date.now() - 0 < 2000` true and wrongly swallow
// turn-1 detection — an artifact of the test clock, not the plugin.)
const BASE = 1_700_000_000_000
let NOW = BASE
const realDateNow = Date.now
Date.now = () => NOW
function at(ms) {
  NOW = BASE + ms
}

// ── helpers ──────────────────────────────────────────────────────────────────
const PROJECT_CWD = "/home/user/.ProjectMan/projects/verify-b"
function slugFor(cwd) {
  return cwd.replace(/[\/\.]/g, "-").replace(/^-+/, "")
}
function statusPathFor(cwd) {
  return path.join(STATUS_DIR, slugFor(cwd) + ".json")
}

// A fresh plugin instance == a fresh closure (lastIdleAt reset). Mirrors a new
// opencode process per project dir.
async function freshHandlers(cwd = PROJECT_CWD) {
  // ProjectManStatus is async — it returns a Promise resolving to the handlers.
  return await ProjectManStatus({ directory: cwd, worktree: undefined, project: { id: "proj-1" } })
}

async function busEvent(handlers, type, properties) {
  await handlers.event({ event: { type, properties: properties || {} } })
}

function clearStatusFile(cwd = PROJECT_CWD) {
  try {
    fs.unlinkSync(statusPathFor(cwd))
  } catch (_) {}
}
function readState(cwd = PROJECT_CWD) {
  const p = statusPathFor(cwd)
  if (!fs.existsSync(p)) return null
  return JSON.parse(fs.readFileSync(p, "utf8"))
}
function fileExists(cwd = PROJECT_CWD) {
  return fs.existsSync(statusPathFor(cwd))
}
// Write a sentinel done file whose `event` marker the plugin would never emit,
// so "done holds, no write" is provable: if the plugin writes, `event` changes.
const SENTINEL_EVENT = "__SENTINEL_DONE__"
function seedSentinelDone(cwd = PROJECT_CWD) {
  fs.mkdirSync(STATUS_DIR, { recursive: true })
  fs.writeFileSync(
    statusPathFor(cwd),
    JSON.stringify({ state: "done", event: SENTINEL_EVENT, cwd, ts: 1, session: "seed" })
  )
}
function sentinelIntact(cwd = PROJECT_CWD) {
  const s = readState(cwd)
  return !!s && s.event === SENTINEL_EVENT && s.state === "done"
}

// ── tally ─────────────────────────────────────────────────────────────────────
let passed = 0
let failed = 0
const failures = []
function check(name, cond) {
  if (cond) {
    passed++
    console.log(`  PASS  ${name}`)
  } else {
    failed++
    failures.push(name)
    console.log(`  FAIL  ${name}`)
  }
}

async function run() {
  // ===================================================================
  // BINDING SEQUENCES T-A .. T-F (spec: round-3 fix spec section).
  // Final state after the last event of each step is the assertion.
  // ===================================================================

  // T-A defect literal: message.updated(t0)→working; session.idle(t1)→done;
  // session.updated(t1+78000ms)→done holds, NO write; then
  // message.part.updated→working.
  {
    clearStatusFile()
    const h = await freshHandlers()
    at(0)
    await busEvent(h, "message.updated", { sessionID: "s1" })
    check("T-A step1 message.updated → working", readState().state === "working")

    at(2400)
    await busEvent(h, "session.idle", { sessionID: "s1" })
    check("T-A step2 session.idle → done", readState().state === "done")

    // Re-seed with a sentinel so a stray write to the same 'done' state is still
    // detectable (the plugin must NOT touch the file at all here).
    seedSentinelDone()
    at(2400 + 78000)
    await busEvent(h, "session.updated", { sessionID: "s1" })
    check("T-A step3 session.updated(+78s) → done holds, no write", sentinelIntact())

    at(2400 + 78000 + 10)
    await busEvent(h, "message.part.updated", { sessionID: "s1" })
    check("T-A step4 message.part.updated → working", readState().state === "working")
  }

  // T-B round-2 regression: session.idle(t)→done; session.updated(+12ms)→done
  // holds; message.updated(+20ms)→done holds (inside the 2000ms window).
  {
    clearStatusFile()
    const h = await freshHandlers()
    at(5000)
    await busEvent(h, "session.idle", { sessionID: "s2" })
    check("T-B step1 session.idle → done", readState().state === "done")

    seedSentinelDone()
    at(5012) // +12ms
    await busEvent(h, "session.updated", { sessionID: "s2" })
    check("T-B step2 session.updated(+12ms) → done holds, no write", sentinelIntact())

    at(5020) // +20ms, still inside IDLE_STICKY_MS — must be swallowed
    await busEvent(h, "message.updated", { sessionID: "s2" })
    check("T-B step3 message.updated(+20ms) → done holds (window)", sentinelIntact())
  }

  // T-C class-not-window: FRESH plugin (no idle ever), bare session.updated →
  // NO working write, no file touch at all.
  {
    clearStatusFile()
    const h = await freshHandlers()
    at(9000)
    await busEvent(h, "session.updated", { sessionID: "s3" })
    check("T-C bare session.updated (fresh) → no file touch", !fileExists())
  }

  // T-D no over-demotion: fresh state, message.updated → working
  // (turn-1 detection preserved).
  {
    clearStatusFile()
    const h = await freshHandlers()
    at(11000)
    await busEvent(h, "message.updated", { sessionID: "s4" })
    check("T-D fresh message.updated → working", readState().state === "working")
  }

  // T-E real next turn: done, then message.part.updated(+120s) → working.
  {
    clearStatusFile()
    const h = await freshHandlers()
    at(20000)
    await busEvent(h, "session.idle", { sessionID: "s5" })
    check("T-E step1 session.idle → done", readState().state === "done")

    at(20000 + 120000) // +120s, far outside the window
    await busEvent(h, "message.part.updated", { sessionID: "s5" })
    check("T-E step2 message.part.updated(+120s) → working", readState().state === "working")
  }

  // T-F status pair: session.status{idle}→done; session.status{busy}(+50ms)→
  // working (strong: bypasses the window, clears the stamp).
  {
    clearStatusFile()
    const h = await freshHandlers()
    at(30000)
    await busEvent(h, "session.status", { status: "idle", sessionID: "s6" })
    check("T-F step1 session.status{idle} → done", readState().state === "done")

    at(30050) // +50ms — well inside IDLE_STICKY_MS; busy is STRONG, must win
    await busEvent(h, "session.status", { status: "busy", sessionID: "s6" })
    check("T-F step2 session.status{busy}(+50ms) → working (bypasses window)", readState().state === "working")
  }

  // ===================================================================
  // FULL MAPPING + GUARD COVERAGE (replaces the unrecoverable T-G round-2
  // checks: every current mapping and guard in projectman.js is exercised).
  // ===================================================================

  // Idle-class spellings → done.
  {
    clearStatusFile()
    const h = await freshHandlers()
    at(40000)
    await busEvent(h, "session.idle", {})
    check("MAP session.idle → done", readState().state === "done")
  }
  {
    clearStatusFile()
    const h = await freshHandlers()
    at(40100)
    await busEvent(h, "session.compacted", {})
    check("MAP session.compacted → done", readState().state === "done")
  }

  // session.status idle shapes: string and object (.type and .status).
  {
    clearStatusFile()
    const h = await freshHandlers()
    at(41000)
    await busEvent(h, "session.status", { status: "idle" })
    check("MAP session.status string 'idle' → done", readState().state === "done")
  }
  {
    clearStatusFile()
    const h = await freshHandlers()
    at(41100)
    await busEvent(h, "session.status", { status: { type: "idle" } })
    check("MAP session.status {type:'idle'} → done", readState().state === "done")
  }
  {
    clearStatusFile()
    const h = await freshHandlers()
    at(41200)
    await busEvent(h, "session.status", { status: { status: "idle" } })
    check("MAP session.status {status:'idle'} → done", readState().state === "done")
  }

  // session.status busy shapes: string and object (.type and .status).
  {
    clearStatusFile()
    const h = await freshHandlers()
    at(42000)
    await busEvent(h, "session.status", { status: "busy" })
    check("MAP session.status string 'busy' → working", readState().state === "working")
  }
  {
    clearStatusFile()
    const h = await freshHandlers()
    at(42100)
    await busEvent(h, "session.status", { status: { type: "busy" } })
    check("MAP session.status {type:'busy'} → working", readState().state === "working")
  }
  {
    clearStatusFile()
    const h = await freshHandlers()
    at(42200)
    await busEvent(h, "session.status", { status: { status: "busy" } })
    check("MAP session.status {status:'busy'} → working", readState().state === "working")
  }

  // session.status with unrecognised payload → neither (no file touch).
  {
    clearStatusFile()
    const h = await freshHandlers()
    at(43000)
    await busEvent(h, "session.status", { status: "spinning" })
    check("MAP session.status unrecognised → no file touch", !fileExists())
  }

  // Strong waiting via the bus: permission.asked / permission.updated.
  {
    clearStatusFile()
    const h = await freshHandlers()
    at(44000)
    await busEvent(h, "permission.asked", { sessionID: "w1" })
    check("MAP permission.asked → waiting", readState().state === "waiting")
  }
  {
    clearStatusFile()
    const h = await freshHandlers()
    at(44100)
    await busEvent(h, "permission.updated", { sessionID: "w2" })
    check("MAP permission.updated → waiting", readState().state === "waiting")
  }
  // Strong waiting bus event clears the stamp / bypasses the window: after idle,
  // a permission event must write waiting even inside IDLE_STICKY_MS.
  {
    clearStatusFile()
    const h = await freshHandlers()
    at(45000)
    await busEvent(h, "session.idle", {})
    at(45050) // inside window
    await busEvent(h, "permission.updated", {})
    check("MAP permission.updated bypasses idle window → waiting", readState().state === "waiting")
  }

  // Weak working-class: message.part.updated outside any window → working.
  {
    clearStatusFile()
    const h = await freshHandlers()
    at(46000)
    await busEvent(h, "message.part.updated", {})
    check("MAP message.part.updated (fresh) → working", readState().state === "working")
  }

  // Typed hooks: tool.execute.before (strong working) and permission.ask
  // (strong waiting). Both must clear the stamp / bypass the window.
  {
    clearStatusFile()
    const h = await freshHandlers()
    at(47000)
    await h["tool.execute.before"]({ sessionID: "t1" })
    check("HOOK tool.execute.before → working", readState().state === "working")
  }
  {
    clearStatusFile()
    const h = await freshHandlers()
    at(47500)
    await busEvent(h, "session.idle", {})
    at(47550) // inside window
    await h["tool.execute.before"]({ sessionID: "t1" })
    check("HOOK tool.execute.before bypasses idle window → working", readState().state === "working")
  }
  {
    clearStatusFile()
    const h = await freshHandlers()
    at(48000)
    await h["permission.ask"]({ sessionID: "p1" })
    check("HOOK permission.ask → waiting", readState().state === "waiting")
  }
  {
    clearStatusFile()
    const h = await freshHandlers()
    at(48500)
    await busEvent(h, "session.idle", {})
    at(48550) // inside window
    await h["permission.ask"]({ sessionID: "p1" })
    check("HOOK permission.ask bypasses idle window → waiting", readState().state === "waiting")
  }

  // session.deleted → status file removed.
  {
    clearStatusFile()
    const h = await freshHandlers()
    at(49000)
    await busEvent(h, "message.updated", {}) // create the file first
    check("DEL precondition file exists", fileExists())
    await busEvent(h, "session.deleted", {})
    check("DEL session.deleted → file removed", !fileExists())
  }

  // Empty-slug guard: cwd "/" → empty slug → write() and remove() are no-ops.
  {
    const rootSlugFile = path.join(STATUS_DIR, slugFor("/") + ".json") // ".json"
    try {
      fs.unlinkSync(rootSlugFile)
    } catch (_) {}
    const h = await ProjectManStatus({ directory: "/", worktree: undefined, project: { id: "x" } })
    at(50000)
    await busEvent(h, "message.updated", {})
    // No bare ".json" written, and no real-slug file under PROJECT_CWD either.
    check("GUARD empty-slug → no '.json' write", !fs.existsSync(rootSlugFile))
  }

  // Empty/garbage bus events are ignored (no throw, no file touch).
  {
    clearStatusFile()
    const h = await freshHandlers()
    at(51000)
    await h.event({}) // no event
    await h.event({ event: {} }) // no type
    await busEvent(h, "totally.unknown.event", {})
    check("ROBUST malformed/unknown bus events → no file touch", !fileExists())
  }

  // session field falls back to project.id when the event carries no sessionID.
  {
    clearStatusFile()
    const h = await freshHandlers()
    at(52000)
    await busEvent(h, "message.updated", {}) // no sessionID/info
    check("FIELD session falls back to project.id", readState().session === "proj-1")
  }
  // session field prefers event sessionID when present.
  {
    clearStatusFile()
    const h = await freshHandlers()
    at(52100)
    await busEvent(h, "message.updated", { sessionID: "ev-sess" })
    check("FIELD session prefers event sessionID", readState().session === "ev-sess")
  }

  // ── done ──
  console.log("")
  console.log(`Tally: ${passed} passed, ${failed} failed (${passed + failed} checks)`)
  if (failed) {
    console.log("Failures: " + failures.join("; "))
  }
  // restore (cosmetic; process exits next)
  Date.now = realDateNow
  process.exit(failed ? 1 : 0)
}

run().catch((e) => {
  console.error("selftest crashed:", e && e.stack ? e.stack : e)
  process.exit(2)
})
