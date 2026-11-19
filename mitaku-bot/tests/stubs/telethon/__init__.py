from types import SimpleNamespace
from . import events, errors, sessions
class TelegramClient:
    last = None
    def __init__(self, session, api_id, api_hash):
        self.session, self.handlers, self.connected, self.sent, self.cache = session, [], False, [], {}
        TelegramClient.last = self
    async def start(self, bot_token=None):
        self.connected = True; self.session.value = "SESSION-STRING"; return self
    def is_connected(self): return self.connected
    def on(self, ev):
        def deco(fn): self.handlers.append((ev, fn)); return fn
        return deco
    async def get_me(self): return SimpleNamespace(username="mybot")
    async def get_input_entity(self, cid):
        if cid in self.cache: return self.cache[cid]
        raise ValueError("Could not find the input entity")
    async def send_file(self, peer, path, **kw):
        if getattr(self, "flood", 0):
            self.flood -= 1; raise errors.FloodWaitError(2)
        self.sent.append((peer, path, kw))
        if kw.get("progress_callback"): kw["progress_callback"](5, 10); kw["progress_callback"](10, 10)
        return SimpleNamespace(id=555)
    async def disconnect(self): self.connected = False
