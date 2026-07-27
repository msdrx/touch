# sp-server-api — test gate, attempt 3

**Verdict: PASS (green).** Both owned suites are 100% green, the full suite shows
only the two known pre-existing baseline failures (unchanged, character-for-character
identical to attempt-1/attempt-2 records), ownership is clean, and every attempt-2
critique finding now has a named behavioural test behind it.

## 1. Targeted suites (must be 100% green)

Run from the repo root, stdlib-only, standalone executables:

| suite | rc | result |
|---|---|---|
| `python3 tests/test_server_core.py` | 0 | `all server core tests passed` — 22 test functions |
| `python3 tests/test_api.py` | 0 | `all read API + wire tests passed` — 31 test functions |

No failures, no errors, no unexpected skips. The only skips in the wider suite are
the deliberate `TOUCH_MONGO_URI is not set` live-mongod arms (R-42), which is the
required clean-skip behaviour.

## 2. Full-suite regression gate

```
cd /home/laniakea/Projects/touch && rc=0
for t in .claude/shared/monitoring/tests/test_*.py; do (cd "$(dirname "$t")" && python3 "$(basename "$t")") || rc=1; done
for t in tests/test_*.py; do [ -e "$t" ] || continue; python3 "$t" || rc=1; done
exit $rc
```

- **PASS (22):** `.claude/shared/monitoring/tests/test_frontend.py`, `test_server.py`,
  `test_shell.py`, `test_watcher.py` (all four monitoring baselines green);
  `tests/test_agents.py`, `test_api.py`, `test_bootstrap.py`, `test_custom_state.py`,
  `test_fixtures.py`, `test_ingest.py`, `test_legacy.py`, `test_mongo_deploy.py`,
  `test_mongo_store.py`, `test_reducer.py`, `test_refs.py`, `test_server_core.py`,
  `test_slots.py`, `test_stdlib_only.py`, `test_store.py`, `test_tailer.py`,
  `test_usage.py`, `test_ws.py`.

