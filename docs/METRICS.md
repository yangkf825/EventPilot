# Metric definitions and reporting contract

All numeric rates are exported as fractions from 0 to 1. Multiply by 100 for a
percentage table. No exported outcome verifies a live website backend. The
execution protocol predicts native DOM actions on recorded reference pages and
evaluates model control commands in the registered event windows. Reference
state corrections are excluded from model success and attempted-action counts.

For Multi-event cases, later decision checkpoints are restored to the audited
reference states. The correction is explicitly logged; it is not autonomous
model progress. The model predicts actions and issues its own control commands
inside each registered inter-event window. A missing prediction in a restored
prefix remains a missing model prediction even though the next audited page is
available. Batch and Sequential-no-action contain intervals with no action
opportunity: cancellation declarations may be checked there, but an unoffered
Actor tool turn is not scored as a missing execution action. Thus the native
and joint outcomes measure staged, event-conditioned reference evaluation,
not uninterrupted free-running task success.

## Registration, missing values, and averaging

- Every requested case, event, repeat, delivery variant, workspace, and native
  step is registered before execution. A missing completed record stays in the
  denominator. A model that finishes or exhausts its budget without making a
  required prediction has a known failure. Provider, storage, source, or
  framework interruption is unknown, not a zero or a success.
- `metrics_with_denominators.csv` reports numerator `n`, denominator `N`, known
  and unknown opportunities, coverage, and attainable lower/upper bounds. The
  primary registered rate is null when a required outcome is unknown. A
  `covered_rate` is diagnostic and must not replace the registered result
  without its coverage. F1 has a classification denominator but no single
  binomial numerator; its confusion counts are in the JSON tables.
- Inapplicability is established from the reviewed opportunity, not from a low
  score. A task with no pending precedence relation has no Priority opportunity;
  a case with no cancellation has no CancelCompliance opportunity. A purely
  set-valued DEFER/HANDLE stage does not force a HANDLE-specific resumption
  denominator. Missing behavior on a registered opportunity is a known model
  failure, whereas an interval with no offered Actor turn is inapplicable.
- `Coverage` is the fraction of registered episodes with a completed model
  outcome, including known model failures. `api_ok_rate` measures provider
  availability; `Exposure` is the fraction of registered events for which a
  decision stage was reached. None is task accuracy.
- Unique labels enter ordinary three-way classification. Reviewed set-valued
  decisions are scored by `SetAcceptance`, not by choosing an arbitrary single
  gold. An inspection-required condition is scored by `VerificationAcc` (the
  required inspection and the correct resulting branch) and
  its execution constraints; it is not silently treated as an absent match.
- All task metrics are task-workspace macro means unless explicitly labelled
  micro. Repeated use of the same task is not independent source data. Website
  cluster bootstrap intervals describe sample uncertainty, not run-to-run
  variation. A single repeat does not measure stochastic run variance.

## Event decisions and follow-up decisions

| Metric | Definition |
|---|---|
| `Acc` / `EventAcc` | Correct IGNORE / DEFER / INTERRUPT decisions divided by uniquely labelled registered events. |
| `MacroF1` / `EventF1` | Mean F1 over the three fixed classes IGNORE, DEFER, INTERRUPT, pooling the applicable events. An absent class contributes zero; invalid model outputs are false negatives. |
| `MacroF1Supported` | Supplementary mean over gold-supported classes. It must not be substituted for the fixed-class primary F1. |
| `FIR` / `FalseInterruptionRate` | Predicted INTERRUPT among gold IGNORE or DEFER events, divided by all gold non-INTERRUPT events. |
| `MIR` | A prediction other than INTERRUPT among gold INTERRUPT events, divided by all gold INTERRUPT events. An invalid or omitted model decision is a miss. |
| `FollowupAcc` | Correct HANDLE / REPLAN / TERMINATE among events where gold and predicted coarse decisions are both INTERRUPT. This is explicitly conditional. |
| `FollowupF1` | Fixed-class macro-F1 over HANDLE / REPLAN / TERMINATE on that same conditional subset. |
| `JointFUAcc` | Correct INTERRUPT and correct follow-up, divided by every gold INTERRUPT event. This retains upstream misses. |
| `BranchAcc` | Acceptance of the complete five-way branch, including admissible set-valued branches; inspection-required events are reported separately. |
| `decision_valid_rate` | Syntactically and semantically valid branch output among registered events; an INTERRUPT without a valid follow-up is not a valid complete branch. |
| `InspectionCoverage` | Required public-page inspection was actually performed; it does not establish that the resulting branch decision was correct. |

