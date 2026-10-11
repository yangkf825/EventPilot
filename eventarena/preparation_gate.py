"""Check environment coverage before any tested actor is sampled.

Eligibility comes only from common preparation and strict reset audit. It never
uses predicted labels, task scores or tested actor outcomes to select cases.
"""
from collections import Counter

PREPARATION_GATE_POLICY = 'per_experiment_common_environment_coverage_v1'
DEFAULT_MIN_COVERAGE = 0.9


class PreparationCoverageError(RuntimeError):
    pass


def coverage_report(selected, gates, repeats, minimum=DEFAULT_MIN_COVERAGE):
    if not 0 <= minimum <= 1:
        raise ValueError('Minimum preparation coverage must be between 0 and 1')
    experiments = {}
    for experiment, cases in selected.items():
        if experiment == 'final_intent':
            continue  # This baseline has no event checkpoint; entry preflight applies.
        keys = {(c.get('original_case_id', c['case_id']), repeat)
                for c in cases for repeat in range(repeats)}
        eligible = sum(gates.get(key, {}).get('eligible') is True for key in keys)
        count = len(keys)
        coverage = eligible / count if count else None
        reasons = Counter(gates.get(key, {}).get('reason', 'missing_eligibility_record')
                          for key in keys if gates.get(key, {}).get('eligible') is not True)
        experiments[experiment] = {'registered_case_repeats': count, 'eligible': eligible,
                                   'coverage': coverage, 'failure_reasons': dict(reasons),
                                   'passed': count == 0 or eligible > 0 and coverage >= minimum}
    failed = [name for name, row in experiments.items() if not row['passed']]
    return {'policy': PREPARATION_GATE_POLICY, 'minimum_coverage': minimum,
            'diagnostic_threshold': minimum == 0, 'passed': not failed,
            'failed_experiments': failed, 'experiments': experiments,
            'eligibility_reads_tested_actor_results': False,
            'selection_changes_gold_or_registered_cases': False,
            'scope': 'availability gate; not a task-success or model-accuracy metric'}


def enforce_coverage(report):
    if report['passed']:
        return
    details = ', '.join(f"{name}: {row['eligible']}/{row['registered_case_repeats']}"
                        for name, row in report['experiments'].items() if not row['passed'])
    raise PreparationCoverageError(
        f"Preparation coverage is below {report['minimum_coverage']:.0%} ({details}). "
        'Tested actors were not started. Inspect preparation_gate.json and '
        'preparation_eligibility/comparison.csv. After repairing the environment, '
        'resume the same protocol with --resume --retry-infra; do not interpret unavailable cases as model failures.')