- **FAIL (2) — pre-existing baseline, NOT attributable:**
  - `tests/test_mirror.py` rc 1, `FAILED (3)`:
    `…proven by the call count: the held ticks made no attempt`;
    `the first generation lands`;
    `…and identical counts: {'records': 8} == {'records': 8, 'writers': 1}`.
  - `tests/test_sessions.py` rc 1, `FAILED (1)`:
    `wipe + --rebuild reproduces a byte-identical fingerprint`.

  These are character-for-character identical to the baseline recorded in
  `sp-server-api-test-attempt-1.md` §"FAIL (2) — pre-existing baseline" and
  re-confirmed in `sp-server-api-test-attempt-2.md`. They live in
  `aggregator/mirror.py` / `aggregator/sessions.py` + their suites — files owned by
  **sp-mirror-deploy** and **sp-sessions-arm**, two loops that closed RED with open
  findings. This attempt touched none of those files (mtimes: `mirror.py` 07-26 11:29,
  `test_mirror.py` 02:44, `sessions.py` 04:10, `test_sessions.py` 04:14 — all hours
  before this attempt's 19:37–19:43 window). Baseline failures do not fail this gate.
  **No new failure anywhere.**

## 3. Bare-checkout / no-third-party arm (GD-21, R-56)

Re-ran both owned suites with `pymongo` shadowed by a stub module that raises
`ImportError` on import (`PYTHONPATH` shim):

```
PYTHONPATH=<shim> python3 tests/test_server_core.py  → rc 0
PYTHONPATH=<shim> python3 tests/test_api.py          → rc 0
```

`grep -n "pymongo\|import mirror\|from mirror" aggregator/server.py` → **no hits**.
The server needs no driver and does not import `mirror.py`; the R-45 `/health` mirror
block is injected, as the suite's own static guard asserts
(`test_the_server_imports_no_driver`: "only mongo_store.py and mirror.py may import
the driver"; "every import is stdlib: []"). Mongo-dependent arms skip cleanly with no
mongod running and no services started.

## 4. Plan conformance (sp-12 / R-30 / R-31 / R-55:server)

Owned files present and non-empty: `aggregator/server.py` (123 919 B),
`tests/test_server_core.py` (45 769 B), `tests/test_api.py` (58 530 B).

Every owned item is backed by live, non-tautological assertions (the test names
below are verbatim from the green run):

- **R-30 / GD-13 in full** — `test_every_route_but_health_needs_the_token` (exactly
  one open route, and it is `/health`; 401 carries a Bearer challenge),
  `test_all_three_token_carriers_work_and_a_wrong_one_does_not` (256-bit per-boot
  token, `hmac.compare_digest`, no plain-equality shortcut, near-miss rejected),
  `test_the_route_table_is_static_and_has_no_fallback` (literal `(METHOD, route)`
  keys, no prefix match, no trailing-slash resolution, default 404),
  `test_origin_policy` (Origin/Host allowlist at WS upgrade, DNS-rebinding Host
  refused by name, refusals counted), `test_the_defaults_are_gd13s` (loopback
  default, `0.0.0.0` explicit opt-in only, port 8932),
  `test_safe_artifact_path_contains_everything` (traversal, absolute path, symlink
  escape via realpath, extension whitelist, empty path),
  `test_served_files_carry_the_csp_sandbox_and_nosniff`, `test_server_json_is_0600`,
  `test_a_handler_bug_is_a_500_and_not_a_traceback`,
  `test_health_never_carries_a_credential` / `test_health_publishes_no_observation_to_an_unauthenticated_caller`
  (GD-27), `test_health_reports_tailers_and_the_mirror_block` (R-45 block served
  verbatim, mirror-absent reports `absent`, a raising mirror never 500s — GD-22).
- **R-31** — `test_one_validator_400s_malformed_and_404s_unknown` (one shared regex
  validator; malformed 400 and unknown 404 never swap; repeated parameter is 400),
  `test_a_bare_after_is_not_a_cursor` (GD-11), `test_an_unobserved_run_or_stream_is_404_not_an_empty_list`,
  `test_a_zero_limit_cannot_produce_an_endless_page`, `(stream,seq)` cursor grammar
  byte-identical to the events `_id`.
- **R-55:server** — `test_replay_window_is_bounded_and_publishes_its_edge`,
  `test_socket_replays_then_switches_then_tails` (single mode frame at the
  replay→tail boundary, `live:false` on every replayed frame),
  `test_reconnect_resumes_without_duplicates` (union equals a full replay — no
  duplicate, no gap), `test_from_is_applied_or_reported_never_silently_dropped`,
  `test_tokens_coalesce_and_stay_absolute` (≥1 s window per `(stream, ref)`,
  absolutes never deltas), `test_the_current_run_is_the_newest_not_the_alphabetically_largest`,
  `test_a_stream_born_after_the_switch_is_backfilled_not_animated`.
- **GD-22 / server derives nothing** — `test_the_server_derives_nothing_and_differences_nothing`
  (server calls the one sp-10 reducer; never computes `IDLE_LIMIT`, liveness,
  `verdict_of` or `attempt_label`; no executable line differences a token field),
  `test_query_falls_back_to_memory_and_says_so` (documented file-store fallback,
  operators and dotted paths refused, provider signature `find(collection, criteria, limit=)`
  honoured and its limit re-clamped).

### Attempt-2 critique findings — every one now has a test

| finding | covering test |
|---|---|
| M1 hello frame's `oldest`/`truncated` structurally empty vs. the contract table | `test_the_load_older_anchors_are_on_the_frame_that_can_know_them` — hello carries no anchor; the mode frame declares the cut stream with the real oldest seq; "every key the contract shows on a frame is a key the code puts there" |
| M2 one malformed `?cursor=` discarded every resume position | `test_one_malformed_cursor_costs_only_itself` — per-entry parsing, raw rejects returned in order, the good pair still resumes; docstring no longer names an unsendable status code |
| m1 malformed `?stream=` widened to every stream | `test_a_failed_stream_selector_serves_nothing_not_everything` + `test_a_handshake_names_the_parameters_it_could_not_use` — a selector that matched nothing serves NOTHING |
| m2 resume deeper than `MAX_REPLAY_EVENTS` promised no gap | `test_a_resume_deeper_than_the_cap_is_declared_not_silently_gapped` — newest window served, shortfall declared with the load-older seq |
| m3 `tick()` advanced past a held token record | `test_a_held_token_frame_holds_the_cursor_behind_it` — published cursor stays behind the held record and only advances onto the released absolute |
| m4 `Content-Type` bypassed the header sanitizer | `test_a_header_value_can_never_split_the_response` — "a CRLF content type cannot start a header line or split the response either — no field in this method is exempt", plus the non-latin-1 arm in `head_bytes` |
| n1 undocumented `query_source` seam | `test_query_falls_back_to_memory_and_says_so` — exact signature asserted and named in `h_query`'s docstring |
| n2 `inject_token` docstring true of one arm only | `test_the_token_is_injected_into_the_page` — both arms described accurately; raw-substitution safety justified by URL-safe base64 |
| n3 `Api.hits` collected, never published | `test_the_open_route_counts_requests_without_publishing_the_route_table` — served/unrouted/failed published, with no route name leaked to an unauthenticated caller |

## 5. Ownership and git hygiene

`ls -lt` over `aggregator/ tests/ docs/`: only `tests/test_server_core.py` (19:43),
`tests/test_api.py` (19:41) and `aggregator/server.py` (19:37) fall in this attempt's
window; the next newest file is `aggregator/custom_state.py` at 18:21. That is exactly
the sub-plan's owned set — no file outside it changed.

`git log --oneline -1` → `579446e orchestration history`, the prior pass's HEAD.
**No commit was made.** Unrelated in-flight `.claude/` state (orchestrator task
folders, running daemons) was left untouched; the only `.claude/` deltas in
`git status` are the pre-existing in-flight ones plus this findings file.

## 6. Nothing outstanding

No new failures, no ownership violation, no unmet owned item, no plan deviation.
**Gate is green.**
