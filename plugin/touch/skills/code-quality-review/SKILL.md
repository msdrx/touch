---
name: code-quality-review
description: Review code (a diff, PR, file, or module) against Clean Code standards — naming, function shape, comments, error handling, class cohesion, third-party boundaries — and report concrete file:line findings with fixes. Use when asked to review code quality, audit a module, check a PR, or as a final self-review pass after writing or modifying code.
---

# code-quality-review

**Scope**: if a task or sub-plan defines owned files, stay inside them — anything else is a finding, not an edit.
**Conventions**: where this skill and the project's established convention disagree, the project wins; say so once and move on.

## Procedure

1. Establish scope: the diff/PR/files under review. Read changed files in full context, not just hunks — code is read 10:1, so judge by what a newcomer sees.
2. Work the checklists. Per violation: `file:line — [category] problem — suggested fix — severity (blocker / major / minor / nit)`.
3. Ward Cunningham's test on every unit: is each routine "pretty much what you expected"? Having to reverse-engineer it is a finding even if no rule below names it.
4. Report grouped by severity. Don't pad — if the code is clean, say so. This skill detects and reports; it never edits — hand the fixes to the `/touch:refactoring-pass` skill.

## Names

- Every name answers why it exists, what it does, how it's used; a name that needs a comment fails.
- No disinformation: nothing called `List` that isn't one; no near-twins (`getActiveAccount` vs `getActiveAccountInfo`).
- No encodings (`m_`, Hungarian, `I`-prefixed interfaces) or noise words (`Info`, `Data`, `Manager`, `Processor`).
- Pronounceable, searchable; magic numbers → named constants.
- One word per concept codebase-wide — flag mixed `fetch`/`retrieve`/`get` for one idea, or one word punned for two.
- Name length matches scope length: `i` fine in a 3-line loop; wide scope demands long, precise names.

## Functions

- Small — rarely near 20 lines; blocks inside `if`/`while` ideally one well-named call; indent depth ≤ 2.
- Does ONE thing: all statements one abstraction level below the name; an extractable sub-function whose name isn't a restatement of its body = does too much; comment "sections" are the giveaway.
- No mixed abstraction levels (`getHtml()` beside `.append("\n")`); file reads top-down, callees below callers.
- Arguments: 0–2 normal, 3 needs justification, more is a finding; cohesive argument groups → objects (`makeCircle(center, radius)`).
- No boolean flag arguments — `render(true)` must split into `renderForSuite()`/`renderForSingleTest()`.
- No side effects the name doesn't promise (a `checkPassword` that initializes a session is a blocker); no output arguments.
- Command–query separation: do something OR answer something, never both (`if (set(...))` is a finding).

## Comments and dead code

- Challenge every comment: could a function name or explanatory variable say it instead (`employee.isEligibleForFullBenefits()`)?
- Allowed only: legal headers, intent, clarification of unalterable APIs, warnings of consequences, TODOs, amplification.
- **Rationale is never a noise comment and never a free win.** Why this exists, what it prevents, which decision it implements — that is a decision record living beside the code it governs, and it is the one artifact structure cannot be reverse-engineered into (the `/touch:architecture-tradeoffs` skill states the same law). Keep it; if it is long, keep it as a module/file docstring.
- Remove restatements of the code, redundant javadoc/docstrings, change journals and attribution comments — not the *why*.
- Delete on sight: commented-out code (VCS remembers), dead functions, unreachable branches.

## Error handling and null

- Exceptions, not error codes; error handling separated from logic — a function that handles errors does nothing else; try/catch bodies extracted into their own functions.
- Exceptions carry context (source + intent of failure); exception classes defined by how callers catch them; third-party APIs wrapped to throw your types.
- **Calibration — a deliberate degrade path is not an error-code smell.** Where a subsystem's absence is *designed* to be a non-event, check that it reports its state (a health field, a log line) rather than demanding it throw. "The optional store is unreachable, so the feature reports itself absent" is the design; converting it into an exception is the defect.
- No returning null that callers must defensively check everywhere — prefer Special Case objects or empty collections; no passing null (a null argument is a caller bug, not something to check for at every entry).
- **Calibration — the `None` idiom.** In languages where `None`/`nil`/`Option` is the idiomatic "no value" (Python, Go, Rust), the rule is *documented absence*, not "never return it". The smell is an undocumented null leaking defensive checks into every caller.

## Classes, data, coupling

- One responsibility / one reason to change; describable in ~25 words without "if/and/or/but"; a subset of methods sharing a subset of fields is a class trying to get out.
- Objects vs data structures chosen deliberately — no half-object/half-data hybrids; no reflexive getters/setters on every field; DTOs stay pure data with rules elsewhere.
- Law of Demeter: no train wrecks (`a.getB().getC().doD()`) through objects — tell, don't ask; pure data structures exempt.
- Repeatedly-modified classes → extension (new subclass/strategy) over edit-in-place; volatile dependencies behind interfaces with injected implementations.
- Construction/wiring separated from use — no scattered `new` of volatile concretes deep in logic; construct in main/factories/DI.
- Cyclomatic complexity per function ≲ 10 (prefer < 5); ask whether the complexity is essential (domain) or accidental (code).

## Duplication and boundaries

- DRY: repeated code, or repeated switch/if-else chains over the same condition set → extract function/class or polymorphism.
- BUT verify duplication is *true* (copies always change together), not *accidental* (they'll diverge — e.g., a view model merely mirroring a DB record today). Never unify accidental duplication.
- Third-party types (`Map`, framework classes) don't leak through public APIs — wrap behind your own narrow class/adapter.

## Calibration — don't over-flag

- Minimizing class/method count is the lowest-priority design rule — don't demand pointless tiny classes or sophisticated decomposition of a tiny program.
- Test code meets the same cleanliness bar; efficiency may relax there.
- Boy Scout Rule: every check-in should leave code a little cleaner. An author who touched a mess without improving it: minor, not blocker.

Sources: Clean Code, Clean Architecture, Fundamentals of Software Architecture.
