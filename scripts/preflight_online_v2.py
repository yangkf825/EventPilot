#!/usr/bin/env python3
"""Check registered public URLs before paid calls; reachability is not task success."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]

SITE_PREFLIGHT_POLICY = 'registered_public_url_availability_v1'
DEFAULT_MIN_COVERAGE = 0.9
RUNTIME_TIMEOUT_SECONDS = 45
CLEANUP_TIMEOUT_SECONDS = 10
EXPERIMENTS = ('main', 'ablation', 'counterfactual', 'multi', 'baseline', 'final_intent')
BLOCKED = re.compile(
    r'access denied|403\s+forbidden|verify (?:that )?you are (?:a )?human|'
    r'unusual traffic|just a moment|checking (?:your )?browser|'
    r'please (?:complete|solve) (?:the )?captcha|'
    r'enable javascript and cookies to continue|security verification|'
    r'confirm (?:that )?you are not a robot|robot check|attention required', re.I)


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def valid_url(value):
    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit(value)
        return parts.scheme in {'http', 'https'} and bool(parts.hostname) and not (
            parts.username or parts.password)
    except ValueError:
        return False


def selected_plan(args):
    """Use the runner's own selection, including entire registered CF groups.

    URL collection reads executable registrations, never Gold labels or actor
    results. Every setup and event URL is checked, even if an actor might choose
    not to open that event. The browser still has to verify a real checkpoint.
    """
    if args.mode == 'profiles':
        from config.online_v2_authoring import PROFILES
        profiles = PROFILES[:args.limit] if args.limit is not None else PROFILES
        selected = {'profiles': [
            {'case_id': f'F{i:02d}', 'url': p['url'], 'site': p.get('site', '')}
            for i, p in enumerate(profiles, 1)]}
    else:
        from run_online_v2 import select_cases
        selected = select_cases(args)
    unique = {}
    cases = []
    for experiment, registered in selected.items():
        for case in registered:
            cid = case['case_id']
            registrations = [('primary', 'A', case.get('url'))]
            for setup in case.get('setup_tasks', []):
                registrations.append(('setup', setup.get('id', 'setup'), setup.get('url')))
            events = case.get('events') or ([case['event']] if case.get('event') else [])
            for event in events:
                execution = event.get('execution', {})
                # Research tasks require their own registered entry. For other
                # effects inspect a URL only if one is actually registered.
                if execution.get('kind') == 'research_task' or execution.get('url'):
                    registrations.append(('event', event.get('id', 'event'), execution.get('url')))
            urls = set()
            for role, node, value in registrations:
                url = value if isinstance(value, str) else ''
                urls.add(url)
                target = unique.setdefault(url, {
                    'entry_url': url, 'references': [], 'site': case.get('site', ''),
                    'family': case.get('task_family_id', case.get('source_family_id', cid)),
                })
                reference = {'experiment': experiment, 'case_id': cid, 'role': role, 'node': node}
                if reference not in target['references']:
                    target['references'].append(reference)
            cases.append({'experiment': experiment, 'case_id': cid, 'urls': sorted(urls)})
    targets = [{**row, 'target_id': f'U{i:04d}'}
               for i, row in enumerate(unique.values(), 1)]
    # The hash freezes the exact selected registrations; no raw hidden labels
    # or task outcomes are exposed by the report.
    selected_hash = hashlib.sha256(json.dumps(selected, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {'mode': args.mode, 'targets': targets, 'cases': cases,
            'selection_sha256': selected_hash,
            'counts': {experiment: len(registered) for experiment, registered in selected.items()}}


def classify_observation(status, observation):
    """Conservative availability classification, with no challenge bypass."""
    if not isinstance(status, int) or isinstance(status, bool):
        return 'navigation_no_response'
    if status == 403:
        return 'http_forbidden'
    if status < 200 or status >= 400:
        return 'http_error'
    if not isinstance(observation, dict) or not isinstance(observation.get('text'), str):
        return 'observation_error'
    title = observation.get('title', '')
    title = title if isinstance(title, str) else ''
    text = observation['text']
    if (BLOCKED.search(title + ' ' + text[:6000])
            or re.search(r'captcha|cloudflare challenge', title, re.I)):
        return 'blocked_challenge'
    if not valid_url(observation.get('url')) or not text.strip():
        return 'blank_page'
    return 'entry_available'


def coverage_report(plan, rows, minimum):
    available = {row['entry_url'] for row in rows if row['category'] == 'entry_available'}
    total = len(plan['targets'])
    url_coverage = len(available) / total if total else None
    by_experiment = {}
    for case in plan['cases']:
        summary = by_experiment.setdefault(case['experiment'], {'registered_cases': 0, 'available_cases': 0})
        summary['registered_cases'] += 1
        summary['available_cases'] += bool(case['urls']) and set(case['urls']).issubset(available)
    for summary in by_experiment.values():
        count = summary['registered_cases']
        coverage = summary['available_cases'] / count if count else None
        summary.update(coverage=coverage, passed=count > 0 and coverage >= minimum)
    failed = [name for name, summary in by_experiment.items() if not summary['passed']]
    passed = total > 0 and bool(available) and url_coverage >= minimum and not failed
    return {
        'policy': SITE_PREFLIGHT_POLICY, 'minimum_coverage': minimum,
        'unique_registered_urls': total, 'available_urls': len(available),
        'coverage': url_coverage, 'experiments': by_experiment,
        'failure_categories': dict(Counter(row['category'] for row in rows
                                           if row['category'] != 'entry_available')),
        'failed_experiments': failed, 'passed': passed,
        'task_success_verified': False, 'uses_tested_actor_results': False,
        'scope': ('Registered primary/setup/event URL availability only. An available entry page '
                  'does not prove a destination page, trigger, checkpoint, task result or model success.'),
    }


def save_report(args, plan, rows):
    from eventarena.storage_io import atomic_write_json
    report = {**coverage_report(plan, rows, args.min_coverage), 'timestamp_utc': timestamp(),
              'selection': {'mode': plan['mode'], 'sha256': plan['selection_sha256'],
                            'counts': plan['counts'], 'cases': plan['cases']},
              'rows': rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fields = ['target_id', 'family', 'site', 'entry_url', 'category', 'http_status', 'final_url',
              'title', 'visible_controls', 'text_chars', 'timestamp_utc', 'task_success_verified',
              'references', 'error_type', 'cleanup_error_type', 'screenshot_error_type']
    temporary = args.output.with_name(args.output.name + '.tmp')
    with temporary.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, 'references': json.dumps(row['references'], ensure_ascii=False)})
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(args.output)
    atomic_write_json(args.output.with_suffix('.json'), report)
    return report


async def inspect_sites(args, *, playwright_factory=None):
    """No model calls, no authentication, and bounded browser/page lifetimes."""
    plan = selected_plan(args)
    targets = plan['targets']
    rows = []
    directory = args.output.parent / 'site_observations'
    semaphore = asyncio.Semaphore(args.workers)
    browser = playwright = None

    def base_row(target):
        return {**target, 'timestamp_utc': timestamp(), 'category': 'unavailable',
                'task_success_verified': False}

    async def inspect(target):
        async with semaphore:
            row = base_row(target)
            page = None
            stage = 'navigation'
            try:
                if not valid_url(target['entry_url']):
                    row['category'] = 'invalid_url'
                    return row
                # One deadline covers creating the page, navigation and DOM
                # capture; a hanging evaluate cannot bypass the goto timeout.
                async with asyncio.timeout(args.timeout):
                    page = await browser.new_page()
                    response = await page.goto(target['entry_url'], timeout=args.timeout * 1000,
                                               wait_until='domcontentloaded')
                    stage = 'observation'
                    from eventarena.browser import OBSERVE_JS
                    observation = await page.evaluate(OBSERVE_JS)
                status = response.status if response else None
                category = classify_observation(status, observation)
                row.update(http_status=status, category=category)
                if isinstance(observation, dict):
                    row.update(final_url=observation.get('url'), title=observation.get('title'),
                               visible_controls=len(observation.get('candidates', []))
                               if isinstance(observation.get('candidates', []), list) else 0,
                               text_chars=len(observation.get('text', ''))
                               if isinstance(observation.get('text'), str) else 0)
                    from eventarena.storage_io import atomic_write_json
                    stage = 'artifact'
                    directory.mkdir(parents=True, exist_ok=True)
                    atomic_write_json(directory / (target['target_id'] + '.json'), observation)
                    if args.screenshots:
                        try:
                            await asyncio.wait_for(page.screenshot(path=directory / (target['target_id'] + '.png')),
                                                   timeout=args.timeout)
                        except Exception as error:
                            row['screenshot_error_type'] = type(error).__name__
            except TimeoutError:
                row.update(category='timeout', error_type='TimeoutError')
            except Exception as error:
                from eventarena.storage_io import StorageIOError
                # Storage failure is not a website outage and must not produce
                # a reassuring report with missing artifacts.
                if isinstance(error, StorageIOError) or stage == 'artifact':
                    raise
                row.update(category='timeout' if type(error).__name__ == 'TimeoutError' else
                           'observation_error' if stage == 'observation' else 'navigation_error',
                           error_type=type(error).__name__)
            finally:
                if page is not None:
                    try:
                        await asyncio.wait_for(page.close(), timeout=CLEANUP_TIMEOUT_SECONDS)
                    except Exception as error:
                        row['cleanup_error_type'] = type(error).__name__
                progress = {key: value for key, value in row.items() if key != 'references'}
                progress['registered_references'] = len(row['references'])
                print(json.dumps(progress, ensure_ascii=False), flush=True)
            return row

    try:
        if not targets:
            return save_report(args, plan, rows)
        if playwright_factory is None:
            from playwright.async_api import async_playwright
            playwright_factory = async_playwright
        playwright = await asyncio.wait_for(playwright_factory().start(), timeout=RUNTIME_TIMEOUT_SECONDS)
        browser = await asyncio.wait_for(playwright.chromium.launch(headless=True), timeout=RUNTIME_TIMEOUT_SECONDS)
        tasks = [asyncio.create_task(inspect(target)) for target in targets]
        try:
            rows = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
    except (ImportError, TimeoutError) as error:
        rows = [{**base_row(target), 'category': 'runtime_unavailable', 'error_type': type(error).__name__}
                for target in targets]
    except Exception as error:
        if browser is not None:
            raise  # Never conceal report/artifact I/O or an internal defect.
        rows = [{**base_row(target), 'category': 'browser_launch_error', 'error_type': type(error).__name__}
                for target in targets]
    finally:
        for resource, method in ((browser, 'close'), (playwright, 'stop')):
            if resource is not None:
                try:
                    await asyncio.wait_for(getattr(resource, method)(), timeout=CLEANUP_TIMEOUT_SECONDS)
                except Exception:
                    # Page-level observations already have known availability;
                    # resource cleanup is bounded and cannot turn errors true.
                    pass
    rows.sort(key=lambda row: row['target_id'])
    return save_report(args, plan, rows)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--output', type=Path, default=ROOT / 'logs/online_v2/site_preflight.csv')
    result.add_argument('--mode', choices=('dataset', 'profiles'), default='dataset',
                        help='Default checks the exact runner selection; profiles is a legacy diagnostic')
    result.add_argument('--data-dir', type=Path, default=ROOT / 'data/online_v2')
    result.add_argument('--experiments', default='all')
    result.add_argument('--case-ids')
    result.add_argument('--cf-group-ids')
    result.add_argument('--multi-case-ids')
    result.add_argument('--single-limit', type=int, default=100)
    result.add_argument('--cf-groups', type=int, default=10)
    result.add_argument('--multi-limit', type=int, default=20)
    result.add_argument('--split', choices=('all', 'development', 'heldout_candidate'), default='all')
    result.add_argument('--limit', type=int,
                        help='Legacy first-N profiles diagnostic; implies --mode profiles unless a mode is specified')
    result.add_argument('--workers', type=int, default=4)
    result.add_argument('--timeout', type=float, default=25)
    result.add_argument('--min-coverage', type=float, default=DEFAULT_MIN_COVERAGE)
    result.add_argument('--screenshots', action='store_true')
    return result


def main(argv=None):
    cli = parser()
    tokens = list(argv) if argv is not None else sys.argv[1:]
    args = cli.parse_args(tokens)
    mode_explicit = any(token == '--mode' or token.startswith('--mode=') for token in tokens)
    if args.limit is not None and not mode_explicit:
        args.mode = 'profiles'  # Preserve the pre-existing --limit CLI diagnostic.
    if (not math.isfinite(args.timeout) or
            min(args.workers, args.timeout, args.single_limit, args.multi_limit, args.cf_groups) <= 0):
        cli.error('All limits must be positive')
    if args.limit is not None and (args.limit < 1 or args.mode != 'profiles'):
        cli.error('--limit is a positive legacy profiles-mode limit; use --mode profiles')
    if not 0 <= args.min_coverage <= 1:
        cli.error('--min-coverage must be between 0 and 1')
    if args.mode == 'profiles' and any((args.case_ids, args.cf_group_ids, args.multi_case_ids)):
        cli.error('Registered case/group ID selections require --mode dataset')
    if args.output.suffix.casefold() != '.csv':
        cli.error('--output must be a .csv path; the JSON report uses the same filename stem')
    args.experiments = list(EXPERIMENTS) if args.experiments == 'all' else args.experiments.split(',')
    if not args.experiments or any(name not in EXPERIMENTS for name in args.experiments):
        cli.error('Unknown experiment selection')
    runtime = os.environ.get('EA_M2W_RUNTIME')
    if runtime:
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', runtime + '/browsers')
    try:
        report = asyncio.run(inspect_sites(args))
    except ValueError as error:
        print(f'Preflight could not be registered: {error}', file=sys.stderr, flush=True)
        return 2
    except OSError as error:
        print(f'Preflight persistence/read failed: {type(error).__name__} errno={error.errno}; '
              'check dataset and output storage before starting the run.', file=sys.stderr, flush=True)
        return 2
    print(json.dumps({key: value for key, value in report.items() if key not in {'rows', 'selection'}},
                     ensure_ascii=False), flush=True)
    return 0 if report['passed'] else 3


if __name__ == '__main__':
    raise SystemExit(main())
