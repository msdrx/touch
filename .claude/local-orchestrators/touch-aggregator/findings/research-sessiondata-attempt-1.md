# Research findings — PERSISTED SESSION DATA MODEL (attempt 1)

Perspective: what an aggregator can read from `~/.claude`, when it is written,
what is redacted or spilled out-of-band, and what a "terminal view" can and
cannot be reconstructed from.

All claims below are from primary sources: the live session transcript
`~/.claude/projects/-home-laniakea-Projects-touch/dd469822-2546-47d9-aaa3-31db4cb705e8.jsonl`
(297→300+ lines, written while this research ran), the exited session
`…/e144bb01-a2e6-4eac-a79b-acb96925c084.jsonl` (17 lines), the eight subagent
transcripts under `…/dd469822…/subagents/`, `~/.claude/sessions/622.json`,
`~/.claude.json`, `~/.claude/file-history/`, `~/.claude/history.jsonl`, and
`grep -a` / `dd` reads of the CLI binary
`/home/agent/.local/share/claude/versions/2.1.220` (v2.1.220, GIT_SHA
`4073f59596e272f39393db4f96abc5f4b10eff21`, BUILD_TIME `2026-07-24T22:17:45Z`).

---

## Reference: the record-type taxonomy (ground truth, from the binary)

Proof: `grep -a -o -E 'nB_=\{.{0,2400}'` on the binary, byte offset 259354279.
The CLI classifies every transcript record type into one of four buckets, and
uses that classification when it rewrites a transcript:

| bucket | record types |
|---|---|
| `transcript` | `user`, `assistant`, `system`, `attachment` |
| `boundary-cleared` | `progress`, `file-history-snapshot`, `file-history-delta`, `last-prompt`, `marble-origami-commit`, `marble-origami-snapshot`, `marble-origami-reset` |
| `accumulate` | `content-replacement`, `fork-context-ref`, `frame-link` |
| `last-wins` | `summary`, `custom-title`, `ended-by-model`, `ai-title`, `tag`, `relocated`, `agent-name`, `agent-color`, `agent-setting`, `pr-link`, `bridge-session`, `attribution-snapshot`, `mode`, `permission-mode`, `isolation-latch`, `worktree-state`, `queue-operation`, `observer-ref` |

Adjacent to it: `aB_=new Set(["bash_progress","powershell_progress","mcp_progress","repl_tool_call","tool_heartbeat","agent_api_retry"])` — the *ephemeral*
`progress` payload kinds. Other progress kinds seen in code: `agent_progress`,
`skill_progress`, `workflow_log`.

Sibling files the CLI itself enumerates (same grep region, retention sweep):
`<sessionId>.jsonl`, `<sessionId>.cast`, `<sessionId>.ccr-tip.json`,
`<sessionId>.precompact.json`, `*.ccr-tip.json.tmp.*`,
`*.precompact.json.tmp.*`, `*.jsonl.compact.tmp.*`, and the directory
`<sessionId>/` (subagents + workflows).

Writer internals (binary, offset 259265400, class `wsp`):
`FLUSH_INTERVAL_MS = 100`, `MAX_CHUNK_BYTES = 104857600`,
`backstopThresholdBytes = tbr = 20971520`, `eB_ = 5242880` (min size for a
transcript rewrite), `xI = 65536` (tail-read window; metadata re-appended every
`xI/2` = 32 KiB written), `ZF_ = 52428800` (max size for the fallback
whole-file rewrite), `Z2o = !1` (transcript-compaction gate, default **off**).

---

## SESSIONDATA-1 — `usage` is duplicated on every split assistant record; naive summing over-counts tokens 2.09x

**Proof / severity:** `blocker`.
`~/.claude/projects/-home-laniakea-Projects-touch/dd469822-2546-47d9-aaa3-31db4cb705e8.jsonl`
lines 10, 11, 13 (one API response, `requestId` `req_011CdMzWfroAZexCFHM4FjYy`,
`message.id` `msg_011CdMzWh9BpxjV25rjwnVDN`) are three separate `assistant`
records — `['text']`, `['tool_use']`, `['tool_use']` — and **all three carry the
identical `usage` object** (`output_tokens: 232`, `input_tokens: 2`,
`cache_read_input_tokens: 20628`).

Measured over the whole file: 50 of 63 `(requestId, message.id)` groups contain
more than one record; `sum(output_tokens)` over records = **115 605**,
deduped by `message.id` = **55 396** — a **2.09x** over-count.

**Scenario:** Touch's per-agent / per-session token counter and any derived cost
figure are roughly double the truth, and the error grows with how many tool
calls a turn makes (so the *most* active agents look worst). The bug is silent —
the numbers look plausible.

