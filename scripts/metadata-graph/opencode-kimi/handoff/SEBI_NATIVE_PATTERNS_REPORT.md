# SEBI-Native Patterns Report

## Executive Summary

The Kimi + OpenCode pipeline is currently optimized for SEBI corpus memory:
hard, evidence-grounded, multi-document QAs that force the model to learn
facts, rules, dates, actors, exceptions, procedures, and document relationships.
That remains useful, but the eventual target harness changes the objective. If
both the base model and trained model can grep every SEBI file, durable lift
will come less from memorizing isolated answers and more from learning
SEBI-native priors:

- which document families exist;
- which companion documents usually complete a matter;
- what search hooks are reliable;
- where operative text hides inside template-heavy files;
- how SEBI rules move from proposal to circular to master circular to
  amendment, clarification, relaxation, and enforcement;
- how enforcement/recovery/corporate-action sequences unfold.

The current pipeline exposes many of these patterns incidentally and sometimes
strongly, especially lineage, thresholds, workflows, and enforcement chains. It
does not yet make the patterns themselves first-class training targets. That is
the core gap.

## What I Inspected

- Pipeline goal, state machine, explorer playbook, generation instructions,
  reviewer instructions, and question taste guide.
- Frontier ledger and explorer artifacts under
  `scripts/metadata-graph/output/opencode_kimi`.
- Generated/accepted dataset
  `questions/Kimi+Opencode_Questions.csv`.
- Eval Question Gen prompts, clustering, validation, judging, and exported eval
  bank.
- Metadata graph v2 SQLite/JSONL artifacts and build report.

I did not mutate live pipeline state or run LLM workers; this report is the
only file added.

## The SEBI Patterns That Matter

### 1. Corpus Geography Is Predictive

SEBI files are not one homogeneous legal corpus. The graph shows a few dominant
document families:

- `Enforcements/Recovery Proceedings`: 6,361 Vespa items.
- `Enforcements/Orders`: 2,425.
- `Filings/Takeovers`: 1,442.
- `Reports/Publications`: 1,341.
- `Legal/Circulars`: 328.
- `Legal/Regulations`: 258.
- `Enforcements/Auction Notice under Recovery Proceedings`: 219.
- `Reports/Reports`: 216.
- `Filings/Tender Offers`: 190.
- `Legal/Master Circulars`: 33.

This matters for a grep harness because folder location is a strong prior. A
trained model should know that a "current rule" usually lives in circulars,
regulations, or master circulars; a "what happened after the order" question
often lives in recovery proceedings; a deal sequence lives in filings and
scheme notices; and statistical/market-background material may live in
publications or bulletins.

Current pipeline exposure: partial. The accepted Kimi/OpenCode questions cite
Legal/Circulars and Master Circulars heavily relative to their graph footprint,
and underexpose huge areas like Publications, Public Issues, Debt Offer
Documents, Rights Issues, and much of Takeovers.

### 2. SEBI Documents Come In Companion Grammars

The strongest SEBI-native pattern is not a topic; it is a document-form grammar.
Common grammars include:

- consultation paper -> board decision or press release -> final circular;
- baseline circular/master circular -> amendment -> clarification -> extension
  or relaxation;
- regulation -> amendment notification -> operational circular -> association
  or market-infrastructure implementation;
- adjudication/WTM/QJA/SAT order -> recovery certificate -> demand notice ->
  attachment/prohibitory order -> remittance/release/completion/cancellation;
- public announcement -> detailed public statement -> draft letter of offer ->
  letter of offer -> corrigendum -> post-offer/post-buyback announcement;
- scheme notice -> newspaper advertisement -> voting/meeting mechanics ->
  downstream open offer or listing/disclosure filing;
- same provision across entity classes, e.g. REIT/InvIT, IA/RA, stock
  broker/DP, AIF/MF, equity/commodity derivatives.

This is exactly the kind of "innate SEBI knowledge" that can make a trained
model better in a tool harness: once it finds one file, it should know which
file forms probably complete the answer.

