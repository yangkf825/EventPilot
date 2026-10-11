"""Reversible references to the readable current page; never summarize history."""
from __future__ import annotations
import copy
import hashlib
import json
import re

from eventarena.online_v2_engine import (_resolve_payload_value, decode_actor_payload,
                                       decode_lossless_trajectory, _common_string_edge)

POLICY='lossless_current_page_references_v2'
INSTRUCTIONS=''' In this run, a full_trajectory.values descriptor ["current",path]
refers to the exact value at that path in this same user JSON object. For example,
["current",["observation","text"]] is the complete directly readable current
page text; ["current",["observation","candidates",3]] is the complete fourth
current control. Read the value there without parsing it as another reference.
An optional third item lists keys to omit from a referenced current dictionary;
historical node IDs are restored by their own overlay, never replaced by live IDs.
A values descriptor ["css_path",p] expands each p segment tag or tag[n] into
tag:nth-of-type(1) or tag:nth-of-type(n), joined by >. The transport_encoding
current_locators field lists the current candidate indices whose dom_path uses
this same CSS notation. Live node_id, text, href and all other fields stay readable.
The current_control_defaults dictionary is merged into each current control first;
explicit per-control fields override it (including false, null and empty values).
A descriptor ["dict_patch",base,changes] copies the referenced dictionary and
replaces named fields with the decoded changes. Pool reference names are opaque.
Unchanged current-page values are printed once. Every historical step, observation,
control and character remains accessible. No history has been summarized or cut.'''


def short_css_path(value):
    if not isinstance(value,str):return None
    matches=[re.fullmatch(r'([A-Za-z][\w-]*):nth-of-type\(([1-9][0-9]*)\)',part)
             for part in value.split('>')]
    if not matches or not all(matches):return None
    return '>'.join(m[1]+('['+m[2]+']' if m[2]!='1' else '') for m in matches)


def expand_css_path(value):
    if not isinstance(value,str):raise ValueError('Invalid CSS path code')
    matches=[re.fullmatch(r'([A-Za-z][\w-]*)(?:\[([1-9][0-9]*)\])?',part)
             for part in value.split('>')]
    if not matches or not all(matches):raise ValueError('Invalid CSS path code')
    return '>'.join(m[1]+':nth-of-type('+(m[2] or '1')+')' for m in matches)


def compact_json(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':'))


def value_key(value):
    # Dict insertion order can differ after resolving a control overlay.
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,
                                     separators=(',',':')).encode()).digest()


def current_anchors(payload):
    anchors={}
    roots=[]
    if isinstance(payload.get('observation'),dict):
        roots=[(['observation',k],v) for k,v in payload['observation'].items()
               if k in {'url','title','text','candidates'}]
    elif isinstance(payload.get('current_state'),dict):
        roots=[(['current_state',k],v) for k,v in payload['current_state'].items()
               if k in {'url','summary','candidates'}]
    def visit(path,value):
        if isinstance(value,(str,list,dict)) and len(compact_json(value))>=80:
            anchors.setdefault(value_key(value),path)
        if isinstance(value,dict):
            for k,v in value.items():visit(path+[k],v)
        elif isinstance(value,list):
            for k,v in enumerate(value):visit(path+[k],v)
    for path,value in roots:visit(path,value)
    return anchors


def at_path(payload,path):
    value=payload
    for segment in path:value=value[segment]
    return copy.deepcopy(value)


def reference_keys(value):
    if isinstance(value,dict):
        if set(value)=={'$ref'} and isinstance(value['$ref'],str):
            yield value['$ref']
        else:
            for child in value.values():yield from reference_keys(child)
    elif isinstance(value,list):
        for child in value:yield from reference_keys(child)


def descriptor_keys(descriptor):
    kind=descriptor[0]
    if kind=='dict':
        for value in descriptor[1].values():yield from reference_keys(value)
    elif kind=='list':
        for value in descriptor[1]:yield from reference_keys(value)
    elif kind=='row':
        yield from reference_keys(descriptor[1])
        for value in descriptor[2]:yield from reference_keys(value)
    elif kind in {'overlay','splice','text_patch'}:
        yield from reference_keys(descriptor[1])
    elif kind=='dict_patch':
        yield from reference_keys(descriptor[1])
        yield from reference_keys(descriptor[2])
    # string/current values and overlay overrides are exact source literals.


