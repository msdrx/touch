# Settling probes — evidence record

**Item:** R-04 (touch-full-recon plan) + the Mongo provisioning appendix
required by R-38 (touch-mongo-live amendment).
**Run:** 2026-07-26, 23:48Z–23:57Z UTC, inside the sandbox
(`/home/laniakea/Projects/touch`), Claude Code CLI **2.1.220**.

This file is an **evidence artifact, not a plan**. Every entry follows
AUDIT-16's provenance convention: the exact command, the date it was run, and
the raw result. Where a probe was not settled, it says so in those words — a
probe with no result is a recorded open question, never an assumption.

Nothing here is a decision. R-34…R-37 (control plane) and the GD-19 branch cite
this file; the decisions themselves stay in the plans.

## Environment (2026-07-26T23:48:18Z)

| fact | value | command |
|---|---|---|
| CLI | `2.1.220 (Claude Code)` at `/home/agent/.local/bin/claude` | `claude --version` |
| Python | `3.13.7 (main, Mar 3 2026, 12:19:54) [GCC 15.2.0]` | `python3 -VV` |
| pymongo | `4.17.0` at `~/.local/lib/python3.13/site-packages/pymongo/` | `python3 -c "import pymongo;print(pymongo.version)"` |
| dnspython | present, `~/.local/lib/python3.13/site-packages/dns/` | `python3 -c "import dns;print(dns.__file__)"` |
| host mongod / mongosh | **absent** (`which mongod mongosh` → nothing) | `which mongod mongosh` |
| docker | `/usr/bin/docker`, image `mongo:7` present locally | `docker images` |

---

## Probe 1 — hook hot-reload into an already-running session

**Question (AUDIT-3, PLANS-8; GD-19 branches on it):** can a hook be added to a
session's settings *after* the session started and take effect without a
restart? If yes, Touch can install a deterministic stop/pause gate into an
**observed** session it did not spawn.

**Method.** One long-lived process fed two turns over stream-json, so the
"session" is provably the same across the settings edit:

```bash
# driver: hook_probe.py (scratchpad), 2026-07-26T23:51:33Z
claude -p --input-format stream-json --output-format stream-json --verbose \
       --permission-mode acceptEdits \
       --settings /tmp/touch-probe-hooks/probe-settings.json
```

- Turn 1 (`echo turn1`) ran with **no hooks** in either settings source.
- Between turns the driver wrote a `PreToolUse`/`Bash` hook
  (`printf 'x' >> <marker>`) into **both** sources: the project
  `.claude/settings.json` *and* the file passed to `--settings`.
- Turn 2 (`echo turn2`) ran in the same process.

**Result — hot-reload WORKS, from both settings sources.**

```
turn1 result: success | session: b08154dc-17c4-4e3e-8d8e-6847c72b3aef
  markers after turn1: proj=False arg=False
hooks written mid-session at 23:51:40Z
turn2 result: success
  markers after turn2: proj=True arg=True          # each marker exactly 1 byte
```

Each marker file was **1 byte** — the hook fired exactly once per tool call, no
double-fire, no replay of turn 1. A fresh process started afterwards with the
same settings also fired both hooks (restart arm, `T3`), so the hot-reload path
is additive to the normal path, not a substitute for it.

**Consequences.**
- GD-19 takes the **"hot-reload works"** branch: the hook gate is available for
  observed sessions, and R-36's hook pack does not need the owned-session
  spawner (T9 slice) pulled forward.
- The honest pause verb (`inception.md` §4, GD-4) has a delivery path for
  sessions Touch did not spawn — still per-agent, still effective only at the
  next tool boundary, and still nothing the CLI itself calls "pause".
- **Security note, unprompted by any item:** the same run printed
  `Ignoring 1 permissions.allow entry from .claude/settings.json: this
  workspace has not been trusted` — yet the **hook from that same untrusted
  file executed**. Permission entries are trust-gated; hook commands were not.
  Treat "a settings file in the project" as arbitrary code execution when
  reasoning about GD-13/GD-27 postures.

## Probe 2 — hooks under an interactive (PTY) session via `--settings`

**Question:** does the `--settings` hook path work in the *interactive* TUI, or
only under `-p`? Touch's owned-session tier hosts a real PTY, so this is the
mode that matters for v1 controls.

**Method** (`pty_hook_probe.py`, 2026-07-26T23:55:32Z): real `pty.openpty()`,
`claude --settings /tmp/touch-probe-hooks/pty-settings.json --permission-mode
acceptEdits`, typed instruction, waited for the marker file.

**Result — hooks fire under an interactive PTY.**

```
marker fired under PTY: True | bytes: 1
PTYDONE seen in output: True
```

