# Frontier Discovery Playbook

Frontiers are underexplored evidence regions, not question requests. A good
frontier points Kimi toward a productive memory region while leaving discovery open.
Optimize for future SEBI-document memory coverage, not novelty for its own sake.

## How To Find Frontiers

Create new frontiers only when the user explicitly asks for ledger refresh.
Before exploring, refresh and read the compact previous-region memory:

```bash
python3 scripts/metadata-graph/opencode-kimi/frontier_ledger.py export-explored-regions
```

Read:

- `scripts/metadata-graph/opencode-kimi/handoff/PIPELINE_GOAL.md`
- `scripts/metadata-graph/output/opencode_kimi/previous_explored_regions.md`

Do not read or request graph coverage statistics for frontier discovery unless
the user explicitly asks for that. Aggregate stats can over-steer exploration.
Use the previous-region memory only to avoid repeating queued, active,
saturated, rejected, or duplicate regions.

Use the graph and corpus yourself. Do not read coverage stats, do not crawl old
summary artifacts by default, and do not bulk-import notes from prior runs.

Frontier lenses to try in the graph/corpus:

These are inspiration, not a taxonomy and not allowed relationship types. Search
smartly for whatever coherent relationship the corpus naturally supports.

- graph neighborhoods around productive or oddly connected docs
- citation chains, amendment trails, master circular updates, and
  consultation/proposal/final/relaxation paths
- same matter, issuer, scheme, appeal, defaulter, recovery certificate, or
  corporate-action sequence
- enforcement order -> recovery certificate -> attachment/remittance/release
  chains
- open offer PA/DPS/DLOF/LOF/corrigendum/post-offer lineages
- scheme meeting notices, newspaper ads, explanatory statements, voting records,
  and downstream transaction filings
- repeated obligations across intermediary classes or market infrastructures
- the same mechanism appearing across different regulations or circulars
- exception, carve-out, transition, exemption, deadline, or implementation
  timelines
- investor workflow or regulated-entity compliance workflow chains
- operational infrastructure dependencies, payment/settlement flows, disclosure
  packages, or approval authorities
- rule plus enforcement/recovery example when both documents truly illuminate
  one practical memory target
- cross-corpus patterns with a clear comparison axis
- "weird neighbor" graph relationships that become meaningful after document
  inspection

Use broad search to find a region, then write a frontier only when there is a
plausible multi-document angle. A summary note is a clue, not a frontier.
Spend the main reasoning budget on graph exploration, not on interpreting
pipeline metrics.

## Good Frontier Shape

A frontier should contain:

- a short title
- optional seed docs
- a short scope hint explaining why the docs may be worth reading together
- why it is not already covered
- avoid notes for nearby prior work

Keep it compact. Do not paste evidence chunks into the ledger.

Treat `scope_hint` as a true hint, not an evidence report. It should point Kimi
at the relationship and candidate axes to inspect. Do not put exact deltas,
thresholds, legal conclusions, final answers, or nuanced provision summaries in
the scope hint. Prefer "verify the timing, price-band, and delivery axes" over
"the final circular changed X to Y."

## Core Frontier Sanity

These are learning bullets, not a rigid taxonomy. Use them to keep frontiers
honest while still exploring freely.

- **READ ENOUGH TO VERIFY THE SEED RELATIONSHIP.** Doc titles and `doc-meta`
  dates are useful for orientation, but they are not enough. Open the actual
  seed-doc content with `sebi_research.py chunks` or nearby commands to confirm
  that the docs belong in one evidence region. If you cannot verify the
  relationship, do not add the frontier yet.

- A frontier is a coherent evidence region, not a topic pile. It should point
  toward durable SEBI-document memory: rules, dates, actors, exceptions,
  procedures, filings, document lineage, or practical relationships.
- The scope hint is a launchpad, not evidence. If it says "same company",
  "same certificate", "same scheme", "same matter", "consultation-to-final", or
  "amendment lineage", every seed doc must satisfy that broad relationship.
  Leave exact values and final deltas for the question-generation worker.