def pack_payload(payload):
    if payload.get('transport_encoding',{}).get('policy')==POLICY:
        return copy.deepcopy(payload),{'policy':POLICY,'replaced_values':0,
                                       'removed_unreachable_values':0,'already_packed':True}
    # Earlier official runs may already use current-page references. Restore
    # those exact values before examining descriptors; packing is idempotent.
    payload=expand_payload(payload)
    pool=payload.get('full_trajectory',{})
    if not isinstance(pool,dict) or pool.get('encoding')!='lossless_nested_references_v3':
        return copy.deepcopy(payload),{'policy':POLICY,'replaced_values':0,'removed_unreachable_values':0}
    packed=copy.deepcopy(payload)
    target=packed['full_trajectory']
    anchors=current_anchors(payload)
    root='observation' if isinstance(payload.get('observation'),dict) else 'current_state'
    controls=payload.get(root,{}).get('candidates',[])
    cores={value_key({k:v for k,v in c.items() if k not in {'node_id','id'}}):i
           for i,c in enumerate(controls) if isinstance(c,dict)}
    replaced=0
    css_paths=0;dict_patches=0;control_bases={}
    for key in pool['values']:
        value=_resolve_payload_value(pool,{'$ref':key})
        path=anchors.get(value_key(value))
        if path is not None and at_path(payload,path)==value:
            descriptor=['current',path]
            if len(compact_json(descriptor))<len(compact_json(target['values'][key])):
                target['values'][key]=descriptor;replaced+=1
        if isinstance(value,dict) and value_key(value) in cores:
            path=[root,'candidates',cores[value_key(value)]]
            core={k:v for k,v in at_path(payload,path).items() if k not in {'node_id','id'}}
            descriptor=['current',path,['node_id','id']]
            if core==value and len(compact_json(descriptor))<len(compact_json(target['values'][key])):
                target['values'][key]=descriptor;replaced+=1
        if isinstance(value,str):
            short=short_css_path(value)
            descriptor=['css_path',short]
            if short is not None and len(compact_json(descriptor))<len(compact_json(target['values'][key])):
                target['values'][key]=descriptor;css_paths+=1
        original=pool['values'][key]
        # Controls that share semantics but occur at different DOM positions
        # share their complete base. Keep bases shallow and preserve locators.
        if original[0]=='row' and isinstance(value,dict) and 'dom_path' in value:
            signature=value_key({k:v for k,v in value.items() if k!='dom_path'})
            if signature in control_bases:
                base=control_bases[signature]
                schema=_resolve_payload_value(pool,original[1])
                descriptor=['dict_patch',{'$ref':base},{'dom_path':original[2][schema.index('dom_path')]}]
                if len(compact_json(descriptor))<len(compact_json(target['values'][key])):
                    target['values'][key]=descriptor;dict_patches+=1
            else:control_bases[signature]=key
    # Only remove values no longer reachable from the public payload. The
    # referenced states, fields and chronological records are all retained.
    pending=list(reference_keys(target['fields']))
    if target.get('record_values_shared'):pending.extend(reference_keys(target['records']))
    if packed.get('payload_encoding') in {'lossless_actor_shared_v1','lossless_actor_shared_v2'}:
        encoded={k:v for k,v in packed.items() if k not in {'observation','full_trajectory'}}
        pending.extend(reference_keys(encoded))
        observation=packed.get('observation',{})
        if packed['payload_encoding']=='lossless_actor_shared_v1':
            pending.extend(reference_keys(observation))
        elif 'checkpoint_projection' in observation:
            pending.extend(reference_keys(observation['checkpoint_projection']))
    reachable=set()
    while pending:
        key=pending.pop()
        if key in reachable:continue
        if key not in target['values']:raise ValueError('Missing source trajectory value')
        reachable.add(key)
        pending.extend(descriptor_keys(target['values'][key]))
    before=len(target['values'])
    target['values']={k:v for k,v in target['values'].items() if k in reachable}
    # Long locator paths and URLs share complete prefixes. Use the existing
    # lossless splice format, keeping its bases complete to avoid long chains.
    protected={d[1]['$ref'] for d in target['values'].values()
               if d[0] in {'splice','text_patch'} and isinstance(d[1],dict) and '$ref' in d[1]}
    bases=[];splices=0
    for key,descriptor in list(target['values'].items()):
        if descriptor[0]!='string' or not isinstance(descriptor[1],str):continue
        value=descriptor[1]
        if len(value)<128:continue
        best=descriptor;best_size=len(compact_json(descriptor))
        if key not in protected:
            for base_key,base in bases[-64:]:
                prefix=_common_string_edge(base,value)
                if prefix<64:continue
                suffix=_common_string_edge(base,value,suffix=True,limit=min(len(base),len(value))-prefix)
                end=len(value)-suffix if suffix else len(value)
                candidate=['splice',{'$ref':base_key},prefix,len(base)-prefix-suffix,value[prefix:end]]
                size=len(compact_json(candidate))
                if size+32<best_size:best,best_size=candidate,size
        target['values'][key]=best
        if best[0]=='string':bases.append((key,value))
        else:splices+=1
    # Only strict, reversible nth-of-type locators use the shorter notation.
    # Current actionable IDs and semantic control fields remain plain JSON.
    indices=[]
    for i,control in enumerate(packed.get(root,{}).get('candidates',[])):
        if not isinstance(control,dict):continue
        short=short_css_path(control.get('dom_path'))
        if short is not None and len(short)<len(control['dom_path']):
            control['dom_path']=short;indices.append(i)
    current=packed.get(root,{}).get('candidates',[])
    defaults={}
    if current and all(isinstance(c,dict) for c in current):
        # Share repeated technical defaults only. Action IDs and each control's
        # text/tag/URL/path remain individually readable. Never create a field
        # that was absent from any original control.
        common=set.intersection(*(set(c) for c in current))
        for field in sorted(common & {'role','name','type','checked','selected',
                                       'placeholder','value','disabled','options'}):
            groups={}
            for c in current:
                digest=value_key(c[field])
                if digest not in groups:groups[digest]=[c[field],0]
                groups[digest][1]+=1
            value,count=max(groups.values(),key=lambda item:item[1])
            field_size=len(compact_json({field:value}))-1
            if (count-1)*field_size>48:
                defaults[field]=copy.deepcopy(value)
                for c in current:
                    if value_key(c[field])==value_key(value):del c[field]
    if 'transport_encoding' in packed:raise ValueError('Reserved transport field collision')
    packed['transport_encoding']={'policy':POLICY,'current_locators':{'root':root,'indices':indices},
                                  'current_control_defaults':defaults}
    # Short opaque pool IDs change reference spelling only, never source data.
    digits='0123456789abcdefghijklmnopqrstuvwxyz'
    def short_id(number):
        result=''
        while number:result=digits[number%36]+result;number//=36
        return result or '0'
    names={key:short_id(i) for i,key in enumerate(target['values'])}
    def rename(value):
        if isinstance(value,dict):
            if set(value)=={'$ref'}:return {'$ref':names[value['$ref']]}
            return {k:rename(v) for k,v in value.items()}
        if isinstance(value,list):return [rename(v) for v in value]
        return value
    values={}
    for key,original in target['values'].items():
        d=copy.deepcopy(original);kind=d[0]
        if kind=='dict':d[1]={k:rename(v) for k,v in d[1].items()}
        elif kind in {'list','row','overlay','splice','text_patch','dict_patch'}:d[1]=rename(d[1])
        if kind=='row':d[2]=rename(d[2])
        elif kind=='dict_patch':d[2]={k:rename(v) for k,v in d[2].items()}
        values[names[key]]=d
    target['values']=values;target['fields']=rename(target['fields'])
    if target.get('record_values_shared'):target['records']=rename(target['records'])
    if packed.get('payload_encoding') in {'lossless_actor_shared_v1','lossless_actor_shared_v2'}:
        for key in packed:
            if key not in {'observation','full_trajectory','transport_encoding'}:packed[key]=rename(packed[key])
        observation=packed.get('observation',{})
        if packed['payload_encoding']=='lossless_actor_shared_v1':packed['observation']=rename(observation)
        elif 'checkpoint_projection' in observation:observation['checkpoint_projection']=rename(observation['checkpoint_projection'])
    return packed,{'policy':POLICY,'replaced_values':replaced,
                   'removed_unreachable_values':before-len(reachable),'locator_splices':splices,
                   'css_paths':css_paths,'current_css_paths':len(indices),'control_patches':dict_patches,
                   'shared_current_defaults':len(defaults)}