**First attempt failed and is recorded because its failure is the finding:**
the very first run of this probe hit the *trust dialog* ("Is this a project you
created or one you trust?"), which consumed the typed prompt as its menu answer;
no tool ran and no marker appeared. A driver that types into a fresh workspace
must expect that dialog. The same first attempt also printed, in the TUI status
line:

```
⚠ Transcript saving is off — inherited CLAUDE_CODE_CHILD_SESSION marker
```

which confirms first-hand, this run, the spawn-hygiene rule in `inception.md`
§5: a child session that inherits `CLAUDE_CODE_CHILD_SESSION` writes **no
transcript**, and would starve Touch's entire read side. The retry ran under
`env -u CLAUDE_CODE_CHILD_SESSION`.

## Probe 3 — one settings file mixing `command` and `http` hook types

**Question:** is a settings file with both hook types accepted, and does an
unreachable `http` hook break the `command` hook beside it?

**Method:** the `--settings` file used in probe 1's turn 2 carried, under one
`PreToolUse`/`Bash` matcher, a `command` hook **and** an `http` hook pointed at
`http://127.0.0.1:9/never` (a closed port), `timeout: 2`.

**Result — accepted; the command hook still fired; nothing surfaced.** The turn
completed `success`, the marker was written (1 byte), and the stream carried
`"permission_denials":[]` with no hook error, no `ECONNREFUSED`, and no mention
of the unreachable URL anywhere in the output stream.

Read this as: mixed hook arrays are legal, and an `http` hook failing silently
is the *default* behaviour. A Touch hook must therefore never be the only
evidence that something happened — the file-append `command` form stays the
deterministic channel (GD-29), and any future `http` push channel needs its own
liveness signal because the CLI will not report its absence.

## Probe 4 — `time claude agents --json`

**Question (MONITORING-3 context):** is the registry CLI cheap enough to poll,
and does it carry what the session key needs?

```bash
$ time claude agents --json      # 2026-07-26T23:49Z
real 0m0.335s   user 0m0.280s   sys 0m0.118s
```

```json
[ { "pid": 15934, "cwd": "/home/laniakea/Projects/touch", "kind": "interactive",
    "startedAt": 1784987605035, "sessionId": "06e081e6-…", "name": "touch-36",
    "status": "busy" } ]
```

**Result — fast (0.34 s), but it is NOT a substitute for reading
`~/.claude/sessions/`.** The JSON has `startedAt` (epoch ms) and **no
`procStart`**, while the registry file for the same pid has
`procStart: "4101211"` (the `/proc/<pid>/stat` field-22 clock-tick string).
`procStart` is half of Touch's session key `(pid, procStart)` and the only
value that makes pid reuse detectable, so the session arm keeps reading the
registry files directly (R-25/R-46); this CLI is a convenience cross-check only.

## Probe 5 — where a `run_in_background` Agent-tool spawn writes, and whether tokens are recoverable

**Question (GD-8 depends on it):** the Agent-tool profile claims `taskId`
present and `TaskStop` usable. Where does a *background* spawn's transcript
land, and does it carry `message.usage`?

**Method** (2026-07-26T23:56:07Z): a real background spawn in the probe project,
then a disk scan.

```bash
claude -p --permission-mode acceptEdits \
  'Use the Agent tool (subagent_type general-purpose) with run_in_background
   set to true … run `echo bgprobe` and reply BGDONE …'
```

**Results, all first-hand:**

1. **Transcript location — identical to a foreground Agent-tool spawn:**
   `~/.claude/projects/<cwd-slug>/<parent-sessionId>/subagents/agent-<agentId>.jsonl`
   (+ `.meta.json`). Background-ness changes nothing about the path.
2. **`.meta.json` is thinner than the foreground one.** Background:
   `{"agentType":"general-purpose","description":"Run echo bgprobe probe","toolUseId":"toolu_01RRE…","spawnDepth":1}`
   — **no `model` key**, where the foreground specimens on disk
   (`dd469822…/subagents/agent-a4e343a0f7d73268c.meta.json`) carry
   `"model":"opus"`. Every `.meta.json` field must stay nullable (R-48 already
   requires this; here is the specimen that proves it for the background arm).
3. **Tokens are recoverable.** The 6-record transcript carried **2 `message.usage`
   rows over 2 distinct `message.id`s**. The larger foreground specimens show the
   split-record shape that forces `$max`: `agent-a483cae616edffe81.jsonl` has
   **70 usage rows over 21 distinct `message.id`s**, `agent-a4e343a0f7d73268c.jsonl`
   **78 over 26**.
4. **The launch record is a first-class join.** The parent's `toolUseResult`:

   ```json
   { "isAsync": true, "status": "async_launched",
     "agentId": "a4720c56159c68ada", "description": "Run echo bgprobe probe",
     "resolvedModel": "claude-fable-5", "prompt": "…",
     "outputFile": "/tmp/claude-1000/<slug>/<sessionId>/tasks/a4720c56159c68ada.output",
     "canReadOutputFile": true }
   ```

5. **In this CLI version the Agent-tool "taskId" IS the 17-hex `agentId`.** The
   follow-up poll in the same transcript is
   `TaskOutput {"task_id": "a4720c56159c68ada", "block": true, "timeout": 60000}`
   — the same string as `toolUseResult.agentId`. This is a **refinement of
   GD-8**: the Agent-tool profile needs no separate id space to address a stop,
   unlike the Workflow profile whose handle is the opaque run-level
   `taskId` (`w4hiywrt6`-shaped, CONVO-12). Recorded here; the plan text is not
   changed by this file.
6. **`outputFile` is a symlink to the transcript itself**
   (`tasks/<agentId>.output -> …/subagents/agent-<agentId>.jsonl`), so it is a
   second name for data Touch already tails — not a new source, and not a
   completion signal.

**Not settled by this probe:** whether `TaskStop`/`KillTask` against that id
actually terminates a *running* background agent (the probe agent finished in
~4.7 s, before a stop could be attempted). R-35 must probe the kill path
itself; nothing here licenses the assumption that it works.

---

## Appendix A (R-38) — Mongo provisioning evidence

Recorded here rather than in `docs/mongo.md` because it is evidence, not a
recipe. All results 2026-07-26T23:48Z–23:50Z, against a container started with
the R-42 recipe verbatim (loopback bind, `--auth`, named volume, port 27317 so
it could not collide with the sub-plan containers already running):

```bash
docker run -d --name touch-mongo-probe -p 127.0.0.1:27317:27017 \
  -e MONGO_INITDB_ROOT_USERNAME=touchadmin \
  -e MONGO_INITDB_ROOT_PASSWORD="$TOUCH_PROBE_PASS" \
  -v touch-mongo-probe-data:/data/db mongo:7 --auth
python3 mongo_probe.py     # scratchpad driver; container + volume removed after
```

| # | probe | result |
|---|---|---|
| A0 | `pymongo==4.17.0` installs through the proxy | **yes** — `pip download --no-deps pymongo==4.17.0` → `pymongo-4.17.0-cp313-…-manylinux_2_28_x86_64.whl` (2.1 MB), "Successfully downloaded pymongo"; `dnspython` importable |
| A1 | `mongo:7` pulls and runs | **yes** — image `sha256:9bdaeb6dac6e…`, created `2026-07-22T22:13:51Z`; server reports **7.0.39**, storage engine `wiredTiger` |
| A2 | host `mongod`/`mongosh` | **absent** — Docker is the only path; `docker exec … mongosh` is the shell |
| A3 | **sub-document `_id` field-order sensitivity** | **CONFIRMED, reproduced a fourth time.** Inserting `_id={"s":"x","n":1}` then `_id={"n":1,"s":"x"}` raised **no** DuplicateKeyError: **2 documents**, and `find({_id:{"n":1,"s":"x"}})` matched exactly **1**. A BSON sub-document is never a key (GD-24, MONGOSCHEMA-6 ≡ CUSTOMSTATE-4 ≡ LIVEFLOW-2) |
| A4 | **BSON type strictness** under `$jsonSchema` | pins are exact: `{pid:int, procStart:string}` accepted; **float `pid` REJECTED**, string `pid` REJECTED, int `procStart` REJECTED (all `WriteError`). `42.0` is not an int to BSON — the aggregator must emit true ints (GD-24) |
| A5 | change streams on a standalone | **unavailable** — `db.watch()` → `OperationFailure` **code 40573**, "The $changeStream stage is only supported on replica sets". Third independent reproduction; GD-22's discard stands |
| A6 | dotted keys inside a sub-document | **stored without complaint** by server 7 — the server does not protect the schema here, so R-44's `_raw`-wrapping + validator is the only guard |
| A7 | anonymous connect against the `--auth` container | **refused** — `OperationFailure` code **13** (Unauthorized) on write; `usersInfo` shows exactly `[('touchadmin','admin')]`. The GD-27 zero-users refusal has a real signal to read |

The probe container and its named volume were removed after the run
(`docker rm -f touch-mongo-probe`, `docker volume rm touch-mongo-probe-data`);
only names constructed by the probe were touched (GD-27). The probe password was
generated with `secrets.token_urlsafe`, kept in a 0600 scratchpad file outside
the repo, and appears in no file here.
