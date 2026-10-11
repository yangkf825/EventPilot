"""Bounded views and exact, scoped retrieval of the authorized public payload.

This is an input interface, not a summary or an oracle: every retrieved value
comes from the already projected actor/decision input, never the full episode.
"""
from __future__ import annotations
import copy
import hashlib
import json

from .transport import compact_json,decode_payload,prepare_messages
from eventarena.json_protocol import extract_object

POLICY='inline_or_indexed_public_context_v1'
LIMITS={'max_read_chars':16000,'max_reads_per_call':12,
        'max_reads_per_episode':80,'retained_read_results':4}
INSTRUCTIONS=''' Additional context tool: READ_CONTEXT reads exact data from the
authorized public input for this call. You may return a JSON object
{"op":"READ_CONTEXT","path":["full_trajectory","observations",0,"text"],"offset":0,"limit":12000}
instead of a browser action or event decision. Then read the returned data and
return your next tool request or final action/decision. A {$context:{path,...}}
is an index entry, not missing data: use its exact path to read the complete value.
When an input is indexed, public_input holds the decoded original input with
ordinary actions/observations arrays. READ_CONTEXT paths omit public_input.
READ_CONTEXT always addresses decoded data: full_trajectory has observations
and actions even when an inline input represents them using a shared value pool.
All historical actions and observations remain accessible. No history is summarized.
For strings offset/limit count Unicode characters; for lists/dictionaries they
count items/fields. Results report next_offset and has_more. For example, to find
a live control use {"op":"READ_CONTEXT","path":["observation","candidates"],"query":"Compare","limit":20}.
query performs a literal case-insensitive search, not semantic inference. Returned
list items include original indices. Read only paths present in this view; no
hidden goals, pre-event history or Gold labels can be retrieved. Only the most
recent retrieval responses stay inline (at most four, fewer if the budget is
small); earlier data can be read again from the same complete public source.
The limits are 16000 result characters, 12 reads per call and 80 per episode.
Reading context performs no browser action or task completion. Evidence must
still be grounded in real observed page text. Return only one JSON object.'''


def get_path(value,path):
    if not isinstance(path,list) or len(path)>32:raise ValueError('Invalid context path')
    for part in path:
        if isinstance(value,dict) and isinstance(part,str) and part in value:value=value[part]
        elif isinstance(value,list) and isinstance(part,int) and not isinstance(part,bool) and 0<=part<len(value):value=value[part]
        else:raise ValueError('Path not present in the authorized input')
    return value


def read_request(raw):
    obj=extract_object(raw)
    if obj.get('op')!='READ_CONTEXT':return None
    path=obj.get('path')
    if not isinstance(path,list) or len(path)>32 or not all(isinstance(p,str) or isinstance(p,int) and not isinstance(p,bool) for p in path):
        raise ValueError('Invalid context path')
    for field in ('offset','limit','message_index'):
        if field in obj and (not isinstance(obj[field],int) or isinstance(obj[field],bool) or obj[field]<0):
            raise ValueError('Invalid context range')
    if obj.get('limit',1)==0:raise ValueError('Empty context range')
    if 'query' in obj and (not isinstance(obj['query'],str) or not obj['query'] or len(obj['query'])>256):
        raise ValueError('Invalid literal context search')
    return {k:copy.deepcopy(v) for k,v in obj.items() if k in {'op','path','offset','limit','message_index','query'}}


def pointer(value,path,message_index):
    kind='string' if isinstance(value,str) else 'list' if isinstance(value,list) else 'dict'
    entry={'$context':{'message_index':message_index,'path':path,'kind':kind,'length':len(value)}}
    if isinstance(value,str):entry['preview']=value[:256]
    elif isinstance(value,dict):
        # These are exact scalar fields, not an interpretation of the state.
        visible={k:v for k,v in value.items() if k in {'url','title','index','task_id','action_index'} and isinstance(v,(str,int,bool))}
        if visible:entry['preview']=visible
    return entry


def indexed_value(value,path,message_index,current_limit=200000,leaf_limit=2000):
    if isinstance(value,dict):
        if '$context' in value or path and path[-1] in {'before','after'}:
            return pointer(value,path,message_index)
        return {k:indexed_value(v,path+[k],message_index,current_limit,leaf_limit) for k,v in value.items()}
    if isinstance(value,list):
        timeline=len(path)>=2 and path[-2:] in [['full_trajectory','observations'],['full_trajectory','actions']]
        current=len(path)==2 and path[0] in {'observation','current_state'} and path[1]=='candidates'
        threshold=current_limit if current else leaf_limit
        if (not timeline or current_limit<4000) and len(compact_json(value))>threshold:
            return pointer(value,path,message_index)
        return [indexed_value(v,path+[i],message_index,current_limit,leaf_limit) for i,v in enumerate(value)]
    if isinstance(value,str):
        current=len(path)==2 and path[0] in {'observation','current_state'} and path[1] in {'text','summary'}
        if len(value)>(current_limit if current else leaf_limit):return pointer(value,path,message_index)
    return copy.deepcopy(value)


def reconstruct_index(value,source):
    """Engineering QA: restore indexed values exactly, including literal refs."""
    if isinstance(value,dict):
        if '$context' in value:
            return copy.deepcopy(get_path(source,value['$context']['path']))
        return {k:reconstruct_index(v,source) for k,v in value.items()}
    if isinstance(value,list):return [reconstruct_index(v,source) for v in value]
    return copy.deepcopy(value)


