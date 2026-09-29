"""Sandbox actions for the Support-Ticket Triage agent.

Every tool has a typed input model and a typed output model. All execution goes
through ToolGateway, which validates arguments, enforces a per-call timeout,
validates the tool's output, retries retryable failures within MAX_TOOL_RETRIES,
and is the single place where Arena tool faults are injected.

Nothing here touches a real system: each run gets a deep copy of the sample data
and "writes" (triage, drafts, approval requests) only change that copy.
"""
import asyncio
import copy
import json
import re
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Callable, Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from app.config import ROOT
from app.models import ToolTrace

TICKET_ID = r'^T-\d{4}$'
ORDER_ID = r'^A\d{3}$'
Category = Literal['billing', 'shipping', 'technical', 'account', 'general']
Priority = Literal['P1', 'P2', 'P3', 'P4']
Queue = Literal['billing', 'logistics', 'tech_support', 'account_security', 'general']
CATEGORY_TO_QUEUE = {'billing': 'billing', 'shipping': 'logistics', 'technical': 'tech_support',
                     'account': 'account_security', 'general': 'general'}

def load_sample_data() -> dict:
    return json.loads((ROOT / 'data/sample_data.json').read_text(encoding='utf-8'))

_SAMPLE = load_sample_data()

class ToolRejected(Exception):
    """Business-rule rejection (not found, closed ticket...). Not retryable."""

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')

# ---------------------------------------------------------------- inputs ---
class SearchTicketsArgs(Strict):
    query: str | None = Field(default=None, min_length=2, max_length=100)
    customer_email: str | None = Field(default=None, max_length=120)
    status: Literal['open', 'closed', 'any'] = 'open'

class TicketArgs(Strict):
    ticket_id: str = Field(pattern=TICKET_ID)

class OrderArgs(Strict):
    order_id: str = Field(pattern=ORDER_ID)

class SearchKbArgs(Strict):
    query: str = Field(min_length=2, max_length=100)

class TriageArgs(Strict):
    ticket_id: str = Field(pattern=TICKET_ID)
    category: Category
    priority: Priority
    queue: Queue
    reason: str = Field(min_length=5, max_length=300)

class DraftReplyArgs(Strict):
    ticket_id: str = Field(pattern=TICKET_ID)
    body: str = Field(min_length=20, max_length=1200)

class ApprovalArgs(Strict):
    action: str
    ticket_id: str | None = Field(default=None, pattern=TICKET_ID)
    order_id: str | None = Field(default=None, pattern=ORDER_ID)
    reason: str = Field(min_length=3, max_length=300)

# --------------------------------------------------------------- outputs ---
class TicketSummary(Strict):
    ticket_id: str
    subject: str
    customer_id: str
    status: str
    created_at: str

class SearchTicketsResult(Strict):
    total_matches: int = Field(ge=0)
    tickets: list[TicketSummary]

class TicketView(Strict):
    ticket_id: str
    status: Literal['open', 'closed']
    channel: str
    created_at: str
    subject: str
    body: str
    customer_id: str
    customer_name: str
    customer_tier: Literal['standard', 'gold', 'enterprise']
    order_id: str | None
    current_triage: dict | None

class OrderView(Strict):
    order_id: str
    customer_id: str
    status: Literal['processing', 'shipped', 'delivered', 'cancelled']
    total_usd: float = Field(ge=0)
    placed_on: str
    items: list[str]
    payment: str
    charges: int = Field(ge=0)
    carrier_eta: str | None = None

class KbArticle(Strict):
    article_id: str
    title: str
    text: str

class SearchKbResult(Strict):
    articles: list[KbArticle]

class TriageResult(Strict):
    ticket_id: str
    category: Category
    priority: Priority
    queue: Queue
    saved: Literal[True]

class DraftResult(Strict):
    ticket_id: str
    draft_id: str
    sent: Literal[False]
    note: str

class ApprovalResult(Strict):
    approval_id: str
    action: str
    state: Literal['pending_human_approval']

# ---------------------------------------------------------------- sandbox --
class Sandbox:
    """An isolated, per-run copy of the support desk."""
    def __init__(self, data: dict | None = None):
        self.data = copy.deepcopy(data or _SAMPLE)
        self.triage: dict[str, dict] = {}
        self.drafts: dict[str, dict] = {}
        self.approvals: list[dict] = []

    def ticket(self, ticket_id: str) -> dict:
        ticket = self.data['tickets'].get(ticket_id)
        if ticket is None:
            raise ToolRejected(f'ticket {ticket_id} does not exist')
        return ticket

def _words(text: str) -> set[str]:
    return set(re.findall(r'[a-z0-9]+', text.lower()))