**Recommendation:** key usage into a map by `message.id`, falling back to
`uuid`, then to `path+lineno` for id-less rows, and sum the map's values — not
the records. `/home/laniakea/Projects/touch/.claude/shared/monitoring/decision_watcher.py:154-197`
(`agent_tokens`) already implements exactly this and is battle-tested; reuse it
verbatim rather than re-deriving. Input volume = `input_tokens +
cache_creation_input_tokens + cache_read_input_tokens`; keep cache read/write
broken out, because cache reads dominate and cost ~10x less.

---

## SESSIONDATA-2 — thinking text is never persisted: every `thinking` block on disk has `thinking: ""`

**Proof / severity:** `blocker` (for the "faithful terminal view" claim).
Across **all nine** transcripts in this project, every `thinking` content block
has an empty `thinking` string and only a non-empty `signature`:

```
main session          44/44 empty
agent-a4e343a0f7d73268c  25/25 empty
agent-a483cae616edffe81  20/20 empty
6 workflow subagents     10–12 each, all empty
```

(first example: `dd469822….jsonl` line 15,
`{"type":"thinking","thinking":"","signature":"CAIS3AIKhwEIEBgC…"}`) — and this
is with `~/.claude/settings.json` `"alwaysThinkingEnabled": true`, i.e. the case
where the terminal shows the *most* reasoning.

**Scenario:** the README's "main terminal is web view over claude code session"
implies the browser shows what the terminal shows. The terminal renders the
model's reasoning; the transcript contains a placeholder. Any Touch pane that
promises "thinking" from persisted data renders an empty box for every turn.

**Recommendation:** do not offer a thinking pane sourced from the transcript.
The one honest thing derivable is *elapsed* thinking: the gap between the
previous record's `timestamp` and the `thinking` record's `timestamp` (block
records are written when the block completes — see SESSIONDATA-5), so render
"thought for 8s" as a collapsed marker. If real reasoning text is a product
requirement, it can only come from owning the PTY (SESSIONDATA-12) — decide
this before designing the terminal view, not after.

---

## SESSIONDATA-3 — the transcript is **not** append-only: the CLI truncates and whole-file-rewrites it. Byte-offset tailing will break.

**Proof / severity:** `blocker` for any offset-based tailer.
Binary offset 259265400 onwards, class `wsp`:

- `performRemoveByUuid(e,t)` opens the file `r+`, reads the last `xI` (64 KiB),
  finds `"uuid":"<uuid>"`, then `await n.truncate(A)` and rewrites the tail
  after the removed line. **The file shrinks.** If the uuid is not in the last
  64 KiB and the file is under `ZF_` (50 MiB), it falls back to
  `readFile → filter → writeFile` — a full rewrite. Over 50 MiB it logs
  "Skipping tombstone removal: session file too large" and gives up.
- `performCompactTranscript(e)` writes `${file}.compact.tmp.${randomBytes(4).hex}`
  and renames over the original, **dropping every line before the
  `compact_boundary` except the preserved segment**. It is armed when
  `bytesSinceCompact >= 20971520` and the file is `>= 5242880` bytes.
  Currently gated by `Z2o = !1` (off), but `Z2o` has a setter, so it can be
  turned on by a config/gate flip in any release or remotely.

**Scenario:** Touch's aggregator tails from a saved byte offset. A user hits
esc-esc / rewinds / deletes a message; the file shrinks below the saved offset;
the reader's next `seek(offset)` reads garbage or nothing and the terminal pane
silently freezes for the rest of the session. Under the compaction gate, the
same thing happens with no user action at all.

**Recommendation:** never trust a bare byte offset. Persist
`(st_dev, st_ino, size, offset)`; on every poll, if inode changed **or**
`size < offset`, discard state and re-read the file from 0. Key every record by
`uuid` and make the in-memory model idempotent on re-ingest (upsert-by-uuid,
not append), so a full re-read is a no-op for the UI. Handle
`<file>.compact.tmp.*` appearing next to the transcript as "rewrite in flight —
back off 200ms and re-read".

---

## SESSIONDATA-4 — `mode`, `permission-mode`, `ai-title`, `last-prompt` etc. are re-appended state, not events

**Proof / severity:** `major`.
In a 297-line transcript: `mode` ×20, `permission-mode` ×20, `last-prompt` ×19,
`ai-title` ×15 — for a session whose mode never changed. The binary
(`planReAppendSessionMetadata`, offset ~259270500) re-appends the current value
of `last-prompt`, `custom-title`, `ai-title`, `tag`, `relocated`, `agent-name`,
`agent-color`, `agent-setting`, `mode`, `permission-mode`, `isolation-latch`,
`worktree-state`, `pr-link`, `bridge-session` every time
`bytesSinceMetadataReAppend >= xI/2` (32 KiB), deduping only against the last
32 KiB of the file.

