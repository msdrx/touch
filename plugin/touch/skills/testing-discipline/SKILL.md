---
name: testing-discipline
description: Write, review, or restructure tests to F.I.R.S.T. standards with TDD discipline, and use testability pain as an architecture signal (Humble Objects, seams, fitness functions). Use when adding or reviewing tests, when code turns out hard to test, when a suite is slow/flaky/fragile, or when setting the test strategy for a feature.
---

# testing-discipline

**Scope**: if a task or sub-plan defines owned files, stay inside them — anything else is a finding, not an edit.
**Conventions**: where this skill and the project's established convention disagree, the project wins; say so once and move on.

## Ground rules

- Tests remove the fear of change; without them refactoring stops and code rots.
- Test code meets the production cleanliness bar (efficiency may relax; cleanliness never). Dirty tests are worse than none — hardest to maintain, eventually discarded.
- Tests are the outermost architecture circle: they depend inward, nothing depends on them. Design them; don't bolt them on.

## TDD loop (new code)

Work in tight red-green-refactor cycles: write no more test than suffices to fail, no more production code than suffices to pass, then refactor test and production code together (dedupe, express intent, minimize). The binding rule is that **tests land in the same change as the code they cover** — test-first is the discipline that makes that easy, not an end in itself. Write a transaction's error/cleanup path first.

## Quality checklist (writing or reviewing)

- F.I.R.S.T.: **F**ast (slow tests don't get run) · **I**ndependent (no shared state, no cascading failures) · **R**epeatable in any environment · **S**elf-validating (boolean pass/fail, no log inspection) · **T**imely (written with, not long after, the production code).
- One concept per test, minimal asserts, given-when-then / build-operate-check; split grab-bag tests — splitting also exposes missing boundary cases.
- Boundary conditions exhaustive; coverage tools find untested branches.
- A skipped or ignored test is a documented question about ambiguous requirements — never silent.
- Tests read through a domain-specific testing API (`makePages`/`submitRequest`/`assertResponseContains`), not raw plumbing; refactor it continuously.

## Suite structure

- Never verify business rules through volatile things (GUI navigation) — one navigation change must not break a thousand tests.
- Don't let a test know more about a unit's internals than its callers do. Mirroring the source layout file-for-file is fine — many projects mandate exactly that, one test file per module — but mirroring the *implementation* freezes it. Test through the surface the production code exposes, so tests and production code can evolve independently.
- Give the testing API superpowers: bypass security, skip expensive resources, force testable states; deploy its dangerous parts as a separate component.

## Hard-to-test code = missing boundary, not an over-ambitious test

1. Find the volatile dependency (clock, network, DB, GUI, third-party API).
2. Interface owned by the policy side; inject the implementation (real gateway in production, fixed stub in tests).
3. Humble Object split: logic → testable Presenter/Interactor; untestable rump (View, gateway impl, listener) → bare data-moving.
4. Construction/wiring → Main/factories/DI so tests assemble their own object graph.

## Third-party and architecture tests

- Learning tests per non-trivial third-party API: encode your understanding of its behavior — free (you had to learn it anyway), flags behavior changes on upgrades.
- Fitness functions: encode each architecture rule as an ordinary test in the project's existing suite — a check that walks the import graph and asserts no cycle is usually 30 lines, and layer-access rules, metric thresholds and performance budgets are the same shape. Reach for a dedicated architecture-testing tool only if the project already has one; governance must be executable (characteristics are important-but-not-urgent, and urgency otherwise wins). Explain each function's purpose before imposing it.
- Grow the suite from escapes: every QA/production defect → a test + testing-checklist item; every failed deployment's root cause → release-checklist item; automate any checklist item that can be a test.

## Concurrency (when threads are involved)

Non-threaded code working first; threading pluggable/tunable; more threads than cores, all target platforms; jiggle execution (yield/sleep instrumentation) to force rare orderings; ANY spurious failure = real threading bug, never a one-off.

Sources: Clean Code, Clean Architecture, Fundamentals of Software Architecture.
