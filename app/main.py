import logging
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from time import monotonic
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.api import router
from app.config import ROOT, settings
from app.memory import Memory

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')

class RateLimiter:
    """Sliding one-minute window per client address for the public chat UI."""
    def __init__(self, per_minute: int):
        self.per_minute, self.hits = per_minute, defaultdict(deque)
    def allow(self, key: str) -> bool:
        now, window = monotonic(), self.hits[key]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self.per_minute:
            return False
        window.append(now)
        return True

@asynccontextmanager
async def lifespan(app):
    app.state.memory = Memory()
    app.state.busy = set()
    app.state.chat_limiter = RateLimiter(settings.chat_requests_per_minute)
    yield
app = FastAPI(title='TicketPilot - Agent Arena', lifespan=lifespan)
app.include_router(router)
app.mount('/static', StaticFiles(directory=ROOT / 'app/static'), name='static')