def expand_payload(payload):
    expanded=copy.deepcopy(payload)
    metadata=expanded.pop('transport_encoding',None)
    if metadata is not None:
        if metadata.get('policy')!=POLICY:raise ValueError('Unsupported official transport encoding')
        locators=metadata['current_locators']
        if metadata.get('current_control_defaults'):
            current=expanded[locators['root']]['candidates']
            expanded[locators['root']]['candidates']=[{**copy.deepcopy(metadata['current_control_defaults']),**c} for c in current]
        if locators['indices']:
            controls=expanded[locators['root']]['candidates']
            for index in locators['indices']:controls[index]['dom_path']=expand_css_path(controls[index]['dom_path'])
    pool=expanded.get('full_trajectory',{})
    if isinstance(pool,dict):
        for key,descriptor in list(pool.get('values',{}).items()):
            if descriptor[0]=='current':
                # The legacy resolver returns this data as an exact literal,
                # including dict/list values. It never reinterprets $ref text.
                value=at_path(expanded,descriptor[1])
                if len(descriptor)==3:value={k:v for k,v in value.items() if k not in descriptor[2]}
                pool['values'][key]=['string',value]
            elif descriptor[0]=='css_path':
                pool['values'][key]=['string',expand_css_path(descriptor[1])]
        # Resolve sparse dictionary changes in dependency order. Their values
        # are decoded once and installed as source literals for legacy decode.
        pending={k for k,d in pool.get('values',{}).items() if d[0]=='dict_patch'}
        while pending:
            progress=False
            for key in list(pending):
                d=pool['values'][key]
                if pending.intersection(descriptor_keys(d)):continue
                value=_resolve_payload_value(pool,d[1])
                if not isinstance(value,dict):raise ValueError('Invalid control patch base')
                value.update({k:_resolve_payload_value(pool,v) for k,v in d[2].items()})
                pool['values'][key]=['string',value];pending.remove(key);progress=True
            if not progress:raise ValueError('Cyclic control patch reference')
    return expanded


