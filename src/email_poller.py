"""Cloud Function email-poller: IMAP -> агент (Responses API) -> SMTP.

Запускается timer-триггером раз в минуту. Для каждого непрочитанного письма
берёт текст, отправляет в агента help-desk и возвращает ответ отправителю.
Каждое письмо помечается \\Seen — даже при ошибке, чтобы poller не зациклился.
"""
import email
import email.policy
import functools
import imaplib
import json
import os
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import parseaddr

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
SYSTEM_PROMPT = (
    "Ты — Help Desk-агент службы поддержки. Отвечай сотруднику вежливо и кратко, "
    "на языке обращения. Если не знаешь ответа — честно скажи об этом и предложи "
    "создать тикет."
)

MAX_BODY_CHARS = 8000


def _iam_token(context) -> str:
    token = getattr(context, "token", None)
    if token and token.get("access_token"):
        return token["access_token"]
    req = urllib.request.Request(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.load(resp)["access_token"]


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


def _ask_agent(text: str, token: str) -> str:
    # Шаг 4: без инструментов и RAG — обращаемся к модели напрямую. Вызов
    # через агента (prompt.id) вернёт позже, когда агент получит инструменты.
    body = {
        "model": f"gpt://{FOLDER_ID}/{MODEL}",
        "instructions": SYSTEM_PROMPT,
        "input": text,
    }
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
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:500]}") from exc
    if data.get("output_text"):
        return data["output_text"].strip()
    chunks = []
    for item in data.get("output", []):
        for content in item.get("content", []) or []:
            if content.get("text"):
                chunks.append(content["text"])
    return "\n".join(chunks).strip()


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

                answer = _ask_agent(text, token)
                print(f"AGENT_OK len={len(answer)}")
                if not answer:
                    raise RuntimeError("empty agent answer")

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
