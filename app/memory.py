"""Bounded, in-memory chat history as LangChain HumanMessage/AIMessage objects.

Each AIMessage carries the run's status and active goal in additional_kwargs,
so a later turn can tell that a clarification is pending and resume that goal.
In-memory only: lost on restart, so the service runs with one worker.
"""
from collections import OrderedDict
from langchain_core.messages import HumanMessage, AIMessage
MAX_MESSAGES = 12          # six recent turns
MAX_CHARS = 24000          # character ceiling (not a token counter)
MAX_SESSIONS = 100
class Memory:
    def __init__(self):
        self.sessions = OrderedDict()
    def get(self, session):
        return list(self.sessions.get(session, []))
    def add(self, session, user, assistant, meta=None):
        reply = AIMessage(content=assistant, additional_kwargs=dict(meta or {}))
        messages = self.get(session) + [HumanMessage(content=user), reply]
        while len(messages) > MAX_MESSAGES or sum(len(str(m.content)) for m in messages) > MAX_CHARS:
            messages = messages[2:]
        self.sessions[session] = messages
        self.sessions.move_to_end(session)
        while len(self.sessions) > MAX_SESSIONS:
            self.sessions.popitem(last=False)
    def clear(self, session):
        self.sessions.pop(session, None)
