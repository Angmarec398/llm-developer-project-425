"""Базовое PII-маскирование текста перед записью в YDB.

Используется и в Cloud Function ydb-tickets (текст тикета), и в
email-poller (история переписки в messages) — единая логика, поэтому
вынесена в отдельный модуль, который упаковывается в оба deployment zip'а.
"""
import re

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"(?<!\w)(\+?\d[\d\-\s()]{8,}\d)(?!\w)")


def mask_pii(text: str) -> str:
    text = _EMAIL_RE.sub("[email]", text)
    text = _PHONE_RE.sub("[phone]", text)
    return text
