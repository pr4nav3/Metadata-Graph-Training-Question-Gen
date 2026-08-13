# OpenCode SEBI Research Instructions

You are researching SEBI documents to create gold multi-document training
questions. Use the metadata graph and corpus search as maps. Use document chunks
as evidence. The goal is SEBI-document memory and corpus coverage: write hard,
useful QAs that teach distinct facts, rules, exceptions, dates, actors,
procedures, and document relationships. Uniqueness is only an anti-duplicate
guardrail, not the objective.

## Scope

Work on one natural memory region per run. A memory region can be a matter, issuer,
regulatory lineage, circular/master-circular chain, scheme family, enforcement
order family, recovery flow, or filing lineage. If no seed docs are supplied,
choose the memory region yourself from the graph/corpus. `pass_id` is just a batch
label. `scope_hint`, if present, is only a soft starting note.

If the run card contains `frontier_*` fields, treat them as Codex-curated
starting context for an underexplored evidence region. A frontier is not a
question type, not a quota, and not a hard document boundary. Use it to start
faster, then expand or reject it using the same evidence standard as any other
memory region. Do not read the whole frontier ledger unless explicitly asked.

Expand laterally only when the new document deepens the same practical question
space. If a lead points elsewhere, record it as a future lead and do not pursue
it in this session.

Do not assume the hydration cache is exhaustive. If a document is missing, fetch
it from Vespa through `sebi_research.py`. Raw PDFs are fallback exploration only;
final records should cite stable chunk IDs like `clf-...#23`.

## Working Style

Move quickly from broad survey to a tentative memory region. Use compact broad
searches first. Prefer `search-corpus-brief` for broad discovery; if it reports
`fallback: graph_metadata_only` or an error, do not retry the same broad search.
Choose promising doc IDs from the fallback/graph output and switch to targeted
`doc-overview`, `search-doc`, `around`, and `chunks`. Once you have at least two
promising documents in one coherent memory region, write `memory_choice_path`; then
read deeply.

Do not keep searching silently. After a few broad searches, either write
`memory_choice_path` for the best concrete multi-document lead or write a
checkpoint explaining why the frontier is thin. If the memory region later proves
weak, duplicate, too small, or not truly multi-doc, do not force questions:
write a checkpoint/summary with inspected docs, rejection reasons, and future
leads. Do not salvage a weak assigned frontier by pivoting into a merely
adjacent memory region; either stay on the assigned practical question space or mark
`Frontier outcome: no_multidoc_angle` / `needs_review` with concrete future
leads.

Append each accepted question to the JSONL as soon as it is accepted. Do not run
a long research pass without writing `memory_choice_path`, `checkpoint_path`,
or an output row.

Copy artifact paths exactly from the run card or run prompt. Do not reconstruct
paths from the current working directory or from another worktree.

## Human Questions

Use `questions/SEBI_questions_answers.csv` for tone and difficulty patterns, not
as evidence. Good human SEBI questions are practical, standalone, and often
messy: they ask for applied judgment, filtering, absence checks, version deltas,
cross-source identity resolution, counts, timelines, or remedies.

Think of the following as a menu of human question frames, not a checklist or
quota. Use the frame the evidence naturally supports; one strong question may
blend more than one frame.

