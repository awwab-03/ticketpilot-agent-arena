"""Bounded observe -> decide -> validate -> act -> observe loop.

The model only proposes the next step. The system owns: step/time/token budgets,
contract validation and bounded repair, the autonomy gate, tool execution with
retries, fault injection, and every stopping decision.
"""
import json
import logging
from time import perf_counter
from langchain_core.messages import BaseMessage
from app import llm
from app.config import settings
from app.models import AgentDecision, AgentState, ArenaRequest, ArenaResponse, Metrics, Observation, ToolTrace
from app.prompts import build_messages, grounding_text, new_fence, resolve_goal
from app.tools import APPROVAL_ACTIONS, APPROVAL_TOOL, BLOCKED_ACTIONS, TOOLS, Sandbox, ToolGateway
from app.validation import check_semantics, normalize, parse_decision

log = logging.getLogger('agent')

# What the model boundary returns when the Arena injects invalid_agent_decision:
# valid JSON that violates the enum, the action registry and field types.
CORRUPTED_DECISION = json.dumps({'status': 'proceed', 'action': 'refund_everything',
                                 'arguments': 'all', 'user_message': 42})
TIME_MARGIN_S = 3.0

def _short(value, limit=200):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + '...'

class Run:
    def __init__(self, request: ArenaRequest, history: list[BaseMessage], model: str,
                 deadline: float | None = None):
        self.request, self.history, self.model = request, history, model
        self.started = perf_counter()
        # Stop a margin before the outer wrapper's hard timeout so the trace is never lost.
        self.deadline = (deadline or self.started + settings.run_timeout_seconds) - TIME_MARGIN_S
        goal, self.goal_note = resolve_goal(request.task, history)
        self.state = AgentState(request_id=request.request_id, goal=request.task, active_goal=goal,
                                max_steps=request.arena_config.max_steps)
        fault = request.arena_config.fault.type
        self.decision_fault = fault == 'invalid_agent_decision'
        self.sandbox = Sandbox()
        self.gateway = ToolGateway(self.sandbox, fault, settings.max_tool_retries,
                                   settings.tool_timeout_seconds, self.deadline)
        self.fence = new_fence()
        self.tool_calls: list[ToolTrace] = []
        self.approvals: list[dict] = []
        self.last_pending: list[str] = []

    # ------------------------------------------------------------- stopping --
    def stop(self, status: str, reason: str, message: str):
        s = self.state
        s.status, s.stop_reason = status, reason
        s.final_response = (message or reason).strip()[:2000]
        s.event('agent_stop', status=status, reason=reason)

    def _progress_summary(self) -> str:
        done = [f'{o.action}({", ".join(str(v) for v in o.arguments.values())[:60]})'
                for o in self.state.observations if o.ok]
        return ', '.join(done) if done else 'nothing yet'

    # ------------------------------------------------------------ main loop --
    async def run(self) -> ArenaResponse:
        s = self.state
        s.event('agent_start', goal=_short(s.active_goal, 300), max_steps=s.max_steps,
                fault=self.request.arena_config.fault.type, model=self.model)
        client = llm.get_client(self.model)
        if client is None:
            self.stop('failed', 'model_not_configured',
                      'No language model is configured on this server, so the agent cannot run.')
        while s.status == 'running':
            if not self._within_budget():
                break
            s.step += 1
            raw = await self._decide(client)
            if raw is None:
                break
            decision = self._validate(raw)
            if decision is None:
                continue
            await self._act(decision)
        return self._response()

    def _within_budget(self) -> bool:
        s = self.state
        pending = ', '.join(self.last_pending) or 'the rest of the request'
        if s.step >= s.max_steps:
            self.stop('budget_exceeded', 'step_budget_reached',
                      f'Stopped: the step budget ({s.max_steps}) ran out before the goal was finished. '
                      f'Completed so far: {self._progress_summary()}. Not finished: {pending}.')
        elif self.deadline - perf_counter() < 1.5:
            self.stop('budget_exceeded', 'time_budget_reached',
                      f'Stopped: the time budget ran out. Completed so far: {self._progress_summary()}.')
        elif (s.input_tokens or 0) + (s.output_tokens or 0) >= settings.max_run_tokens:
            self.stop('budget_exceeded', 'token_budget_reached',
                      f'Stopped: the token budget ({settings.max_run_tokens}) was used up. '
                      f'Completed so far: {self._progress_summary()}.')
        return s.status == 'running'

    async def _decide(self, client) -> str | None:
        s = self.state
        if self.decision_fault:  # first matching operation: the first model decision
            self.decision_fault = False
            s.event('fault_injected', fault='invalid_agent_decision')
            return CORRUPTED_DECISION
        messages = build_messages(s, self.request.task, self.goal_note, self.history,
                                  self.request.external_context, self.fence, settings.max_history_messages)
        timeout = min(settings.model_timeout_seconds, self.deadline - perf_counter())
        try:
            reply = await client.complete(messages, timeout=timeout)
        except llm.ModelUnavailable as exc:
            s.errors.append({'type': 'model_unavailable', 'step': s.step, 'detail': str(exc)})
            self.stop('failed', 'model_unavailable',
                      f'The language model is unavailable ({exc}). No further actions were taken. '
                      f'Completed so far: {self._progress_summary()}.')
            return None
        s.model_calls += 1
        if reply.input_tokens is not None and reply.output_tokens is not None:
            s.input_tokens = (s.input_tokens or 0) + reply.input_tokens
            s.output_tokens = (s.output_tokens or 0) + reply.output_tokens
            cost = llm.estimate_cost(reply.model or self.model, reply.input_tokens, reply.output_tokens)
            s.cost_usd = None if cost is None else round((s.cost_usd or 0) + cost, 8)
        s.event('model_call', model=reply.model or self.model, latency_ms=round(reply.latency_ms, 1),
                input_tokens=reply.input_tokens, output_tokens=reply.output_tokens,
                finish_reason=reply.finish_reason)
        return reply.text

    def _validate(self, raw: str) -> AgentDecision | None:
        s = self.state
        decision, violations = parse_decision(raw)
        if decision is not None:
            grounding = grounding_text(self.request.task, self.history, self.request.external_context, s)
            violations = check_semantics(decision, s, grounding)
        if not violations:
            s.consecutive_invalid, s.repair_feedback, s.last_invalid_output = 0, [], None
            notes = normalize(decision)
            s.event('decision', status=decision.status, action=decision.action,
                    arguments=_short(decision.arguments), valid=True,
                    complete_on_success=decision.complete_on_success, **({'normalized': notes} if notes else {}))
            return decision
        s.consecutive_invalid += 1
        s.errors.append({'type': 'contract_violation', 'step': s.step, 'violations': violations[:5]})
        s.event('decision_rejected', valid=False, violations=violations[:5])
        if s.consecutive_invalid > settings.max_decision_repairs:
            self.stop('contract_error', 'invalid_decision_after_repairs',
                      f'Stopped: the model produced {s.consecutive_invalid} invalid decisions in a row, exceeding '
                      f'the repair limit ({settings.max_decision_repairs}). Nothing unvalidated was executed. '
                      f'Completed so far: {self._progress_summary()}.')
        elif s.step >= s.max_steps and any(o.ok for o in s.observations):
            # Valid work was under way and the budget ran out during a repair: budget is the stop reason.
            pending = ', '.join(self.last_pending) or 'the rest of the request'
            self.stop('budget_exceeded', 'step_budget_reached',
                      f'Stopped: the step budget ({s.max_steps}) ran out (the last decision was invalid and was not '
                      f'executed). Completed so far: {self._progress_summary()}. Not finished: {pending}.')
        elif s.step >= s.max_steps:
            self.stop('contract_error', 'invalid_decision_no_budget_for_repair',
                      'Stopped: the last model decision was invalid and no step budget remained to repair it. '
                      f'Nothing unvalidated was executed. Completed so far: {self._progress_summary()}.')
        else:
            s.repair_feedback, s.last_invalid_output = violations[:6], raw[:1000]
            s.event('repair_requested', attempt=s.consecutive_invalid, limit=settings.max_decision_repairs)
        return None

    # ---------------------------------------------------------------- acting --
    async def _act(self, d: AgentDecision):
        s = self.state
        for a in d.user_requested_actions:
            if (a in APPROVAL_ACTIONS or a in BLOCKED_ACTIONS) and a not in s.user_requested_actions:
                s.user_requested_actions.append(a)
        self.last_pending = d.pending_items
        message = (d.user_message or '').strip()

        if d.status == 'continue' and d.action in TOOLS:
            ok = await self._run_tool(d.action, d.arguments)
            if ok and d.complete_on_success:
                await self._complete(message)
            return
        if d.action in BLOCKED_ACTIONS or (d.status == 'blocked' and self._requested(BLOCKED_ACTIONS)):
            self._block(d.action or self._requested(BLOCKED_ACTIONS)[0], message)
            return
        if d.action in APPROVAL_ACTIONS:
            filed = await self._file_approval(d.action, d.arguments, message or d.rationale)
            if filed and (d.status != 'continue' or not d.pending_items):
                self._stop_for_approval(message)
            return
        if d.status == 'approval_required':
            for action in self._requested(APPROVAL_ACTIONS):
                if not await self._file_approval(action, {}, message or d.rationale):
                    return
            self._stop_for_approval(message)
        elif d.status == 'blocked':
            self.stop('blocked', 'request_out_of_scope', message)
        elif d.status == 'needs_clarification':
            self.stop('needs_clarification', 'missing_information', message)
        elif d.status == 'completed' and d.pending_items and not self._requested(APPROVAL_ACTIONS):
            # The model says "done" but lists unfinished work: keep working while budget remains.
            s.event('completion_deferred', pending=d.pending_items[:5])
            s.repair_feedback = [f'You reported completion but pending_items is not empty: {d.pending_items[:5]}. '
                                 'Continue with that work, or remove items that need approval, are blocked, '
                                 'or can only be done by a human.']
            s.last_invalid_output = None
        elif d.status == 'completed':
            await self._complete(message)
        elif d.status == 'failed':
            if s.unavailable_tools:
                self.stop('tool_error', 'tool_failure_unrecovered',
                          f'{message}\n\nUnavailable after retries: {", ".join(s.unavailable_tools)}.')
            else:
                self.stop('failed', 'agent_cannot_proceed', message)

    def _requested(self, catalog: dict) -> list[str]:
        return [a for a in self.state.user_requested_actions if a in catalog]

    async def _complete(self, message: str):
        # The system, not the model, decides whether "done" is allowed to mean done:
        # a request that included a restricted action can never end as plain "completed".
        blocked = self._requested(BLOCKED_ACTIONS)
        if blocked:
            self._block(blocked[0], message)
            return
        filed = {a['action'] for a in self.approvals}
        missing = [a for a in self._requested(APPROVAL_ACTIONS) if a not in filed]
        if missing:
            self.state.event('policy_override', reason='user requested approval-only actions', actions=missing)
        for action in missing:
            if not await self._file_approval(action, {}, message or f'User requested {action}'):
                return
        if self.approvals:
            self._stop_for_approval(message)
        else:
            self.stop('completed', 'goal_completed', message)

    def _block(self, action: str, message: str):
        s = self.state
        self.tool_calls.append(ToolTrace(step=max(s.step, 1), tool=action, outcome='rejected'))
        s.event('autonomy_gate', action=action, decision='blocked')
        note = f'Policy: "{action}" ({BLOCKED_ACTIONS[action].lower()}) is outside this agent\'s authority. Nothing was changed.'
        self.stop('blocked', 'action_outside_authority', f'{message}\n\n{note}' if message else note)

    async def _file_approval(self, action: str, arguments: dict, reason: str) -> bool:
        s = self.state
        if any(a['action'] == action for a in self.approvals):
            return True
        self.tool_calls.append(ToolTrace(step=max(s.step, 1), tool=action, outcome='rejected'))
        s.event('autonomy_gate', action=action, decision='approval_required')
        args = {'action': action, 'reason': (reason or f'User requested {action}')[:300].ljust(3)}
        for key in ('ticket_id', 'order_id'):
            if isinstance(arguments.get(key), str):
                args[key] = arguments[key]
        ok = await self._run_tool(APPROVAL_TOOL.name, args, spec=APPROVAL_TOOL)
        if ok:
            self.approvals.append(s.observations[-1].result)
        return ok

    def _stop_for_approval(self, message: str):
        notes = [f'{a["action"]} -> {a["approval_id"]} (pending human approval)' for a in self.approvals]
        note = ('Policy: consequential actions are never executed automatically. '
                + ('; '.join(notes) if notes else 'Human approval is required.') + '. Nothing was executed.')
        self.stop('approval_required', 'approval_required', f'{message}\n\n{note}' if message else note)

    async def _run_tool(self, name: str, arguments: dict, spec=None) -> bool:
        s = self.state
        spec = spec or TOOLS[name]
        outcome = await self.gateway.execute(s.step, spec, arguments)
        self.tool_calls += outcome.traces
        failures = [t.outcome for t in outcome.traces if t.outcome not in ('success', 'rejected')]
        s.observations.append(Observation(step=s.step, action=name, arguments=arguments, ok=outcome.ok,
                                          result=outcome.result, error=outcome.error))
        s.event('tool_result', tool=name, ok=outcome.ok, attempts=len(outcome.traces),
                outcomes=[t.outcome for t in outcome.traces],
                observation=_short(outcome.result if outcome.ok else outcome.error, 300))
        if failures and outcome.ok:
            s.errors.append({'type': 'tool_fault_recovered', 'step': s.step, 'tool': name, 'failures': failures})
        if not outcome.ok:
            kind = 'tool_failure' if outcome.exhausted else 'tool_rejected'
            s.errors.append({'type': kind, 'step': s.step, 'tool': name, 'detail': outcome.error})
            if outcome.exhausted and name not in s.unavailable_tools:
                s.unavailable_tools.append(name)
                if spec is APPROVAL_TOOL:
                    self.stop('tool_error', 'tool_failure_unrecovered',
                              f'Could not file the approval request ({outcome.error}). Nothing was executed.')
            return False
        if name == 'get_ticket':
            s.known_tickets[outcome.result['ticket_id']] = {'status': outcome.result['status'], 'triaged': False}
        elif name == 'get_order':
            s.known_orders[outcome.result['order_id']] = outcome.result
        elif name == 'triage_ticket':
            s.known_tickets.setdefault(outcome.result['ticket_id'], {})['triaged'] = True
        return True

    # -------------------------------------------------------------- response --
    def _response(self) -> ArenaResponse:
        s = self.state
        if s.status == 'running':  # defensive: the loop never exits without a stop
            self.stop('failed', 'internal_error', 'The run ended unexpectedly.')
        log.info(json.dumps({'request_id': s.request_id, 'status': s.status, 'stop_reason': s.stop_reason,
                             'steps': s.step, 'events': s.events}, default=str)[:20000])
        return ArenaResponse(
            request_id=s.request_id, status=s.status, final_response=s.final_response or s.stop_reason,
            steps=s.step, stop_reason=s.stop_reason, tool_calls=self.tool_calls, errors=s.errors,
            events=s.events,
            metrics=Metrics(model_calls=s.model_calls, input_tokens=s.input_tokens,
                            output_tokens=s.output_tokens, estimated_cost_usd=s.cost_usd))

async def run_agent(request, history, model, deadline=None):
    return await Run(request, history, model, deadline).run()
