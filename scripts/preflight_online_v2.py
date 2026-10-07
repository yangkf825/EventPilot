#!/usr/bin/env python3
"""Check runtime and public entry-page availability; not task success evidence."""
from __future__ import annotations
import argparse
import asyncio
import csv
import json
import os
from pathlib import Path
import re
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def inspect_sites(args):
    from playwright.async_api import async_playwright
    from config.online_v2_authoring import PROFILES
    from eventarena.browser import OBSERVE_JS
    rows = []
    semaphore = asyncio.Semaphore(args.workers)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        async def inspect(index, profile):
            async with semaphore:
                page = await browser.new_page()
                row = {'family': f'F{index:02d}', 'site': profile['site'], 'entry_url': profile['url'],
                       'timestamp_utc': datetime.now(timezone.utc).isoformat(),
                       'category': 'unavailable', 'task_success_verified': False}
                try:
                    response = await page.goto(profile['url'], timeout=args.timeout * 1000,
                                               wait_until='domcontentloaded')
                    observation = await page.evaluate(OBSERVE_JS)
                    status = response.status if response else None
                    block = bool(re.search(r'access denied|verify (?:that )?you are human|unusual traffic|403 forbidden|just a moment',
                                           observation['title'] + ' ' + observation['text'][:1600], re.I))
                    row.update(http_status=status, final_url=observation['url'], title=observation['title'],
                               visible_controls=len(observation['candidates']), text_chars=len(observation['text']),
                               category='blocked' if block or status and status >= 400 else 'entry_available')
                    directory = args.output.parent / 'site_observations'
                    directory.mkdir(parents=True, exist_ok=True)
                    (directory / f'F{index:02d}.json').write_text(json.dumps(observation, ensure_ascii=False, indent=2), encoding='utf-8')
                    await page.screenshot(path=directory / f'F{index:02d}.png')
                except Exception as error:
                    row['error_type'] = type(error).__name__
                finally:
                    await page.close()
                rows.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
        await asyncio.gather(*(inspect(i, p) for i, p in enumerate(PROFILES[:args.limit], 1)))
        await browser.close()
    rows.sort(key=lambda row: row['family'])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with args.output.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'logs/online_v2/site_preflight.csv')
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--timeout', type=int, default=25)
    args = parser.parse_args()
    if min(args.limit, args.workers, args.timeout) < 1:
        parser.error('All limits must be positive')
    runtime = os.environ.get('EA_M2W_RUNTIME')
    if runtime:
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', runtime + '/browsers')
    asyncio.run(inspect_sites(args))


if __name__ == '__main__':
    main()