Current pipeline exposure: strong for some grammars. The explorer playbook and
generation instructions explicitly reward citation chains, amendment trails,
master circular updates, consultation/proposal/final paths, recovery chains,
open offer lineages, scheme notices, repeated obligations, exception/deadline
mechanics, and cross-corpus comparisons. The accepted CSV shows this in
practice: top frontiers include CSCRF amendment trail, HUL demerger/open offer,
T+0 settlement evolution, UPI block mechanism consultation-to-final, Anand
Rathi buyback sequence, KYC/KRA operational changes, and recovery chains.

### 3. Current-Law Reasoning Requires Version Discipline

SEBI frequently publishes drafts, consultation papers, final circulars,
clarifications, timeline extensions, master circular consolidations, and later
amendments. A capable SEBI model must learn:

- consultation papers are not law unless followed by an action document;
- master circulars consolidate but can lag or be amended;
- clarification/extension circulars may change practical compliance more than
  the baseline circular;
- final answer often requires "what changed, what stayed, what was dropped, and
  what is current now."

Current pipeline exposure: strong. About half the Kimi/OpenCode CSV has
lineage/versioning signals by heuristic. Question types like
`document_diff_or_lineage`, `applied_compliance_scenario`, and
`deadline_or_effective_date_trap` are doing useful work here.

Remaining gap: the training row often gives the companion documents implicitly
through the question and evidence. The model may learn the delta, but not always
the search habit: "if I found a consultation paper, grep for the same title,
subject phrase, circular number family, board meeting note, and later master
circular."

### 4. SEBI Uses Threshold/Exception Logic Everywhere

Many SEBI obligations are not simple yes/no rules. They depend on:

- entity category: Qualified/Mid-size/Small/Self-certification REs, QSBs,
  registered vs unregistered NPOs, IA/RA, AIF/VCF manager, issuer class;
- thresholds: clients, AUM, turnover, trading volume, public shareholding,
  minimum investment, subscription amount, days elapsed;
- carve-outs: less than 100 clients, government securities only FPIs,
  self-certification categories, existing accounts vs new accounts, transition
  windows;
- "highest category wins" or "mutatis mutandis" style application.

Current pipeline exposure: strong. Heuristic scan of accepted questions found
threshold/exception/scope signals in roughly 48% of rows. This is one of the
pipeline's best pattern-teaching channels.

Potential improvement: distinguish "classification pattern" from "fact answer"
in metadata so training can intentionally cover recurring threshold schemas
rather than simply generating many threshold facts.

### 5. Enforcement Is a Lifecycle, Not a Folder

The graph is dominated by enforcement and recovery. Relevant search anchors
include RC numbers, PANs, order numbers, WTM/QJA/AO/SAT references, defaulter
names, matter names, attachment proceeding numbers, and statutory recovery
provisions. Graph-wide extracted references include approximately:

- 1,946 recovery-certificate references;
- 12,142 PAN-like references;
- 4,787 order-number-like references;
- 41,537 legal provision references;
- 7,123 circular-number-like references.

A trained model should know how to climb an enforcement chain:

- find the originating order;
- identify noticees/defaulters and amounts;
- trace recovery certificate/demand/attachment/remittance/release/completion;
- distinguish parallel defaulters in the same matter;
- separate boilerplate authority text from the variable facts that matter.

Current pipeline exposure: medium to strong by row count, but low by corpus
coverage. Roughly 38% of accepted questions have enforcement/recovery/remedy
signals. Yet only 35 unique `Enforcements/Recovery Proceedings` docs are cited
out of 6,361 graph items, and only 173 unique `Enforcements/Orders` docs out of
2,425. The pipeline teaches some excellent enforcement schemas, but the corpus
surface is vast and repetitive.

### 6. Corporate Actions Have Predictable Filing Sequences

SEBI corporate-action material often forms event chains:

- buyback approval -> public announcement -> letter of offer -> entitlement ->
  tendering -> post-buyback announcement;
- takeover PA/DPS/DLOF/LOF/corrigendum/post-offer sequence;
- scheme meeting notice -> explanatory statement/newspaper ad/voting cut-off ->
  downstream listing/open offer/transaction filing;
- rights/public/debt offer documents with draft/final/corrigendum stages.

Current pipeline exposure: good in a few rich regions, but sparse overall.
Anand Rathi, HUL/Kwality Walls, and some takeover/informal guidance rows are
high quality. But the graph has 1,442 Takeovers docs, 319 Public Issues docs,
190 Tender Offers docs, 123 Debt Offer Documents, and 30 Rights Issues; accepted
questions cite only tiny slices of these.

