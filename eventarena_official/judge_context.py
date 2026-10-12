"""Bounded exact evidence access for the fixed Judge; never summarize sources."""
import copy
import json
from .context_access import ContextAccess,read_request,LIMITS

POLICY='fixed_judge_exact_evidence_access_v6'
MAX_INLINE_CHARS=90000

async def call_evidence_judge(model,messages,audit_records):
    access=ContextAccess(messages);reads=[]
    # Unlike a browser Actor, the Judge can only retrieve its actual evidence
    # payload and return the originally required verification JSON.
    while True:
        remaining=LIMITS['max_reads_per_call']-len(reads)
        transmitted,audit=access.messages(MAX_INLINE_CHARS,reads,remaining)
        for message in transmitted:
            if message.get('role')=='system':
                message['content']+=' For this call you are the outcome Judge. READ_CONTEXT is only an evidence retrieval operation; no browser action is permitted. Your final response must follow the verification JSON contract above, including all registered criteria when required. Inability to determine adequacy from retrieved evidence is null, never a guessed true.'
        record={'policy':POLICY,'context_id':access.context_id,'transport':audit,
                'messages':transmitted,'api_ok':False}
        audit_records.append(record)
        if sum(len(m['content']) for m in transmitted)>MAX_INLINE_CHARS+2000:
            request=None;raw=None
        else:
            raw=await model.call(transmitted);record.update(raw=str(raw),api_ok=True)
            try:request=read_request(raw)
            except ValueError:request=None
        if raw is not None and request is None:return raw
        if raw is None or remaining<=0:
            payload=next(iter(access.sources.values()),{})
            unknown={'success':None,'reason':'Fixed Judge evidence access budget exhausted','evidence':[]}
            criteria=payload.get('registered_requirements',[])
            ids=[c['id'] for c in criteria if isinstance(c,dict) and 'id' in c]
            if ids:unknown['criterion_results']={cid:copy.deepcopy(unknown) for cid in ids}
            return json.dumps(unknown)
        result=access.read(request,max_chars=LIMITS['max_read_chars'])
        reads.append({'request':request,'result':result})
        record['read_result']=copy.deepcopy(result)
