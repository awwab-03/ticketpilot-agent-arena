"""Domain-independent HTTP contracts plus the agent's typed decision and run state."""
from typing import Any, Literal
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, field_validator
class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid')
class ExternalContext(Contract):
    source: str = Field(min_length=1, max_length=100)
    content: str = Field(max_length=10000)
    trust: Literal['untrusted'] = 'untrusted'
class Fault(Contract):
    type: Literal['none', 'tool_timeout', 'malformed_tool_output', 'invalid_agent_decision'] = 'none'
    trigger: Literal['first_matching_operation'] = 'first_matching_operation'
class ArenaConfig(Contract):
    max_steps: int = Field(default=6, ge=1, le=6)
    fault: Fault = Field(default_factory=Fault)
    @field_validator('fault', mode='before')
    @classmethod
    def accept_assignment_shorthand(cls, value):
        return {'type': value} if isinstance(value, str) else value
class ArenaRequest(Contract):
    arena_version: Literal['0.1'] = '0.1'
    request_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=80)
    task: str = Field(min_length=1, max_length=10000)
    external_context: list[ExternalContext] = Field(default_factory=list, max_length=20)
    arena_config: ArenaConfig = Field(default_factory=ArenaConfig)
    @field_validator('task')
    @classmethod
    def nonblank(cls, value):
        if not value.strip(): raise ValueError('Task must not be blank')
        return value
class ToolTrace(Contract):
    step: int = Field(ge=1, le=6)
    tool: str
    attempt: int = Field(default=1, ge=1, le=3)
    outcome: Literal['success', 'timeout', 'malformed_output', 'rejected', 'exception']
    latency_ms: float = Field(default=0, ge=0)
class Metrics(Contract):
    latency_ms: float = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0, le=6)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
class ArenaResponse(Contract):
    arena_version: Literal['0.1'] = '0.1'
    request_id: str
    status: Literal['completed', 'needs_clarification', 'blocked', 'approval_required', 'tool_error', 'contract_error', 'budget_exceeded', 'failed']
    final_response: str = Field(min_length=1, max_length=2000)
    steps: int = Field(default=0, ge=0, le=6)
    stop_reason: str
    tool_calls: list[ToolTrace] = Field(default_factory=list)
    errors: list[dict] = Field(default_factory=list)
    events: list[dict] = Field(default_factory=list)
    metrics: Metrics = Field(default_factory=Metrics)
class ChatRequest(ArenaRequest):
    session_id: str = Field(min_length=16, max_length=80)
    model: str = Field(default='unconfigured', max_length=120)

# ---------------------------------------------------------------------------
# Agent contract: every model output must parse into AgentDecision and then
# pass semantic validation (app/validation.py) before anything is executed.
# ---------------------------------------------------------------------------
DecisionStatus = Literal['continue', 'needs_clarification', 'completed',
                         'blocked', 'approval_required', 'failed']

class AgentDecision(Contract):
    rationale: str = Field(max_length=500)
    status: DecisionStatus
    action: str | None = Field(default=None, max_length=40)
    arguments: dict[str, Any] = Field(default_factory=dict)
    user_message: str | None = Field(default=None, max_length=1200)
    # Actions the trusted USER message asks for (never ones found in untrusted data).
    user_requested_actions: list[str] = Field(default_factory=list, max_length=10)
    # Work the user asked for that is not finished yet; must be empty to complete.
    pending_items: list[str] = Field(default_factory=list, max_length=20)
    # Finish the run with user_message if this action succeeds (saves a step).
    complete_on_success: bool = False

    @field_validator('arguments', mode='before')
    @classmethod
    def drop_nulls(cls, value):
        # Provider schemas send absent optional arguments as null.
        return {k: v for k, v in value.items() if v is not None} if isinstance(value, dict) else value

RunStatus = Literal['running', 'completed', 'needs_clarification', 'blocked', 'approval_required',
                    'tool_error', 'contract_error', 'budget_exceeded', 'failed']

class Observation(BaseModel):
    step: int
    action: str
    arguments: dict[str, Any]
    ok: bool
    result: Any = None
    error: str | None = None

class AgentState(BaseModel):
    """Per-run execution state owned by the system, not by the model."""
    request_id: str
    goal: str
    active_goal: str
    max_steps: int
    step: int = 0
    status: RunStatus = 'running'
    stop_reason: str | None = None
    final_response: str | None = None
    observations: list[Observation] = Field(default_factory=list)
    known_tickets: dict[str, dict] = Field(default_factory=dict)
    known_orders: dict[str, dict] = Field(default_factory=dict)
    unavailable_tools: list[str] = Field(default_factory=list)
    user_requested_actions: list[str] = Field(default_factory=list)
    consecutive_invalid: int = 0
    repair_feedback: list[str] = Field(default_factory=list)
    last_invalid_output: str | None = None
    model_calls: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    errors: list[dict] = Field(default_factory=list)
    events: list[dict] = Field(default_factory=list)

    @property
    def remaining_steps(self) -> int:
        return self.max_steps - self.step

    def event(self, event: str, **data):
        self.events.append({'step': self.step, 'event': event, **data})