**Scenario:** a naive aggregator that maps records to timeline events shows the
session flipping permission mode 20 times, and shows 15 "title changed" entries
for one title. Worse, the *first* `ai-title` in the file is not the current one —
reading forward and stopping at the first match yields a stale title.

**Recommendation:** classify by the table at the top of this document.
`last-wins` types: keep only the **last** occurrence, keyed by
`type:sessionId` (for `summary`, key by `type:leafUuid` — that is the CLI's own
key function `I($)`). `boundary-cleared` types: keep, but never render as user-
visible events. Only `user`/`assistant`/`system`/`attachment` belong on a
timeline.

---

## SESSIONDATA-5 — writes are batched (100 ms) into a single large append; torn tails are guaranteed, and latency is ~100 ms

**Proof / severity:** `major` (correctness) — and a *positive* result on latency.

Latency, measured live: a polling loop started inside a Bash tool call saw the
`assistant`/`tool_use` record **for that very call** land **+0.10 s** after the
poll began (`timestamp 2026-07-25T03:01:33.485Z`), i.e. before the tool had
finished. So intra-turn visibility is real and sub-second — good enough to drive
a live view without any extra instrumentation.

Correctness: `drainQueuesOnce` (binary, offset ~259268400) concatenates queued
entries into one string `o` and issues **one** `gc.appendFile(e, o)` per batch,
splitting only at `MAX_CHUNK_BYTES = 104857600` (100 MiB). Observed max line
length in this project: 46 567 bytes (`agent-a82d2e2591c84a3d7.jsonl`),
38 899 bytes in the main transcript. A reader polling every 100–250 ms will
routinely observe a batch mid-write.

**Scenario:** the aggregator reads to EOF and `json.loads` the last chunk;
it raises on a half-written 46 KB line, and (if the reader advances its offset
regardless) that record is lost forever, so a tool call shows as issued but
never completed.

**Recommendation:** read to EOF, cut the buffer at the **last** `\n`, keep the
remainder in memory for the next poll, and advance the offset only by the
consumed prefix. Decode with `errors="ignore"`/incremental decoder — a cut can
land mid-UTF-8. `decision_watcher.py:471` (`read whole journal lines appended
since offset; defer a torn tail`) is the existing implementation of this
pattern; copy its semantics.

---

## SESSIONDATA-6 — `/clear` starts a **new sessionId and a new file** under the same pid; and the transcript file is created lazily

**Proof / severity:** `major`.
The CLI's own command definition (binary, `name:"clear"`):
`description:"Start a new session with empty context; previous session stays on
disk (resumable with /resume)"`. So `/clear` does **not** truncate or rotate the
existing file — it leaves it and begins writing a different
`<newSessionId>.jsonl` in the same project directory, while `process.pid` is
unchanged.

Laziness, measured: a throwaway interactive run under
`/tmp/claude-1000/…/scratchpad/clearprobe` produced
`~/.claude.json → projects[…].lastSessionId = "98c68dbd-7d15-431f-a35d-ef124694c151"`
but **no `.jsonl` at all** in
`~/.claude/projects/-tmp-claude-1000-…-clearprobe/` (only an empty `memory/`).
A session that exists has no transcript until it records its first message.

**Scenario:** the sidebar pins terminal "touch-2b" to sessionId
`dd469822…`; the user types `/clear`; the pane goes permanently silent because
the process is now writing to a different file. Symmetrically, a freshly opened
terminal appears in the sidebar with a session id but 404s on its transcript,
and the aggregator reports an error instead of an empty terminal.

**Recommendation:** the sidebar's unit of identity must be the **pid**
(`~/.claude/sessions/<pid>.json`), not the sessionId. Re-read
`sessions/<pid>.json` on every poll; when `sessionId` changes, close the old
tail, open the new file, and render a "context cleared" divider rather than
losing the pane. Treat `ENOENT` on the transcript as "session live, no messages
yet", not as an error.

---

## SESSIONDATA-7 — `~/.claude/sessions/<pid>.json` is not a heartbeat and is written non-atomically

**Proof / severity:** `major`.
`~/.claude/sessions/622.json` at the time of measurement:

```
{"pid":622,"sessionId":"dd469822-…","cwd":"/home/laniakea/Projects/touch",
 "startedAt":1784946693282,"procStart":"10028","version":"2.1.220",
 "peerProtocol":1,"kind":"interactive","entrypoint":"cli","name":"touch-2b",
 "nameSource":"derived","status":"busy","updatedAt":1784948171977,
 "statusUpdatedAt":1784948171977}
```

