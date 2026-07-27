# Deviation handoff — `custom_state`'s head order field wants sp-05's fence

**Owner of the fix:** sp-05 (`aggregator/mongo_store.py`).
**Raised by:** sp-11 (custom-state), attempt 4, closing critique-3's M1.
**Severity:** hardening. Nothing is wrong today; the rule is held by a test in
sp-11 instead of by the spec that owns the collection.

## What changed in sp-11

`custom_state`'s head is one space for the whole installation, while `seq` is
**per-stream** (the WAL's counter; a control/ledger file's line number). Two
events from different streams can therefore share `(refId, stateKey, seq)` —
two control files each stopping the same slot on their line 1 is the reachable
case — and the old guard `{seq: {$lt: newSeq}}` refused whichever arrived
second, leaving arrival order to pick the stored payload (GD-25's named
failure, invisible to the counts assertion: both events ARE stored).

`aggregator/custom_state.py` now writes and guards on a composite order:

```python
HEAD_ORDER_FIELD = "order"            # "<seq:012d>|<escaped stream>"
update  = merge_ops(op_max({"seq": seq, HEAD_ORDER_FIELD: order}),
                    op_set(payload), collection="custom_state")
require = {HEAD_ORDER_FIELD: {"$lt": order}}
```

Its primary component is the same zero-padded `seq` the event `_id` uses, so
within one stream it is exactly R-52's `{seq:{$lt:newSeq}}`; across streams the
stream id breaks the tie deterministically instead of the tick's read order.
`seq` and `fromSeq` stay on the document, unchanged in meaning.

## The one-line paste sp-05 owns

`custom_state` already fences `seq` — `$set` on it is an `OperatorError` rather
than a code review. `order` is the same kind of field and wants the same fence:

```python
    "custom_state": CollectionSpec(
        …
        accumulable=("seq", "order"),      # was: ("seq",)
```

Optionally also pin the type, since the guard compares strings:

```python
        types={…, "order": _STR, …},
```

## Until it lands

`aggregator/custom_state.py` reaches `order` through `$max` and nothing else,
and `tests/test_custom_state.py`
(`test_every_head_write_carries_one_fixed_key_set`) asserts it from the emitted
updates rather than from this sentence — so the module cannot start `$set`ting
it while the declaration is missing. The day sp-05 pastes the line, that
assertion simply stops being the only thing holding the rule.
