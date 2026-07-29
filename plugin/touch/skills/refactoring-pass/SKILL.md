---
name: refactoring-pass
description: Execute a safe, incremental, behavior-preserving refactoring — Boy Scout cleanups, deduplication, conditionals-to-polymorphism, coupling reduction — with a tests-first safety net and stopping rules. Use when asked to refactor or clean up code, when touching messy/legacy code as part of another change, or after tests go green in a TDD cycle.
---

# refactoring-pass

**Scope**: if a task or sub-plan defines owned files, stay inside them — anything else is a finding, not an edit.
**Conventions**: where this skill and the project's established convention disagree, the project wins; say so once and move on.

## Rules of engagement

- Behavior-preserving, tiny steps: change a few lines, re-run tests, repeat.
- Safety net first: no refactoring without tests — if the target has none, first write characterization tests pinning current behavior.
- Boy Scout scale, not grand redesign — redesign races against the legacy system take years and produce a new mess. "Later equals never": one small cleanup NOW, in this change, then stop.
- Keep refactoring commits separate from behavior-changing commits.

## Ordered playbook (tests green before each step)

1. **Delete free wins** on sight, no discussion: commented-out code, dead functions, unreachable branches, change-journal and noise comments. VCS remembers. **Not a free win:** a comment recording *why* — what it prevents, which decision it implements. That is a decision record, not noise; keep it, and if it is long keep it as a module/file docstring (the `/touch:code-quality-review` skill carries the same exemption).
2. **Rename for intent**: intention-revealing, pronounceable, searchable; one word per concept; magic values → named constants (connascence of name is the weakest, safest coupling).
3. **Replace restating comments with code**: extract the commented block into a function named what the comment said, or introduce an explanatory variable — for comments that describe *what* the code does, never for the ones that record why.
4. **Shrink functions**: extract until each does one thing at one abstraction level; callees below callers (stepdown/newspaper); split flag-argument functions into two; separate commands from queries; group cohesive argument clusters into objects.
5. **Kill duplication — after verifying it's real**: extract repeats; repeated switch/if-else chains over the same conditions → polymorphism; Template Method or Strategy for similar-but-not-identical algorithms. True-versus-accidental duplication is the gate, and the `/touch:code-quality-review` skill carries that calibration — never unify copies that will diverge.
6. **Conditionals → objects where variation churns**: state machines → State; algorithm dispatch → Strategy — but keep the `if`s when variants are few and stable (the `/touch:pattern-selection` skill has the when-NOT rules).
7. **Split overgrown classes**: a subset of methods sharing a subset of fields is a class trying to get out; split by reason-to-change/actor.
8. **Weaken coupling**:
   - Train wrecks (`a.getB().getC().doD()`) → tell-don't-ask methods on the collaborator.
   - Strong connascence (position/meaning/algorithm) → name; the greater the distance between modules, the weaker the connascence allowed; push strong forms inside encapsulation boundaries.
   - Repeatedly-edited classes → open for extension: abstract base + one derivative per operation.
   - Volatile concrete dependencies → interface + injection; hoist their `new` to main/factories.
9. **Seal boundaries**: third-party types leaking through signatures → wrap in your own narrow class/adapter.

## Definition of done — four rules, priority order

(1) all tests pass, (2) no duplication, (3) expresses intent, (4) minimal classes/methods — rule 4 is LOWEST: don't multiply tiny classes dogmatically.

## Stopping rules — when NOT to keep going

- The variation has no realistic reason to change — extraction only adds indirection.
- The pattern would be speculative, not a response to a recognized problem.
- The codebase is tiny — decomposing a 200-line program is wasted effort.
- The next step changes observable behavior — that's a feature/bugfix; note it, handle separately (fix bugs in the class directly, don't "extend around" them).
- The cleanup crosses an architectural boundary or ownership line outside the task — record a finding instead.

## Red flags (quick scan)

Long functions with comment "sections" · flag arguments · output arguments · train wrecks · duplicated switch statements · half-object/half-data hybrids · null returns/null checks everywhere · commented-out blocks · God classes named `Manager`/`Processor` · magic numbers · `new` of volatile concretes deep in business logic.

Sources: Dive Into Design Patterns, Clean Code, Clean Architecture, Fundamentals of Software Architecture.
