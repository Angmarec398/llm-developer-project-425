"""Cloud Function email-poller: IMAP -> агент (Responses API, с MCP-инструментами
ydb-tickets) -> SMTP, история переписки и телеметрия пишутся в YDB напрямую.

Запускается timer-триггером раз в минуту. Для каждого непрочитанного письма
берёт текст, отправляет в YandexGPT (с подключённым MCP-инструментом
ydb-tickets и историей переписки пользователя) и возвращает ответ отправителю.
Каждое письмо помечается \\Seen — даже при ошибке, чтобы poller не зациклился.
"""
import datetime
import email
import email.policy
import functools
import imaplib
import json
import os
import smtplib
import time
import urllib.error
import urllib.request
import uuid
from email.message import EmailMessage
from email.utils import parseaddr

import ydb

from pii_mask import mask_pii

# Сбрасываем буфер на каждой записи, иначе последние строки лога могут
# потеряться при завершении функции.
print = functools.partial(print, flush=True)  # noqa: A001

RESPONSES_URL = "https://rest-assistant.api.cloud.yandex.net/v1/responses"
METADATA_TOKEN_URL = (
    "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token"
)

IMAP_HOST = os.environ.get("IMAP_HOST", "imap.yandex.ru")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.yandex.ru")
IMAP_USER = os.environ.get("IMAP_USER", "")
SMTP_USER = os.environ.get("SMTP_USER", IMAP_USER)
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
HELPDESK_MAILBOX = os.environ.get("HELPDESK_MAILBOX", IMAP_USER)
FOLDER_ID = os.environ.get("YC_FOLDER_ID", "")
AGENT_ID = os.environ.get("AI_STUDIO_AGENT_ID", "")
MODEL = os.environ.get("MODEL", "yandexgpt/latest")

YDB_ENDPOINT = os.environ.get("YDB_ENDPOINT", "")
YDB_DATABASE = os.environ.get("YDB_DATABASE", "")
MCP_GATEWAY_URL = os.environ.get("MCP_GATEWAY_URL", "")
HISTORY_LIMIT = int(os.environ.get("HISTORY_LIMIT", "10"))

SYSTEM_PROMPT = (
    "Ты — Help Desk-агент службы поддержки. Отвечай сотруднику вежливо и кратко, "
    "на языке обращения. Если не знаешь ответа — честно скажи об этом.\n\n"
    "У тебя есть два инструмента:\n"
    "- create-ticket(user_id, category, text) — заводи тикет, если сотрудник явно "
    "просит создать заявку/тикет, либо если ты не можешь помочь сам и обращение "
    "нужно передать оператору. category — одно из: bug, docs, feature, access. "
    "Не заводи повторный тикет на один и тот же вопрос в рамках одного диалога.\n"
    "- list-my-tickets(user_id) — вызывай, если сотрудник спрашивает статус своих "
    "ранее созданных заявок или просит их список.\n"
    "Для обоих инструментов user_id — email отправителя, он указан ниже."
)

MAX_BODY_CHARS = 8000

_driver = None
_pool = None


