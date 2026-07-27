# Mongo: deployment, security, and the mirror

Touch mirrors what it reads from `~/.claude` into MongoDB so history survives
the CLI's own rewrites (`performRemoveByUuid` truncates and rewrites a
transcript; `performCompactTranscript` rewrites the whole file). This page is
the operator's half of that: how to run the database, how Touch reaches it,
what it will refuse to do, and what it costs on disk.

Normative sources: **GD-21** (dependency policy), **GD-22** (the mirror is a
derived, rebuildable projection), **GD-26** (durability: upsert-only, no TTL),
**GD-27** (security posture), **GD-30** (latency budget), items **R-42**,
**R-45**, **R-57**.

---

## 0. Mongo down is a non-event

Read this before anything else, because it decides how much the rest matters.

**Touch works completely without MongoDB.** The in-memory reduction is the
single source for `/ws` and the read API, and `.touch/` JSONL is the
crash-durable system of record (GD-22). The mirror is a write-behind
projection, and it is *fully rebuildable from files*.

| situation | what degrades | what does not |
|---|---|---|
| `pymongo` not installed | `/health` reports `mirror: "absent"` | everything else |
| no mongod running | `/health` reports `mirror: "down"` | sessions, agent rows, loop cards, token counters |
| mongod slow or erroring | `/health` reports `mirror: "degraded"`, drops counted | the live view |
| another process holds the writer lease | `mirror: "refused"`; this process serves reads only, and re-takes the lease automatically once it expires (30 s TTL) | the live view |

Mongo's contribution to the agent-action → pixel path is **0 ms** by design: it
is written from a bounded queue drained by a separate task, never inline in the
poll loop (GD-30). A dead port costs one 500 ms server-selection timeout, then
the circuit breaker holds for 30 s — not the 30.1 s per tick that pymongo's
default `serverSelectionTimeoutMS` produces.

`/health`'s `mirror` block is
`{state, lastError, notes, queued, dropped, tolerated_dups, lease, backend, db,
counters}` — `backend` names the driver in use (`mongo` or `memory`, `null`
when there is none) and `db` the derived database name (§3), never a URI.
`lastError` is only ever a **fault**; a condition Touch has already decided is
fine — a correctly-configured least-privilege user that cannot run `usersInfo`,
for instance — goes in `notes` instead. A `live` mirror never publishes a
`lastError`, so an alert rule can read that field literally: the fault text is
cleared by the same tick that promotes the state, while the *counters* (`dropped`,
`write_errors`, …) stay, because they are the durable record. `live` likewise
implies `lease.held` — the two are set on the same path, and the pair
`state:"live"` beside `lease:{held:false}` is a shape `/health` cannot produce.

**A degraded or down mirror recovers on its own, with or without traffic.** The
state is a statement about *now*: a tick that completes a real server round trip
— a write, or the lease renewal an idle process makes every ~15 s (half of the
30 s TTL) — clears the failure count and settles the state back to `live`. A tick that makes no round
trip promotes nothing, so a dead server is never talked back to health by a quiet
session; a mirror that is `down` while the queue is empty is a mirror whose last
renewal failed, not one nobody has written to lately.

`counters` carries, in full: `queued`, `written`, `dropped`, `tolerated_dups`,
`rejected`, `write_errors`, `retracted`, `renumbered`, `sweeps`, `ticks`,
`backfilled`, `refused_no_lease`, `refused_policy`, `refused_future_ts`,
`refused_no_source`, `unmapped`, `skipped_absent`. The two refusal counters are
kept apart on purpose, because `refused` means three different things and only
one of them is worth hunting for a second process over: **`refused_no_lease`**
is GD-29 — another writer holds the stream's lease, or the operation named a
stream this process does not hold. **`refused_policy`** is a refusal Touch made
itself and will not retry on a timer: a mongod with zero configured users
(GD-27), or a schema it will not write to. A nonzero `refused_no_lease` against
a single-process deployment is a bug; a nonzero `refused_policy` is `/health`'s
`lastError` telling you to read §4.

So: if you do not want a database, do not run one. Nothing in Touch will nag,
fail, or block a test.

---

## 1. Run the database (the exact recipe)

Two invariants make this recipe non-negotiable (GD-27): a plain
`docker run -p 27017:27017 mongo:7` is an **unauthenticated database on
0.0.0.0** holding the exact unredacted transcripts Touch's security posture
exists to protect, with an anonymous volume that a `docker rm` throws away.