This is a major opportunity for harness-focused training because filing
documents are exactly where grep/tool navigation should shine.

### 7. Boilerplate Versus Operative Delta Is a Core Skill

SEBI documents are template-heavy. Circulars repeat addressees, legal authority,
website availability, and implementation directions. Recovery files repeat
statutory authority, bank/demat attachment language, and payment instructions.
Court/RTI orders repeat procedural paragraphs. Offer documents repeat risk and
process sections.

The trained model should learn to ask:

- what variable changed in this otherwise standard form?
- which paragraph/table/schedule/annexure carries the operative payload?
- which repeated clause is boilerplate and which is a new exception or timeline?

Current pipeline exposure: partial. The questions often require extracting
operative deltas, but the output does not explicitly teach "this was boilerplate
and this was the variable." Eval Question Gen has many single-document local
rows, but those mostly test answer support from seen chunks, not document-form
navigation.

### 8. Absence And Non-Adoption Are Real SEBI Reasoning Modes

SEBI work often requires proving that something did not happen:

- a proposal was not retained in the final circular;
- no physical shares were tendered;
- no final circular exists yet;
- a consultation remains draft-stage;
- a document is merely adjacent, not part of the same matter.

Current pipeline exposure: strong as a question shape. `negative_or_absence`
and fake-negative difficulty features are common. The weakness is that the
evidence boundary is usually the cited docs. A grep harness requires broader
absence discipline: what was searched, what counts as enough, and which nearby
documents do not close the loop.

### 9. Legal Authority Has A Repeating Grammar

Certain provisions are recurring anchors:

- Section 11(1): market-development/regulatory circular authority;
- Section 11B: directions/remedial orders;
- Section 12 and intermediary registration;
- Sections 15HA/15HB/15J/15JB: penalty and settlement/adjudication logic;
- Section 28A and Income-tax Act Section 222: recovery machinery;
- Companies Act provisions in offer documents and scheme filings.

Current pipeline exposure: mixed. The generated answers cite these provisions
when needed, but there is no explicit curriculum around "what kind of SEBI
document tends to invoke which authority, and what that tells you to grep next."

## Current Pipeline Coverage

### Strengths

1. **It already targets real SEBI companion grammars.** The explorer playbook is
   well aligned with SEBI-native structures: lineages, amendments, recovery
   chains, open offers, scheme notices, repeated obligations, deadlines, and
   cross-regime comparisons.

2. **It forces multi-document necessity.** This is good for learning
   relationships rather than isolated facts. Most accepted rows use two docs,
   with a smaller set of three-plus document rows.

3. **Question taste is practical.** The accepted questions sound like
   compliance officers, investors, lawyers, or analysts resolving real
   confusion. That makes the training signal closer to harness use than
   exam-style recall.

4. **The reviewer standard protects against fake multi-doc.** The review layer
   rejects second documents that are only background or repetition.

5. **It teaches high-value reasoning forms.** In the accepted CSV, heuristic
   signals suggest high exposure to:
   - lineage/versioning: about 50%;
   - threshold/scope/exception: about 48%;
   - workflow/deadline/transition: about 44%;
   - enforcement/recovery/remedy: about 38%;
   - corporate-action/filing sequence: about 18%.

### Weaknesses

1. **It trains answer resolution more than evidence discovery.** Only a small
   fraction of accepted rows explicitly ask for search strategy or document
   choice. The model may learn answers and some latent structure, but not
   reliably learn how to use grep better than the base model.

2. **Document-family balance is skewed.** Legal circular/master-circular
   coverage is strong relative to graph size. Large filing/publication/recovery
   areas remain sparse or untouched.

3. **Pattern labels are not first-class.** Rows have `question_type` and
   `difficulty_features`, but not `sebi_pattern`, `companion_relation`,
   `search_hooks`, `boilerplate_vs_delta`, or `expected_grep_path`. Without
   these, the training signal may not consistently emphasize the reusable
   structure.

4. **Two-document bias can hide longer SEBI chains.** Many SEBI matters are
   three-to-six document lifecycles. The current dataset mostly resolves
   two-document relationships.

