"""Bounded, resumable entry-page tests with compact factual observations."""
from __future__ import annotations

import asyncio
from collections import Counter
import csv
import io
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from eventarena.storage_io import atomic_write_json
from .storage import atomic_write_bytes
from .corpus import digest, now

POLICY = 'official_entry_availability_v2'
OBSERVATION_JS = """() => ({url:location.href,title:document.title,
text:(document.body?.innerText||'').slice(0,60000),
controls:[...document.querySelectorAll('a,button,input,select,textarea,[role="button"]')]
.filter(e=>e.getClientRects().length && getComputedStyle(e).visibility!=='hidden')
.slice(0,120).map(e=>({tag:e.tagName.toLowerCase(),text:(e.innerText||e.getAttribute('aria-label')||'').slice(0,240),
type:e.getAttribute('type'),placeholder:e.getAttribute('placeholder'),href:e.getAttribute('href')}))})"""
CHALLENGE = re.compile(r'access denied|403\s+forbidden|verify (?:that )?you are (?:a )?human|'
                       r'unusual traffic|just a moment|checking (?:your )?browser|'
                       r'please (?:complete|solve) (?:the )?captcha|enable javascript and cookies to continue|'
                       r'confirm (?:that )?you are not a robot|attention required|robot check|'
                       r'complete (?:the )?verification test|security verification|安全验证|人机验证', re.I)


def classify(status, observation):
    if status is None:
        return 'navigation_no_response'
    if status == 403:
        return 'http_forbidden'
    if status >= 400 or status < 200:
        return 'http_error'
    text = observation.get('text', '')
    title = observation.get('title', '')
    if re.search(r'captcha|cloudflare challenge', title, re.I) or CHALLENGE.search(title + ' ' + text[:6000]):
        return 'blocked_challenge'
    if not text.strip():
        return 'blank_page'
    if any(value in text.lower() for value in ('no longer accessible from mainland china', 'not available in your region')):
        return 'region_restricted'
    if len(text.strip())<220 and re.search(r'cookies|cookie consent|enable javascript',text,re.I):
        return 'consent_or_script_only'
    return 'entry_available'


def audit_saved_report(report_path,output):
    """Correct a classifier using immutable captured evidence, no new site requests.

    The old report is kept. All entries are reclassified, not selected by model
    accuracy; rows without an observed page retain the original transport error.
    """
    report_path,output=Path(report_path),Path(output)
    previous=json.loads(report_path.read_text())
    rows=[]
    for original in previous['rows']:
        row=dict(original)
        artifact=report_path.parent/'observations'/(row['target_id']+'.json')
        if artifact.exists():
            obs=json.loads(artifact.read_text())
            row['previous_category']=row['category']
            row['category']=classify(row.get('http_status'),obs)
            row['observation_sha256']=digest(artifact.read_bytes())
            row['observation_file']=str(artifact)
        rows.append(row)
    tasks=[{'task_id':t['task_id'],'goal':t['goal'],'url':t['entry_url'],'level':t['level']} for t in previous['tasks']]
    result=summary(tasks,rows)
    result.update(classifier_audit='all_saved_observations_reclassified_without_model_results',
                  captured_report_sha256=digest(report_path.read_bytes()),
                  source_report=str(report_path),new_network_requests=0)
    atomic_write_json(output/'availability.json',result)
    plan=json.loads((report_path.parent/'manifest.json').read_text())
    plan.update(policy=POLICY,audit_source_sha256=result['captured_report_sha256'])
    atomic_write_json(output/'manifest.json',plan)
    save_csv(output/'websites.csv',rows);save_csv(output/'tasks.csv',result['tasks'])
    return result


def targets(tasks):
    groups = {}
    for task in tasks:
        groups.setdefault(task['url'], []).append(task['task_id'])
    return [{'entry_url': url, 'task_ids': ids, 'target_id': 'URL_' + digest(url.encode())[:16]}
            for url, ids in groups.items()]


def summary(tasks, rows):
    by_url = {row['entry_url']: row for row in rows}
    task_rows = []
    for task in tasks:
        row = by_url.get(task['url'], {})
        task_rows.append({'task_id': task['task_id'], 'goal': task['goal'], 'entry_url': task['url'],
                          'level': task['level'], 'entry_category': row.get('category', 'not_tested'),
                          'http_status': row.get('http_status'), 'final_url': row.get('final_url'),
                          'task_success_verified': False,
                          'task_status': 'not_executed',
                          'candidate_for_baseline': row.get('category') == 'entry_available'})
    return {'policy': POLICY, 'timestamp_utc': now(), 'tested_urls': len(rows),
            'available_urls': sum(r['category'] == 'entry_available' for r in rows),
            'entry_available_tasks': sum(r['candidate_for_baseline'] for r in task_rows),
            'registered_tasks': len(tasks), 'categories': dict(Counter(r['category'] for r in rows)),
            'verified_task_successes': 0,
            'scope': 'Entry-page access only; no task, destination, event trigger or checkpoint success inferred.',
            'rows': rows, 'tasks': task_rows}