```bash
# 1. a password that never appears in a repo file, a prompt, or your history
export TOUCH_MONGO_PASS="$(openssl rand -base64 24)"

# 2. the container: LOOPBACK bind, --auth, a NAMED volume
docker run -d --name touch-mongo \
  -p 127.0.0.1:27017:27017 \
  -e MONGO_INITDB_ROOT_USERNAME=touchadmin \
  -e MONGO_INITDB_ROOT_PASSWORD="$TOUCH_MONGO_PASS" \
  -v touch-mongo-data:/data/db \
  mongo:7 --auth
```

Line by line, because each one is load-bearing:

* `-p 127.0.0.1:27017:27017` — the bind is **loopback only**. `-p 27017:27017`
  publishes on `0.0.0.0`; never use it. There is no supported Touch deployment
  where the database is reachable from another host.
* `--auth` (and the two `MONGO_INITDB_ROOT_*` variables, which switch the
  official image into authenticated mode anyway) — a mongod with zero
  configured users accepts anonymous connections, and Touch **refuses to
  mirror into one** (§4).
* `-v touch-mongo-data:/data/db` — a **named** volume. An anonymous volume is
  deleted with `docker rm -v` and orphaned without it; either way the mirror's
  whole point (surviving deletion) is defeated by its own storage.
* `mongo:7` — the version this was probed against. Change streams are *not*
  used, so a standalone mongod is a fully supported deployment (a standalone
  answers `db.watch()` with `Location40573`; GD-22 records the decision).

### Never publish 27017

Touch runs in a sandbox whose ports reach the host only when explicitly
published. The Touch server itself is published that way:

```bash
sbx ports "$SANDBOX_VM_ID" --publish 8932:8932/tcp    # Touch UI — yes
```

**Do not publish 27017.** Not `sbx ports … --publish 27017:27017`, not a
variant, not "just for a minute with mongosh". Publishing it re-creates on the
host exactly the ungoverned network surface the loopback bind removed, and the
database holds unredacted transcripts. Use `docker exec touch-mongo mongosh …`
inside the sandbox instead.

---

## 2. Bootstrap the least-privilege user

The root user above exists to create the real one. Touch's ingest user gets
insert/update but **not remove** — GD-26's upsert-only rule enforced by the
database itself, not only by the code:

```bash
docker exec -i touch-mongo mongosh -u touchadmin -p "$TOUCH_MONGO_PASS" \
  --authenticationDatabase admin <<'EOF'
const db_name = "touch_<derived>";          // see §3
const app = db.getSiblingDB(db_name);

app.createRole({
  role: "touchIngest",
  privileges: [
    // the mirror: read + upsert, and the index/validator bootstrap it runs
    { resource: { db: db_name, collection: "" },
      actions: ["find", "insert", "update", "createIndex", "createCollection",
                "listCollections", "listIndexes", "collMod"] },
    // GD-26's ONE legal delete: renumbered positional stream_meta documents
    { resource: { db: db_name, collection: "stream_meta" },
      actions: ["remove"] },
    // GD-23's drop-and-rebuild of the reducer-owned collection
    { resource: { db: db_name, collection: "derived" },
      actions: ["dropCollection"] }
  ],
  roles: []
});

app.createUser({
  user: "touch",
  pwd: passwordPrompt(),
  roles: [{ role: "touchIngest", db: db_name }]
});
EOF
```

The role is the point: with it, a bug that tries to `deleteMany` on `records`
fails at the server. Without it, the only thing standing between the CLI's
destruction of history and the mirror re-importing it is a code review.

---

## 3. How Touch reaches it

**Database name is derived, never a constant** (GD-27):

    touch_<sha1(realpath-of-repo)[:8]>          e.g. touch_4d03799a

Two checkouts on one machine therefore never share a database. Override with
`TOUCH_MONGO_DB` if you must; it is still fenced to the **`touch_` prefix —
with the underscore**. `touch_scratch` is accepted, `touchdown_prod` is
refused: the fence is what makes "Touch drops only databases it constructed"
checkable, and a bare `touch` prefix would admit names nobody here built.
Print the one this checkout uses:

```bash
python3 -m aggregator.mirror --check      # config + derived db name; never the URI
```

**Credentials live in `.touch/mongo.json`, mode 0600** — the same handling as
`server.json`'s per-boot token (GD-13):