`date +%s` = `1784949034` — the file was **863 s (14.4 min) stale** while the
session was actively running six subagents. Polled every 0.3 s for 18 s: zero
changes. The writer (binary, `iHt(e)`) is
`readFile → {...n, ...e} → writeFile` — a read-modify-write with **no**
temp+rename, so a concurrent reader can observe a truncated or empty file.

`procStart` is `/proc/<pid>/stat` field 22: verified,
`awk '{print $22}' /proc/622/stat` → `10028`.

Exited sessions are reaped: the earlier session `e144bb01…` has **no** pid file,
only `622.json` remains.

**Scenario 1:** Touch shows a session as "stale / possibly dead" because
`updatedAt` is 14 minutes old, while it is in fact the busiest one on the box.
**Scenario 2:** Touch's sidebar poll catches a zero-byte `622.json` mid-write,
`json.loads` throws, and the whole session list blanks for one frame.

**Recommendation:** liveness = `os.path.exists(f"/proc/{pid}")` **and**
`field22(/proc/pid/stat) == procStart` (guards pid reuse). Never use
`updatedAt`/`statusUpdatedAt` for liveness — only for "last known state
change". Wrap every registry read in try/except with one immediate retry, and
keep the previous good value on failure. Use `name`/`nameSource` for the
sidebar label and `cwd` for grouping; `kind` (`interactive`/`bg`) tells you
which panes are user-drivable.

---

## SESSIONDATA-8 — there is no session-end record in the transcript

**Proof / severity:** `major`.
`e144bb01-a2e6-4eac-a79b-acb96925c084.jsonl` is a complete, cleanly `/exit`-ed
session. Its final line (17) is a plain `user` record
`<local-command-stdout>Goodbye!</local-command-stdout>`. There is no
terminator, no status, no summary. A crashed session is byte-for-byte
indistinguishable from one that is merely idle.

The CLI *has* the concept — the `SessionEnd` hook fires with a reason from
`D4l = ["clear","resume","logout","prompt_input_exit","other","bypass_permissions_disabled"]`
— but nothing is written to the JSONL.

**Scenario:** the sidebar shows five "running" terminals; four of the
processes died an hour ago. The per-terminal graph shows an agent stuck at
"running" forever because the final record never arrived.

**Recommendation:** derive ended-ness from process liveness (SESSIONDATA-7),
not from the file, and label it as inferred ("ended — process gone"). For a
*precise* end reason, install a `SessionEnd` hook that appends to Touch's own
event log — that is the only place `reason` is available. Also read
`~/.claude.json → projects[cwd].lastGracefulShutdown` as a weak corroborating
signal (observed `true` for the clean probes, `false` for the killed one).

---

## SESSIONDATA-9 — an async `Workflow` run has **no** completion record in the parent transcript

**Proof / severity:** `major`.
`dd469822….jsonl:296`, the `toolUseResult` for the `Workflow` tool call at
line 295:

```json
{"status":"async_launched","taskId":"wpbwj76b3","taskType":"local_workflow",
 "workflowName":"touch-aggregator-research","runId":"wf_829e6f58-b2f",
 "summary":"Research how to build Touch …",
 "transcriptDir":"/home/agent/.claude/projects/-home-laniakea-Projects-touch/dd469822-…/subagents/workflows/wf_829e6f58-b2f",
 "scriptPath":"…/orch-scripts/research.workflow.js"}
```

That is the *only* record the parent transcript will ever contain about that
run. Contrast the synchronous `Agent` tool (lines 207, 215), whose result
carries `status:"completed"`, `agentId`, `agentType`, `resolvedModel`,
`totalDurationMs`, `totalTokens`, `totalToolUseCount`, a full `usage` object
(with `iterations[]`) and `toolStats` (`readCount`, `bashCount`,
`editFileCount`, `linesAdded`, `linesRemoved`, …).

**Scenario:** Touch renders the n8n-style graph from the parent transcript.
Every `/execute-research` and `/implement-plan` run — i.e. exactly the loops the
README says Touch must visualise and control — appears as a single node that
never completes, with zero children and zero tokens.

**Recommendation:** for `Workflow` results, take `runId` + `transcriptDir` from
the tool result and switch data sources: watch
`<transcriptDir>/journal.jsonl` for the run graph and
`<transcriptDir>/agent-<agentId>.jsonl` for each node's detail. This is exactly
what `.claude/shared/monitoring/decision_watcher.py` already does — the
aggregator should absorb it rather than reimplement it.

---