def save_csv(path, rows):
    if not rows:
        atomic_write_bytes(path, b'')
        return
    out = io.StringIO(newline='')
    fields = list(dict.fromkeys(k for r in rows for k in r))
    writer = csv.DictWriter(out, fieldnames=fields)
    writer.writeheader()
    writer.writerows({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict,list)) else v
                     for k,v in row.items()} for row in rows)
    atomic_write_bytes(path, out.getvalue().encode('utf-8-sig'))


async def check_sites(tasks, corpus_manifest, output, *, workers=3, timeout=25, resume=False,
                      retry_failed=False, screenshots=False, playwright_factory=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    plan = {'policy': POLICY, 'source_sha256': corpus_manifest['raw_sha256'],
            'selection_sha256': digest(json.dumps(tasks,sort_keys=True).encode()),
            'workers': workers, 'timeout_s': timeout, 'screenshots': screenshots,
            'observation_policy': 'first_60000_text_chars_and_120_controls_for_availability_only'}
    previous = output / 'manifest.json'
    if previous.exists():
        if not resume or json.loads(previous.read_text()) != plan:
            raise ValueError('Existing site test requires --resume and identical corpus/selection/settings')
    atomic_write_json(previous, plan)
    checks = targets(tasks)
    semaphore = asyncio.Semaphore(workers)
    completed = 0
    rows = []
    if playwright_factory is None:
        from playwright.async_api import async_playwright
        playwright_factory = async_playwright
    pw = browser = None

    async def inspect(target):
        nonlocal completed
        async with semaphore:
            cache = output / 'url_results' / (target['target_id'] + '.json')
            if resume and cache.exists():
                row = json.loads(cache.read_text())
                if row['entry_url'] != target['entry_url']:
                    raise ValueError('Cached URL identity mismatch')
                if not retry_failed or row['category'] == 'entry_available':
                    completed += 1
                    print(f"[SITE {completed}/{len(checks)}] CACHED {target['entry_url']} {row['category']}", flush=True)
                    return row
            row = target | {'timestamp_utc': now(), 'category': 'not_tested', 'task_success_verified': False}
            context = None
            stage = 'navigation'
            try:
                print(f"[SITE START] {target['entry_url']}", flush=True)
                async with asyncio.timeout(timeout):
                    # One isolated cookie/localStorage state per site, no HAR/trace.
                    context = await browser.new_context(viewport={'width':1280,'height':900})
                    page = await context.new_page()
                    response = await page.goto(target['entry_url'], wait_until='domcontentloaded', timeout=timeout*1000)
                    stage = 'observation'
                    await page.wait_for_timeout(1500)
                    obs = await page.evaluate(OBSERVATION_JS)
                    row.update(http_status=response.status if response else None,
                               final_url=obs['url'], title=obs['title'], text_chars=len(obs['text']),
                               visible_controls=len(obs['controls']))
                    row['category'] = classify(row['http_status'],obs)
                    stage = 'persistence'
                    atomic_write_json(output / 'observations' / (target['target_id']+'.json'), obs)
                    if screenshots:
                        await page.screenshot(path=output / 'observations' / (target['target_id']+'.png'))
            except Exception as exc:
                if stage == 'persistence':
                    raise
                row.update(category='timeout' if 'Timeout' in type(exc).__name__ else 'navigation_error',
                           error_type=type(exc).__name__)
            finally:
                if context is not None:
                    try:
                        await asyncio.wait_for(context.close(), 8)
                    except Exception:
                        row['cleanup_warning'] = True
            atomic_write_json(cache, row)
            completed += 1
            print(f"[SITE {completed}/{len(checks)}] {target['entry_url']} {row['category']} HTTP={row.get('http_status')}",flush=True)
            return row
    try:
        pw = await asyncio.wait_for(playwright_factory().start(),45)
        browser = await asyncio.wait_for(pw.chromium.launch(headless=True),45)
        # Structured cancellation ensures no detached writes after an I/O error.
        async with asyncio.TaskGroup() as group:
            jobs = [group.create_task(inspect(target)) for target in checks]
        rows = [job.result() for job in jobs]
    finally:
        for resource, method in ((browser,'close'),(pw,'stop')):
            if resource is not None:
                try:
                    await asyncio.wait_for(getattr(resource,method)(),10)
                except Exception:
                    pass
    report = summary(tasks, rows)
    atomic_write_json(output / 'availability.json',report)
    save_csv(output / 'websites.csv',rows)
    save_csv(output / 'tasks.csv',report['tasks'])
    return report
