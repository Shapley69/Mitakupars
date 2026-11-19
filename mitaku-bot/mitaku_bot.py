#!/usr/bin/env python3
"""
mitaku_bot.py — точка входа Telegram-бота (aiogram 3.7+) для управления парсером косплей-сайтов.

Запуск:  python mitaku_bot.py
Настройка — переменные окружения / файл .env (BOT_TOKEN, ADMIN_IDS, MITAKU_OUT, CHANNEL_ID,
DATABASE_URL, MT_API_ID, MT_API_HASH, MT_STORAGE_CHAT, LOCAL_API_URL; см. README).

Логика лежит в пакете botapp/ (описание модулей — в README, раздел «Структура кода»).
Этот файл — запуск и обратная совместимость: `import mitaku_bot` по-прежнему даёт доступ ко всем
именам (mitaku_bot.publish_post, mitaku_bot.jm …), а присваивание `mitaku_bot.имя = значение`
меняет имя в том модуле пакета, где оно определено — так работают тесты и скрипты.
"""
import asyncio
import importlib
import sys
import types

from botapp import app, registry


def _home(name):
    module = registry.REGISTRY.get(name)
    return importlib.import_module(f"botapp.{module}") if module else None


def _loaded_modules():
    return [sys.modules[f"botapp.{name}"] for name in registry.MODULES
            if f"botapp.{name}" in sys.modules]


class _Facade(types.ModuleType):
    """Как будто весь код по-прежнему в одном модуле: чтение, присваивание и удаление имён
    направляются в модуль пакета, где имя определено (или импортировано)."""

    def __getattr__(self, name):
        home = _home(name)
        if home is not None:
            return getattr(home, name)
        for module in _loaded_modules():              # прочее: asyncio, mp, ms, fetch_archive …
            if name in vars(module):
                return vars(module)[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        home = _home(name)
        targets = [home] if home is not None else [m for m in _loaded_modules() if name in vars(m)]
        for module in targets:
            setattr(module, name, value)
        if not targets:
            super().__setattr__(name, value)

    def __delattr__(self, name):
        home = _home(name)
        targets = [home] if home is not None else [m for m in _loaded_modules() if name in vars(m)]
        for module in targets:
            delattr(module, name)
        if not targets:
            super().__delattr__(name)


sys.modules[__name__].__class__ = _Facade

if __name__ == "__main__":
    asyncio.run(app.main())
