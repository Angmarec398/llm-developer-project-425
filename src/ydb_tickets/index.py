"""Cloud Function ydb-tickets: MCP-инструменты create-ticket и list-my-tickets.

Хранилище — таблицы tickets/messages в YDB Serverless (см. schema.sql).
Один и тот же action можно получить тремя разными способами (см.
_resolve_action): прямой yc serverless function invoke, HTTP через API
Gateway, вызов из MCP Hub (аргументы инструмента приходят без обёртки).
"""
import datetime
import functools
import json
import os
import urllib.request
import uuid

import ydb

from pii_mask import mask_pii

# Сбрасываем буфер на каждой записи — иначе последние строки лога теряются
# при завершении функции (см. email_poller.py).
print = functools.partial(print, flush=True)  # noqa: A001

METADATA_TOKEN_URL = (
    "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token"
)

YDB_ENDPOINT = os.environ.get("YDB_ENDPOINT", "")
YDB_DATABASE = os.environ.get("YDB_DATABASE", "")

_driver = None
_pool = None


def _iam_token(context) -> str:
    token = getattr(context, "token", None)
    if token and token.get("access_token"):
        return token["access_token"]
    req = urllib.request.Request(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.load(resp)["access_token"]


def _pool_for(context) -> ydb.SessionPool:
    # Переиспользуем драйвер/пул между "тёплыми" вызовами функции.
    global _driver, _pool
    if _pool is not None:
        return _pool
    config = ydb.DriverConfig(
        YDB_ENDPOINT,
        YDB_DATABASE,
        credentials=ydb.AccessTokenCredentials(_iam_token(context)),
    )
    _driver = ydb.Driver(config)
    _driver.wait(timeout=10, fail_fast=True)
    _pool = ydb.SessionPool(_driver)
    return _pool


def _resolve_action(event):
    """Определяет action и его аргументы по одному из трёх контрактов вызова."""
    if not isinstance(event, dict):
        return None, {}
    if "httpMethod" in event:
        try:
            body = json.loads(event.get("body") or "{}")
        except json.JSONDecodeError:
            body = {}
        return body.get("action"), body
    if "action" in event:
        return event["action"], event
    # MCP Hub: аргументы инструмента приходят напрямую, без обёртки и без
    # ключа action — диспетчеризуем по набору полей.
    if "category" in event and "text" in event:
        return "create-ticket", event
    if "user_id" in event:
        return "list-my-tickets", event
    return None, event


def _create_ticket(pool: ydb.SessionPool, args: dict) -> dict:
    ticket_id = str(uuid.uuid4())
    now = datetime.datetime.now(datetime.timezone.utc)
    query = """
        DECLARE $id AS Utf8;
        DECLARE $user_id AS Utf8;
        DECLARE $category AS Utf8;
        DECLARE $status AS Utf8;
        DECLARE $text AS Utf8;
        DECLARE $created_at AS Timestamp;
        DECLARE $updated_at AS Timestamp;
        INSERT INTO tickets (id, user_id, category, status, text, created_at, updated_at)
        VALUES ($id, $user_id, $category, $status, $text, $created_at, $updated_at);
    """
    params = {
        "$id": ticket_id,
        "$user_id": args["user_id"],
        "$category": args["category"],
        "$status": "open",
        "$text": mask_pii(args["text"]),
        "$created_at": now,
        "$updated_at": now,
    }

    def _exec(session):
        prepared = session.prepare(query)
        session.transaction(ydb.SerializableReadWrite()).execute(prepared, params, commit_tx=True)

    pool.retry_operation_sync(_exec)
    return {"ticket_id": ticket_id, "created_at": now.isoformat()}


def _format_timestamp(value) -> str:
    # ydb-python-sdk отдаёт Timestamp-колонки как int (микросекунды от эпохи),
    # а не как datetime — конвертируем сами.
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return datetime.datetime.fromtimestamp(value / 1_000_000, tz=datetime.timezone.utc).isoformat()


def _list_my_tickets(pool: ydb.SessionPool, args: dict) -> list:
    query = """
        DECLARE $user_id AS Utf8;
        SELECT id, status, category, text, created_at
        FROM tickets VIEW tickets_by_user
        WHERE user_id = $user_id
        ORDER BY created_at DESC;
    """
    params = {"$user_id": args["user_id"]}

    def _exec(session):
        prepared = session.prepare(query)
        result = session.transaction(ydb.SerializableReadWrite()).execute(
            prepared, params, commit_tx=True
        )
        return result[0].rows

    rows = pool.retry_operation_sync(_exec)
    return [
        {
            "id": row.id,
            "status": row.status,
            "category": row.category,
            "text": row.text,
            "created_at": _format_timestamp(row.created_at),
        }
        for row in rows
    ]


def _wrap(payload, status: int, is_http: bool):
    # MCP Hub и прямой invoke ожидают "голый" JSON-результат; HTTP-контракт
    # (API Gateway) требует обёртку statusCode/body.
    if is_http:
        return {"statusCode": status, "body": json.dumps(payload, ensure_ascii=False)}
    return payload


def handle(event, context):
    is_http = isinstance(event, dict) and "httpMethod" in event
    action, args = _resolve_action(event)
    print(f"ACTION={action} keys={sorted(args.keys()) if isinstance(args, dict) else args}")

    if action not in ("create-ticket", "list-my-tickets"):
        return _wrap({"error": f"unknown action: {action}"}, 400, is_http)

    try:
        pool = _pool_for(context)
        if action == "create-ticket":
            result = _create_ticket(pool, args)
        else:
            result = _list_my_tickets(pool, args)
    except Exception as exc:  # noqa: BLE001
        print(f"ACTION_FAIL action={action} err={exc!r}")
        return _wrap({"error": str(exc)}, 500, is_http)

    return _wrap(result, 200, is_http)
