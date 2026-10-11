"""Author-registered read-only paths to test task feasibility without a paid LLM.

These diagnostics are not model predictions or official WebJudge scores. Every
navigation follows a link actually present in the previous live document.
"""
from __future__ import annotations
import asyncio
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from eventarena.storage_io import atomic_write_json, StorageIOError
from .corpus import digest,now
from .availability import classify, save_csv

CAPTURE_JS="""() => ({url:location.href,title:document.title,text:document.body?.innerText||'',
 links:[...document.querySelectorAll('a[href]')].filter(e=>e.getClientRects().length && getComputedStyle(e).visibility!=='hidden')
 .map(e=>({text:(e.innerText||e.getAttribute('aria-label')||'').trim(),href:e.href}))})"""


def check_requirements(plan,observations):
    checked=[]
    for requirement in plan['requirements']:
        observation=observations[requirement.get('step',-1)]
        field=observation[requirement.get('field','text')]
        passed=bool(re.search(requirement['regex'],field,re.I))
        checked.append({**requirement,'passed':passed})
    return checked


async def probe_tasks(tasks,plans,manifest,output,timeout=35):
    from playwright.async_api import async_playwright
    index={t['task_id']:t for t in tasks}
    if not plans or len({p['task_id'] for p in plans})!=len(plans):
        raise ValueError('Unique nonempty registered task plans required')
    for p in plans:
        if p['task_id'] not in index or not p.get('requirements'):
            raise ValueError('Unknown task or missing task-specific acceptance checks')
    output=Path(output)
    if (output/'manifest.json').exists():
        raise ValueError('Use a new diagnostic output directory; preserve earlier results')
    atomic_write_json(output/'manifest.json',{'schema':'official_task_path_diagnostic_v1',
                      'source_sha256':manifest['raw_sha256'],'plans':plans,'timeout_s':timeout,
                      'actor':'author_registered_script','model_calls':0,'official_leaderboard_comparable':False})
    results=[]
    async with async_playwright() as pw:
        browser=await asyncio.wait_for(pw.chromium.launch(headless=True),45)
        try:
            for n,plan in enumerate(plans,1):
                task=index[plan['task_id']]
                row={'task_id':task['task_id'],'goal':task['goal'],'entry_url':task['url'],
                     'timestamp_utc':now(),'status':'started','task_path_verified':False,
                     'task_success_verified':False,'model_calls':0,'official_leaderboard_comparable':False}
                observations,actions=[],[]
                context=await browser.new_context(viewport={'width':1280,'height':900})
                page=await context.new_page()
                folder=output/('OMW_'+task['task_id'])
                async def navigate(url,origin):
                    async with asyncio.timeout(timeout):
                        response=await page.goto(url,wait_until='domcontentloaded',timeout=timeout*1000)
                        await page.wait_for_timeout(1000)
                        obs=await page.evaluate(CAPTURE_JS)
                        obs['http_status']=response.status if response else None
                        observations.append(obs)
                        actions.append({'op':'GOTO','url':url,'origin':origin,'observed_url':obs['url'],
                                        'index':len(actions),'http_status':obs['http_status']})
                        atomic_write_json(folder/f'observation_{len(observations)-1:02d}.json',obs)
                        if classify(obs['http_status'],obs)!='entry_available':
                            raise RuntimeError('observed_page_unavailable')
                    print(f"  [PATH] {task['task_id']} {page.url}",flush=True)
                try:
                    print(f"[TASK PATH {n}/{len(plans)}] START {task['task_id']}",flush=True)
                    await navigate(task['url'],'exact_official_entry')
                    for step in plan.get('steps',[]):
                        matching={link['href'] for link in observations[-1]['links']
                                  if re.search(step['href_regex'],link['href']) and urlsplit(link['href']).scheme in {'http','https'}}
                        if len(matching)!=1:
                            row['path_error']='missing_or_ambiguous_observed_link'
                            raise ValueError('missing_or_ambiguous_observed_link')
                        await navigate(next(iter(matching)),'link_observed_on_previous_live_page')
                    checks=check_requirements(plan,observations)
                    passed=bool(checks) and all(check['passed'] for check in checks)
                    row.update(status='scripted_path_verified' if passed else 'acceptance_check_failed',
                               task_path_verified=passed,checks=checks,final_url=page.url,
                               scope='Navigation and registered task-specific page checks; no LLM outcome score or human review')
                    try:
                        await asyncio.wait_for(page.screenshot(path=folder/'final.png'),timeout)
                    except Exception as exc:
                        row['screenshot_warning']=type(exc).__name__
                except StorageIOError:
                    raise
                except Exception as exc:
                    row.update(status='timeout' if 'Timeout' in type(exc).__name__ else 'path_unverified',
                               error_type=type(exc).__name__)
                finally:
                    atomic_write_json(folder/'actions.json',actions)
                    atomic_write_json(folder/'result.json',row)
                    await asyncio.wait_for(context.close(),10)
                results.append(row)
                atomic_write_json(output/'task_paths.json',{'results':results,'registered':len(plans),
                                      'verified_paths':sum(r['task_path_verified'] for r in results),
                                      'model_calls':0,'task_success_metric_reported':False})
                save_csv(output/'task_paths.csv',results)
                print(f"[TASK PATH {n}/{len(plans)}] {task['task_id']} {row['status']}",flush=True)
        finally:
            await asyncio.wait_for(browser.close(),10)
    return {'registered':len(plans),'verified_paths':sum(r['task_path_verified'] for r in results),
            'model_calls':0,'task_success_metric_reported':False,'output':str(output)}
