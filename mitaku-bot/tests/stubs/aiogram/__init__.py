class _Any:
    def __getattr__(self, n): return self
    def __call__(self, *a, **k): return self
    def __invert__(self): return self
    def __and__(self, o): return self
    def __or__(self, o): return self
F = _Any()
class Bot:
    def __init__(self, *a, **k): self.session = None
    async def __call__(self, method, request_timeout=None): return None
class Dispatcher:
    def __init__(self, *a, **k): pass
REGISTERED = []
class _Reg:
    def __init__(self, kind=''): self.kind = kind
    def __call__(self, *a, **k):
        def deco(fn):
            REGISTERED.append((self.kind, fn.__name__)); return fn
        return deco
    def filter(self, *a, **k): pass
    def outer_middleware(self, *a, **k): pass
class Router:
    def __init__(self, *a, **k):
        self.message = _Reg('m'); self.callback_query = _Reg('c')
