---
name: pattern-selection
description: Match a recognized code problem (recurring conditionals, subclass explosion, telescoping constructors, incompatible interfaces, event fan-out, undo/queueing) to the right GoF design pattern — or argue against using one — with costs weighed. Use when deciding whether/which pattern to apply, when refactoring smell-heavy code toward a pattern, or when reviewing code that over-uses patterns.
---

# pattern-selection

**Scope**: if a task or sub-plan defines owned files, stay inside them — anything else is a finding, not an edit.
**Conventions**: where this skill and the project's established convention disagree, the project wins; say so once and move on.

## Gate — earn the pattern first

Every pattern is bought with complexity. All three must hold, else recommend NO pattern and say why:

1. **Its specific problem exists in the code now** — not one you imagine arriving later; speculative application does more harm than good.
2. **The variation is real**: a plausible external reason to change (tax rules, formats, backends). Extracting stable code adds indirection for nothing.
3. **Nothing simpler suffices** — simplest thing that can possibly work; with first-class functions, a lambda often replaces a Strategy class.

## In a dynamic language, a function is usually the pattern

The catalogue below was written for statically-typed class hierarchies. In Python, JavaScript, Ruby, Elixir, Lisp and friends, plain functions, closures and modules already provide most of what several patterns exist to simulate — recommend the class only when the simpler form has actually run out:

- **Strategy** → pass the function. A dict or match mapping keys to callables is the dispatch table; a class per algorithm adds a name and nothing else.
- **Command** → a closure (or a small tuple/record) carries the call and its arguments; reach for the class only when undo/redo needs paired operations and stored state.
- **Template Method** → a function taking the varying steps as callable arguments, or a generator yielding at the variation points; no subclass required.
- **Factory / Abstract Factory** → the module or the class object *is* the factory; a function returning a configured object covers most of it.
- **Singleton** → a module-level value is already one per process; don't build `getInstance()` around it.
- **Decorator** → a higher-order function wrapping a callable (and, where the language has them, decorator syntax) — reserve the wrapper-class form for objects with a wide interface.
- **Adapter / Facade** → a module of thin functions over the foreign API is usually enough; a class earns its place once it holds state.

The patterns that stay valuable in dynamic languages are the ones about *object relationships*, not about substituting a missing first-class construct: Composite, Observer, Proxy, State (once transitions carry state), and the boundary wrappers below.

## Principles behind the choice

- **Encapsulate what varies** — isolate the changing aspect behind a method/class boundary; nearly every pattern is this applied to one kind of variation (creation, algorithm, state, platform).
- **Program to an interface**: declare exactly what the client needs; depend on that — only where you genuinely expect extension, not everywhere by default.
- **Favor composition over inheritance**: two+ independent variation dimensions (car/truck × electric/gas × manual/auto) must each become a delegate hierarchy, or subclasses explode combinatorially. Reserve inheritance for one dimension with a true behavioral subtype.

## Symptom → pattern → when NOT

- **Factory Method** — scattered `new ConcreteX()` where the type may vary; framework needs a substitutable component; expensive objects to pool. NOT: no realistic variation in what's created — a creator hierarchy for nothing.
- **Abstract Factory** — *families* of products that must match (Win/Mac widgets); a class grew a blurry set of factory methods. NOT: single product, no variants — heavy class/interface cost.
- **Builder** — telescoping constructors / many optional params / overload forest; same steps, different representations; assembling trees. NOT: simple objects — always adds classes; a language with keyword or default arguments rarely needs one.
- **Singleton** — genuinely single shared resource (one DB gateway) needing controlled global access. NOT: almost everywhere else — a disguised global: hidden coupling, SRP violation, untestable clients. Prefer DI/explicit passing; if used, keep creation inside `getInstance()` so the constraint can relax later.
- **Adapter** — third-party/legacy class with the wrong interface. NOT: you own the class — just change it to fit.
- **Composite** — genuinely tree-shaped part-whole domain (GUI trees, nested orders); leaf and subtree treated alike. NOT: candidate classes differ too much — a forced common interface overgeneralizes.
- **Decorator** — add/remove responsibilities per object at runtime; behavior combos (compress+encrypt) would explode subclasses; `final` blocks inheritance. NOT: deep or order-sensitive wrapper stacks would result — keep stacks shallow, centralize assembly.
- **Facade** — using a fraction of a sprawling subsystem; layers need a single entry point. NOT: drifting into a god object — keep it a thin shortcut; let advanced clients bypass it.
- **Proxy** — interpose on access: lazy init, access control, remote call, logging, caching. NOT: the extra hop/classes cost more than the interposition is worth.
- **Observer** — one object's state change must notify an open-ended, runtime-changing set. NOT: logic would depend on notification order — order is unspecified.
- **Strategy** — a conditional dispatches interchangeable algorithm variants; runtime swap; near-duplicate classes differing in one behavior. NOT: few rarely-changing variants; or a function/lambda does it without class ceremony.
- **Command** — requests need queueing, scheduling, logging, sending, composing, or undo; UI elements parameterized with operations. NOT: direct one-off calls — it inserts a whole layer between sender and receiver.
- **State** — behavior depends on state; field-value conditionals repeated across methods; states/transitions churn. NOT: few stable states — keep the `if`s.
- **Template Method** — classes share an algorithm skeleton with varying steps; clients should extend only specific steps. NOT: runtime switching needed (use Strategy); many steps degrade maintainability; a subclass would suppress a step (LSP violation).

Cross-book placements: Adapter + your own narrow wrapper at every third-party boundary, and "the interface we wish we had" for not-yet-existing dependencies; Abstract Factory to create volatile concretes without source dependencies on them; a one-way Strategy-style interface or Facade doubles as a cheap partial architectural boundary.

## New class hierarchy? LSP checklist

- [ ] Parameter types equal or more abstract than the parent's; return types equal or more specific.
- [ ] No new exception types thrown.
- [ ] Pre-conditions not strengthened; post-conditions not weakened; superclass invariants preserved.
- [ ] Subclass can't honor the contract? Restructure: make the *restricted* variant the base, the capable one the extension.

## Over-patterning red flags (reviewing)

- Pattern with one implementation and no second variant in sight, "for flexibility".
- Singleton as a convenience global; tests painfully mocking around it.
- Interfaces subdivided beyond any client's need — more interfaces is more complexity too.
- A Facade every class in the app touches (god object).
- Inheritance for code reuse rather than a true subtype.
- A class hierarchy in a dynamic language doing what one function and a lookup table would do.

## Name the pattern you apply

Use standard names in code and explanation (`ShippingStrategy`, `AccountVisitor`, `JobQueue`): shared nomenclature is part of expressiveness and gives the team precise language ("that facade is becoming a god object").

Sources: Dive Into Design Patterns, Clean Code, Clean Architecture, Fundamentals of Software Architecture.