## SESSIONDATA-10 — the workflow journal has no timestamps, and its `result` payload is not JSON

**Proof / severity:** `major`.
`…/subagents/workflows/wf_829e6f58-b2f/journal.jsonl`, live:

```
{"type":"started","key":"v2:c13a866b…","agentId":"a2fc883c96ff7b837"}
…
{"type":"result","key":"v2:03b353da…","agentId":"a82d2e2591c84a3d7",
 "result":"{'findings': [{'id': 'AGENTGRAPH-2', 'file': '…', 'line': 141, …"}
```

Two facts: (a) **neither** `started` nor `result` carries a timestamp — the
only fields are `type`, `key`, `agentId`, and `result`; (b) `result` is a
Python-`repr`-style string with single quotes — `json.loads` on it fails.

**Scenario 1:** the graph cannot show node durations or a Gantt/waterfall,
because nothing in the journal says when anything happened.
**Scenario 2:** an aggregator that does `JSON.parse(entry.result)` to pull out
the agent's structured findings throws on every completed node.

**Recommendation:** derive per-node timing from the agent transcript
(`agent-<agentId>.jsonl` first record `timestamp` = start, last record
`timestamp` = end) and use the journal only for topology and terminal state —
which is what `decision_watcher.py:306` (`completion timestamp for an agent
whose journal result just landed`) already does. Treat `result` as an opaque
display string; never parse it. Poll the journal by size+offset with the
torn-tail rule from SESSIONDATA-5.

---

## SESSIONDATA-11 — workflow subagent `.meta.json` omits `description`/`toolUseId`; the only label is the `[monitor]` prompt marker

**Proof / severity:** `major`.
Direct `Agent` spawn —
`…/subagents/agent-a4e343a0f7d73268c.meta.json`:

```json
{"agentType":"general-purpose","description":"Assess control and UI feasibility",
 "toolUseId":"toolu_017UzEDnR28ARKERuMw2PGwX","spawnDepth":1,"model":"opus"}
```

Workflow-spawned agent —
`…/subagents/workflows/wf_829e6f58-b2f/agent-a74f0c93253253ef5.meta.json`
(all six are byte-identical, 63 bytes):

```json
{"agentType":"workflow-subagent","spawnDepth":1,"model":"opus"}
```

There is no `description`, no `toolUseId`, and nothing tying the agent to a
plan/stage/role. The only discriminator on disk is the marker the orchestrator
embeds in the prompt, visible as the first line of the agent's first `user`
record — e.g. `agent-a2fc883c96ff7b837.jsonl:1`:
`"\n[monitor] plan=research stage=sessiondata role=research attempt=1\n…"`.

**Scenario:** Touch's graph renders six identical nodes labelled
"workflow-subagent", indistinguishable from each other — the exact opposite of
the README's per-stage UML view.