class ContextAccess:
    def __init__(self,messages):
        self.original=copy.deepcopy(messages);self.sources={};self.encoded={}
        for index,message in enumerate(messages):
            if message.get('role')!='user':continue
            try:payload=json.loads(message['content'])
            except (ValueError,TypeError):continue
            if isinstance(payload,dict):
                self.encoded[str(index)]=payload;self.sources[index]=decode_payload(payload)
        self.context_id=hashlib.sha256(compact_json(self.encoded).encode()).hexdigest()

    def source_record(self):
        return {'policy':POLICY,'context_id':self.context_id,'encoded_public_payloads':self.encoded}

    def messages(self,budget,reads,remaining):
        transmitted,audit=prepare_messages(self.original)
        for message in transmitted:
            if message.get('role')=='system':message['content']+=INSTRUCTIONS
        additions=[]
        for row in reads[-LIMITS['retained_read_results']:]:
            additions.extend([{'role':'assistant','content':compact_json(row['request'])},
                              {'role':'user','content':'Context tool result: '+compact_json(row['result'])}])
        if sum(len(m['content']) for m in transmitted+additions)<=budget:
            return transmitted+additions,{**audit,'context_access_policy':POLICY,'mode':'inline',
                'sent_chars':sum(len(m['content']) for m in transmitted+additions),'context_id':self.context_id}
        # Full data stays in the immutable source. Reduce literal chunks, not
        # the set of available records. Never retrieve outside this projection.
        for threshold in (200000,100000,50000,16000,4000,512):
            indexed=copy.deepcopy(self.original)
            for message in indexed:
                if message.get('role')=='system':message['content']+=INSTRUCTIONS
            for index,source in self.sources.items():
                view=indexed_value(source,[],index,current_limit=threshold,leaf_limit=min(2000,threshold))
                # This metadata is supplied by the runner, not task evidence.
                indexed[index]['content']=compact_json({'public_input':view,'context_access':{
                    'policy':POLICY,'message_index':index,
                    'path_root':'public_input; READ_CONTEXT paths start at the original public input root',
                    'reads_remaining':remaining,'limits':LIMITS}})
            result=indexed+additions
            size=sum(len(m['content']) for m in result)
            if size<=budget:break
        retained=len(additions)//2
        while size>budget and retained>1:
            additions=additions[2:];retained-=1;result=indexed+additions
            size=sum(len(m['content']) for m in result)
        return result,{'policy':audit['policy'],'context_access_policy':POLICY,'mode':'indexed',
                       'original_chars':sum(len(m['content']) for m in self.original),
                       'sent_chars':size,'context_id':self.context_id,'current_literal_limit':threshold,
                       'retained_read_results':retained}

    def read(self,request,max_chars=None):
        try:
            index=request.get('message_index',next(iter(self.sources)))
            if index not in self.sources:raise ValueError('Message is not authorized public JSON')
            path=request['path'];value=get_path(self.sources[index],path)
            offset=request.get('offset',0);limit=request.get('limit',12000)
            query=request.get('query');cap=min(LIMITS['max_read_chars'],max_chars or LIMITS['max_read_chars'])
            result={'message_index':index,'path':path,'offset':offset}
            if isinstance(value,str):
                if query is not None:
                    # casefold can change string length; use original-character
                    # search with re so returned offsets index the real text.
                    import re
                    hits=[]
                    for match in re.finditer(re.escape(query),value,re.I):
                        if match.start()<offset:continue
                        hits.append(match.start())
                        if len(hits)>min(limit,100):break
                    more=len(hits)>min(limit,100);hits=hits[:min(limit,100)]
                    result.update(kind='string_search',match_positions=hits,length=len(value),
                                  next_offset=(hits[-1]+1 if hits else len(value)),has_more=more)
                else:
                    end=min(len(value),offset+min(limit,max(1,cap-1000)))
                    result.update(kind='string',value=value[offset:end],length=len(value),next_offset=end,has_more=end<len(value))
                    while len(compact_json(result))>cap and end>offset:
                        end=offset+(end-offset)//2
                        result.update(value=value[offset:end],next_offset=end,has_more=end<len(value))
            elif isinstance(value,(list,dict)):
                items=list(enumerate(value)) if isinstance(value,list) else list(value.items())
                if query is not None:items=[(k,v) for k,v in items if query.casefold() in compact_json(v).casefold()]
                returned=[];end=offset
                for key,item in items[offset:offset+min(limit,100)]:
                    projected=indexed_value(item,path+[key],index,current_limit=2000,leaf_limit=1000)
                    entry={'index' if isinstance(value,list) else 'key':key,'value':projected}
                    if len(compact_json({**result,'items':returned+[entry]}))>cap-1500:
                        if returned:break
                        entry['value']=pointer(item,path+[key],index)
                    returned.append(entry);end+=1
                result.update(kind='list' if isinstance(value,list) else 'dict',items=returned,
                              length=len(value),matches=len(items),next_offset=end,has_more=end<len(items))
            else:result.update(kind='scalar',value=copy.deepcopy(value),next_offset=0,has_more=False)
            if len(compact_json(result))>cap:raise ValueError('Context result exceeds registered read budget')
            return result
        except (ValueError,KeyError,TypeError,StopIteration):
            return {'error':'invalid_or_unavailable_public_path',
                    'message':'Use a path visible in this input; hidden fields and filesystem paths are unavailable.'}
