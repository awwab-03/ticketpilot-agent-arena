"""Model boundary. The agent sees one interface: complete(messages) -> ModelReply.

GeminiClient calls the Gemini REST API (generateContent) with JSON output, a
response schema and a hard max-output-token cap. ScriptedClient replays fixed
outputs so the whole agent can be tested offline without cost.
"""
import asyncio
import json
from dataclasses import dataclass
from time import perf_counter
from typing import Callable, Protocol
import httpx
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from app.config import MODEL_PRICES, settings
from app.tools import ALL_ACTIONS

GEMINI_URL = 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'

@dataclass
class ModelReply:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float = 0.0
    finish_reason: str | None = None

class ModelUnavailable(Exception):
    """The provider could not produce a reply (auth, quota, outage, timeout)."""

class ModelClient(Protocol):
    name: str
    async def complete(self, messages: list[BaseMessage], timeout: float) -> ModelReply: ...

def estimate_cost(model: str, input_tokens: int | None, output_tokens: int | None) -> float | None:
    price = MODEL_PRICES.get(model)
    if price is None or input_tokens is None or output_tokens is None:
        return None
    return round((input_tokens * price[0] + output_tokens * price[1]) / 1_000_000, 8)

_nullable = {'type': ['string', 'null']}
DECISION_JSON_SCHEMA = {
    'type': 'object',
    'properties': {
        'rationale': {'type': 'string'},
        'status': {'type': 'string', 'enum': ['continue', 'needs_clarification', 'completed',
                                              'blocked', 'approval_required', 'failed']},
        'action': {'anyOf': [{'type': 'string', 'enum': sorted(ALL_ACTIONS)}, {'type': 'null'}]},
        'arguments': {'type': 'object', 'properties': {k: _nullable for k in (
            'ticket_id', 'order_id', 'query', 'customer_email', 'status', 'category',
            'priority', 'queue', 'reason', 'body')}},
        'user_message': _nullable,
        'user_requested_actions': {'type': 'array', 'items': {'type': 'string', 'enum': sorted(ALL_ACTIONS)}},
        'pending_items': {'type': 'array', 'items': {'type': 'string'}},
        'complete_on_success': {'type': 'boolean'},
    },
    'required': ['rationale', 'status', 'action', 'arguments', 'user_message',
                 'user_requested_actions', 'pending_items', 'complete_on_success'],
}

def _retry_delay(response: httpx.Response) -> float:
    """Seconds the provider asks us to wait (RetryInfo.retryDelay like "7s"), default 2."""
    try:
        for detail in response.json().get('error', {}).get('details', []):
            if 'retryDelay' in detail:
                return float(str(detail['retryDelay']).rstrip('s'))
    except (ValueError, AttributeError):
        pass
    return 2.0

class GeminiClient:
    # Optional request features dropped (per model, per process) if the API rejects them.
    _unsupported: dict[str, set[str]] = {}

    def __init__(self, name: str, api_key: str):
        self.name, self._key = name, api_key

    def _body(self, messages: list[BaseMessage]) -> dict:
        system = '\n\n'.join(str(m.content) for m in messages if isinstance(m, SystemMessage))
        contents = [{'role': 'model' if isinstance(m, AIMessage) else 'user', 'parts': [{'text': str(m.content)}]}
                    for m in messages if not isinstance(m, SystemMessage)]
        config = {'temperature': 0, 'maxOutputTokens': settings.max_output_tokens,
                  'responseMimeType': 'application/json'}
        dropped = self._unsupported.setdefault(self.name, set())
        if 'schema' not in dropped:
            config['responseJsonSchema'] = DECISION_JSON_SCHEMA
        if settings.gemini_thinking_level and 'thinking' not in dropped:
            config['thinkingConfig'] = {'thinkingLevel': settings.gemini_thinking_level}
        return {'systemInstruction': {'parts': [{'text': system}]}, 'contents': contents,
                'generationConfig': config}

    async def complete(self, messages: list[BaseMessage], timeout: float) -> ModelReply:
        deadline = perf_counter() + timeout
        attempts = 0
        async with httpx.AsyncClient() as http:
            while True:
                attempts += 1
                remaining = deadline - perf_counter()
                if remaining <= 0.5:
                    raise ModelUnavailable('model call exceeded its time budget')
                started = perf_counter()
                try:
                    r = await http.post(GEMINI_URL.format(model=self.name), json=self._body(messages),
                                        headers={'x-goog-api-key': self._key}, timeout=remaining)
                except httpx.TimeoutException:
                    raise ModelUnavailable('model call timed out')
                except httpx.HTTPError as exc:
                    raise ModelUnavailable(f'network error: {type(exc).__name__}')
                if r.status_code == 400 and attempts <= 3:
                    detail = r.text.lower()
                    dropped = self._unsupported.setdefault(self.name, set())
                    if 'thinking' in detail and 'thinking' not in dropped:
                        dropped.add('thinking'); continue
                    if 'schema' in detail and 'schema' not in dropped:
                        dropped.add('schema'); continue
                if r.status_code in (429, 500, 502, 503, 504) and attempts < 3:
                    wait = _retry_delay(r) if r.status_code == 429 else 1.0
                    if wait < deadline - perf_counter() - 2:  # only if a retry can still finish in time
                        await asyncio.sleep(wait)
                        continue
                if r.status_code != 200:
                    raise ModelUnavailable(f'provider returned HTTP {r.status_code}')
                data = r.json()
                break
        candidate = (data.get('candidates') or [{}])[0]
        parts = (candidate.get('content') or {}).get('parts') or []
        text = ''.join(p.get('text', '') for p in parts if not p.get('thought'))
        usage = data.get('usageMetadata') or {}
        out = usage.get('candidatesTokenCount')
        if out is not None:
            out += usage.get('thoughtsTokenCount') or 0
        return ModelReply(text=text, input_tokens=usage.get('promptTokenCount'), output_tokens=out,
                          latency_ms=(perf_counter() - started) * 1000,
                          finish_reason=candidate.get('finishReason'))

class ScriptedClient:
    """Deterministic stand-in for tests: each call returns the next scripted output."""
    def __init__(self, outputs: list, name: str = 'scripted'):
        self.name, self.outputs, self.calls = name, list(outputs), []

    async def complete(self, messages: list[BaseMessage], timeout: float) -> ModelReply:
        self.calls.append(messages)
        if not self.outputs:
            raise ModelUnavailable('script exhausted')
        item = self.outputs.pop(0)
        if callable(item):
            item = item(messages)
        if isinstance(item, Exception):
            raise item
        text = item if isinstance(item, str) else json.dumps(item)
        return ModelReply(text=text, input_tokens=100, output_tokens=40, latency_ms=1.0)

# Tests replace this to inject a ScriptedClient without touching the network.
client_override: Callable[[str], ModelClient] | None = None

def get_client(model: str) -> ModelClient | None:
    if client_override is not None:
        return client_override(model)
    if model in settings.models():
        return GeminiClient(model, settings.gemini_api_key)
    return None