**Recommendation:** label workflow nodes by parsing
`^\[monitor\] plan=(\S+) stage=(\S+) role=(\S+) attempt=(\d+)` out of the first
`user` record of `agent-<id>.jsonl`. This is already the normative protocol
(`.claude/shared/monitoring/monitoring.md`, `decision_watcher.py:115` "generic
plug-in protocol"). Fall back to `agentType` + first 60 chars of the prompt.
Do **not** build the labelling on `.meta.json`.

---

## SESSIONDATA-12 — a faithful terminal replay is not reconstructible; the built-in asciicast recorder is present but not reachable

**Proof / severity:** `major` (product-defining).

*Missing from the transcript* (verified by enumerating every content block and
record type in all nine files): ANSI/SGR output, streaming text deltas
(records land only when a block **completes** — SESSIONDATA-5), thinking text
(SESSIONDATA-2), the spinner/status line, permission dialogs, the system prompt,
and the terminal's own truncation of long tool output.

*Present and richer than the terminal*: the **full, untruncated** tool result —
`Read` results carry `{filePath, content, numLines, startLine, totalLines}`
(line 17), `Write`/`Edit` results carry `content`/`oldString`/`newString` plus a
`structuredPatch`, `Bash` results carry full `stdout`/`stderr` plus
`interrupted` and `noOutputExpected` flags (largest observed single result:
17 086 chars). The terminal shows "… +N lines"; the transcript has all of it.

*The recorder that would solve this*: the binary contains a complete asciicast
v2 recorder (module `a7f`, byte offset 264552421) —
`installAsciicastRecorder` monkey-patches `process.stdout.write`, emits
`[t,"o",data]` frames and `[t,"r","<cols>x<rows>"]` on resize, flushes every
500 ms / 50 frames / 10 MiB, and `renameRecordingForSession` moves the file to
`~/.claude/projects/<slug>/<sessionId>-<timestamp>.cast`. The env var
`CLAUDE_CODE_TERMINAL_RECORDING` is declared (`De.str()`).
**But it did not activate.** Three throwaway probes under
`/tmp/claude-1000/…/scratchpad/`: `CLAUDE_CODE_TERMINAL_RECORDING=1 claude -p`,
`CLAUDE_CODE_TERMINAL_RECORDING=$D/rec.cast claude -p`, and
`CLAUDE_CODE_TERMINAL_RECORDING=1` under a real pty (`pty.fork` + interactive
UI confirmed rendering) — `find ~/.claude/projects -name '*.cast'` returns
nothing in every case. The literal string appears only twice in the binary,
both in env-schema tables, so nothing appears to read it in 2.1.220.

**Scenario:** the README's "main terminal is web view over claude code session"
is read as "replay the terminal". Building on the transcript yields a view that
is visibly *not* the terminal (no colour, no live typing, no dialogs); building
on `.cast` yields a view that renders nothing at all.

**Recommendation:** decide explicitly, and write the decision into the plan:
**(a)** define Touch's terminal as a *semantic re-render* of the transcript
(prompt / text / tool-call cards / result cards / turn dividers) — buildable
today, sub-second, and strictly richer in content than the TTY; or **(b)** if
byte-faithful replay and *driving* the session are required, Touch must own the
PTY (spawn `claude` under a pty it controls) and treat `~/.claude` as
metadata only. Do not plan around `CLAUDE_CODE_TERMINAL_RECORDING`; it is
unverified in this build. Keep the `.cast` filename convention
(`<sessionId>-<ts>.cast`, asciicast v2) in mind only as a forward-compatible
ingest path if it ever starts producing files.

---

## SESSIONDATA-13 — retention deletes both the transcript and the whole subagent tree

**Proof / severity:** `major`.
Binary, byte offset ~262552400 (`GDb`): the periodic sweep walks
`~/.claude/projects/*/`, and for every file older than the retention cutoff
whose name ends in `.jsonl`, `.cast`, `.ccr-tip.json`, `.precompact.json` (or
matching the `.tmp.` variants) it unlinks the file; when the deleted file is a
`.jsonl` it additionally does
`n.rm(path.join(a, d), {recursive:true, force:true})` — i.e. it removes the
entire `<sessionId>/` directory containing `subagents/` and
`subagents/workflows/<runId>/`.

**Scenario:** Touch is sold as the place you look at past runs. A month later
the graphs are empty, because the CLI garbage-collected the only copy — and
`CLAUDE.md`'s own rule ("Never delete a finished task folder or its
`events.jsonl` — completed runs are monitor history") is silently violated by a
process Touch does not control.

**Recommendation:** the aggregator must **own** its history: project every
ingested record into Touch's own append-only store (or reuse the existing
`.claude/local-orchestrators/<task>/events.jsonl` model) at ingest time, and
treat `~/.claude` strictly as a live tap. Never make a UI feature depend on a
`~/.claude` file still existing.

---

## SESSIONDATA-14 — one content block per record: UI grouping must be by `(requestId, message.id)`

**Proof / severity:** `minor`.
Measured across all nine transcripts: `blocks-per-record` is `{1: N}` for every
single file — the CLI never writes a multi-block `content` array. A single API
response is therefore 1–4 records (max observed: 4 records sharing one
`requestId`; 63 distinct `requestId`s for 125 assistant records). `tool_result`
blocks likewise arrive one per `user` record.

Also observed: `is_error` is present on only 52 of 68 `tool_result` blocks —
absent means "not an error", not "unknown".

**Scenario:** a renderer that treats each `assistant` record as a separate
"message" shows one model turn as three stacked bubbles — text, then two
orphaned tool cards — and any "messages" count is inflated ~2x.

**Recommendation:** group assistant records by `message.id` (`requestId` is a
1:1 alias in every observed case) into a single turn object, then render its
blocks in file order. Treat missing `is_error` as `false`. Use
`message.stop_reason` (`tool_use` = more coming, `end_turn` = turn over,
`null` = still streaming/interrupted) as the turn's terminal state, and the
`system`/`turn_duration` record (`durationMs`, `messageCount`) as the
authoritative end-of-turn marker — 6 such records in this session, e.g. line 71
`{"type":"system","subtype":"turn_duration","durationMs":90511,"messageCount":51,…}`.

---

## SESSIONDATA-15 — transcripts contain unredacted file contents, command output and prompts; serving them is an exfiltration surface