def search_tickets(sb: Sandbox, a: SearchTicketsArgs) -> dict:
    matches = []
    email_owner = None
    if a.customer_email:
        email_owner = next((cid for cid, c in sb.data['customers'].items()
                            if c['email'].lower() == a.customer_email.lower()), None)
        if email_owner is None:
            raise ToolRejected(f'no customer with email {a.customer_email}')
    for tid, t in sb.data['tickets'].items():
        if a.status != 'any' and t['status'] != a.status:
            continue
        if email_owner and t['customer_id'] != email_owner:
            continue
        if a.query:
            haystack = _words(t['subject'] + ' ' + t['body'] + ' ' + (t['order_id'] or ''))
            wanted = _words(a.query)
            if not wanted or len(wanted & haystack) < max(1, (len(wanted) + 1) // 2):
                continue
        matches.append({'ticket_id': tid, 'subject': t['subject'], 'customer_id': t['customer_id'],
                        'status': t['status'], 'created_at': t['created_at']})
    return {'total_matches': len(matches), 'tickets': matches[:8]}

def get_ticket(sb: Sandbox, a: TicketArgs) -> dict:
    t = sb.ticket(a.ticket_id)
    c = sb.data['customers'][t['customer_id']]
    return {'ticket_id': a.ticket_id, 'status': t['status'], 'channel': t['channel'],
            'created_at': t['created_at'], 'subject': t['subject'], 'body': t['body'],
            'customer_id': t['customer_id'], 'customer_name': c['name'], 'customer_tier': c['tier'],
            'order_id': t['order_id'], 'current_triage': sb.triage.get(a.ticket_id)}

def get_order(sb: Sandbox, a: OrderArgs) -> dict:
    o = sb.data['orders'].get(a.order_id)
    if o is None:
        raise ToolRejected(f'order {a.order_id} does not exist')
    return {'order_id': a.order_id, **o}

def search_kb(sb: Sandbox, a: SearchKbArgs) -> dict:
    wanted = _words(a.query)
    scored = []
    for aid, art in sb.data['kb_articles'].items():
        words = _words(art['title'] + ' ' + ' '.join(art['tags']))
        score = len(wanted & words)
        if score:
            scored.append((score, aid, art))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return {'articles': [{'article_id': aid, 'title': art['title'], 'text': art['text']}
                         for _, aid, art in scored[:3]]}

def triage_ticket(sb: Sandbox, a: TriageArgs) -> dict:
    t = sb.ticket(a.ticket_id)
    if t['status'] != 'open':
        raise ToolRejected(f'ticket {a.ticket_id} is {t["status"]}; only open tickets can be triaged')
    if CATEGORY_TO_QUEUE[a.category] != a.queue:
        raise ToolRejected(f'queue {a.queue} does not handle category {a.category}')
    sb.triage[a.ticket_id] = {'category': a.category, 'priority': a.priority,
                              'queue': a.queue, 'reason': a.reason}
    return {'ticket_id': a.ticket_id, 'category': a.category, 'priority': a.priority,
            'queue': a.queue, 'saved': True}

def draft_reply(sb: Sandbox, a: DraftReplyArgs) -> dict:
    t = sb.ticket(a.ticket_id)
    if t['status'] != 'open':
        raise ToolRejected(f'ticket {a.ticket_id} is {t["status"]}; drafts are only for open tickets')
    draft_id = f'D-{a.ticket_id[2:]}'
    sb.drafts[a.ticket_id] = {'draft_id': draft_id, 'body': a.body}
    return {'ticket_id': a.ticket_id, 'draft_id': draft_id, 'sent': False,
            'note': 'Saved as a draft for human review. Nothing was sent.'}

def request_approval(sb: Sandbox, a: ApprovalArgs) -> dict:
    # Idempotent: a retried request for the same action/target reuses its id.
    key = (a.action, a.ticket_id, a.order_id)
    for existing in sb.approvals:
        if (existing['action'], existing['ticket_id'], existing['order_id']) == key:
            return {'approval_id': existing['approval_id'], 'action': a.action,
                    'state': 'pending_human_approval'}
    approval_id = f'APR-{len(sb.approvals) + 1:03d}'
    sb.approvals.append({'approval_id': approval_id, **a.model_dump()})
    return {'approval_id': approval_id, 'action': a.action, 'state': 'pending_human_approval'}

@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[Strict]
    output_model: type[Strict]
    fn: Callable[[Sandbox, Any], dict]
    needs_known_ticket: bool = False

TOOLS: dict[str, ToolSpec] = {s.name: s for s in [
    ToolSpec('search_tickets', 'Find tickets by keywords and/or customer email. args: query?, customer_email?, status (open|closed|any, default open).',
             SearchTicketsArgs, SearchTicketsResult, search_tickets),
    ToolSpec('get_ticket', 'Read one ticket with its customer tier and linked order id. args: ticket_id (T-####).',
             TicketArgs, TicketView, get_ticket),
    ToolSpec('get_order', 'Read one order: status, total, charges count, carrier ETA. args: order_id (A###).',
             OrderArgs, OrderView, get_order),
    ToolSpec('search_kb', 'Search support policy articles. args: query.',
             SearchKbArgs, SearchKbResult, search_kb),
    ToolSpec('triage_ticket', 'Save category, priority and queue on an OPEN ticket you already read with get_ticket. args: ticket_id, category, priority, queue, reason.',
             TriageArgs, TriageResult, triage_ticket, needs_known_ticket=True),
    ToolSpec('draft_reply', 'Save (never send) a reply draft for an OPEN ticket you already read with get_ticket. args: ticket_id, body.',
             DraftReplyArgs, DraftResult, draft_reply, needs_known_ticket=True),
]}
# Executed only by the system when the autonomy gate downgrades a restricted action.
APPROVAL_TOOL = ToolSpec('request_approval', 'System-only: file a pending approval request.',
                         ApprovalArgs, ApprovalResult, request_approval)

# Consequential actions the model may *propose* but the system never executes.
APPROVAL_ACTIONS = {
    'issue_refund': 'Refund or reverse a charge to a customer',
    'cancel_order': 'Cancel a customer order',
    'send_email': 'Send any message to a customer (drafting is allowed instead)',
    'close_ticket': 'Close or resolve a ticket',
    'apply_credit': 'Give a discount, voucher or account credit',
}
BLOCKED_ACTIONS = {
    'delete_account': 'Delete a customer account or personal data',
    'modify_account': "Change a customer's email, password, address or payment details",
    'bulk_message': 'Message many customers at once',
    'export_customer_data': 'Export or disclose customer personal data',
}
ALL_ACTIONS = set(TOOLS) | set(APPROVAL_ACTIONS) | set(BLOCKED_ACTIONS)

# ---------------------------------------------------------------- gateway --
TraceOutcome = Literal['success', 'timeout', 'malformed_output', 'rejected', 'exception']

@dataclass
class ToolOutcome:
    ok: bool
    result: dict | None = None
    error: str | None = None
    exhausted: bool = False  # retryable failures used up every attempt
    traces: list[ToolTrace] = field(default_factory=list)

class ToolGateway:
    """Single execution path for tools; owns timeouts, output checks, retries and faults."""
    def __init__(self, sandbox: Sandbox, fault: str, max_retries: int, timeout_s: float, deadline: float):
        self.sandbox = sandbox
        self.pending_fault = fault if fault in ('tool_timeout', 'malformed_tool_output') else None
        self.max_retries = max_retries
        self.timeout_s = timeout_s
        self.deadline = deadline

    def _take_fault(self) -> str | None:
        fault, self.pending_fault = self.pending_fault, None  # first matching operation, once
        return fault

    async def _invoke(self, spec: ToolSpec, args: Strict, fault: str | None) -> Any:
        if fault == 'tool_timeout':
            await asyncio.sleep(self.timeout_s + 30)  # dependency hangs; our timeout must catch it
        raw = spec.fn(self.sandbox, args)
        if fault == 'malformed_tool_output':
            return {'status': 'OK', 'payload': '<html><title>502 Bad Gateway</title></html>'}
        return raw

    async def execute(self, step: int, spec: ToolSpec, arguments: dict) -> ToolOutcome:
        outcome = ToolOutcome(ok=False)
        try:
            args = spec.args_model.model_validate(arguments)
        except ValidationError as exc:
            outcome.traces.append(ToolTrace(step=step, tool=spec.name, attempt=1, outcome='rejected'))
            outcome.error = 'invalid arguments: ' + '; '.join(e['msg'] for e in exc.errors()[:3])
            return outcome
        for attempt in range(1, self.max_retries + 2):
            remaining = self.deadline - perf_counter()
            if remaining <= 0.5:
                outcome.error = outcome.error or 'no time left for another attempt'
                break
            started = perf_counter()
            result: TraceOutcome
            try:
                raw = await asyncio.wait_for(self._invoke(spec, args, self._take_fault()),
                                             timeout=min(self.timeout_s, remaining - 0.25))
                outcome.result = spec.output_model.model_validate(raw).model_dump()
                outcome.ok, result = True, 'success'
            except ToolRejected as exc:
                outcome.error, result = str(exc), 'rejected'
            except TimeoutError:
                outcome.error, result = f'{spec.name} timed out after {self.timeout_s:g}s', 'timeout'
            except ValidationError:
                outcome.error, result = f'{spec.name} returned output that failed its schema', 'malformed_output'
            except Exception as exc:  # dependency crash: contained, never propagated
                outcome.error, result = f'{spec.name} raised {type(exc).__name__}', 'exception'
            outcome.traces.append(ToolTrace(step=step, tool=spec.name, attempt=attempt, outcome=result,
                                            latency_ms=round((perf_counter() - started) * 1000, 1)))
            if result in ('success', 'rejected'):
                if result == 'success':
                    outcome.error = None
                return outcome
        outcome.exhausted = True
        return outcome