def decode_payload(payload):
    expanded=expand_payload(payload)
    if expanded.get('payload_encoding') in {'lossless_actor_shared_v1','lossless_actor_shared_v2'}:
        return decode_actor_payload(expanded)
    pool=expanded.get('full_trajectory')
    if isinstance(pool,dict) and pool.get('encoding')=='lossless_nested_references_v3':
        expanded['full_trajectory']=decode_lossless_trajectory(pool)
    return expanded


def prepare_messages(messages):
    result=copy.deepcopy(messages)
    audits=[]
    for index,message in enumerate(result):
        if message.get('role')!='user':continue
        try:payload=json.loads(message['content'])
        except (TypeError,ValueError):continue
        if not isinstance(payload,dict):continue
        packed,audit=pack_payload(payload)
        message['content']=compact_json(packed)
        audit.update(message_index=index,original_chars=len(messages[index]['content']),
                     sent_chars=len(message['content']))
        audits.append(audit)
    if any(a['replaced_values'] or a.get('already_packed') or a.get('css_paths') or a.get('current_css_paths') for a in audits):
        for message in result:
            if message.get('role')=='system' and INSTRUCTIONS not in message['content']:
                message['content']+=INSTRUCTIONS
    return result,{'policy':POLICY,'messages':audits,
                   'original_chars':sum(len(m['content']) for m in messages),
                   'sent_chars':sum(len(m['content']) for m in result)}
