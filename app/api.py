import json
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import FileResponse
from app.config import ROOT, settings
from app.models import ArenaRequest, ArenaResponse, ChatRequest
from app.arena import execute
router = APIRouter()
@router.get('/')
def index(): return FileResponse(ROOT / 'app/static/index.html')
@router.get('/health')
def health():
    return {'status': 'ok', 'implementation': 'agent', 'agent': 'TicketPilot',
            'model_configured': bool(settings.models()), 'default_model': settings.model_name or None}
@router.get('/arena/manifest')
def manifest(): return json.loads((ROOT / 'arena_manifest.json').read_text())
@router.get('/models')
def models():
    # Only configured, allow-listed models; keys are never exposed.
    return {'models': settings.models() or ['unconfigured'], 'default': settings.model_name or None}
@router.post('/arena/run', response_model=ArenaResponse)
async def arena_run(payload: ArenaRequest):
    return await execute(payload, model=settings.model_name or 'unconfigured')
@router.post('/chat', response_model=ArenaResponse)
async def chat(payload: ChatRequest, request: Request):
    if payload.model not in models()['models']:
        raise HTTPException(400, 'Model is not enabled')
    if not request.app.state.chat_limiter.allow(request.client.host if request.client else 'unknown'):
        raise HTTPException(429, 'Too many chat requests; wait a minute and try again')
    busy = request.app.state.busy
    if payload.session_id in busy: raise HTTPException(409, 'This chat is already running')
    busy.add(payload.session_id)
    memory = request.app.state.memory
    try:
        result = await execute(payload, memory.get(payload.session_id), payload.model)
        start = next((e for e in result.events if e.get('event') == 'agent_start'), {})
        memory.add(payload.session_id, payload.task, result.final_response,
                   {'status': result.status, 'goal': start.get('goal', payload.task)})
        return result
    finally:
        busy.discard(payload.session_id)
@router.delete('/chat/{session_id}')
def reset(session_id: str, request: Request):
    if session_id in request.app.state.busy: raise HTTPException(409, 'Chat is running')
    request.app.state.memory.clear(session_id)
    return {'status': 'cleared'}
