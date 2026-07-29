---
name: architecture-tradeoffs
description: Analyze a significant architectural decision as an explicit trade-off — select driving characteristics, compare options/styles in a trade-off table, check distributed-computing realities, and record the outcome where the project already records decisions. Use when choosing between architecture styles or technologies (monolith vs microservices, sync vs async, DB/queue/framework selection), when a decision keeps getting re-litigated, or when asked to document or justify an architectural choice.
---

# architecture-tradeoffs

**Scope**: if a task or sub-plan defines owned files, stay inside them — anything else is a finding, not an edit.
**Conventions**: where this skill and the project's established convention disagree, the project wins; say so once and move on.

**A decision already recorded and accepted is an INPUT to this skill, not its subject.** Re-open one only when the user asks, or when new evidence contradicts the recorded consequences — and then supersede the record in place rather than writing a rival document beside it.

## Laws to hold throughout

1. Everything is a trade-off — a trade-off-free option means you haven't found its cost yet. Never recommend without disadvantages.
2. Why beats how: the reasoning is the most valuable artifact — structure can be reverse-engineered, rationale cannot.
3. Aim for least-worst, not best: "no wrong answers in architecture, only expensive ones."

## Procedure

**1. Frame.** State the decision, scope, business context. Translate stakeholder language into characteristics: "mergers" → interoperability/scalability/extensibility; "time to market" → agility = testability + deployability; "zero downtime" → availability; user counts → scalability AND elasticity (bursts). Mine implicit characteristics (security, availability) the requirements never state.

**2. Driving characteristics — at most three.** Let the fewest characteristics drive the design: ask stakeholders for their unordered top three; test criticality with "if we must drop one, which?" Supports-everything capsizes (the Vasa). Define each measurably (deployment success rate, p95/p99 latency budget, coverage) — maximums/percentiles, never just averages.

**3. Scope by quantum.** Quantum = independently deployable artifact with high functional cohesion and synchronous connascence; characteristics attach to quanta, not the whole system. One uniform set → monolith suffices; genuinely different characteristics per part (auctioneer vs bidder availability) → distributed. A shared database collapses services into one quantum.

**4. Trade-off table.** Per option, list advantages AND disadvantages against the drivers (model: topics → extensibility/decoupling; queues → security, heterogeneous contracts, monitorable scaling); ask "which matters more in this context?"; decide with stakeholders. Styles:

- **Layered monolith** — simple systems, tight budget/time. Cost: poor deployability/elasticity; sinkhole if >80% of requests pass layers untouched.
- **Pipeline** — one-way data transformation stages. Cost: technical partitioning.
- **Microkernel** — customization-heavy products (per-client/jurisdiction plug-ins). Cost: core–plugin contract discipline.
- **Service-based** — coarse domain services; most pragmatic distributed style; keeps ACID. Cost: coarser granularity than microservices.
- **Event-driven** — top-tier performance/scalability/fault tolerance/evolvability. Cost: low testability; eventual consistency.
- **Space-based** — extreme elasticity/scalability (DB out of the synchronous path). Cost: very complex, costly, hard to test.
- **Microservices** — bounded contexts; independent evolution/deploy; per-service data. Cost: weak performance; prefers duplication to coupling; "micro" is a label, not a size target — merge services that must chat constantly.
- **Orchestration-driven SOA** — cautionary tale: maximizing reuse maximized coupling, worst of monolith AND distributed.

Weigh: domain understanding, structure-impacting characteristics, data architecture, org factors, ops maturity, domain–architecture isomorphism. Hybrids are legitimate.

**5. Any distribution? Fallacy check.** The network is NOT: reliable, zero-latency, infinite-bandwidth, secure, static, singly-administered, free, homogeneous. Chained calls multiply latency (10 hops × 100 ms = 1 s); judge by p95–p99, not averages; no stamp coupling (500 KB payload for 200 needed bytes); timeouts + circuit breakers; sagas over distributed transactions — better, draw service boundaries so transactions stay inside one service. Default **synchronous**; queues/topics only where quanta differ in throughput/operational characteristics or fire-and-forget is worth the error-handling cost — record that reasoning with the decision.

**6. Defer what you can.** Maximize decisions NOT made: split what must be decided now from what can stay open behind an interface (DB, broker, framework) until more information arrives. Last responsible moment — deliberate deferral, not avoidance.

**7. Record it where the project already records decisions** — otherwise the debate recurs. If the project has a plan file, a decision log, a design-law document or an authority ladder, append there, in that document's own numbering and voice. Do **not** stand up a second system of record: a fresh `docs/adr/` beside an existing decision log outranks nothing and reads authoritative, which is the Groundhog Day this step exists to prevent. Only where a project records decisions nowhere is a new record the right move, and then this shape works:

```markdown
# ADR-NNN: <Title>
Status: Proposed | Accepted | Superseded by ADR-MMM  (RFC until <deadline> to stop analysis paralysis)
## Context: forces, driving characteristics, constraints — why this came up
## Decision: We will use <X> — commanding voice; technical AND business justification
## Consequences: the trade-off table's losers — what gets worse, what we accepted and why
## Compliance: governance — ideally an executable check; who may vary it
## Notes: author, date, superseded links
```

Whatever the form: one system of record, organized by scope; notify by link only, only those impacted.

**8. Govern and de-risk.**
- Turn each recorded rule into an executable check in the project's existing suite (cycle detection, layer-access rules, distance-from-main-sequence thresholds, performance budgets), run however that project runs its tests; explain each one's purpose to developers before imposing it. Governance nobody executes is a wish.
- Risk = impact (1–3) × likelihood (1–3), impact first; unproven/unknown technology automatically scores 9. High risk → prototype and benchmark in a production-like environment: "demonstration defeats discussion."

## Anti-patterns to call out

- **Groundhog Day** — same debate keeps recurring → record the why, with technical AND business justification, in the project's existing record.
- **Covering Your Assets** — decision endlessly avoided → last responsible moment; collaborate with implementers.
- **Email-Driven Architecture** — decisions living in inboxes → single system of record, link-only notification.
- **Ivory Tower** — deciding in isolation from the implementing team → decide with them; designs are first drafts refined by implementation feedback.
- **Vasa / generic architecture** — "supports everything" characteristic list → cut to the top three drivers.
- **Over-specified guidance** — mandating one named library when "a reactive framework" is the real constraint → guide choices; mandate tech only to protect a critical characteristic.

Sources: Clean Code, Clean Architecture, Fundamentals of Software Architecture.
