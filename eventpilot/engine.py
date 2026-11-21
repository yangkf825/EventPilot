"""Reviewed-event evaluation on recorded pages; no simulated backend outcomes.

Native actions are independently scored before loading the reference successor.
Multi-event decision states are restored to the reviewed reference checkpoints;
these explicit corrections never receive model-action/control credit.
"""
import copy
import json
import re
from .prompts import prefix_messages, decision_messages, actor_messages
from .input_budget import read_resource

SCOPE = 'staged_event_conditioned_teacher_forced_actions_and_control'
INFRA = {'provider_error','environment_error','source_unavailable','framework_error','registered_pending'}
BRANCHES = {'IGNORE','DEFER','HANDLE','REPLAN','TERMINATE'}


def conjunction(values):
    values = list(values)
    return None if any(v is None for v in values) else all(values)


def canonical_branch(decision, follow_up=None):
    if decision in ('IGNORE','DEFER'):
        return decision
    if decision == 'INTERRUPT' and follow_up in ('HANDLE','REPLAN','TERMINATE'):
        return follow_up
    return None


def schedule_valid(schedule, expected):
    """Every legal topological order is accepted, never a single oracle string."""
    if not isinstance(schedule, list) or any(not isinstance(r,str) for r in schedule):
        return False
    pending = set(expected.get('required_tasks',[])) - set(expected.get('completed_tasks',[])) - set(expected.get('cancelled_tasks',[]))
    if len(schedule) != len(set(schedule)) or set(schedule) != pending:
        return False
    pos = {r:i for i,r in enumerate(schedule)}
    return all(pos[x] < pos[y] for x,y in expected.get('precedence_constraints',[]) if x in pos and y in pos)


def default_state(tasks, checkpoint, registered=None):
    registered = list(registered or ['A'])
    return {'active_workspace':'A','recorded_pointers':{r:checkpoint if r=='A' else 0 for r in tasks},
            'required_tasks':registered,'completed_tasks':[], 'cancelled_tasks':[],
            'cancelled_future_work':[], 'precedence_constraints':[],
            'task_status':{r:'active' if r=='A' else 'not_started' for r in registered},
            'next_allowed_tasks':['A']}


def registered_native_scopes(case, delivery='sequential'):
    """Frozen denominators, available even before the first model/API request."""
    private=case.get('private',{})
    lengths=private.get('source_lengths',{})
    if not lengths:
        raise ValueError('Frozen private source lengths are required')
    lengths={r:int(lengths.get(r,lengths.get(t['source_task_id'],0))) for r,t in case['tasks'].items()}
    if any(n<1 for n in lengths.values()):
        raise ValueError('Invalid frozen native task length')
    cp=case.get('checkpoint',0)
    stages=private.get('stages',[])
    if delivery in ('batch','sequential_no_action') and private.get('batch_control'):
        stages=private['batch_control']['stages']
    needed={r:set() for r in case['tasks']}
    needed['A'].update(range(cp))
    if delivery=='sequential':
        for event in case.get('events',[])[1:]:
            for step in event.get('arrival',{}).get('after_reference_controls',[]):
                if step.get('cmd')=='reference_step':
                    needed[step['task']].add(step['source_step'])
    if stages:
        post=stages[-1].get('post_verification_state') or stages[-1]['post_state']
        final=set(post.get('required_tasks',[]))
        pointers=post['recorded_pointers']
    else:
        final=set(case.get('initial_registered_roles',['A']))
        pointers={r:cp if r=='A' else 0 for r in case['tasks']}
    for role in final:
        needed[role].update(range(pointers[role],lengths[role]))
    descriptors=[]
    def add(name,role,indices,required):
        descriptors.append({'scope':name,'workspace':role,
            'source_task_id':case['tasks'][role]['source_task_id'],
            'registered_steps':sorted(indices),'required':bool(required)})
    if cp:add('A_pre','A',range(cp),True)
    add('A_post','A',range(cp,lengths['A']),'A' in final)
    add('A_full','A',range(lengths['A']),'A' in final)
    if 'A' not in final and needed['A']-set(range(cp)):
        add('A_effective','A',needed['A']-set(range(cp)),True)
    for role in case['tasks']:
        if role=='A':continue
        indices=set(range(lengths[role])) if role in final else needed[role]
        add(role,role,indices,bool(indices))
    return descriptors