- Actor situation: "We are restructuring promoter holdings into family trusts; when can SEBI exempt us from an open offer?"
- Current-rule lookup: "Which documents must a Research Analyst check to know the current audit and certification rules?"
- Version delta with consequence: "What changed for REIT complaint review after the amendment?"
- Current scope or eligibility: "Which transactions actually qualify for the new netting route?"
- Proposal versus final rule: "Which consultation-paper proposal was retained, changed, or dropped in the final circular?"
- Deadline or effective-date trap: "Was the filing already late, or did SEBI extend the deadline?"
- Threshold or exception trap: "Does the lower minimum investment apply here, or only if the fund meets a condition?"
- Approval authority: "Who has to approve the extension, waiver, or partial fund-raising route?"
- Specific-axis comparison: "How do the two recovery certificates differ in amount and basis of dues?"
- Parallel case comparison: "How are the Sandhar and MPS exemption orders similar in rationale and different in acquisition route?"
- Amount reconciliation: "Did the amount sought at remittance stay the same as the attachment amount?"
- Cost or burden allocation: "Which cost falls on the scheme first, and when does the AMC bear the excess?"
- Scope exclusion: "Which cases remain outside the new framework even after the relaxation?"
- Procedural timeline: "What happened from demand notice to attachment to remittance?"
- Procedural escalation: "Why did SEBI move from ordinary account attachment to a prohibitory order?"
- Completion or release consequence: "What changed after the recovery was completed?"
- Entity or role disambiguation: "Which individuals and business names carried through from the SCN to the final order?"
- Related-party trace: "Why did SEBI target the N Kumar companies in the Linkhouse recovery?"
- Common obligation across documents: "What post-exemption obligations appear in all three orders?"
- Operational meeting or voting mechanics: "Who could vote at the scheme meetings and what were the cut-off dates?"
- Investor-disclosure comparison: "What should an investor notice about the ratings, cover, coupon, or minimum bid?"
- Market-structure or regime comparison: "How do block deals and special call auctions approach price discovery differently?"
- Absence or non-implementation check: "Was the proposed moratorium retained in the later circular or master circular?"
- Supporting-package question: "What documents did the notices tell voters to read before approving the scheme?"
- Count or corpus filter: "Which entities appear as defaulters across these recovery proceedings?"
- Policy rationale or lesson: "What problem was SEBI trying to solve, and what should the regulated party or investor learn?"

Useful `question_type` values:

- `applied_compliance_scenario`
- `negative_or_absence_check`
- `document_diff_or_lineage`
- `entity_cross_reference`
- `count_or_exhaustive_filter`
- `checklist_compliance_audit`
- `operational_timeline_or_remedy`
- `conditional_table_extraction`

Useful `difficulty_features` include fake negatives, threshold/date/consent
traps, entity disambiguation, relationship chains, version deltas, explicit
exclusions, and practical answer shapes such as tables, timelines, yes/no with
caveats, or next steps.

Taste test: Ask as if a lawyer, compliance officer, investor, journalist, or
analyst came with a real confusion, not as if you are summarizing two source
documents. A comparison question is good when it has a meaningful axis: amount,
deadline, actor, condition, rationale, consequence, procedure, exception, or
implementation. Exact dates, circular names, PANs, RC numbers, and section
numbers are useful when they disambiguate the issue or create the trap; otherwise
prefer the actor, event, obligation, or practical problem.

Prefer crisp standalone wording. A difficult question can be short. Before
accepting a row, read it once as a human question: would someone plausibly ask
this before knowing the exact documents, and does the answer require multiple
documents to resolve a decision, classification, amount, deadline, next step, or
understanding? If two candidates use the same evidence and ask the same practical
thing with different wording, keep the stronger one.

Do not make a question better by stacking clauses. Prefer one clear practical
confusion, one main axis, and one answer shape. If a question has multiple
subparts, each subpart must be necessary to resolve the same real user need.

Prefer natural type variety when the memory region supports it. Never force a type or
ratio.

Avoid graph-shaped wording, chunk language, "provided documents", "these
orders", "the same two", answer leakage, and unnecessary PAN/RC/section
specificity. If the answer may be negative, ask neutrally: "what direction, if
any..." rather than assuming a direction exists.

## Multi-Doc Necessity

Each final question must truly require at least two documents. Removing any
cited document should make the answer incomplete, materially weaker, or change
the practical meaning.

Strong patterns:

- baseline + amendment
- proposal/rationale + final circular
- order + recovery/remittance/release/completion
- comparison across filings, schemes, parties, meetings, instruments, timelines,
  or obligations
- addendum/corrigendum/master-circular/final-filing lineage

Reject leads where the second document is only background, confirms existence,
repeats the same fact, or is merely related.

## Uniqueness

Read the uniqueness packet near the start. It is project memory, not evidence.
Same documents, entities, matters, chunks, or graph areas may be reused when the
new question has a different practical intent and answer shape.

Reject exact repeated question text, same-intent questions, and same chunk-set
questions with only cosmetic wording changes. Do not reject solely because two
candidates use the same documents, chunks, or broad frame. Similarity is useful
when the newer question teaches a distinct memorization target: a different rule
condition, exception, date, actor, amount, approval authority, procedural step,
document lineage, or factual pattern. Reject when the later question would not
add a meaningful memorization target beyond an already accepted question.

Do not default to the same few comfortable question shapes. Use the frame the
evidence naturally supports, and vary the frame when the evidence supports a
different user need.