5. **Absence checks are evidence-local.** They teach useful negative reasoning,
   but not enough corpus-wide search discipline.

6. **Eval Question Gen is mostly local.** The eval bank has 3,411 accepted rows,
   but 3,353 are single-document. It is useful for seen-chunk reasoning and
   support checking; it is not a major source of companion-discovery learning.

## Recommendations

### 1. Add SEBI Pattern Metadata To Generated Rows

Extend output rows with optional fields such as:

- `sebi_pattern`: `consultation_to_final`, `master_circular_amendment`,
  `recovery_lifecycle`, `corporate_action_filing_sequence`,
  `threshold_exception_classification`, `cross_regime_obligation`,
  `boilerplate_delta`, `absence_non_adoption`, `litigation_forum_chain`.
- `companion_relation`: why the documents belong together.
- `search_hooks`: circular number, RC number, PAN, order number, regulation,
  title phrase, issuer/matter name, folder path.
- `harness_skill`: `find_current_rule`, `find_followup_order`,
  `trace_matter`, `distinguish_draft_from_final`, `grep_by_identifier`,
  `extract_delta_from_template`.

This turns latent structure into explicit supervision.

### 2. Create Harness-Style Training Rows

Add a second kind of training question whose answer includes a compact search
plan plus the substantive answer. Example shapes:

- "A user found a consultation paper on X. Which companion documents should be
  searched to know whether it became current law, and what is the result?"
- "You found a recovery demand notice with RC ####. What grep hooks identify
  the attachment/remittance/release trail, and what happened?"
- "You found a Letter of Offer for a buyback. What predecessor and successor
  filings complete the SEBI sequence?"
- "You found a master circular paragraph. How do you check whether a later
  circular clarified or relaxed it?"

This directly trains the future harness behavior.

### 3. Balance By Document Family, Not Just Frontier Novelty

Introduce periodic coverage goals by SEBI document family:

- recovery proceedings;
- orders/adjudication/settlement/SAT/courts;
- takeovers/tender offers/buybacks;
- public/rights/debt offer documents;
- scheme meeting notices;
- reports/public comments/publications;
- circulars/master circulars/regulations;
- informal guidance;
- press releases/public notices/speeches where they are procedural companions.

Do not force equal counts; force enough examples for each recurring grammar.

### 4. Teach Boilerplate-Differentiation Explicitly

Create questions that ask what changed across otherwise standard documents:

- same form, different amounts;
- same recovery template, different RC/PAN/defaulter/remittance consequence;
- same circular boilerplate, different implementation actor or timeline;
- same scheme notice template, different voting/cutoff/meeting mechanics.

This is a high-value harness skill because grep will often retrieve many
near-duplicates.

### 5. Promote Longer Lifecycle Frontiers

Keep two-doc questions, but add deliberate lifecycle frontiers with 3-6 docs:

- consultation -> final -> clarification -> extension;
- order -> RC -> demand -> attachment -> remittance -> release/completion;
- PA -> DPS -> DLOF -> LOF -> corrigendum -> post-offer;
- meeting notice -> newspaper ad -> voting result/downstream filing/open offer.

These teach the model the full SEBI sequence, not just one edge.

### 6. Make Absence Checks Search-Aware

For non-adoption/draft-only questions, include what was searched or what
document family would have closed the loop. This teaches the model when to stop
and how to avoid converting a consultation into current law.

### 7. Use Eval Question Gen As Local Reasoning, Not Pattern Coverage

Keep Eval Question Gen for strict seen-chunk evals and support testing. Do not
expect it to teach companion discovery unless assignment selection is changed to
prefer multi-document clusters and include search/lineage labels.

## Bottom Line

The current Training Gen pipeline is already good at producing hard SEBI
multi-document memory questions. It exposes many of the right patterns, but
mostly through the final QA surface. For the future grep-enabled harness, the
training objective should explicitly shift from "remember this SEBI fact" toward
"internalize the SEBI document grammar and search behavior that finds and
interprets the right files."

The most valuable next evolution is not to abandon memory questions. It is to
add a pattern-aware layer on top of them: explicit SEBI pattern tags, companion
relations, search hooks, boilerplate/delta distinctions, and harness-style
search-plan answers. That would make the trained model more likely to outperform
the base model even when both have access to the same complete SEBI corpus.