class Session:
    def __init__(self, case, corpus, model='unknown', experiment='main', view='trajectory', repeat=0,
                 delivery='sequential', max_commands=128, candidate_limit=50):
        self.case, self.corpus = copy.deepcopy(case), corpus
        self.model, self.experiment, self.view, self.repeat = model, experiment, view, repeat
        self.delivery, self.max_commands, self.candidate_limit = delivery, max_commands, candidate_limit
        self.lengths = {r:len(corpus.tasks[t['source_task_id']]['steps']) for r,t in case['tasks'].items()}
        self.private = self.case.get('private',{})
        stages = self.private.get('stages',[])
        if delivery in ('batch','sequential_no_action') and self.private.get('batch_control'):
            stages = self.private['batch_control']['stages']
        self.stages = copy.deepcopy(stages)
        self.events = self.case.get('events',[])
        if len(self.events) != len(self.stages):
            raise ValueError('One private stage is required for every public event')
        self.initial_roles = set(case.get('initial_registered_roles',['A']))
        self.visible_roles = set(self.initial_roles)
        self.active = 'A'
        self.pointers = {r:case.get('checkpoint',0) if r=='A' else 0 for r in case['tasks']}
        self.model_cancelled, self.model_adopted = set(), None
        self.resources, self.public_history, self.observations = {}, [], {}
        self.decision_resources = {}
        self.last_tool_result, self.decision_tool_result, self.last_anchor_notice = None, None, None
        self.last_report, self.last_candidate_pool = None, None
        self.pending_ids, self.delivered_count = [], 0
        self.group_index, self.group_indices = -1, []
        self.command_count, self.format_errors, self.prefix_index = 0, 0, 0
        self.window = None
        self.finished, self.status, self.awaiting = False, None, None
        self._closed = None
        self._verified, self._reads = {}, {}
        self._encoded_reads = {}
        self.stage_tools = 0
        self.episode = {'schema_version':'eventpilot.reviewed.11','execution_scope':SCOPE,
                        'case_id':case['case_id'],'model':model,'experiment':experiment,'view':view,
                        'repeat':repeat,'delivery':delivery,'calls':[], 'decision_stages':[],
                        'native_actions':[], 'control_actions':[], 'reference_checkpoint_corrections':[],
                        'model_grounding_evaluated':True,'real_website_outcome_evaluated':False}
        self.stage_records = {}
        self.groups = ([list(range(len(stages)))] if delivery=='batch' and stages else [[i] for i in range(len(stages))])
        if not stages:
            # A no-event baseline is the same Actor/action protocol, without a decision.
            self.groups = [[]]
        self.expected_native = {r:set() for r in case['tasks']}
        self.expected_native['A'].update(range(case.get('checkpoint',0)))
        self.final_required = set()
        # Native registration is independent of whether an API/model ever reaches
        # a later event. No denominator is constructed from successful attempts.
        self.native_registry=registered_native_scopes(self.case,delivery)
        self.registered_required_pairs={(d['workspace'],i) for d in self.native_registry if d['required']
                                       for i in d['registered_steps']}

    @property
    def has_future_events(self):
        return self.delivered_count < len(self.events)

    def _history_observation(self, role, index):
        tid = self.case['tasks'][role]['source_task_id']
        obs = self.corpus.observation(tid,index)
        ref = obs['snapshot_sha256']
        self.observations[ref] = obs
        return ref

    def trajectory(self):
        return {'encoding':'all observed reference/model steps; explicit independent state corrections',
                'steps':copy.deepcopy(self.public_history), 'observations_by_ref':copy.deepcopy(self.observations)}

    def observation(self, role=None):
        role = role or self.active
        if role not in self.visible_roles:
            raise ValueError('Cannot observe an undisclosed workspace')
        pointer = self.pointers[role]
        if pointer >= self.lengths[role]:
            return {'active_workspace':role,'reference_end_reached':True,
                    'meaning':'end of the recorded workspace, not backend success'}
        return {'active_workspace':role,'reference_end_reached':False,
                **self.corpus.observation(self.case['tasks'][role]['source_task_id'],pointer)}

    def public_workspaces(self):
        return {r:{'goal':self.case['tasks'][r]['goal'], 'active':self.active==r,
                   'reference_end_reached':self.pointers[r]>=self.lengths[r],
                   'cancelled_by_your_tool':r in self.model_cancelled,
                   'selected_replacement_by_your_tool':r==self.model_adopted}
                for r in sorted(self.visible_roles)}

    def _enter_group(self):
        self.group_index += 1
        if self.group_index >= len(self.groups):
            self.finished = True
            self.status = self.status or 'agent_finished'
            return
        self.group_indices = self.groups[self.group_index]
        self.pending_ids = [self.events[i]['id'] for i in self.group_indices]
        self.stage_tools, self.decision_tool_result = 0, None
        self.decision_resources = {}
        if self.group_indices:
            pre = self.stages[self.group_indices[0]]['pre_state']
        else:
            pre = default_state(self.case['tasks'],self.case.get('checkpoint',0),self.initial_roles)
        old = dict(self.pointers)
        for role, pointer in pre.get('recorded_pointers',{}).items():
            if role not in self.pointers or not 0 <= pointer <= self.lengths[role]:
                raise ValueError('Invalid reviewed checkpoint')
            self.pointers[role] = pointer
        self.active = pre.get('active_workspace','A')
        for role in pre.get('task_status',{}):
            self.visible_roles.add(role)
        if self.group_index > 0:
            self.last_anchor_notice = {'origin':'independent_reference_checkpoint_correction',
                'not_model_credit':True, 'meaning':'registered recorded state restored; missing model work remains missing'}
            self.episode['reference_checkpoint_corrections'].append({
                'before':old,'after':dict(self.pointers),'active_workspace':self.active,
                'before_event_id':self.pending_ids[0] if self.pending_ids else None,
                'model_credit':False})
            # Only newly observed past reference actions enter public history.
            # Never expose the step at the current pointer or any future action.
            for role, pointer in self.pointers.items():
                if role not in self.visible_roles:
                    continue
                for i in range(old.get(role,0),pointer):
                    tid = self.case['tasks'][role]['source_task_id']
                    self.public_history.append({'origin':'independent_reference_checkpoint_correction_not_model_action',
                        'workspace':role,'observation_ref':self._history_observation(role,i),
                        'recorded_action':self.corpus.tasks[tid]['steps'][i]['action']})
        for index in self.group_indices:
            event, stage = self.events[index], self.stages[index]
            # Workspace identities are a tool interface to tasks already mentioned
            # in this delivered message, not a list of required/effective answers.
            for role, task in self.case['tasks'].items():
                if role == 'A' or task['goal'].rstrip('.').casefold() in event['text'].casefold() or re.search(r'\bTask\s+'+re.escape(role)+r'\b',event['text']):
                    self.visible_roles.add(role)
            for op in stage.get('semantic_operations',[]):
                if op.get('op')=='add':
                    self.visible_roles.add(op['task'])
            self.delivered_count = max(self.delivered_count,index+1)
        self.window = None
        self.awaiting = None

    def _expected_state(self, stage):
        return copy.deepcopy(stage.get('post_verification_state') or stage['post_state'])

    def _start_window(self):
        if self.group_indices:
            expected = self._expected_state(self.stages[self.group_indices[-1]])
        else:
            expected = default_state(self.case['tasks'],self.case.get('checkpoint',0),self.initial_roles)
        self.final_required = set(expected.get('required_tasks',[]))
        last_group = self.group_index == len(self.groups)-1
        pairs = []
        if last_group:
            for role in expected.get('required_tasks',[]):
                for i in range(self.pointers[role],self.lengths[role]):
                    pairs.append((role,i))
            limit = None
        elif self.delivery=='sequential_no_action':
            limit = 0
        else:
            next_index = self.groups[self.group_index+1][0]
            controls = self.events[next_index].get('arrival',{}).get('after_reference_controls',[])
            pairs = [(x['task'],x['source_step']) for x in controls if x.get('cmd')=='reference_step']
            # An inspection-only gap has no registered native action opportunity.
            limit = len(pairs)
        for role,index in pairs:
            self.expected_native[role].add(index)
        self.window = {'stage_indices':list(self.group_indices),'expected':expected,
                       'start_pointers':dict(self.pointers),'required_pairs':set(pairs),
                       'action_limit':limit,'acted':0,'actions':[], 'controls':[],
                       'priority_checks':[],'cancel_violation':False,'explicit_finish':False,
                       'last_group':last_group,'closed':False}

    def next_request(self):
        if self.awaiting is not None:
            return self.awaiting
        while not self.finished:
            cp = self.case.get('checkpoint',0)
            if self.prefix_index < cp:
                tid = self.case['tasks']['A']['source_task_id']
                messages,pool = prefix_messages(self.corpus,tid,self.prefix_index,self.candidate_limit,self.resources)
                self.last_candidate_pool = pool
                self.awaiting = {'role':'prefix','messages':messages}
                return self.awaiting
            if self.group_index == -1:
                self._enter_group()
            if self.pending_ids:
                self.awaiting = {'role':'decision','messages':decision_messages(self,self.view)}
                return self.awaiting
            if self.window is None:
                self._start_window()
            if self.window['action_limit'] == 0:
                self._finish_window()
                self._enter_group()
                continue
            if self.command_count >= self.max_commands:
                self.status, self.finished = 'step_budget_exhausted', True
                break
            self.awaiting = {'role':'actor','messages':actor_messages(self)}
            return self.awaiting
        return None

    def _record_native(self, role, index, prediction, phase=None):
        tid = self.case['tasks'][role]['source_task_id']
        action = {k:str(prediction.get(k,'')) for k in ('node_id','op','value')}
        pool = self.last_candidate_pool or {'nodes':[]}
        result = {'workspace':role,'phase':phase or ('A_post' if role=='A' else role),
                  'action':copy.deepcopy(action),'attempted':True,'status':'scored',
                  'window_index':self.group_index,
                  **self.corpus.score_action(tid,index,action),
                  'candidate_recalled':bool({n['node_id'] for n in pool['nodes']} & set(self.corpus.tasks[tid]['steps'][index]['positive_node_ids']))}
        self.episode['native_actions'].append(result)
        return result

    def _verification(self, role, query=None, text=None, offset=0, complete_search=False):
        for index in self.group_indices:
            stage = self.stages[index]
            condition = stage.get('condition') or {}
            if condition.get('resolved') != 'Unknown':
                continue
            target = condition.get('predicate_workspace',stage['pre_state']['active_workspace'])
            if role != target:
                continue
            needle = condition.get('query','')
            if complete_search and query is not None and query.casefold()==needle.casefold():
                self._verified[stage['event_id']] = True
            elif text is not None:
                key=(stage['event_id'],role,self.pointers[role])
                ranges=self._reads.setdefault(key,[])
                ranges.append((offset,offset+len(text)))
                if needle.casefold() in text.casefold():
                    self._verified[stage['event_id']] = True
                # Read overlaps are allowed; no-match requires complete coverage.
                end=0
                for start,stop in sorted(ranges):
                    if start>end:break
                    end=max(end,stop)
                total=len(self.corpus.page(self.case['tasks'][role]['source_task_id'],self.pointers[role])['full_text'])
                if end>=total:
                    self._verified[stage['event_id']]=True

    def _read(self, command, decision=False):
        if command.get('cmd')=='read_input':
            # A decision ablation never has access to Actor's larger registry.
            if decision and self.view in ('event-only','goal'):
                return {'ok':False,'reason':'page_evidence_not_available_in_this_information_setting'}
            registry=self.decision_resources if decision else self.resources
            result=read_resource(registry,command)
            self._verify_input_chunk(registry,command,result)
            return result
        role=command.get('task',self.active)
        if role not in self.visible_roles:
            return {'ok':False,'reason':'undisclosed_workspace'}
        if decision and (self.view in ('event-only','goal') or self.case.get('cf_type')=='goal'):
            return {'ok':False,'reason':'page_evidence_not_available_in_this_information_setting'}
        index=self.pointers[role]
        if index>=self.lengths[role]:
            return {'ok':False,'reason':'recorded_workspace_complete'}
        off=command.get('offset',0)
        if not isinstance(off,int) or isinstance(off,bool) or off<0:
            return {'ok':False,'reason':'invalid_offset'}
        tid=self.case['tasks'][role]['source_task_id']
        if command['cmd']=='read_page':
            full=self.corpus.page(tid,index)['full_text']
            text=full[off:off+6000]
            result={'ok':True,'workspace':role,'text':text,'offset':off,'next_offset':off+len(text),
                    'more':off+len(text)<len(full),'text_characters':len(full)}
            self._verification(role,text=text,offset=off)
        else:
            query=command.get('query','')
            if not isinstance(query,str):
                return {'ok':False,'reason':'invalid_query'}
            full=self.corpus.page(tid,index)['full_text']
            result={'ok':True,'workspace':role,**self.corpus.candidates(tid,index,query,off),
                    'complete_page_literal_search':{'query':query,'matched':query.casefold() in full.casefold(),
                                                    'complete':True,'method':'case-insensitive literal full recorded-page text search'}}
            if query:
                self._verification(role,query=query,complete_search=True)
        self.public_history.append({'origin':'model_public_inspection','workspace':role,
                                    'action':copy.deepcopy(command),'tool_result':copy.deepcopy(result)})
        return result

    def _verify_input_chunk(self, registry, command, result):
        """A read_input of current-page evidence can verify a public condition.

        Page identity must match; old/unrelated resources do not qualify. Reading
        a projected excerpt cannot establish absence from a longer complete page.
        """
        if not result.get('ok') or result.get('format')!='public_json_chunk':return
        ref=command.get('resource_id')
        value=registry.get(ref)
        owner=value if isinstance(value,dict) and 'snapshot_sha256' in value else None
        for key in command.get('path',[]):
            if isinstance(value,dict) and 'snapshot_sha256' in value:owner=value
            try:value=value[key]
            except (KeyError,IndexError,TypeError):return
        if isinstance(value,dict) and 'snapshot_sha256' in value:owner=value
        if owner is None:return
        encoded=json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False)
        ranges=self._encoded_reads.setdefault((ref,tuple(command.get('path',[]))),[])
        ranges.append((result['offset'],result['next_offset']))
        end=0
        for start,stop in sorted(ranges):
            if start>end:break
            end=max(end,stop)
        for index in self.group_indices:
            stage=self.stages[index];condition=stage.get('condition') or {}
            if condition.get('resolved')!='Unknown':continue
            role=condition.get('predicate_workspace',stage['pre_state']['active_workspace'])
            if self.pointers[role]>=self.lengths[role]:continue
            page=self.corpus.page(self.case['tasks'][role]['source_task_id'],self.pointers[role])
            if owner.get('snapshot_sha256')!=page['snapshot_sha256']:continue
            needle=condition.get('query','')
            if needle and needle.casefold() in result['text'].casefold():
                self._verified[stage['event_id']]=True
            if end>=len(encoded) and not owner.get('text_projection_truncated',False):
                if value is owner or isinstance(value,str) and value==owner.get('text'):
                    self._verified[stage['event_id']]=True

    def _decision(self, parsed):
        if isinstance(parsed,dict) and parsed.get('cmd') in ('read_page','inspect','read_input'):
            self.stage_tools += 1
            self.decision_tool_result=self._read(parsed,decision=True)
            if self.stage_tools<16:
                return
            parsed=None  # A bounded inspection loop cannot stall a job forever.
        report=parsed if isinstance(parsed,dict) else {}
        self.last_report=copy.deepcopy(report)
        decisions=report.get('decisions',{})
        followups=report.get('follow_ups',{})
        decisions=decisions if isinstance(decisions,dict) else {}
        followups=followups if isinstance(followups,dict) else {}
        for index in self.group_indices:
            stage=self.stages[index]
            eid=stage['event_id']
            condition=stage.get('condition') or {}
            allowed=list(stage.get('allowed_branches',[]))
            unknown=condition.get('resolved')=='Unknown'
            verified=self._verified.get(eid,False) if unknown else None
            post_gold=stage.get('post_verification_gold')
            if unknown and post_gold:
                allowed=[canonical_branch(post_gold['decision'],post_gold.get('follow_up'))]
            predicted=canonical_branch(decisions.get(eid),followups.get(eid))
            expected=self._expected_state(stage)
            final_in_group=index==self.group_indices[-1]
            schedule_ok=schedule_valid(report.get('schedule'),expected) if final_in_group else None
            required=report.get('required_tasks')
            cancelled=report.get('cancelled_tasks')
            state_ok=(isinstance(required,list) and set(required)==set(expected.get('required_tasks',[]))
                      and isinstance(cancelled,list) and set(cancelled)==set(expected.get('cancelled_tasks',[]))) if final_in_group else None
            classification_scope='verification_required' if unknown and not verified else 'unique' if len(allowed)==1 else 'set_valued'
            record={'event_id':eid,'predicted_branch':predicted,'allowed_branches':allowed,
                    'classification_scope':classification_scope,'verification_passed':verified,
                    'expected_state':expected,'reported':copy.deepcopy(report),
                    'declared_schedule_correct':schedule_ok,'state_declaration_correct':state_ok,
                    'decision_correct':predicted in allowed,
                    'execution_success':None,'control_success':None,'priority_correct':None,
                    'actual_schedule_correct':None,'cancel_compliant':None,
                    'resume_success':None,'updated_goal_success':None}
            if self.events[index].get('critical') is not None:
                record['critical']=self.events[index]['critical']
            self.episode['decision_stages'].append(record)
            self.stage_records[eid]=record
        self.pending_ids=[]

    def _ready(self, expected):
        pending=set(expected.get('required_tasks',[])) - set(expected.get('cancelled_tasks',[]))
        pending={r for r in pending if self.pointers[r]<self.lengths[r]}
        return {r for r in pending if not any(y==r and x in pending for x,y in expected.get('precedence_constraints',[]))}

    def _actor(self, parsed):
        self.command_count+=1
        cmd=parsed if isinstance(parsed,dict) else {}
        name=cmd.get('cmd')
        expected=self.window['expected']
        result={'ok':False,'reason':'invalid_command'}
        before_active=self.active
        if name in ('read_page','inspect','read_input'):
            result=self._read(cmd)
        elif name=='switch':
            target=cmd.get('task')
            if target not in self.visible_roles:
                result={'ok':False,'reason':'undisclosed_workspace'}
            elif target in self.model_cancelled:
                result={'ok':False,'reason':'cancelled_by_your_tool_restore_first'}
            elif self.pointers[target]>=self.lengths[target]:
                result={'ok':False,'reason':'recorded_workspace_complete'}
            else:
                self.active=target
                result={'ok':True,'active_workspace':target}
        elif name=='cancel':
            roles=cmd.get('tasks')
            if isinstance(roles,list) and all(isinstance(r,str) and r in self.visible_roles for r in roles):
                self.model_cancelled.update(roles)
                result={'ok':True,'cancelled_by_your_tool':sorted(self.model_cancelled),'history_retained':True}
        elif name=='restore':
            role=cmd.get('task')
            if role in self.visible_roles:
                self.model_cancelled.discard(role)
                result={'ok':True,'restored_workspace':role,'saved_progress_retained':True}
        elif name=='adopt_goal':
            role=cmd.get('task')
            if role in self.visible_roles and role!='A':
                self.model_adopted=role
                if self.pointers['A']<self.lengths['A']:
                    self.model_cancelled.add('A')
                self.model_cancelled.discard(role)
                result={'ok':True,'adopted_workspace':role,'switch_executed':False}
        elif name=='act':
            role=self.active
            index=self.pointers[role]
            self.window['acted']+=1
            cancelled=set(expected.get('cancelled_future_work',expected.get('cancelled_tasks',[])))
            if role in cancelled:
                self.window['cancel_violation']=True
            self.window['priority_checks'].append(role in self._ready(expected))
            if role in self.model_cancelled:
                result={'ok':False,'reason':'cancelled_by_your_tool_restore_first'}
            elif index>=self.lengths[role]:
                result={'ok':False,'reason':'recorded_workspace_complete'}
            else:
                obsref=self._history_observation(role,index)
                self._record_native(role,index,cmd)
                self.window['actions'].append((role,index))
                self.pointers[role]+=1
                tid=self.case['tasks'][role]['source_task_id']
                result={'ok':True,'reference_successor_loaded_independently_of_prediction':True,
                        'model_action_executed_on_live_website':False,
                        'reference_end_reached':self.pointers[role]>=self.lengths[role]}
                # Future prompts get the observed reference action, never its score.
                self.public_history.append({'origin':'model_prediction_then_independent_reference_state_correction',
                    'workspace':role,'observation_ref':obsref,'model_prediction':copy.deepcopy(cmd),
                    'recorded_action':self.corpus.tasks[tid]['steps'][index]['action'],
                    'tool_result':copy.deepcopy(result)})
        elif name in ('wait','finish'):
            if name=='finish':
                self.window['explicit_finish']=True
            result={'ok':True,'action_window_ended':True,'inbox_open':self.has_future_events}
        self.last_tool_result=result
        entry={'command':copy.deepcopy(cmd),'result':copy.deepcopy(result),'window_index':self.group_index,
               'before_active':before_active,'after_active':self.active}
        self.episode['control_actions'].append(entry)
        self.window['controls'].append(entry)
        if name!='act' and name not in ('read_page','inspect','read_input'):
            self.public_history.append({'origin':'model_control','workspace':before_active,'action':copy.deepcopy(cmd),'tool_result':copy.deepcopy(result)})
        if result.get('reason')=='invalid_command':
            self.format_errors+=1
        if self.format_errors>=3:
            self.status,self.finished='invalid_model_output',True
        elif name in ('wait','finish') or self.window['action_limit'] is not None and self.window['acted']>=self.window['action_limit']:
            self._finish_window()
            self._enter_group()

    def accept(self, parsed, role):
        if self.awaiting is None or role!=self.awaiting['role']:
            raise ValueError('Response role does not match the outstanding request')
        self.awaiting=None
        if role=='prefix':
            action={k:str(parsed.get(k,'')) for k in ('node_id','op','value')} if isinstance(parsed,dict) else {}
            index=self.prefix_index
            self._record_native('A',index,action,phase='A_pre')
            tid=self.case['tasks']['A']['source_task_id']
            self.public_history.append({'origin':'independent_model_prefix_prediction_then_reference_state',
                'workspace':'A','observation_ref':self._history_observation('A',index),
                'model_prediction':copy.deepcopy(action),'recorded_action':self.corpus.tasks[tid]['steps'][index]['action']})
            self.prefix_index+=1
        elif role=='decision':
            self._decision(parsed)
        elif role=='actor':
            self._actor(parsed)
        else:
            raise ValueError('Unknown request role')
        self.last_candidate_pool=None

    def _finish_window(self):
        if not self.window or self.window['closed']:
            return
        window=self.window
        window['closed']=True
        expected=window['expected']
        observed=set(window['actions'])
        coverage=window['required_pairs']<=observed
        priorities=window['priority_checks']
        priority=all(priorities) if priorities else False if window['required_pairs'] and expected.get('precedence_constraints') else None
        required_cancels=set(expected.get('cancelled_tasks',[]))
        cancel_ok=required_cancels<=self.model_cancelled and not window['cancel_violation']
        # No-action delivery deliberately gives no tool opportunity between
        # messages. A cancellation declaration is evaluated now; requiring a
        # cancel tool before any Actor call would make the control impossible.
        cancel_checked=cancel_ok if window['action_limit']!=0 else None
        # Model-only progress; forced reference checkpoints never establish success.
        actual_ok=(coverage and (priority is not False) and cancel_ok)
        if window['action_limit']==0:
            actual_ok=None
        first_action=next(iter(window['actions']),None)
        # The first native action identifies a real model choice; an anchor is not a switch.
        for index in window['stage_indices']:
            record=self.stage_records[self.events[index]['id']]
            final=index==window['stage_indices'][-1]
            verified=record['verification_passed'] is not False
            required_update=record['state_declaration_correct'] if final else True
            decision=record['decision_correct']
            native_rows=[r for r in self.episode['native_actions'] if r['window_index']==self.group_index]
            native_map={(r['workspace'],r['source_step']):r for r in native_rows}
            native_ok=all(native_map.get(p,{}).get('step_correct',False) for p in window['required_pairs'])
            local_control=(decision and verified and required_update is not False and cancel_checked is not False
                           and (actual_ok is not False))
            record.update(priority_correct=priority if final else None,
                          actual_schedule_correct=actual_ok if final else None,
                          cancel_compliant=cancel_checked if required_cancels else None,
                          control_success=local_control,
                          execution_success=local_control and native_ok)
            pending=expected.get('required_tasks',[])
            if 'A' in pending and any(r!='A' for r in pending):
                # Count actual model actions across windows, excluding injected
                # reference progress. Returning after an earlier B window counts.
                seq=[a['workspace'] for a in self.episode['native_actions'] if a['phase']!='A_pre']
                a_after_b=any(r=='A' and any(x!='A' for x in seq[:j]) for j,r in enumerate(seq))
                record['resume_success']=a_after_b if window['last_group'] else None
            if 'REPLAN' in record['allowed_branches']:
                record['updated_goal_success']=(self.model_adopted in pending and self.model_adopted!='A' and actual_ok is True and native_ok) if window['action_limit']!=0 else None
        window['result']={'control_success':actual_ok,'coverage':coverage,'priority_correct':priority,
                          'cancel_compliant':cancel_checked,'cancel_violation':window['cancel_violation']}
        self.episode.setdefault('action_windows',[]).append({
            'window_index':self.group_index,'event_ids':[self.events[i]['id'] for i in window['stage_indices']],
            'registered_native_opportunities':len(window['required_pairs']),
            'predicted_native_actions':len(window['actions']), 'required_pairs':[list(x) for x in sorted(window['required_pairs'])],
            'actual_actions':[list(x) for x in window['actions']], **window['result']})

    def snapshot(self):
        return copy.deepcopy({**self.episode,'active_workspace':self.active,
                             'recorded_pointers':self.pointers,'delivered_events':self.delivered_count,
                             'command_count':self.command_count,'status':self.status or 'in_progress'})

    def close(self, status=None, diagnostics=None):
        if self._closed is not None:
            return copy.deepcopy(self._closed)
        status=status or self.status or 'agent_finished'
        unknown=status in INFRA
        self._finish_window()
        result=self.snapshot()
        result['status']=status
        if diagnostics is not None:
            result['error_diagnostics']=copy.deepcopy(diagnostics)
        # Registered missing predictions are known model failures, unless the
        # provider/source interrupted the run. Use the FIRST prediction per step.
        first={}
        for row in self.episode['native_actions']:
            first.setdefault((row['workspace'],row['source_step']),row)
        scopes=[]
        def scope(descriptor):
            name,role,indices,required=(descriptor[k] for k in ('scope','workspace','registered_steps','required'))
            rows=[]
            for i in sorted(indices):
                row=copy.deepcopy(first.get((role,i)))
                if row is None:
                    row={'workspace':role,'source_task_id':self.case['tasks'][role]['source_task_id'],
                         'source_step':i,'attempted':False,'status':'unobserved_infrastructure' if unknown else 'not_attempted_model_stop',
                         'element_correct':None if unknown else False,'operation_f1':None if unknown else 0.0,
                         'step_correct':None if unknown else False,'candidate_recalled':None}
                rows.append(row)
            scopes.append({'scope':name,'workspace':role,'source_task_id':self.case['tasks'][role]['source_task_id'],
                           'registered_steps':sorted(indices),'steps':rows,'required':bool(required)})
        for descriptor in self.native_registry:
            scope(descriptor)
        result['native_scopes']=scopes
        # Includes legitimate earlier work later cancelled, without crediting
        # any reference checkpoint corrections as predictions.
        required_pairs=self.registered_required_pairs | {(r,i) for r,inds in self.expected_native.items() for i in inds}
        native_ok=all(first.get(p,{}).get('step_correct',False) for p in required_pairs)
        stages=result['decision_stages']
        all_delivered=self.delivered_count==len(self.events)
        decisions_ok=all(s['decision_correct'] and s['verification_passed'] is not False for s in stages) and all_delivered
        windows=result.get('action_windows',[])
        actual=[w['control_success'] for w in windows if w['control_success'] is not None]
        controls_ok=all(actual) and all(s['control_success'] is True for s in stages) and all_delivered
        if not self.events:
            controls_ok=all(actual) and self.finished
        cancelled_windows=[w for w in windows if w.get('cancel_violation') is not None]
        termination_stages=[s for s in stages if 'TERMINATE' in s['allowed_branches']]
        actual_schedule=all(actual) and all_delivered and bool(windows)
        declared=[s['declared_schedule_correct'] for s in stages if s['declared_schedule_correct'] is not None]
        priority=[s['priority_correct'] for s in stages if s['priority_correct'] is not None]
        resume=[s['resume_success'] for s in stages if s['resume_success'] is not None]
        updated=[s['updated_goal_success'] for s in stages if s['updated_goal_success'] is not None]
        declared_priorities=[]
        for s in stages:
            if s['declared_schedule_correct'] is None:continue
            schedule=s['reported'].get('schedule')
            positions={r:i for i,r in enumerate(schedule)} if isinstance(schedule,list) and all(isinstance(r,str) for r in schedule) else {}
            pending=set(s['expected_state'].get('required_tasks',[]))-set(s['expected_state'].get('completed_tasks',[]))
            for x,y in s['expected_state'].get('precedence_constraints',[]):
                if x in pending and y in pending:
                    declared_priorities.append(x in positions and y in positions and positions[x]<positions[y])
        seq=[a for a in self.episode['native_actions'] if a['phase']!='A_pre']
        resumed_at=[i for i,a in enumerate(seq) if a['workspace']=='A' and any(x['workspace']!='A' for x in seq[:i])]
        resumed=bool(resumed_at)
        resume_position=True if resumed else False
        # switch preserves pointers by construction; verify the first returned
        # action also follows the last actually observed/saved A pointer.
        for i in resumed_at[:1]:
            prior=[x['source_step'] for x in seq[:i] if x['workspace']=='A']
            expected_index=max(prior)+1 if prior else self.case.get('checkpoint',0)
            resume_position=seq[i]['source_step']==expected_index
        unique_branches={s['allowed_branches'][0] for s in stages if len(s['allowed_branches'])==1}
        has_handle='HANDLE' in unique_branches
        has_defer='DEFER' in unique_branches
        has_ignore='IGNORE' in unique_branches
        has_replan='REPLAN' in unique_branches
        resumed_a_ok=all(first.get(('A',i),{}).get('step_correct',False) for i in range(self.case.get('checkpoint',0),self.lengths['A']))
        new_required={(r,i) for r,i in required_pairs if r!='A'}
        new_native_ok=all(first.get(p,{}).get('step_correct',False) for p in new_required)
        deferred_attempt=bool(new_required & set(first))
        cancellation_checks=[w['cancel_compliant'] for w in windows if w.get('cancel_compliant') is not None]
        evaluation={'episode_success':controls_ok and native_ok,'control_success':controls_ok,
                    'task_completion':native_ok and controls_ok,'event_handling_success':all(s['execution_success'] is True for s in stages) if stages else None,
                    'ptar':any(w.get('cancel_violation') for w in windows if any(s['event_id'] in w['event_ids'] for s in termination_stages)) if termination_stages else None,
                    'priority_accuracy':sum(declared_priorities)/len(declared_priorities) if declared_priorities else None,
                    'priority_correct':all(declared_priorities) if declared_priorities else None,
                    'actual_priority_accuracy':sum(priority)/len(priority) if priority else None,
                    'actual_priority_correct':all(priority) if priority else None,
                    'declared_schedule_correct':all(declared) if declared else None,
                    'actual_schedule_correct':actual_schedule,
                    'resumed':resumed if has_handle else None,
                    'resume_success':resumed and resumed_a_ok if has_handle else None,
                    'resume_correct_position':resume_position if has_handle else None,
                    'updated_goal_success':('A' in self.model_cancelled and bool(new_required) and new_native_ok
                                            and controls_ok and any(a['workspace']!='A' for a in seq)) if has_replan else None,
                    'complete_case_decision_correct':decisions_ok,
                    'effective_constraints':controls_ok,
                    'termination_compliant':all(s['cancel_compliant'] is True for s in termination_stages) if termination_stages else None,
                    'cancel_compliant':all(cancellation_checks) if any(s['expected_state'].get('cancelled_tasks') for s in stages) else None,
                    'ignore_compliant':controls_ok if has_ignore else None,
                    'event_deferred_correctly':controls_ok if has_defer else None,
                    'original_before_deferred':all(priority) if has_defer and priority else None,
                    'deferred_event_resumed':deferred_attempt if has_defer and new_required else None,
                    'deferred_event_success':new_native_ok and controls_ok if has_defer and new_required else None,
                    'event_timely':all(priority) if priority else None,
                    'required_native_steps':len(required_pairs),
                    'attempted_required_native_steps':len(required_pairs & set(first)),
                    'backend_task_success':None}
        if unknown:
            for key,value in list(evaluation.items()):
                if key not in ('required_native_steps','attempted_required_native_steps','backend_task_success'):
                    evaluation[key]=None
            for stage in stages:
                for key in ('execution_success','control_success','priority_correct','actual_schedule_correct','cancel_compliant','resume_success','updated_goal_success'):
                    stage[key]=None
        result['evaluation']=evaluation
        result['registered_required_steps']={r:sorted(v) for r,v in self.expected_native.items()}
        self._closed=copy.deepcopy(result)
        return result
