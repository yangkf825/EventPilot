#!/usr/bin/env python3
"""Send short connectivity probes per configured model; redact credentials."""
import argparse
import concurrent.futures
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from run_models import ROOT, load_config, parse_label, DECISIONS, tls_context


def safe_message(value, keys):
    text=str(value or '')
    for key in keys:
        if key:text=text.replace(key,'[REDACTED]')
    text=re.sub(r'(?i)Bearer\s+[^\s"\']+','Bearer [REDACTED]',text)
    text=re.sub(r'(?i)sk-[A-Za-z0-9_-]{8,}','[REDACTED]',text)
    return text[:800]


def probe_timeout(model, override=None, config_timeout=None):
    """Use the experiment timeout unless a probe-specific override was supplied."""
    value=override if override is not None else model.get('timeout')
    if value is None:value=config_timeout if config_timeout is not None else 180
    value=float(value)
    if value<=0:raise ValueError('timeout must be positive')
    return value


def probe(model, timeout=None, retries=0, config_timeout=None):
    timeout=probe_timeout(model,timeout,config_timeout)
    if retries<0:raise ValueError('retries must be non-negative')
    key=os.environ.get(model['api_key_env'])
    if not key:
        return {'name':model['name'],'model':model['model'],'api_ok':False,
                'category':'missing_environment_variable','message':model['api_key_env'],
                'attempts':0,'timeout_s':timeout}
    body={'model':model['model'],'stream':False,'messages':[
        {'role':'system','content':'For this connectivity test, reply with only IGNORE.'},
        {'role':'user','content':'Reply with exactly IGNORE.'}]}
    for field in ('temperature','max_tokens','max_completion_tokens','top_p'):
        if model.get(field) is not None:body[field]=model[field]
    body.update(model.get('extra_body',{}))
    request=urllib.request.Request(model['endpoint'],data=json.dumps(body).encode('utf-8'),method='POST',
                                   headers={'Content-Type':'application/json','Authorization':'Bearer '+key})
    started=time.monotonic()
    for attempt in range(1,retries+2):
        row={'name':model['name'],'model':model['model'],'api_ok':False,
             'attempts':attempt,'timeout_s':timeout}
        print(json.dumps({'event':'probe_start','name':model['name'],'model':model['model'],
                          'attempt':attempt,'timeout_s':timeout},ensure_ascii=False),flush=True)
        retryable=False
        try:
            with urllib.request.urlopen(request,timeout=timeout,context=tls_context()) as response:
                row['http_status']=response.status
                data=json.load(response)
            first=(data.get('choices') or [{}])[0] if isinstance(data,dict) else {}
            content=first.get('message',{}).get('content') if isinstance(first,dict) else None
            if isinstance(content,list):content=''.join(p.get('text','') for p in content if isinstance(p,dict))
            row['api_ok']=isinstance(content,str) and bool(content.strip())
            row['category']='ok' if row['api_ok'] else 'missing_text_content'
            row['decision']=parse_label(content,DECISIONS,'decision') if isinstance(content,str) else None
            row['finish_reason']=first.get('finish_reason') if isinstance(first,dict) else None
        except urllib.error.HTTPError as error:
            row.update(http_status=error.code,category='http_error')
            retryable=error.code==429 or 500<=error.code<600
            try:
                data=json.load(error)
                detail=data.get('error',data) if isinstance(data,dict) else {}
                message=detail.get('message','') if isinstance(detail,dict) else str(detail)
            except (ValueError,OSError):message='Non-JSON provider error'
            row['message']=safe_message(message,[key])
        except (urllib.error.URLError,TimeoutError,OSError,ValueError) as error:
            row['category']=type(error).__name__
            row['message']=safe_message(str(error),[key])
            retryable=isinstance(error,(urllib.error.URLError,TimeoutError,OSError))
        if not retryable or attempt>retries:break
        time.sleep(min(2**(attempt-1),10))
    row['duration_s']=round(time.monotonic()-started,3)
    return row


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default=str(ROOT/'models.gateway.json'))
    parser.add_argument('--only',help='Comma-separated configured model names (default: all enabled models)')
    parser.add_argument('--timeout',type=float,help='Override model/config timeout (fallback: 180 seconds)')
    parser.add_argument('--retries',type=int,default=0,help='Retries for 429, 5xx and transport errors (default: 0)')
    parser.add_argument('--output',help='Optional sanitized JSON diagnostic report')
    args=parser.parse_args(argv)
    config=load_config(Path(args.config).expanduser().resolve())
    models=[m for m in config['models'] if m.get('enabled') and m['backend']=='chat-completions']
    if args.only is not None:
        names=[name.strip() for name in args.only.split(',')]
        if any(not name for name in names):parser.error('--only contains an empty model name')
        duplicates=sorted({name for name in names if names.count(name)>1})
        if duplicates:parser.error('--only contains duplicate model names: '+', '.join(duplicates))
        unknown=sorted(set(names)-{m['name'] for m in models})
        if unknown:parser.error('Unknown or unavailable models: '+', '.join(unknown))
        models=[m for m in models if m['name'] in names]
    if not models:parser.error('No selected enabled chat-completions model')
    if args.timeout is not None and args.timeout<=0:parser.error('timeout must be positive')
    if args.retries<0:parser.error('retries must be non-negative')
    try:
        for model in models:probe_timeout(model,args.timeout,config.get('timeout'))
    except (TypeError,ValueError) as error:parser.error(str(error))
    missing=[m['api_key_env'] for m in models if not os.environ.get(m['api_key_env'])]
    if missing:parser.error('Set environment variables: '+', '.join(missing))
    rows=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(models)) as pool:
        futures=[pool.submit(probe,m,args.timeout,args.retries,config.get('timeout')) for m in models]
        for future in concurrent.futures.as_completed(futures):
            row=future.result();rows.append(row)
            print(json.dumps(row,ensure_ascii=False),flush=True)
    rows.sort(key=lambda r:next(i for i,m in enumerate(models) if m['name']==r['name']))
    if args.output:
        path=Path(args.output).expanduser();path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return 0 if all(r['api_ok'] for r in rows) else 1


if __name__=='__main__':raise SystemExit(main())
