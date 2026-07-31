# The dev loop, and why `.claude/settings.json` is two keys long (GD-C1)

Development notes for anyone hacking on Touch, or on any plugin that carries a
hook. Nothing here is needed to *use* Touch; it is the reasoning behind two
settings that look like they are missing something.

## The dev loop

```
claude --plugin-dir plugin/touch
```

That is the whole loop: `--plugin-dir` loads the payload out of a checkout, and
this repository's committed `.claude/settings.json` enables exactly one plugin
id — `touch@inline` — so the session comes up with Touch on.

To exercise the *marketplace install path* instead, add the checkout as a
catalog in a **throwaway** `CLAUDE_CONFIG_DIR`:

```bash
CLAUDE_CONFIG_DIR="$(mktemp -d)" claude plugin marketplace add <checkout>
```

Never via committed settings — see the next section for what that costs.

## Why the settings file is that short, and must stay that way

`.claude/settings.json` carries exactly two keys: the status line, and
`"enabledPlugins": {"touch@inline": true}`. It registers **no** hooks. It once
also carried an `extraKnownMarketplaces` entry pointing a catalog name at this
checkout, plus a second enabled id. Both are gone:

- **Marketplace registration is keyed by catalog NAME and stored per user,
  globally**, so a same-name add silently REPLACES the previous registration.
  Anyone who had installed the published plugin would have their real catalog
  repointed at a working tree the moment they trusted this folder — in every
  project on that machine. That hijack is the whole reason.
- **An `enabledPlugins` entry at any scope overrides `defaultEnabled: false`**,
  which is the manifest's entire consent posture for a hook-carrying plugin.
  One id is one deliberate opt-in for the dev loop; two ids is two.
- **The key bought nothing anyway.** The `claude plugin install` /
  `claude plugin marketplace` subcommands do not read `extraKnownMarketplaces`
  (reproduced twice), and `--plugin-dir` already serves the dev loop.

## The hook registration is single, and nothing depends on shadowing

Touch's hooks are registered exactly once, by the plugin's own
`hooks/hooks.json` — the file sits beside the hook scripts, and `plugin.json`
carries no `hooks` key (GD-U5). The project settings file carries no `hooks`
block either: the two registrations had the same matcher and fired the hook
**twice** per tool call (measured 2 vs 1).

Measured, so the fear is not re-litigated: with the same plugin present both as
`--plugin-dir` and as an installed copy, a hook fires **once** — the
`--plugin-dir` copy shadows the installed one (probe plugins, one appended line
per invocation, 1 fire not 2). That shadowing rule is unwritten upstream, so
nothing may depend on it; the single-registration fix removes the dependency
instead of documenting a reliance on it.

The consequence, accepted deliberately: a session started WITHOUT the plugin has
no scope guard. That is fine — the guard is inert without an `ACTIVE` sentinel,
and every orchestration run needs the plugin's `bin/` anyway. Do not "restore"
the settings-file form.

## The status line is the one foreign-interpreter exception

`.claude/statusline.sh` shells out to `jq`. That is a **status-line-only**
exception and is not a licence for `jq` anywhere in Touch's own code, wrappers
or tests: the shipped wrappers are held to bash + Python 3 stdlib, and a test
enforces it.