```json
{ "uri": "mongodb://touch:<password>@127.0.0.1:27017/touch_4d03799a?authSource=touch_4d03799a" }
```

```bash
install -m 600 /dev/null .touch/mongo.json      # create it 0600, not chmod after
```

Touch **refuses to read the file** if any group or other bit is set
(`mode & 0o177`), and refuses a symlink outright (a 0600 symlink says nothing
about its target). The URI is transported to the aggregator in
`TOUCH_MONGO_URI`; the file is where it lives.

Credentials never appear in: a repo file, a prompt, an agent transcript,
`events.jsonl`, `/health`, or any API response. Every string the mirror
publishes — including `lastError`, which is where a driver exception would
otherwise embed the whole URI — is redacted first.

`.gitignore` already covers `mongo-data/`, `mongo-dump/`, `*.bson` and
`.touch/`. **No Mongo data directory inside the repo.**

### Never mirrored

`.touch/server.json` (any field), `~/.claude/.credentials.json`,
`~/.claude.json`, and any environment variable matching
`(?i)(token|secret|key|password|auth)`. These are not redacted downstream —
they are never read.

Behind that deny-list sits a **document backstop** for the case a transcript
*quotes* an environment dump: on the way out of the mirror's queue, string
values whose field name matches the same pattern are replaced with
`[redacted]`. It is scoped so it cannot corrupt the schema:

* field names GD-24 declares (`sessionKey`, `stateKey`, `author`, …, read out
  of `refs.KIND_SPECS` and `mongo_store.COLLECTIONS` rather than hand-listed)
  are data, not secrets, and are left alone — a `[redacted]` there would break
  the `{"ref.sessionKey": …}` joins the schema mandates;
* a canonical `ref{}` that **validates** against a declared kind is skipped
  whole: one of GD-24's seven union members has a closed key set and per-field
  value pins, so there is nowhere in it for a credential to sit — and a
  `[redacted]` there would break the same joins. Validated, not merely
  *labelled*: the check is `refs.validate_ref`, which rejects an undeclared
  field, and not `refs.classify`, which reads the `kind` key and nothing else.
  A `ref` with `kind:"unknown"`, and a `ref` naming a real kind while carrying a
  key that kind does not declare, are both *not* skipped: the first is GD-11's
  open tail, retained precisely because Touch could not classify it, and the
  second is the same open tail wearing a pinned kind's name. Both carry whatever
  keys their author wrote and are scrubbed like any other payload;
* `key`/`keys` are the exception to the exception — they are *also* a declared
  field name (`run_nodes.key`), so their **value** decides: a keystroke or a
  short classification survives, anything else is redacted.

---

## 4. What Touch refuses to do

| refusal | why |
|---|---|
| mirror into a mongod reporting **zero configured users** | that database is anonymous-writable; `/health` says so with `mirror: "refused"` |
| read `.touch/mongo.json` if any other account can | a credentials file the group can read is the failure GD-27 exists to prevent |
| write when another process holds the **writer lease** | duplicate-key is both idempotent replay (healthy) and two racing writers (a bug); one writer per stream, GD-29. The refusal is not permanent: the lease has a 30 s TTL and the refusing process retries once per TTL, so a holder that crashed or stalled does not wedge the mirror. The other two refusals above are deliberate and are **never** retried on a timer |
| `deleteOne`/`deleteMany`/`drop`/`$unset` on a mirror collection | except the one scoped `stream_meta` renumber; GD-26 |
| create any index with `expireAfterSeconds` | **no TTL on any Touch collection, ever** — a TTL re-imports the CLI's destruction of history on a timer nobody is watching. `ensure_schema` reads the server's indexes back and refuses to start if one appeared by hand |
| `$inc` for token accounting | re-ingest after a transcript rewrite is mandatory, and summed deltas double (GD-25) |
| backfill an operation whose **source file cannot be named** | the mtime guard below (§5) cannot be evaluated, and it fails closed rather than widening to `now()` |
| mirror an observation **no mapper claims** | it is counted and reported, never dropped quietly; `/health` goes `degraded` and stays there, because no later tick can un-refuse it |
| replay a `--rebuild` whose **drop of `derived` failed** | dropping is the precondition, not a step: a store that is neither the old projection nor the new one is worse than one left untouched. The report comes back `droppedDerived: false, replayed: 0` with the reason on `/health` — never as a traceback |
| queue anything that is not an operation — a malformed backfill item, a shape `enqueue` cannot read | counted under `rejected`, named on `/health`, and the walk carries on: one bad item is a fact about one item, not about the run |