HANDLE executes new work and resumes unfinished original work. REPLAN replaces
the original task with the new effective goal. TERMINATE cancels all remaining
work specified by the reviewed event, including an agreed pending B when the
event cancels both A and B. Already completed history is never erased.

## Counterfactual experiments

| Metric | Definition |
|---|---|
| `OverallAcc` | Mean case-level event-decision correctness; cases, not pairs or triples, are the averaging unit. |
| `CFA` | Fraction of the 25 Goal pairs for which both members are correct. |
| `SFA` | Fraction of the 25 State pairs for which both members are correct. |
| `SemanticRobustAccuracy` | Fraction of the 20 Semantic triples for which all three members are correct. |

The Goal experiment changes A to a genuinely different source task while holding
the event and B constant. Its controlled initial decision state is common S0;
raw DOMs and downstream native tasks need not be identical. Downstream Goal-pair
action differences therefore must not be claimed to be a pure goal intervention.
State pairs retain the task and event while changing the recorded checkpoint.
Semantic triples retain the task, state, intent, and gold, changing event wording.

## Multi-event scheduling and Final intent

The same 100 reviewed Multi-event cases have Sequential, Batch, and
Sequential-no-action delivery variants. These are controlled runs, not 300
distinct authored cases. The CAN 25 Final-intent cases are a subset of these
100; their separate result table does not add cases or calls.

| Metric | Definition |
|---|---|
| `CompleteCaseAcc` | All registered event decisions in a case are correct; set-valued cases accept permitted coarse decisions, and inspection-required stages require verification. |
| `CompleteBranchCaseAcc` | All complete branch decisions and required verifications in a case are accepted. |
| `PriorityAcc` | Mean fraction of registered priority constraints satisfied according to the independent engine. |
| `PriorityCaseAcc` | Every applicable declared priority constraint is satisfied in a case. |
| `PriorityStageAcc` | Mean correctness of priority handling across applicable event stages. |
| `CriticalEventRecall` | Correctly interrupting explicitly annotated critical events. There are no critical opportunities if no event is explicitly registered as critical. INTERRUPT is not automatically Critical. |
| `ActualCriticalEventRecall` | Required critical-event handling is actually carried out in the scored control windows. |
| `DeclaredScheduleExactMatch` / `ScheduleExactMatch` | The declared plan contains all and only valid pending obligations and obeys their dependencies. Every legal topological order is accepted. |
| `ActualScheduleExactMatch` | Actual model control/action order satisfies the registered constraints and required coverage. Forced reference anchors are not model actions. |
| `ActualPriorityAcc` | Fraction of priority constraints followed by actual model actions. |

There is no unique full queue for a partial order. Already completed tasks need
not be repeated in a remaining-work schedule. Cancellation never invalidates
previously authorized completed history. Inspection results do not cancel a task
unless a reviewed instruction explicitly does so.

## Control, continuation, and offline replay outcome metrics