**Proof / severity:** `major` (security).
Nothing in the transcript is redacted. `Read` results embed whole file bodies;
`Write`/`Edit` results embed `content`, `oldString`, `newString`; `Bash`
results embed full `stdout`/`stderr` (this session's transcript includes
`ls -la ~/.claude` output and the full text of `~/.claude/settings.json`).
`~/.claude/history.jsonl` stores every prompt the user ever typed verbatim,
including `pastedContents`, across all projects — e.g. the line
`{"display":"sbx run --template docker.io/sadradze/claude-net10-sbx:v1 claude . …"}`.
`~/.claude/.credentials.json` sits in the same tree (mode 600).

The sandbox rule from `CLAUDE.md` compounds this: Touch's dev server must bind
`0.0.0.0`, and the user is told to `sbx ports … --publish` it.

**Scenario:** Touch binds `0.0.0.0:8931` with no auth (as
`monitor_server.py` does today), the port is published, and anyone who can
reach the host reads every file the agent has ever opened plus the full prompt
history.

**Recommendation:** require a shared secret on every endpoint that returns
transcript content (query token or `Authorization` header, compared with
`hmac.compare_digest`), generated at startup and printed once; keep the
existing extension-whitelist + `realpath` containment from
`monitor_server.py`; never expose `~/.claude/history.jsonl`,
`~/.claude/.credentials.json`, `~/.claude.json`, or `~/.claude/shell-snapshots/`
through any route. Scope every path parameter to a single resolved project
slug — do not accept an absolute path from the client.

---

## SESSIONDATA-16 — `progress` records are in the schema but never appeared on disk; do not build a live tool-progress pane on them

**Proof / severity:** `minor`.
The type table classifies `progress` as `boundary-cleared`, and the binary emits
`{type:"progress", toolUseID, data:{type:"bash_progress"|"mcp_progress"|
"tool_heartbeat"|"agent_progress"|"skill_progress"|"workflow_log"|…}}` — the
heartbeat fires every `fIs = 30000` ms per running tool, and `agent_progress`
carries `agentId`, `agentType`, `description`, `prompt`, `parentToolUseID`.
Empirically: `grep -c '"type": *"progress"'` across **every** `.jsonl` under
`~/.claude/projects` returns **zero matches** — including sessions that ran two
`Agent` calls, one `Workflow`, and 51 `Bash` calls.

**Scenario:** the plan promises a "what is this bash doing right now" strip and
a per-agent progress bar sourced from `progress` records; the panes are always
empty, and the failure is only discovered at integration.

**Recommendation:** treat `progress` as an optional enrichment only — parse it
if present, never depend on it. Derive "tool still running" from the absence of
a matching `tool_result` for a `tool_use_id` plus wall-clock since the
`tool_use` record's timestamp. If real intra-tool progress is needed, install a
`PreToolUse`/`PostToolUse` hook writing to Touch's own event log.

---

## SESSIONDATA-17 — hooks, not polling, are the deterministic push channel; the plan should budget for them

**Proof / severity:** `major` (design decision, currently unmade).
The binary's hook-event list (adjacent to `D4l`, byte ~247427000) includes:
`PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PostToolBatch`,
`Notification`, `UserPromptSubmit`, `UserPromptExpansion`, `SessionStart`,
`SessionEnd`, `Stop`, `StopFailure`, `SubagentStart`, `SubagentStop`,
`PreCompact`, `PostCompact`, `PermissionRequest`, `PermissionDenied`, `Setup`,
`TeammateIdle`, `TaskCreated`, `TaskCompleted`, `Elicitation`,
`ElicitationResult`, `ConfigChange`, `WorktreeCreate`, `WorktreeRemove`,
`InstructionsLoaded`, `CwdChanged`, `FileChanged`, `DirectoryAdded`,
`MessageDisplay`.

