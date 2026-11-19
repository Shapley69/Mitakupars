from dataclasses import dataclass, field
class _Base:
    def __init__(self, *a, **k):
        self.args, self.kw = a, k
        for key, v in k.items(): setattr(self, key, v)
class BotCommand(_Base): pass
class BufferedInputFile(_Base):
    def __init__(self, data, filename=None): self.data, self.filename = data, filename
class FSInputFile(_Base):
    def __init__(self, path, filename=None): self.path, self.filename = path, filename
class InlineKeyboardButton(_Base): pass
class InlineKeyboardMarkup(_Base): pass
class InputMediaDocument(_Base): pass
class InputMediaPhoto(_Base): pass
class KeyboardButton(_Base): pass
class LinkPreviewOptions(_Base): pass
class ReplyKeyboardMarkup(_Base): pass
class ReplyKeyboardRemove(_Base): pass
class ReplyParameters(_Base): pass
class Message(_Base): pass
class CallbackQuery(_Base): pass
