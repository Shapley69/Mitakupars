class TelegramBadRequest(Exception): pass
class TelegramRetryAfter(Exception):
    retry_after = 1
