# Frozen reviewed English dataset (v11 data, v12 runner)

This release compiles the 460-case workbook approved by the project owner on
2026-11-05. It preserves the reviewed source-task identities, original task goals,
insertion checkpoints, event intent, labels, task dependencies, cancellation scope,
and counterfactual membership. The workbook is preserved read-only as
`review/approved_review.xlsx`.

The source is the original **offline Mind2Web** corpus: recorded DOM snapshots and
reference actions. It is not Online-Mind2Web and does not start website backends.
All event messages and multi-task orchestration rules are author additions. Tasks
A, B, C and D point to genuine Mind2Web source tasks, executed in separate recorded
workspaces. Source text, dates, spelling, action values and node IDs are not silently
modernized or corrected.

## Case allocation

| Reviewed subset | Unique cases | Group structure |
| --- | ---: | --- |
| Main | 200 | IGNORE, DEFER, HANDLE, REPLAN, TERMINATE: 40 each |
| Goal CF | 50 | 25 pairs; different A goals, identical B and event |
| State CF | 50 | 25 pairs; same tasks/event, different real checkpoints |
| Semantic | 60 | 20 triples; equivalent event wording and invariant intent |
| Multi-event | 100 | ORD 25, COND 25, RES 25, CAN 25 |
| **Total** | **460** | **640 event messages** |

CAN is the Final-intent subset of Multi-event. Reporting it separately does not
create 25 additional reviewed cases. Information ablation reuses the 200 Main
cases. Sequential, Batch and Sequential-no-action are delivery variants of each
Multi-event case, not extra members of the 460-case dataset.

There are 362 distinct referenced source tasks and 362 corresponding independent
no-event controls. The source metadata catalog retains 560 tasks from the previous
frozen selection; unused catalog entries are not experimental cases.

For one model and one repeat, the complete default plan registers 1,422 canonical
jobs: Main 200, additional ablation views 400, Counterfactual 160, Multi-event
variants 300, and Baseline 362. Event + Goal + State ablation aliases the Main record,
so there are 1,622 logical experiment entries. Main is Event + Goal + State.
Full Trajectory remains only in the unchanged non-ablation CF/Multi/Baseline protocol.
Each optional paper method repeats this same registration independently. A job contains multiple model calls.

## Meanings of the two decision levels

The first level is IGNORE / DEFER / INTERRUPT. The second level is scored on the
appropriate INTERRUPT cases:

- **IGNORE:** no new user obligation; continue the valid existing work.
- **DEFER:** retain the event's valid work or update, with the reviewed ordering.
- **HANDLE:** switch to the requested work and later resume saved original work.
- **REPLAN:** abandon the unfinished original goal and adopt the valid replacement.
- **TERMINATE:** cancel all remaining work identified by that message, including
  both the original A and the already agreed B in the reviewed global-stop cases.
  It does not mean "cancel A and execute B".

Multi-event updates can cancel only a subset of tasks. Completed history is never
erased. Some reviewed stages admit more than one coarse label or legal task order;
they are retained as set-valued annotations, rather than forced into an arbitrary
single-label classification denominator. Inspection-dependent stages must obtain
public evidence before choosing the applicable branch.

## Files and separation of public inputs from annotations

| File | Contents |
| --- | --- |
| `data/cases.jsonl` | All 460 reviewed cases plus private evaluator facts |
| `data/public_cases.jsonl` | English tasks/messages without private Gold, states, operations or reference controls |
| `data/baselines.jsonl` | 362 independent source-task controls |
| `data/sources.json` | Frozen source metadata and snapshot SHA256 hashes |
| `data/pair_registry.json` | Per-case source-control identities and group membership |
| `data/counterfactual_groups.json` | All 25 Goal pairs, 25 State pairs and 20 Semantic triples |
| `data/manifest.json` | Version, approval scope, source/workbook hashes and frozen dataset digest |
| `data/validation.json` | Structural, English-field and identity validation results |
| `data/authoring/reviewed_spec.en.json` | Frozen English specification used by the portable builder |
| `data/authoring/translation_audit.json` | English translations aligned to source-event hashes and workbook case IDs |
| `review/approved_review.xlsx` | Original approved human-review artifact; includes Chinese reviewer descriptions |

The public JSONL is a dataset-reader export, not a prompt to send in its entirety.
It contains future event records so a benchmark implementer can load the dataset.
The runner must expose only initial agreed tasks and already delivered messages;
future C/D goals, private annotations and future reference actions stay hidden.
Normal model inputs never receive `private`, Gold, semantic operations, future
reference steps, task lengths or remaining-step counts.

Every machine-readable case, including retained private executable facts, passes a
recursive CJK scan. Main has 160 approved mixed-language user events translated
manually into English; 40 already-English website messages are unchanged. All 60
Semantic event messages and all 200 L2/L3 event message sequences remain verbatim
from the latest approved workbook. Translation preserves source parameters and
reviewed intent. The owner approved the source workbook; no independent English
translation-review agreement is claimed. Original source DOM assets remain
unmodified and may retain source-site multilingual fragments.

## Pair controls and limits

Goal CF changes the actual task A, keeping B and event wording identical. Its
controlled decision probe uses the common task-start state S0 with empty history,
without the different task-specific DOM. The native Actor uses the actual source
DOM for its own task; native action differences between these pairs are descriptive
cross-task comparisons, not a claim that raw DOM is identical.

State CF uses the same tasks and message but checkpoints before and after a
recorded milestone. State is grounded in the recorded page and observed reference
actions. Semantic triples preserve tasks, checkpoint, source, intent and Gold;
only equivalent message wording changes.

Each involved task is linked to a source-ID-matched no-event control. Impacts must
compare the same source-step interval on both sides. A-post refers to the original
A suffix after event insertion; A-full also includes its prefix. Cancelled old
work is not required to finish. Tasks merely recommended by a website do not
become required B obligations. A later valid cancellation does not retroactively
turn earlier permitted actions into post-cancellation violations.

## Rebuilding and validation

```bash
python scripts/build_dataset.py --validate-only
python scripts/build_dataset.py
```

The default builder is fully portable and rebuilds from the included frozen English
specification. It needs no spreadsheet application, source review directory,
network or model API. The one-time `data/authoring/import_approved_review.py` records
the original local import procedure; it is provenance tooling and is not needed
for installation or ordinary rebuilds.

Validation checks case counts, all group sizes, exact pair task/message controls,
source goals/checkpoints, single-event Semantic annotation invariance, public/private
projection, English fields, and file/dataset hashes. Engineering validation is not
an LLM result and does not measure annotator agreement. The original workbook was
approved by a single user; inter-annotator agreement is unmeasured and recorded as
null rather than invented.
