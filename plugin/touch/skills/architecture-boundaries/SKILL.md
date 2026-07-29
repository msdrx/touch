---
name: architecture-boundaries
description: Design or audit module boundaries, layering, and dependency direction — Dependency Rule, SOLID, policy-vs-detail separation, component cohesion/coupling metrics. Use when structuring a new project or feature, adding a framework/database/third-party dependency, extracting a component or service, or checking whether business logic is coupled to infrastructure.
---

# architecture-boundaries

**Scope**: if a task or sub-plan defines owned files, stay inside them — anything else is a finding, not an edit.
**Conventions**: where this skill and the project's established convention disagree, the project wins; say so once and move on.

## The one rule everything serves

Source-code dependencies point only inward, toward higher-level policy: no name declared in an outer circle (UI, DB, frameworks, devices) may be mentioned by an inner circle (use cases, entities). "Level" = distance from I/O.

## Calibrate to the codebase first

Boundaries are bought with indirection. A few hundred lines with one author and no volatile dependency does not want ports and adapters — it wants clear names and one obvious seam where I/O happens. Apply the full procedure where a system has multiple actors, a real framework/DB commitment, or components that deploy separately; below that, report the *one* leak that would hurt and stop.

## Procedure A — designing structure (new project / feature / module)

1. **Split policy from detail.** Business rule that would exist without automation → Entity; application-specific orchestration → Use Case; web, DB, UI, queue, framework, hardware → mechanism. Details are plugins; policy never names them.
2. **Find the axes of change.** Group code by actor — who requests changes to it. Different actors → different modules; boundaries belong there.
3. **Choose top-level partitioning consciously**: domain (workflows: `CatalogCheckout`, `Purchase`) vs technical (presentation/business/persistence). Prefer domain when changes are domain-driven, teams are cross-functional, or later distribution is plausible. Entity trap: one `XManager` per DB table is an ORM, not an architecture.
4. **Invert dependencies at every policy→detail call**: insert an interface *owned by the high-level side*; the detail implements it. Define the interface the policy *wishes it had* (`openReport(file)`, not `openFile/readBytes/close`).
5. **Cross boundaries with simple data only**: request/response models deriving from nothing — never Entities, DB rows, or `HttpRequest` — in the form most convenient for the inner circle.
6. **Humble Object at every hard-to-test boundary**: logic → testable Presenter/Interactor; untestable rump (View, gateway impl, listener) → bare data-moving.
7. **Make Main the dirtiest plugin**: all construction, wiring, config, DI-framework use concentrated there; nothing depends on Main. One Main per configuration if useful.
8. **Size each boundary to its risk; defer the rest.** Full boundaries are expensive: where one only *might* be needed, use a partial boundary (full separation deployed as one component, a one-way Strategy-style interface, or a Facade); upgrade when cost-of-ignoring exceeds cost-of-implementing. Keep components in one address space as long as possible (source → deployment → service), able to slide back.
9. **Maximize decisions not made**: don't pick DB, web framework, message broker, or DI container early; write policy agnostic to them even if the org already committed.

## Procedure B — auditing existing structure (report file:line evidence)

1. **Screaming test**: does the top-level structure shout the domain ("accounting system") or the framework? A newcomer should learn what the system *does* from the directory tree.
2. **No-infrastructure test**: every use case unit-testable with no web server running and no DB connected? If not, find the leaked dependency.
3. **Grep for inward leaks**: framework imports/annotations, SQL, HTTP types, ORM entities inside entity/use-case code — policy welded to volatile detail. Framework base classes and injection annotations in business objects marry the framework (it never comes back out): inject in Main only; derive proxies rather than inheriting framework classes.
4. **Interface ownership**: the high-level side owns each policy→detail interface; flow-of-control crossings (use case → presenter) go through inner-owned output ports.
5. **Fat-dependency check**: anything depending on a module/framework containing far more than it uses? Segregate per-client interfaces; weigh transitive baggage — but don't subdivide an already-specific interface.
6. **Substitutability**: implementations forcing type tests / special-casing in clients violate LSP; quarantine third-party contract violations in one config-driven adapter, not scattered ifs.
7. **Component graph metrics** (multi-component systems):
   - No cycles — a cycle fuses components into one giant release unit ("morning-after syndrome"); break with DIP or extract a shared component.
   - Instability I = FanOut/(FanIn+FanOut) must decrease along each dependency edge; heavily-depended-on components should be abstract. Investigate outliers on D = |A+I−1|: Zone of Pain (stable + concrete, e.g. a volatile DB schema everyone depends on), Zone of Uselessness (abstract, no dependents).
   - Connascence: refactor strong/dynamic forms to weaker (name > meaning > position…); greater distance demands weaker connascence; minimize connascence crossing encapsulation boundaries.
8. **Encapsulation enforcement**: compiler-enforced (package-private implementations, module systems, separate source trees) or convention only? All-public types decay into an undisciplined layered blob. Where the rule matters, encode it as an ordinary test in the project's existing suite — a cycle check or a layer-access check over the import graph is usually a few dozen lines and needs no new dependency.

## Red flags (report on sight)

- Entities or DB rows passed across boundaries — couples both sides to one representation.
- Two services/components sharing one database — one quantum, not independently deployable.
- One class serving two stakeholder groups — change for one actor breaks the other; merge collisions.
- Repeated edits to the same class for each new feature — OCP failure; new behavior should arrive as new code.
- Third-party interface type in a public API — many places to fix when it changes; depend on what you control.
- "Reuse" justifying coupling services to shared libraries — maximizing reuse maximizes coupling/fragility (SOA's failure).

## Component grouping tension (when carving components)

Balance REP (release granule = reuse granule), CCP (gather what changes together), CRP (don't force users to depend on what they don't need). Early project: favor CCP (develop-ability); mature/reused: slide toward REP/CRP. Expect the partitioning to jitter — component structure can't be designed top-down once at project start. Iterate: identify components, assign requirements, analyze characteristics, restructure, repeat with developer feedback.

Sources: Clean Code, Clean Architecture, Fundamentals of Software Architecture.