Deleted transcript records are **retracted**, not removed: a full re-ingest
runs under a new per-file generation, and records left behind get
`retracted: true, retractedGen: G` via `updateMany`. They are hidden by
default in the UI, visible on demand, and never shown as current.

---

## 5. Rebuild and backfill

```bash
# health of the mirror as /health reports it (redacted)
python3 -m aggregator.mirror --health

# drop the reducer-owned `derived` collection and replay everything from files
python3 -m aggregator.mirror --rebuild

# one-shot historical walk of ~/.claude/projects/** (live=False, always)
python3 -m aggregator.mirror --backfill

# …of a different tree (a second checkout's history, a restored backup)
TOUCH_CLAUDE_ROOT=/some/other/.claude python3 -m aggregator.mirror --backfill
```

* `--rebuild` is the operation that proves GD-22: Mongo is a projection, so a
  wipe followed by a rebuild must produce a byte-identical fingerprint — over
  the *projection*, which excludes the `writers` lease document. That lease is
  runtime state (a pid, a boot digest, an expiry); no file says what is in it
  and no replay can reproduce it, so counting it would make this criterion false
  for every deployment that actually takes the lease. Two processes that replay
  the same corpus report the same fingerprint. The
  mirror is upsert-only, so a replay lands on its own output and duplicate keys
  are tolerated — and *counted*, since a nonzero steady-state duplicate count
  means a second writer or a key bug. Everything is mapped **before** the one
  destructive step, and the reducer-owned `derived` collection is dropped only
  if that pass produced zero rejections. If any observation could not be mapped
  — an unregistered kind or a mapper that raised — it keeps `derived`, replays
  everything it *can*, and reports `rejected` plus the unmapped kind names on
  `/health` as `degraded`. A rebuild that cannot replay everything does not also
  destroy the reducer's output.
* `--backfill` walks `$TOUCH_CLAUDE_ROOT/projects/**` (default `~/.claude`)
  and hands **each file** to the ingest module that owns its format, so every
  observation knows which file it came from. It hard-codes `live = False`,
  stamps every document `ingestMode: "backfill"`, and **refuses any operation
  carrying a timestamp newer than its source file's mtime**. Journal records
  have no timestamps, so the failure mode is a mapper reaching for `now()` and
  stamping a year of history with today's clock; the guard exists for exactly
  that.
* That guard **fails closed**. If an operation carries a timestamp and its
  source file cannot be named or stat-ed, it is refused (`refused_no_source` on
  `/health`) rather than checked against the import's own clock — which would
  make the test `ts > now()`, a condition nothing reading a historical file can
  ever trip. A guard that cannot be evaluated is not a guard that passes. An
  operation carrying no timestamp at all makes no claim about time and is
  stored normally.
* Custom state (`custom_state_events` / `custom_state`) is the ONE dataset not
  rebuildable from `~/.claude`, which is why it is written to a `.touch/` WAL
  first and mirrored second.

---

## 6. Growth and retention

Measured on this machine's corpus, not estimated:

| quantity | value |
|---|---|
| mirrored corpus on disk | 15.7 MB |
| records | 3 936 |
| average per record | ≈ 4 KB |
| growth per active session | ≈ 1.3 MB h⁻¹ |
| mirror vs raw transcript text | **0.53×** (the parsed shape is smaller than the JSONL) |
| full-corpus ingest | 0.40 s cold, 0.16 s on an idempotent second pass |
| `insertOne` | 0.61 ms at `w:1`, 1.65 ms at `j:true` |

**Retention policy for v0: keep everything.** Sessions, agents, runs,
run_nodes, usage and custom state are all small and all kept forever. Nothing
is prunable in v0, and there is **no TTL index anywhere** — revisit at the
GD-16 threshold with the numbers above, not before. A mirror that expires
history is a mirror that has re-implemented the problem it was built to solve.

Backups, if you want them, are a `mongodump` into `mongo-dump/` (gitignored);
they are not required, because the mirror is rebuildable from files.

---

## 7. Teardown

```bash
docker rm -f touch-mongo          # the container
docker volume rm touch-mongo-data # the data, explicitly and only when you mean it
```

Losing the volume loses only the *mirror*. `.touch/` and `~/.claude` are
untouched, and `--rebuild` reconstructs everything except custom state, which
lives in its own `.touch/` WAL.
