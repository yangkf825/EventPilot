import copy
import json
from pathlib import Path
import tempfile
import unittest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eventpilot.metrics import (decision_records, fixed_macro_f1, native_result,
                               schedule_valid, score_group, summarize)
from eventpilot.report import _load_records, _paired_impacts, export


def case(cid="C1", branch="HANDLE", cf_type=None, group="", member=""):
    post = {"required_tasks": ["A", "B"], "cancelled_tasks": [], "completed_tasks": [],
            "precedence_constraints": [["B", "A"]], "recorded_pointers": {"A": 1, "B": 0}}
    return {"case_id": cid, "experiment": "counterfactual" if cf_type else "main", "cf_type": cf_type,
            "group_id": group, "member": member, "subtype": "test", "website": "example.com", "checkpoint": 1,
            "tasks": {"A": {"source_task_id": "T1", "goal": "First task"}, "B": {"source_task_id": "T2", "goal": "Other task"}},
            "events": [{"id": "E1", "source": "user", "text": "A test event."}],
            "private": {"source_lengths": {"A": 2, "B": 2}, "gold": {"required_roles": ["A", "B"]},
                        "stages": [{"event_id": "E1", "allowed_branches": [branch], "coarse_score_scope": "unique", "post_state": post}]}}


def episode(c, branch="HANDLE", status="agent_finished", repeat=0):
    return {"case_id": c["case_id"], "model": "model", "experiment": c["experiment"], "view": "state",
            "repeat": repeat, "delivery": "sequential", "status": status,
            "decision_stages": [{"event_id": "E1", "predicted_branch": branch,
                                 "execution_success": branch == c["private"]["stages"][0]["allowed_branches"][0],
                                 "verification_passed": None, "reported": {"decision": "INTERRUPT", "follow_up": branch}}],
            "evaluation": {"episode_success": True, "control_success": True, "task_completion": True,
                           "event_handling_success": True}}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def write_lines(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class ClassificationTests(unittest.TestCase):
    def test_closed_method_failure_with_open_stage_is_false_not_unknown(self):
        c=case();c['events'][0]['critical']=True
        e=episode(c,status='method_graph_failure')
        e['decision_stages'][0]['execution_success']=None
        score=score_group([c],[e])
        self.assertEqual(score['Acc'],1)
        self.assertEqual(score['MacroESR'],0)
        self.assertEqual(score['ActualCriticalEventRecall'],0)
        e['status']='provider_error'
        score=score_group([c],[e])
        self.assertIsNone(score['MacroESR'])
        self.assertIsNone(score['ActualCriticalEventRecall'])

    def test_conditional_followup_and_joint_denominators_differ(self):
        cases = [case("H1"), case("H2"), case("I", "IGNORE")]
        episodes = [episode(cases[0]), episode(cases[1], "DEFER"), episode(cases[2], "IGNORE")]
        score = score_group(cases, episodes)
        self.assertEqual(score["Acc"], 2 / 3)
        self.assertEqual(score["FollowupAcc"], 1)
        self.assertEqual(score["denominators"]["FollowupAcc"]["N"], 1)
        self.assertEqual(score["JointFUAcc"], 0.5)
        self.assertEqual(score["MIR"], 0.5)

    def test_interrupt_with_invalid_followup_preserves_coarse_prediction(self):
        c = case()
        e = episode(c)
        e["decision_stages"][0].update(predicted_branch=None,
            reported={"decisions": {"E1": "INTERRUPT"}, "follow_ups": {"E1": "INVALID"}})
        score = score_group([c], [e])
        self.assertEqual(score["Acc"], 1)
        self.assertEqual(score["FollowupAcc"], 0)
        self.assertEqual(score["decision_valid_rate"], 0)

    def test_pending_is_unknown_not_zero(self):
        c = case()
        score = score_group([c], [])
        self.assertIsNone(score["Acc"])
        self.assertIsNone(score["MicroESR"])
        self.assertEqual(score["denominators"]["Acc"]["unknown"], 1)
        self.assertEqual(score["denominators"]["Acc"]["lower_bound"], 0)
        self.assertEqual(score["denominators"]["Acc"]["upper_bound"], 1)
        self.assertEqual(score["Coverage"], 0)

    def test_finished_model_omission_is_wrong(self):
        c = case()
        score = score_group([c], [{"case_id": c["case_id"], "status": "agent_finished", "decision_stages": []}])
        self.assertEqual(score["Acc"], 0)
        self.assertEqual(score["MIR"], 1)

    def test_observed_decision_survives_later_provider_interruption(self):
        c = case()
        e = episode(c, status="provider_error")
        score = score_group([c], [e])
        self.assertEqual(score["Acc"], 1)
        self.assertIsNone(score["MicroESR"])
        self.assertEqual(score["api_ok_rate"], 0)

    def test_fixed_class_macro_does_not_drop_absent_classes(self):
        value = fixed_macro_f1(["IGNORE"], ["IGNORE"])
        self.assertEqual(value["macro_f1"], 1 / 3)
        self.assertEqual(value["supported_macro_f1"], 1)

    def test_set_valued_not_forced_into_unique_gold(self):
        c = case()
        c["private"]["stages"][0].update(allowed_branches=["DEFER", "HANDLE"], coarse_score_scope="set_valued")
        score = score_group([c], [episode(c, "DEFER")])
        self.assertIsNone(score["Acc"])
        self.assertEqual(score["SetAcceptance"], 1)
        self.assertEqual(score["denominators"]["Acc"]["N"], 0)

    def test_ambiguous_handle_does_not_register_mandatory_resumption(self):
        c = case()
        c["private"]["stages"][0].update(allowed_branches=["DEFER", "HANDLE"], coarse_score_scope="set_valued")
        e = episode(c, "DEFER")
        e["evaluation"].update(resumed=None, resume_success=None, resume_correct_position=None)
        score = score_group([c], [e])
        for name in ("RR", "RCR", "RAR"):
            self.assertEqual(score["denominators"][name]["N"], 0)
            self.assertIsNone(score[name])

    def test_no_priority_or_cancel_opportunity_is_inapplicable(self):
        c = case(branch="IGNORE")
        state = c["private"]["stages"][0]["post_state"]
        state.update(required_tasks=["A"], precedence_constraints=[])
        e = episode(c, "IGNORE")
        e["evaluation"].update(priority_accuracy=None, priority_correct=None,
                               actual_priority_accuracy=1, actual_priority_correct=True,
                               cancel_compliant=None, event_timely=True)
        score = score_group([c], [e])
        for name in ("PriorityAcc", "PriorityCaseAcc", "ActualPriorityAcc", "ActualPriorityCaseAcc", "CancelCompliance", "ETS"):
            self.assertEqual(score["denominators"][name]["N"], 0, name)

    def test_registered_actual_priority_omission_is_failure(self):
        c = case(branch="HANDLE")
        e = episode(c)
        e["evaluation"].update(actual_priority_accuracy=None, actual_priority_correct=None)
        score = score_group([c], [e])
        self.assertEqual(score["ActualPriorityAcc"], 0)
        self.assertEqual(score["ActualPriorityCaseAcc"], 0)

    def test_unknown_condition_requires_verification_not_fake_absence(self):
        c = case()
        c["private"]["stages"][0].update(condition={"resolved": "Unknown"})
        e = episode(c)
        e["decision_stages"][0]["verification_passed"] = False
        score = score_group([c], [e])
        self.assertIsNone(score["Acc"])
        self.assertEqual(score["VerificationAcc"], 0)
        self.assertEqual(score["CompleteCaseAcc"], 0)

    def test_inspection_without_correct_conditional_branch_is_not_success(self):
        c = case()
        c["private"]["stages"][0].update(condition={"resolved": "Unknown"},
            post_verification_gold={"decision": "DEFER", "follow_up": None})
        e = episode(c, "HANDLE")
        e["decision_stages"][0]["verification_passed"] = True
        score = score_group([c], [e])
        self.assertEqual(score["InspectionCoverage"], 1)
        self.assertEqual(score["VerificationAcc"], 0)
        self.assertEqual(score["CompleteCaseAcc"], 0)

    def test_counterfactual_group_requires_every_member(self):
        cases = [case("G_A", "HANDLE", "goal", "G", "A"), case("G_B", "IGNORE", "goal", "G", "B")]
        score = score_group(cases, [episode(cases[0]), episode(cases[1], "HANDLE")])
        self.assertEqual(score["CFA"], 0)
        self.assertEqual(score["OverallAcc"], 0.5)
        self.assertEqual(score["denominators"]["CFA"]["N"], 1)

    def test_partial_cf_registration_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            score_group([case("G_A", "HANDLE", "goal", "G", "A")], [])

    def test_no_automatic_critical_annotations(self):
        c = case()
        score = score_group([c], [episode(c)])
        self.assertIsNone(score["CriticalEventRecall"])
        self.assertEqual(score["denominators"]["CriticalEventRecall"]["N"], 0)

    def test_all_legal_topological_orders_are_accepted(self):
        self.assertTrue(schedule_valid(["A", "C", "B"], ["A", "B", "C"], [["A", "B"], ["C", "B"]]))
        self.assertTrue(schedule_valid(["C", "A", "B"], ["A", "B", "C"], [["A", "B"], ["C", "B"]]))
        self.assertFalse(schedule_valid(["A", "B", "C"], ["A", "B", "C"], [["A", "B"], ["C", "B"]]))
        self.assertFalse(schedule_valid(["A", "A", "B"], ["A", "B", "C"], []))


class NativeTests(unittest.TestCase):
    def test_missing_model_actions_are_wrong_and_attempt_coverage_visible(self):
        scope = {"registered_steps": [0, 1], "steps": [{"source_step": 0, "attempted": True,
                 "element_correct": True, "operation_f1": 1, "step_correct": True, "candidate_recalled": True}]}
        result = native_result(scope, "agent_finished")
        self.assertEqual(result["StepSR"], 0.5)
        self.assertEqual(result["AttemptedStepSR"], 1)
        self.assertEqual(result["AttemptCoverage"], 0.5)
        self.assertFalse(result["TaskSR"])

    def test_infrastructure_unknown_primary_but_partial_diagnostics_retained(self):
        scope = {"registered_steps": [0, 1], "steps": [{"source_step": 0, "attempted": True,
                 "element_correct": False, "operation_f1": 1, "step_correct": False}]}
        result = native_result(scope, "provider_error")
        self.assertIsNone(result["StepSR"])
        self.assertIsNone(result["TaskSR"])
        self.assertEqual(result["AttemptCoverage"], 0.5)
        self.assertEqual(result["AttemptedOperationF1"], 1)

    def test_matched_impact_restricts_baseline_to_exact_steps(self):
        event_scope = {"source_task_id": "T1", "workspace": "A", "scope": "A_effective",
                       "registered_steps": [1, 2], "required": True, "steps": []}
        event_row = {"source_task_id": "T1", "required": True, "status": "agent_finished", "website": "example.com",
                     "ElementAcc": .5, "OperationF1": .5, "StepSR": .5, "TaskSR": False}
        baseline_scope = {"source_task_id": "T1", "workspace": "A", "scope": "A_full",
                          "registered_steps": [0, 1, 2], "required": True,
                          "steps": [{"source_step": i, "attempted": True, "element_correct": i > 0,
                                     "operation_f1": float(i > 0), "step_correct": i > 0} for i in range(3)]}
        baseline_row = {"source_task_id": "T1", "required": True, "status": "agent_finished"}
        lookup = {("m", "multi", "trajectory", "sequential", 0, "C", "A", "A_effective"): (event_row, event_scope),
                  ("m", "baseline", "trajectory", "sequential", 0, "BASE_T1", "A", "A_full"): (baseline_row, baseline_scope)}
        registry = [{"case_id": "C", "A_baseline_id": "BASE_T1", "B_baseline_ids": {}}]
        rows, _ = _paired_impacts(lookup, registry, {}, {})
        self.assertEqual(rows[0]["StepSR_baseline"], 1)
        self.assertEqual(rows[0]["StepSR_delta"], -.5)
        self.assertEqual(rows[0]["step_indices"], [1, 2])

    def test_missing_baseline_does_not_improve_event_impact(self):
        event_scope = {"source_task_id": "T1", "workspace": "A", "scope": "A_post", "registered_steps": [1], "required": True, "steps": []}
        row = {"source_task_id": "T1", "required": True, "status": "agent_finished", "website": "example.com",
               "ElementAcc": 1, "OperationF1": 1, "StepSR": 1, "TaskSR": True}
        lookup = {("m", "main", "trajectory", "sequential", 0, "C", "A", "A_post"): (row, event_scope)}
        rows, summary = _paired_impacts(lookup, [{"case_id": "C", "A_baseline_id": "missing"}], {}, {})
        self.assertIsNone(rows[0]["StepSR_delta"])
        self.assertIsNone(summary[0]["StepSR_delta"])
        self.assertEqual(summary[0]["StepSR_known_pairs"], 0)


class ExportTests(unittest.TestCase):
    def test_context_ablation_exports_each_cohort_and_keeps_can_inside_multi(self):
        release = Path(__file__).resolve().parents[1]
        data = [json.loads(line) for line in (release / 'data/cases.jsonl').read_text().splitlines()]
        selected = [c for c in data if c['group_id'] in {'L2G01', 'L2S01'} or c['case_id'] in {'L3_CAN_001', 'L3_ORD_001'}]
        self.assertEqual(len(selected), 6)
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            jobs = []
            for c in selected:
                kind = c.get('cf_type') or 'multi'
                for view in ('event-only', 'goal', 'event-state', 'state'):
                    job = {'model': 'model', 'experiment': 'context_ablation_' + kind,
                           'view': view, 'delivery': 'sequential', 'repeat': 0, 'case_id': c['case_id']}
                    job['key'] = '__'.join(str(job[k]) for k in ('model', 'experiment', 'view', 'repeat', 'case_id', 'delivery'))
                    jobs.append(job)
            write(root / 'run_manifest.json', {'repeats': 1, 'human_reviewed': True})
            write(root / 'jobs.json', jobs)
            write(root / 'aliases.json', [])
            write_lines(root / 'registered_data/cases.jsonl', selected)
            write(root / 'registered_sources.json', {})
            # Re-export must also remove stale legacy subset tables.
            write(root / 'tables/final_intent_model_trajectory_sequential.json', {})
            (root / 'tables/final_intent_comparison.csv').write_text('legacy')
            result = export(root)
            self.assertEqual(result['registered_canonical'], 24)
            scores = json.loads((root / 'tables/scores.json').read_text())
            self.assertFalse(any(s['experiment'] == 'final_intent' for s in scores))
            self.assertEqual(list((root / 'tables').glob('final_intent_*')), [])
            for kind in ('goal', 'state', 'multi'):
                cohort = [s for s in scores if s['experiment'] == 'context_ablation_' + kind]
                self.assertEqual(len(cohort), 4)
                self.assertEqual({s['registered_cases'] for s in cohort}, {2})
                if kind in ('goal', 'state'):
                    self.assertTrue(all(s['denominators']['CFA' if kind == 'goal' else 'SFA']['N'] == 1 for s in cohort))
            self.assertEqual(len([s for s in scores if s['experiment'] == 'context_ablation_multi/can']), 4)
            native = json.loads((root / 'tables/native_metrics.json').read_text())
            self.assertFalse(any(s['experiment'] in {'final_intent', 'counterfactual/goal', 'counterfactual/state'} for s in native))
            self.assertFalse((root / 'tables/context_ablation_comparison.csv').exists())
            for kind in ('goal', 'state', 'multi'):
                self.assertTrue((root / ('tables/context_ablation_' + kind + '_comparison.csv')).exists())

    def registration(self, root, main, alias=True):
        canonical = {"key": "main_job", "model": "model", "experiment": "main", "view": "state", "delivery": "sequential", "repeat": 0, "case_id": main["case_id"]}
        aliases = [{**canonical, "key": "ablation_job", "experiment": "ablation", "canonical_job_key": "main_job"}] if alias else []
        write(root / "run_manifest.json", {"repeats": 1, "models": ["model"], "experiments": ["main", "ablation"] if alias else ["main"], "human_reviewed": True})
        write(root / "jobs.json", [canonical])
        write(root / "aliases.json", aliases)
        write_lines(root / "registered_data/cases.jsonl", [main])
        write_lines(root / "registered_data/baseline.jsonl", [])
        write(root / "registered_sources.json", {"T1": {"steps": [{}, {}]}, "T2": {"steps": [{}, {}]}})
        return canonical

    def test_empty_run_exports_registered_unknowns(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            c = case()
            self.registration(root, c)
            result = export(root)
            self.assertEqual(result["pending"], 1)
            self.assertFalse(result["complete_run"])
            scores = json.loads((root / "tables/scores.json").read_text())
            self.assertEqual(len(scores), 2)
            self.assertIsNone(scores[0]["Acc"])
            self.assertTrue((root / "tables/native_metrics.csv").exists())
            self.assertTrue((root / "tables/case_D_multi_event_failures.json").exists())

    def test_main_state_alias_reuses_exact_episode(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            c = case()
            self.registration(root, c)
            e = episode(c)
            e["job_key"] = "main_job"
            write(root / "records/main_job.json", e)
            result = export(root)
            scores = json.loads((root / "tables/scores.json").read_text())
            self.assertEqual([s["Acc"] for s in scores], [1, 1])
            self.assertEqual(result["recorded"], 1)
            self.assertEqual(result["logical_jobs"], 2)

    def test_record_identity_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            c = case()
            self.registration(root, c)
            e = episode(c)
            e["case_id"] = "wrong"
            write(root / "records/main_job.json", e)
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                export(root)

    def test_export_does_not_retain_all_large_prompt_traces(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            c = case()
            job = self.registration(root, c, alias=False)
            e = episode(c)
            e["calls"] = [{"messages": [{"content": "x" * 100000}]}]
            write(root / "records/main_job.json", e)
            records, _ = _load_records(root, [job], [])
            self.assertNotIn("calls", records["main_job"])
            self.assertIn("decision_stages", records["main_job"])


if __name__ == "__main__":
    unittest.main()
