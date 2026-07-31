# The memory home — `<project>/.touch/memory`

The long form of what `CLAUDE.md` states in a paragraph. Read this before
editing anything under `.touch/memory/`, before changing `touch-selfcheck
--init`, and before touching the memory route group on the monitoring server.
`docs/control-semantics.md` §5 is the normative account of the *file plane*;
`shared/monitoring/monitoring.md` documents the routes; this file is the why.

## One key does the mapping, and it is a program's job

Claude Code's **auto memory** — the `MEMORY.md` index it loads at every
conversation start, plus its topic notes — is mapped into a project at
`<project>/.touch/memory` by exactly one documented key,
**`autoMemoryDirectory`**, merged into `.claude/settings.local.json` by
`touch-selfcheck --init` (G1).

Two things make that a program's job rather than a hand edit:

1. **The value must be absolute** (or `~/`-prefixed). A relative or
   `$VAR`-interpolated path is **silently rejected** — the validator returns
   `undefined` and the CLI falls back to the default with no error and no
   warning. A hand edit that looks right can therefore be inert, and nothing
   tells you.
2. **Three undocumented environment overrides outrank every settings layer**:
   `CLAUDE_COWORK_MEMORY_PATH_OVERRIDE`, `CLAUDE_CODE_REMOTE_MEMORY_DIR`,
   `CLAUDE_MEMORY_STORES`.

So `--init` writes the key and then *verifies*: it prints the effective
directory, reports any of those three that is set — it reports them and
**refuses to read them**, because they are a diagnosis trap and never a
mechanism — and fails loudly when the answer is the default rather than the
directory it just configured. It never writes `.claude/settings.json`; that
file's exactly-two-keys rule (GD-C1, `docs/dev-loop.md`) stands.

`touch-selfcheck`'s later check is the other half: a legacy auto-memory tree
left behind by the mapping is detected and its differing filenames printed,
with the `rm -rf` a human runs themselves. Touch never deletes it and never
reads it as data.

## What moves, and what does not

This table is the scope. Every memory KIND the CLI has is listed, because the
cost of assuming otherwise is a session quietly loading instructions from a
directory nobody is editing.

| kind | location | disposition |
|---|---|---|
| Auto memory (`MEMORY.md` + topic notes) | `~/.claude/projects/<key>/memory/` | **moves** → `.touch/memory` via `autoMemoryDirectory` |
| Project `CLAUDE.md` content | `./CLAUDE.md` | stays; may optionally `@import` a file from `.touch/memory` |
| User `~/.claude/CLAUDE.md`, managed-policy CLAUDE.md | fixed | out of scope, read-only |
| Enclosing-directory CLAUDE.md above the repo | outside the repo | out of scope, read-only |
| Subagent memory (`~/.claude/agent-memory/`, `.claude/agent-memory*/`) | fixed | **no relocation mechanism exists** — out of scope |

The editor writes **only** `<project>/.touch/memory/*.md`. Every other tier is
read-only, and a `~/.claude/**` target is refused with a named 4xx rather than
silently resolved — that refusal is what keeps the "`~/.claude/` is a read-only
tap" promise literally true while a write plane exists. A symlink out of the
memory root is refused rather than followed.

## Four consequences to internalise

- **These bytes are model instructions.** Anything in there is loaded into
  future sessions of this project — the index always, a topic note on demand,
  and a file carrying `pinned:` frontmatter into *every* session, unasked. The
  write path therefore refuses `@`-imports outside code spans, block HTML
  comments, token-shaped and credentialed-URI lines (by category, never
  quoting the match), and `pinned:` without an explicit confirmation.
- **Memory is public.** In this repository `.touch/memory/*.md` is the one
  tracked subtree of `.touch/`; write it as if it ships, because it does.
- **Subagents may not write it.** The scope guard denies subagent
  `Write`/`Edit`/`NotebookEdit` on `.touch/memory/**` (G14), because
  co-locating memory with run history would otherwise hand a subagent a bigger
  capability than the guard exists to withhold. Edits come from the main
  terminal agent or the flag-gated HTTP plane, and the audit log is
  `.touch/memory-audit.jsonl` — never `events.jsonl`, never a plan badge.
- **The two trees stay apart, both ways.** The aggregator's WAL never lives
  under run history, and run history never lives under a tracked subtree.
  `.touch/` holds both, which is exactly why a repository's ignore carve is an
  allowlist of one pattern and not a directory.

## The index has a budget

`MEMORY.md` is always-on: it is read in full at the start of every
conversation, by every agent of every run. It is therefore capped the way
`CLAUDE.md` is, and the rule is:

- **at most 20 entries**, newest first;
- **one line each** — a link, an em dash, and the one thing the note is for;
- detail lives in the topic note, never in the index;
- when the cap is reached, the oldest entry is dropped, not shortened. A note
  worth keeping stays on disk and can be re-linked; an index that grows without
  a ceiling is a tax on every future turn.

In this repository `tests/test_context_budget.py` holds the ceiling at 800
estimated tokens and `python3 -m aggregator.costs --baseline` prints the
measurement.

**Only the main terminal agent (or the flag-gated write plane) can apply that
rule.** The scope guard denies subagent `Write`/`Edit` under `.touch/memory/`
(G14), so a subagent that finds the index over budget reports it rather than
trimming it.

## Three planes, not two (GD-13 as amended)

**read** — the transcripts, journals and event streams both servers tail.
**control** — the verb ladder, unshipped (`CONTROL_ROUTES` is `{}`).
**file** — this section. "Read-only" is scoped to orchestration *state*: the
monitoring server writes no event, and a memory edit is a file operation that
never promotes a session class and emits no `touch-status` event.