Several of these carry information that **exists nowhere in the transcript**:
`SessionEnd` reason (SESSIONDATA-8), `PermissionRequest`/`PermissionDenied`
(the terminal's permission dialogs), `SubagentStart`/`SubagentStop`
(deterministic subagent lifecycle, independent of the journal),
`PreCompact`/`PostCompact` (advance warning of the file rewrite in
SESSIONDATA-3).

**Scenario:** the aggregator is built as a pure poller; it then cannot show
permission prompts (a hard requirement if the browser is the *primary* UI, since
that is where the user must answer them), cannot distinguish exit from crash,
and gets surprised by transcript rewrites.

**Recommendation:** make hook installation a first-class, explicit part of the
aggregator design — a `settings.json` hook block per watched project that
appends normalised events to Touch's own log. Note this changes user config, so
it must be opt-in and reversible; use the `update-config` skill's conventions.
Polling stays as the fallback for sessions started before the hooks were
installed.

---

## SESSIONDATA-18 — `~/.claude.json` is the wrong place to read cost or the current session, and a dangerous place to write

**Proof / severity:** `minor`.
`~/.claude.json → projects["/home/laniakea/Projects/touch"]`:
`lastSessionId = "e144bb01-…"` — the session that **exited at 02:31**, not the
live `dd469822-…`; `lastCost = 0`, `lastTotalInputTokens = 0`,
`lastTotalOutputTokens = 0`, `lastModelUsage = {}` even after a full session.
The fields are written at shutdown and are zero under this auth mode. The file
is a single 42 KB global JSON rewritten constantly — `~/.claude/backups/`
contains five `.claude.json.backup.<epoch_ms>` snapshots inside a four-minute
window.

**Scenario:** Touch labels the sidebar from `lastSessionId` and shows the wrong
(dead) session as current; the cost column reads `$0.00` for everything.

**Recommendation:** current session ⇒ `~/.claude/sessions/<pid>.json`. Cost and
tokens ⇒ computed from `message.usage`, deduped per SESSIONDATA-1, priced from
a Touch-owned model→price table. Never write to `~/.claude.json` — a
read-modify-write race there can corrupt the user's whole CLI config.

---

## SESSIONDATA-19 — file-history gives real pre-edit contents, but is keyed by sessionId and dies with it

**Proof / severity:** `minor`.
`file-history-delta` records (e.g. `dd469822….jsonl:63`) carry
`{messageId, snapshotMessageId, trackingPath, backup:{backupFileName, version,
backupTime, realParentDir}, timestamp}`, and the bodies live at
`~/.claude/file-history/<sessionId>/<pathHash>@v<N>` — verified: five files
including `587ee69deb912a91@v2` / `@v3` whose contents are the successive
versions of `CLAUDE.md`. `file-history-snapshot` records carry the
`trackedFileBackups` map for a message id. `backupFileName: null, version: 1`
means "file did not exist before" (create).

Combined with `structuredPatch` in the `Edit`/`Write` `toolUseResult`, this is
enough for a full before/after diff view per edit.

**Scenario:** Touch offers "show me what this agent changed", then loses it —
the directory is keyed by sessionId, so `/clear` orphans it, and the retention
sweep in SESSIONDATA-13 removes the transcript that referenced it.

**Recommendation:** if a diff view is in scope, render it from
`toolUseResult.structuredPatch` (self-contained, inside the transcript) as the
primary source and use `file-history/` only as an optional "full original file"
enrichment, resolving `<sessionId>` from the record's own session, not the
current one.

---

## SESSIONDATA-20 — the prompt queue is visible (`queue-operation` + `attachment/queued_command`), but is `last-wins`

**Proof / severity:** `nit`.
`dd469822….jsonl:206`
`{"type":"queue-operation","operation":"enqueue","timestamp":…,"sessionId":…,"content":"can we read subagents info? …"}`
and `:216`
`{"type":"attachment","attachment":{"type":"queued_command","prompt":"…","commandMode":"prompt","origin":{"kind":"human"},"timestamp":…}}`.
Four `queue-operation` records in this session.

This is directly useful for the README's "primary user interface" ambition —
Touch can show what the user has queued while the agent is busy. But
`queue-operation` is classified `last-wins`, so a transcript rewrite keeps only
the most recent one.

Other attachment kinds available for context panes: `deferred_tools_delta`
(tool roster), `agent_listing_delta` (available subagent types + descriptions),
`skill_listing` (the skill roster, i.e. where `execute-research` /
`implement-plan` are advertised), `command_permissions` (`allowedTools`),
`task_reminder`.

**Recommendation:** render the queue from the live tail only; do not attempt to
reconstruct queue history from an old transcript. Use `skill_listing` /
`agent_listing_delta` to populate the "which loops can I start" control surface
rather than hardcoding skill names.

---

## Summary of what a terminal view **can** and **cannot** be

**Can** (all sub-second, no extra instrumentation):
user prompts (raw text, including slash-command `<command-name>` and bash-mode
`<bash-input>`/`<bash-stdout>` forms), assistant text, every tool call with its
full input, every tool result with full untruncated output, per-turn duration,
per-response token usage, model id, `effort`, `attributionSkill`, cwd, git
branch, permission mode, session title (`ai-title`/`custom-title`), prompt
queue, file edits with patches, the full subagent tree (`isSidechain`,
`agentId`, `spawnDepth`, `parentUuid`), and workflow topology from
`journal.jsonl`.

**Cannot**: model reasoning text, streaming/partial output, ANSI styling,
permission dialogs, the status line, the system prompt, intra-tool progress,
an explicit session end, or anything after the CLI's retention sweep.