Do not optimize for novelty by itself, and do not optimize for chunk novelty:
chunk overlap is only a warning sign. The winning question is the one that adds
useful memory coverage.

Use these labels in your notes/summary:

- `new`: different user need and answer shape
- `adjacent_new`: same memory region, different evidence theme
- `duplicate`: same user intent and answer shape
- `thin_variant`: technically different wording or detail, but the same
  practical user need and memorization target as another question

Before final summary, reread your JSONL and remove/merge same-intent variants.

## Saturation

Write every useful distinct question the memory region naturally supports. This is a
memory-training dataset, so do not stop after only the most elegant questions.
After the obvious strong questions, do a brief secondary pass for distinct
memory targets: different conditions, exceptions, dates, filing items, actors,
amounts, approval authorities, implementation details, proposal-not-adopted
items, procedural steps, or lineage details.

Do not aim for a fixed count: 0 is fine for a bad memory region, 1-2 for a narrow
memory region, 3-5 is normal, and 6-10 is fine when the same memory region has genuinely
distinct evidence themes. Do not pad with same-intent variants.

Before calling a memory region saturated, try at least one non-obvious lateral route:
later order, addendum, amendment, master circular, meeting notice, related
entity filing, same regulation, same issuer, or nearby time period. Stop when
remaining leads are unsupported, same-intent duplicates, fake multi-doc, or a
different memory region. If useful same-memory targets remain but should be
handled in a later small pass, use `Frontier outcome: continue_generation`.

Be conservative about crossing out assigned frontiers. If you handled one
coherent memory region but saw another substantial unexplored subregion, record that
subregion under `Future Leads` with concrete doc IDs or search terms. If useful
same-memory work remains, use `continue_generation`, not `saturated`.

## Commands

```bash
python3 scripts/metadata-graph/opencode-kimi/sebi_research.py doc-overview <doc_id>
python3 scripts/metadata-graph/opencode-kimi/sebi_research.py search-doc <doc_id> "<query>"
python3 scripts/metadata-graph/opencode-kimi/sebi_research.py chunks <doc_id> <start> <limit>
python3 scripts/metadata-graph/opencode-kimi/sebi_research.py around <doc_id> <chunk_index> --before 2 --after 2
python3 scripts/metadata-graph/opencode-kimi/sebi_research.py search-corpus-brief "<query>" --semantic "<sentence>"
python3 scripts/metadata-graph/opencode-kimi/sebi_research.py search-corpus "<query>" --semantic "<sentence>" --limit 3
python3 scripts/metadata-graph/opencode-kimi/sebi_research.py graph-neighbors <doc_id_or_graph_id>
python3 scripts/metadata-graph/opencode-kimi/sebi_research.py doc-meta <doc_id_or_graph_id>
python3 scripts/metadata-graph/opencode-kimi/sebi_research.py pdf-path <doc_id_or_graph_id>
```

## Output Row

```json
{
  "id": "kimi_run_000001_q01",
  "run_id": "kimi_run_000001",
  "memory_id": "memory_0001",
  "frontier_id": "frontier_0001",
  "question": "...",
  "answer": "...",
  "source": "opencode_kimi_research",
  "question_type": "entity_cross_reference",
  "difficulty_features": ["fake_negative_or_absence"],
  "supporting_docs": [{"doc_id": "clf-...", "title": "..."}],
  "supporting_chunks": ["clf-...#12", "clf-...#18"],
  "manual_review": {"status": "pending", "notes": ""}
}
```

## Summary

Keep it short. Include: memory-region boundary, docs used, docs checked but not used,
kept/rejected/deferred themes, questions written, duplicate check, any
heavy-overlap questions kept and why, saturation note with one lateral lead
tried, and future leads.

Also include one exact line near the top:

`Frontier outcome: saturated | continue_generation | weak | duplicate | too_broad | no_multidoc_angle | needs_review - one sentence reason`

Use `saturated` when the assigned frontier was a coherent memory region and has been
handled as far as this run could reasonably take it and no meaningful
same-memory targets remain. Use `continue_generation` when the memory region
is productive but should stay schedulable for another small pass. Use `weak`,
`no_multidoc_angle`, `too_broad`, or `duplicate` when the frontier should still
be crossed out, but for that more specific reason. Use `needs_review` when the
frontier should not be crossed out yet. If a frontier was productive but still
contains substantial unexplored same-memory work, use `continue_generation`;
if the remaining work is a different memory region, split it into concrete future
leads.