def _iam_token(context) -> str:
    token = getattr(context, "token", None)
    if token and token.get("access_token"):
        return token["access_token"]
    req = urllib.request.Request(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.load(resp)["access_token"]


def _ydb_pool(context) -> ydb.SessionPool:
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


def _fetch_history(pool: ydb.SessionPool, user_id: str, limit: int) -> list[dict]:
    query = """
        DECLARE $user_id AS Utf8;
        DECLARE $limit AS Uint64;
        SELECT role, text FROM messages
        WHERE user_id = $user_id
        ORDER BY created_at DESC
        LIMIT $limit;
    """
    params = {"$user_id": user_id, "$limit": limit}

    def _exec(session):
        prepared = session.prepare(query)
        result = session.transaction(ydb.SerializableReadWrite()).execute(
            prepared, params, commit_tx=True
        )
        return result[0].rows

    rows = pool.retry_operation_sync(_exec)
    # от старых к новым — для multi-turn input агента.
    return [{"role": r.role, "text": r.text} for r in reversed(list(rows))]


def _insert_message(
    pool: ydb.SessionPool,
    *,
    user_id: str,
    role: str,
    text: str,
    ticket_id: str | None = None,
    model: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    latency_ms: int = 0,
) -> str:
    msg_id = str(uuid.uuid4())
    now = datetime.datetime.now(datetime.timezone.utc)
    query = """
        DECLARE $id AS Utf8;
        DECLARE $user_id AS Utf8;
        DECLARE $ticket_id AS Utf8?;
        DECLARE $role AS Utf8;
        DECLARE $text AS Utf8;
        DECLARE $model AS Utf8?;
        DECLARE $tokens_in AS Uint64;
        DECLARE $tokens_out AS Uint64;
        DECLARE $latency_ms AS Uint32;
        DECLARE $created_at AS Timestamp;
        INSERT INTO messages
            (id, user_id, ticket_id, role, text, model, tokens_in, tokens_out, latency_ms, created_at)
        VALUES
            ($id, $user_id, $ticket_id, $role, $text, $model, $tokens_in, $tokens_out, $latency_ms, $created_at);
    """
    params = {
        "$id": msg_id,
        "$user_id": user_id,
        "$ticket_id": ticket_id,
        "$role": role,
        "$text": text,
        "$model": model,
        "$tokens_in": tokens_in,
        "$tokens_out": tokens_out,
        "$latency_ms": latency_ms,
        "$created_at": now,
    }

    def _exec(session):
        prepared = session.prepare(query)
        session.transaction(ydb.SerializableReadWrite()).execute(prepared, params, commit_tx=True)

    pool.retry_operation_sync(_exec)
    return msg_id


def _set_ticket_id(pool: ydb.SessionPool, user_id: str, msg_id: str, ticket_id: str) -> None:
    query = """
        DECLARE $user_id AS Utf8;
        DECLARE $id AS Utf8;
        DECLARE $ticket_id AS Utf8;
        UPDATE messages SET ticket_id = $ticket_id
        WHERE user_id = $user_id AND id = $id;
    """
    params = {"$user_id": user_id, "$id": msg_id, "$ticket_id": ticket_id}

    def _exec(session):
        prepared = session.prepare(query)
        session.transaction(ydb.SerializableReadWrite()).execute(prepared, params, commit_tx=True)

    pool.retry_operation_sync(_exec)


def _extract_text(msg: email.message.EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain",))
    if part is not None:
        return part.get_content().strip()
    # HTML-only письмо: грубый fallback — вытащить текст из html.
    part = msg.get_body(preferencelist=("html",))
    if part is not None:
        from html.parser import HTMLParser

        class _Text(HTMLParser):
            def __init__(self):
                super().__init__()
                self.chunks = []

            def handle_data(self, data):
                self.chunks.append(data)

        parser = _Text()
        parser.feed(part.get_content())
        return " ".join(" ".join(parser.chunks).split())
    return ""


def _ask_agent(text: str, history: list[dict], sender: str, token: str) -> dict:
    input_messages = []
    for item in history:
        role = "assistant" if item["role"] == "agent" else "user"
        input_messages.append({"role": role, "content": item["text"]})
    input_messages.append({"role": "user", "content": text})

    body = {
        "model": f"gpt://{FOLDER_ID}/{MODEL}",
        "instructions": f"{SYSTEM_PROMPT}\n\nEmail отправителя (user_id): {sender}",
        "input": input_messages,
        "tools": [
            {
                "type": "mcp",
                "server_label": "ydb-tickets",
                "server_url": MCP_GATEWAY_URL,
                "require_approval": "never",
            }
        ],
    }
    print(f"REQUEST_PAYLOAD={json.dumps(body, ensure_ascii=False)[:2000]}")

    req = urllib.request.Request(
        RESPONSES_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "x-folder-id": FOLDER_ID,
            "OpenAI-Project": FOLDER_ID,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:500]}") from exc


def _extract_output_text(data: dict) -> str:
    if data.get("output_text"):
        return data["output_text"].strip()
    chunks = []
    for item in data.get("output", []) or []:
        for content in item.get("content", []) or []:
            if content.get("text"):
                chunks.append(content["text"])
    return "\n".join(chunks).strip()


def _extract_ticket_id(data: dict) -> str | None:
    for item in data.get("output", []) or []:
        if item.get("type") != "mcp_call":
            continue
        print(f"mcp_call name={item.get('name')} args={item.get('arguments')}")
        if item.get("name") != "create-ticket":
            continue
        try:
            payload = json.loads(item.get("output") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        ticket_id = payload.get("ticket_id")
        if ticket_id:
            return ticket_id
    return None


def _send_reply(to_addr: str, subject: str, text: str, in_reply_to: str | None) -> None:
    reply = EmailMessage()
    reply["From"] = HELPDESK_MAILBOX
    reply["To"] = to_addr
    reply["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if in_reply_to:
        reply["In-Reply-To"] = in_reply_to
        reply["References"] = in_reply_to
    reply.set_content(text)
    with smtplib.SMTP_SSL(SMTP_HOST, 465) as smtp:
        smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(reply)


def _imap_mark_seen(imap: imaplib.IMAP4_SSL, num: bytes) -> None:
    try:
        imap.store(num, "+FLAGS", "\\Seen")
    except Exception as exc:  # noqa: BLE001
        print(f"MARK_SEEN_FAIL num={num.decode()} err={exc!r}")


def handle(event, context):
    processed = 0
    errors = 0
    token = _iam_token(context)
    pool = _ydb_pool(context)

    imap = imaplib.IMAP4_SSL(IMAP_HOST, 993)
    try:
        imap.login(IMAP_USER, IMAP_PASSWORD)
        imap.select("INBOX")
        _, data = imap.search(None, "UNSEEN")
        nums = data[0].split()
        print(f"GOT_UNSEEN={len(nums)}")

        for num in nums:
            try:
                # PEEK — не ставим \Seen до успешной обработки; ставим явно ниже.
                _, fetched = imap.fetch(num, "(BODY.PEEK[])")
                msg = email.message_from_bytes(fetched[0][1], policy=email.policy.default)
                sender = parseaddr(msg.get("From", ""))[1]
                subject = str(msg.get("Subject", "")) or "(без темы)"
                print(f"MSG num={num.decode()} from={sender} subject={subject}")

                # Не отвечаем самим себе и автоответам — иначе почтовая петля.
                auto = str(msg.get("Auto-Submitted", "no")).lower() != "no"
                if not sender or sender.lower() == HELPDESK_MAILBOX.lower() or auto:
                    print(f"SKIP num={num.decode()} reason=self_or_auto")
                    continue

                text = _extract_text(msg)[:MAX_BODY_CHARS]
                if not text:
                    print(f"SKIP num={num.decode()} reason=empty_body")
                    continue

                history = _fetch_history(pool, sender, HISTORY_LIMIT)
                user_msg_id = _insert_message(
                    pool, user_id=sender, role="user", text=mask_pii(text)
                )

                start = time.monotonic()
                response = _ask_agent(text, history, sender, token)
                latency_ms = int((time.monotonic() - start) * 1000)

                answer = _extract_output_text(response)
                print(f"AGENT_OK len={len(answer)}")
                if not answer:
                    raise RuntimeError("empty agent answer")

                usage = response.get("usage", {}) or {}
                ticket_id = _extract_ticket_id(response)

                _insert_message(
                    pool,
                    user_id=sender,
                    role="agent",
                    text=mask_pii(answer),
                    ticket_id=ticket_id,
                    model=MODEL,
                    tokens_in=usage.get("input_tokens", 0),
                    tokens_out=usage.get("output_tokens", 0),
                    latency_ms=latency_ms,
                )
                if ticket_id:
                    _set_ticket_id(pool, sender, user_msg_id, ticket_id)

                _send_reply(sender, subject, answer, msg.get("Message-ID"))
                print(f"SEND_OK to={sender}")
                processed += 1
            except Exception as exc:  # noqa: BLE001
                errors += 1
                print(f"MSG_FAIL num={num.decode()} err={exc!r}")
            finally:
                _imap_mark_seen(imap, num)
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass

    return {"processed": processed, "errors": errors}
