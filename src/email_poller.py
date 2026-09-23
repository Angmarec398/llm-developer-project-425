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
import re
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
SEARCH_INDEX_ID = os.environ.get("SEARCH_INDEX_ID", "")
HISTORY_LIMIT = int(os.environ.get("HISTORY_LIMIT", "10"))

SYSTEM_PROMPT = (
    "Ты — Help Desk-агент службы поддержки. Отвечай сотруднику вежливо и кратко, "
    "на языке обращения.\n\n"
    "У тебя есть три инструмента:\n"
    "- file_search — база знаний компании (HR, IT, администрирование). ВСЕГДА "
    "обращайся к ней перед ответом на содержательный вопрос, даже если тебе "
    "кажется, что ты уже знаешь ответ. Используй ТОЛЬКО факты, которые "
    "буквально есть в найденном документе: цифры, сроки, проценты, номера "
    "законов, шаги решения проблемы, рекомендации и т.п. НЕ добавляй из "
    "своих общих знаний, даже если они кажутся тебе верными или логичными "
    "— если документ не описывает какой-то шаг или совет, не придумывай "
    "его сам. Не превращай короткое объяснение из документа в развёрнутую "
    "инструкцию из нескольких пунктов, если в документе её не было — "
    "лучше короткий ответ строго по документу, чем длинный, но частично "
    "выдуманный. Отвечай кратко своими словами, не цитируй больше 2-3 "
    "предложений; указывать имя файла-источника не нужно — это делает "
    "система автоматически по тексту твоего ответа.\n"
    "- create-ticket(user_id, category, text) — заводи тикет, если сотрудник явно "
    "просит создать заявку/тикет, либо если file_search не дал релевантного "
    "ответа на содержательный вопрос. Во втором случае вызови create-ticket "
    "СРАЗУ, без уточняющего вопроса «создать ли тикет» — сотрудник ждёт "
    "результата, а не диалога. После вызова инструмента честно скажи «не "
    "знаю» и сообщи, что создал тикет (укажи его номер), чтобы оператор "
    "разобрался; никогда не придумывай ответ вместо этого. category — одно "
    "из: bug, docs, feature, access. Не заводи повторный тикет на один и "
    "тот же вопрос в рамках одного диалога.\n"
    "- list-my-tickets(user_id) — вызывай, если сотрудник спрашивает статус своих "
    "ранее созданных заявок или просит их список.\n"
    "Для create-ticket и list-my-tickets user_id — email отправителя, он указан ниже."
)

MAX_BODY_CHARS = 8000

_WORD_RE = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)


def _load_kb_docs() -> dict[str, str]:
    # Локальная копия корпуса (упакована в zip рядом с кодом) — источник
    # правды для сопоставления ответа с документом. API-поля file_search
    # (results/annotations) оказались недостоверными: перечисляют все
    # файлы индекса без реальной привязки к конкретной цитате (см. CLAUDE.md,
    # раздел «Шаг 7»), поэтому определяем источник сами по тексту ответа.
    kb_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge_base")
    docs = {}
    if os.path.isdir(kb_dir):
        for name in sorted(os.listdir(kb_dir)):
            if name.endswith(".md"):
                with open(os.path.join(kb_dir, name), encoding="utf-8") as f:
                    docs[name] = f.read()
    return docs


_KB_DOCS = _load_kb_docs()


def _best_matching_doc(answer: str) -> str | None:
    answer_words = {w.lower() for w in _WORD_RE.findall(answer) if len(w) > 3}
    if not answer_words:
        return None
    best_name, best_score = None, 0
    for name, text in _KB_DOCS.items():
        doc_words = {w.lower() for w in _WORD_RE.findall(text) if len(w) > 3}
        score = len(answer_words & doc_words)
        if score > best_score:
            best_name, best_score = name, score
    return best_name if best_score >= 3 else None

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
                "type": "file_search",
                "vector_store_ids": [SEARCH_INDEX_ID],
            },
            {
                "type": "mcp",
                "server_label": "ydb-tickets",
                "server_url": MCP_GATEWAY_URL,
                "require_approval": "never",
            },
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


def _log_file_search_calls(data: dict) -> bool:
    called = False
    for item in data.get("output", []) or []:
        if item.get("type") != "file_search_call":
            continue
        called = True
        results = item.get("results") or []
        found = [r.get("filename") or r.get("file_id") for r in results]
        print(f"file_search_call queries={item.get('queries')} results={found}")
    return called


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
                used_file_search = _log_file_search_calls(response)
                if used_file_search:
                    source = _best_matching_doc(answer)
                    print(f"source_match={source}")
                    if source:
                        answer = f"{answer}\n\nИсточник: {source}"
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
