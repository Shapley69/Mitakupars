class RPCError(Exception): pass
class FloodWaitError(RPCError):
    def __init__(self, seconds): super().__init__(f"wait {seconds}"); self.seconds = seconds