- Every seed doc should have a role in the frontier. Examples include baseline,
  consultation, final circular, amendment, notice, letter of offer, post-event
  announcement, attachment, remittance, release, cancellation, and parallel
  comparator. If you cannot name the role, the doc probably does not belong in
  `seed_doc_ids`.
- Do not seed merely adjacent examples. Adjacent examples belong in `avoid`,
  notes, or a separate frontier unless the frontier is explicitly a cross-corpus
  pattern.
- Cross-corpus frontiers are allowed when they teach memory across repeated SEBI
  document forms, but the axis must be explicit. Good axes include deadline
  mechanics, voting eligibility, disclosure-package contents, settlement
  sequence, lock-in rule changes, recovery-step consequences, approval
  authority, threshold changes, or proposal-versus-final treatment.
- A lineage frontier must include actual lineage steps. If "consultation-to-final"
  is the claim, the seeds need a consultation/proposal and a final circular,
  order, amendment, or master circular. If the final document is not found,
  rename the frontier honestly, such as "consultation against existing baseline",
  or leave it out for now.
- A same-matter frontier should share a real anchor, such as certificate number,
  appeal number, issuer/deal, scheme, defaulter, order lineage, regulation, or
  amendment chain.
- Before adding a frontier, run a contradiction check: could this seed be swapped
  with another document from the same broad category without breaking the stated
  scope? If yes, the frontier is probably too vague, unless the scope explicitly
  says it is a cross-corpus pattern and names the comparison axis.
- Prefer fewer, cleaner seeds over broad seeding. Two precise documents are
  better than five loosely related ones.
- If the region is promising but messy, split it. The explorer's job is not to
  maximize ledger rows; it is to leave Kimi workers clean launchpads for
  memory-dense QA generation.
- Verification must be real. Use the graph and corpus to verify that seed docs
  exist and match the hinted relationship. If a search or hydration call falls
  back to graph metadata only, treat the seed relationship as unverified rather
  than confirmed.

## What To Avoid

Avoid frontiers that are only:

- a single isolated document with no natural companion
- “compare two docs” without a meaningful axis
- a vague sector name with no document path
- a duplicate of an existing practical question intent
- a note that says the evidence is absent, future-only, or not yet hydrated

Comparison is allowed when the axis is real: deadline, amount, actor, condition,
procedure, rationale, exception, implementation, or consequence.

## Automated Explorer Output

Automated explorers must not call `frontier_ledger.py add`. They write JSONL
candidates to the exact path in their explorer run card. The deterministic
validator promotes only valid candidates to the live ledger.

Use this candidate shape:

```json
{
  "title": "Short label",
  "kind": "exploratory",
  "seed_doc_ids": ["clf-example-a", "clf-example-b"],
  "candidate_doc_ids": [],
  "scope_hint": "Directional relationship note and candidate axes to inspect; verify all values from source text.",
  "why_unexplored": "Why current accepted/pending questions do not cover it.",
  "avoid": "Nearby memory regions or question intents to avoid.",
  "source": "opencode_explorer",
  "source_ids": ["explorer_run_id"],
  "notes": "Short verification note naming what was checked.",
  "single_doc_ok": false,
  "seed_roles": [
    {"doc_id": "clf-example-a", "role": "consultation", "verified_chunk_ids": ["clf-example-a#0"]},
    {"doc_id": "clf-example-b", "role": "final", "verified_chunk_ids": ["clf-example-b#0"]}
  ]
}
```

## Manual Seeding Command

For manual one-off frontier additions, use:

```bash
python3 scripts/metadata-graph/opencode-kimi/frontier_ledger.py add \
  --title "Short label" \
  --kind exploratory \
  --seed-doc-id clf-example \
  --scope-hint "Directional relationship note and candidate axes to inspect; verify all values from source text." \
  --why-unexplored "Why current accepted/pending questions do not cover it." \
  --avoid "Nearby memory regions or question intents to avoid."
```
