"""Cloud Function email-sender: тонкая обёртка над SMTP для шага httpCall
в workflow daily-escalation (см. src/workflow.yaml, шаг 6 проекта).

YaWL не умеет отправлять почту напрямую и не передаёт IAM-токен в httpCall,
поэтому функция открыта на публичный вызов без аутентификации (yc serverless
function allow-unauthenticated-invoke). Чтобы открытый URL нельзя было
использовать как рассылку на произвольный адрес, адресат (OPERATOR_EMAIL)
берётся из окружения функции, а не из тела запроса — тело задаёт только
subject/body дайджеста.
"""
import functools
import json
import os
import smtplib
from email.message import EmailMessage

# Сбрасываем буфер на каждой записи — иначе последние строки лога теряются
# при завершении функции (см. email_poller.py).
print = functools.partial(print, flush=True)  # noqa: A001

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
HELPDESK_MAILBOX = os.environ.get("HELPDESK_MAILBOX", SMTP_USER)
OPERATOR_EMAIL = os.environ.get("OPERATOR_EMAIL", "")


def _resolve_body(event) -> dict:
    """Прямой invoke отдаёт event как есть; httpCall из YaWL — обёрнутым
    в httpMethod/body (см. ydb_tickets/index.py._resolve_action)."""
    if not isinstance(event, dict):
        return {}
    if "httpMethod" in event:
        try:
            return json.loads(event.get("body") or "{}")
        except json.JSONDecodeError:
            return {}
    return event


def _send(subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = HELPDESK_MAILBOX
    msg["To"] = OPERATOR_EMAIL
    msg["Subject"] = subject or "Дайджест просроченных тикетов"
    msg.set_content(body)
    with smtplib.SMTP_SSL(SMTP_HOST, 465) as smtp:
        smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(msg)


def _wrap(payload: dict, status: int, is_http: bool):
    if is_http:
        return {"statusCode": status, "body": json.dumps(payload, ensure_ascii=False)}
    return payload


def handle(event, context):
    is_http = isinstance(event, dict) and "httpMethod" in event
    args = _resolve_body(event)
    subject = str(args.get("subject") or "")
    body = str(args.get("body") or "")
    print(f"SEND_REQUEST subject={subject!r} body_len={len(body)}")

    if not OPERATOR_EMAIL:
        print("SEND_FAIL reason=no_operator_email")
        return _wrap({"error": "OPERATOR_EMAIL is not configured"}, 500, is_http)
    if not body:
        print("SEND_FAIL reason=empty_body")
        return _wrap({"error": "body is required"}, 400, is_http)

    try:
        _send(subject, body)
    except Exception as exc:  # noqa: BLE001
        print(f"SEND_FAIL err={exc!r}")
        return _wrap({"error": str(exc)}, 500, is_http)

    print(f"SEND_OK to={OPERATOR_EMAIL}")
    return _wrap({"sent": True, "to": OPERATOR_EMAIL}, 200, is_http)
