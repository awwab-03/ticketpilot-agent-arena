"""Common boundary; ordinary Arena requests have no chat memory."""
import asyncio
import logging
from datetime import date
from time import perf_counter
from app.agent import run_agent
from app.config import settings
from app.models import ArenaResponse
log = logging.getLogger('arena')
# Admission control: bounded concurrency and a daily run cap keep provider spend bounded.
_slots = asyncio.Semaphore(settings.max_concurrent_runs)
_daily = {'day': date.today(), 'runs': 0}
def _admit() -> bool:
    if _daily['day'] != date.today():
        _daily.update(day=date.today(), runs=0)
    if _daily['runs'] >= settings.max_daily_runs:
        return False
    _daily['runs'] += 1
    return True
async def execute(request, history=None, model='unconfigured'):
    started = perf_counter()
    request = request.model_copy(deep=True)
    request.arena_config.max_steps = min(request.arena_config.max_steps, settings.max_steps)
    if not _admit():
        return ArenaResponse(request_id=request.request_id, status='failed', stop_reason='daily_run_cap_reached',
            final_response='The service reached its daily run cap; try again tomorrow.')
    try:
        async with asyncio.timeout(settings.run_timeout_seconds):
            async with _slots:
                result = await run_agent(request, history or [], model,
                                         deadline=started + settings.run_timeout_seconds)
            result = ArenaResponse.model_validate(result)
    except TimeoutError:
        result = ArenaResponse(request_id=request.request_id, status='budget_exceeded',
            final_response='The run timed out.', stop_reason='time_budget_reached')
    except Exception:
        log.exception('request=%s internal error', request.request_id)
        result = ArenaResponse(request_id=request.request_id, status='failed',
            final_response='The run stopped due to an internal error.', stop_reason='internal_error')
    result.metrics.latency_ms = (perf_counter() - started) * 1000
    if len(result.model_dump_json().encode()) > 50000:
        result = ArenaResponse(request_id=request.request_id, status='failed',
            final_response='Response exceeded the size limit.', stop_reason='response_too_large')
    log.info('request=%s status=%s stop=%s steps=%s', request.request_id, result.status, result.stop_reason, result.steps)
    return result