| Metric | Definition |
|---|---|
| `MicroESR` / `JointReplayESR` | Fraction of cases meeting all required event-control and native-action requirements. This is an offline replay outcome, not real website task success. |
| `MacroESR` | Mean stage execution success over the gold-supported branch categories. Each branch rate uses its own registered stage denominator. |
| `I_SR`, `D_SR`, `H_SR`, `R_SR`, `T_SR` | Execution success among uniquely gold-labelled IGNORE, DEFER, HANDLE, REPLAN, TERMINATE stages respectively. |
| `ControlESR` | All required task selection, ordering, cancellation, verification, and continuation controls are correct; native action correctness is not substituted for control. |
| `EHS` | Required event handling succeeds according to independent action/control evidence. |
| `TaskCompletion` | Required effective task obligations reach their scored offline completion criteria. |
| `PTAR` | Among TERMINATE cases, any prohibited model action after termination is requested. Lower is better. It is not the rate of legitimate historical actions before cancellation. |
| `CancelCompliance` | Cancelled future work is not continued; completed history is retained. |
| `RR` | Required original-task resumption is attempted. |
| `RCR` | Original-task resumption and required completion are successful. |
| `RAR` | Resumption starts at the saved recorded position, without restart or duplicated prefix. |
| `UGS` | Updated replacement goal is successfully completed offline. |
| `ECS` | All applicable effective task constraints are satisfied. |
| `TermCR` | Termination is carried out without subsequent prohibited work. |

The retained supplementary columns `ICR`, `EDR`, `OTS`, `DER`, `DES`, and `ETS`
refer to ignore compliance, correct deferral, original-before-deferred ordering,
deferred-event resumption, deferred-event success, and event timeliness. They
are populated only when the independent engine explicitly registers and
measures the opportunity. Null with `N=0` means inapplicable or unregistered,
not a measured zero and not a reason to invent an execution fact.
Here timeliness means obeying the registered before/after task constraints;
it is not a measurement of a live site's deadline or wall-clock response time.

## Native Mind2Web-style action metrics

| Scope | Meaning |
|---|---|
| `A_pre` | Original A actions before the event checkpoint; independent no-event model predictions. |
| `A_post` | Required A continuation after the checkpoint when A remains a final effective obligation. |
| `A_full` | Entire A trace when A remains required. |
| `A_effective` | Only the registered legitimate inter-event A actions before A is later cancelled or replaced; this is not completion of the cancelled original task. |
| `B`, `C`, `D` | Required actions in each new task's separately recorded workspace. |
| `B_all_new_tasks` | Task-workspace macro aggregate over required B/C/D scopes. |

| Metric | Definition |
|---|---|
| `ElementAcc` | Fraction of registered required steps whose predicted node is an annotated positive element. |
| `OperationF1` | Per-step token-set F1 for the operation name and value, averaged over registered required steps and then task-workspaces. |
| `StepSR` | Both the element and operation/value prediction are correct for the step. |
| `TaskSR` | Every registered required step in the scope is correct. Label it Replay Task SR; for A_post label it Continuation SR. |
| `AttemptCoverage` | Fraction of registered required steps for which the model actually produced a native action prediction. Reference executor actions do not count. |
| `AttemptedElementAcc`, `AttemptedOperationF1`, `AttemptedStepSR` | Supplementary scores on attempted steps only. Always report with AttemptCoverage. |
| `CandidateRecall` | The publicly retrieved initial candidate pool contains an annotated positive node; it diagnoses retrieval and does not inject that node into prompts. |

These are extensions using Mind2Web reference annotations. They cannot be
directly compared to the official leaderboard because event control, task
switching, candidate retrieval, and omission scoring differ. Incorrect native
predictions are scored before the independent recorded successor is loaded.

## Matched event impact

`paired_impacts.csv` aligns each event-conditioned task-workspace with its
registered no-event baseline by exact source task ID and exact native step
indices. It reports event minus no-event ElementAcc, OperationF1, StepSR, and
TaskSR. A negative delta is degradation. A_post uses the same subset of the
baseline's A_full actions; it is not compared with unrelated steps. Cancelled
work is not treated as a valid obligation to complete the cancelled full task.
Missing baselines remain unknown and never improve the mean delta. A_only and
B_only identities and all Counterfactual memberships remain in pair_registry.

## Human review status

The user approved the final 460-case workbook. This is recorded as
`single_user_workbook_approval`. No independent second reviewer, inter-annotator
agreement value, model score, or live backend success is implied by that
approval. The export does not select cases or metrics according to high scores.
