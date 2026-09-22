### Hexlet tests and linter status:
[![Actions Status](https://github.com/Angmarec398/llm-developer-project-425/actions/workflows/hexlet-check.yml/badge.svg)](https://github.com/Angmarec398/llm-developer-project-425/actions)

## AI-агент службы поддержки

Help Desk-агент: принимает обращения сотрудников по email, ищет ответ в
корпоративной базе знаний (RAG), а если готового ответа нет — создаёт тикет
и сохраняет переписку в YDB. Работает внутри контура Yandex Cloud
(YandexGPT + AI Studio, YDB Serverless, Cloud Functions, Yandex Workflows).

### Окружение (шаг 2)

- Каталог Yandex Cloud: `folder-id` — см. `yc config get folder-id`.
- База YDB Serverless: `help-desk-db` (serverless, режим `RUNNING`).
- Локальные переменные подключения к YDB — в `.env` (`YDB_ENDPOINT`,
  `YDB_DATABASE`); шаблон без значений — в `.env.example`.

### Права и секреты (шаг 3)

**Сервисный аккаунт `ai-studio-sa`** — от его имени работают Cloud Functions
проекта (email-poller и др.), получает IAM-токен через metadata service YC.
Роли на уровне каталога:

| Роль | Зачем |
|---|---|
| `functions.functionInvoker` | вызывать Cloud Functions |
| `serverless.mcpGateways.invoker` | вызывать MCP-инструменты через MCP Hub (шаг про MCP/YDB) |
| `lockbox.payloadViewer` | читать значения секретов из Lockbox |
| `ai.languageModels.user` | вызывать модели YandexGPT через Responses API |
| `ydb.editor` | `insert`/`select` в YDB от имени SA (инструмент `ydb-tickets`) |

**Секреты в Yandex Lockbox** (никаких секретов в коде и в `.env` — только их
ID, сами значения хранятся только в Lockbox):

| Секрет | Содержит | ID секрета/версии | Куда записан ID |
|---|---|---|---|
| `ydb-endpoint` | адрес подключения к YDB (`grpcs://...:2135`) | см. `YDB_ENDPOINT_SECRET_ID` / `YDB_ENDPOINT_SECRET_VERSION_ID` в `.env` | Cloud Function `ydb-tickets` |
| `ydb-database` | путь к базе YDB | см. `YDB_DATABASE_SECRET_ID` / `YDB_DATABASE_SECRET_VERSION_ID` в `.env` | Cloud Function `ydb-tickets` |
| `ai-studio-api-key` | персональный API-ключ AI Studio (scope `yc.ai.languageModels.execute`), опционально — только для вызова моделей напрямую по SDK снаружи Yandex Cloud | см. `AI_STUDIO_API_KEY_SECRET_ID` / `AI_STUDIO_API_KEY_SECRET_VERSION_ID` в `.env` | локальная отладка/QA |

Ключ payload внутри каждого секрета называется `PAYLOAD_KEY` (см.
`LOCKBOX_PAYLOAD_KEY` в `.env`) — это же имя используется при деплое Cloud
Function: `--secret environment-variable=secret-id/version-id/PAYLOAD_KEY`.

Пароль почтового ящика (IMAP/SMTP) хранится в отдельном секрете
`email-credentials` (ключ payload — `email_password`), см. шаг 4.

### Агент в Yandex AI Studio

- Агент `help-desk` создан в UI Agent Atelier, модель `yandexgpt`.
- `agent_id` записан в `.env` как `AI_STUDIO_AGENT_ID`.
- Сервисный аккаунт к агенту не привязывается — авторизация идёт через
  IAM-токен вызывающей Cloud Function (см. таблицу ролей выше).

### Email-workflow (шаг 4)

Почтовый агент: раз в минуту забирает непрочитанные письма, передаёт текст
в YandexGPT и отправляет ответ отправителю.

```
Timer (раз в минуту) → Cloud Function email-poller → IMAP fetch → YandexGPT (Responses API) → SMTP send
```

- **Почта:** ящик `helpdesk_hexlet@cif-raz.ru` на Reg.ru (ISPmanager);
  IMAP и SMTP — `mail.hosting.reg.ru` (порты 993 и 465), вход по логину и
  паролю ящика.
- **Секрет:** `email-credentials` в Lockbox, ключ payload — `email_password`
  (в функцию попадает как `IMAP_PASSWORD` и `SMTP_PASSWORD`).
- **Функция:** `email-poller` (код — [src/email_poller.py](src/email_poller.py),
  точка входа `email_poller.handle`).
- **Триггер:** `email-poller-trigger`, cron `0/1 * * * ? *`, вызывает
  `$latest`-версию функции.
- **Переменные окружения функции:** `YC_FOLDER_ID`, `IMAP_HOST`, `SMTP_HOST`,
  `IMAP_USER`, `SMTP_USER`, `HELPDESK_MAILBOX`, `OPERATOR_EMAIL`, а также
  секреты `IMAP_PASSWORD` и `SMTP_PASSWORD`.
- **Модель:** пока вызывается напрямую (`gpt://<folder>/yandexgpt/latest` +
  `instructions`), а не через агента: `prompt.id` с `agent_id` из Agent
  Atelier отвечает `404`. К агенту вернёмся на шагах с MCP-инструментами и RAG.
- **Защита от петель:** письма от самого ящика и автоответы
  (`Auto-Submitted`) пропускаются; каждое письмо помечается `\Seen`, даже
  если обработка упала.

Проверка по логам: `yc serverless function logs email-poller --limit 30`.
Успешная обработка письма выглядит так:

```
GOT_UNSEEN=1 → MSG num=… from=… subject=… → AGENT_OK len=… → SEND_OK to=…
```

### MCP-инструмент ydb-tickets и история переписки (шаг 5)

Агент получил собственный MCP-инструмент для работы с тикетами и базу
для истории диалога — обе таблицы в YDB Serverless `help-desk-db`.

```
email-poller → Responses API (+ MCP tool ydb-tickets) → YandexGPT
                       │                                      │
                       ▼ история/телеметрия (напрямую)         ▼ create-ticket / list-my-tickets
                    messages                                tickets
```

- **Схема** — [src/ydb_tickets/schema.sql](src/ydb_tickets/schema.sql):
  таблица `tickets` (тикеты, вторичный индекс `tickets_by_user`) и
  `messages` (история переписки, `PRIMARY KEY (user_id, id)`). Применяется
  через [scripts/init_schema.py](scripts/init_schema.py) (Python SDK) или
  вручную в консоли YDB (вкладка Query).
- **Cloud Function `ydb-tickets`** (код —
  [src/ydb_tickets/index.py](src/ydb_tickets/index.py), точка входа
  `index.handle`) отвечает на два action'а: `create-ticket` и
  `list-my-tickets`. Понимает три контракта вызова — прямой
  `yc serverless function invoke`, HTTP через API Gateway и вызов из MCP
  Hub (аргументы инструмента приходят как event напрямую).
- **MCP Hub gateway `ydb-tickets-mcp`** создан через
  `yc serverless mcp-gateway create --tools-file src/ydb_tickets/mcp-tools.yaml`
  ([src/ydb_tickets/mcp-tools.yaml](src/ydb_tickets/mcp-tools.yaml)), оба
  инструмента указывают на CF `ydb-tickets`. Подключается к запросу
  агента inline — тег `tools` в теле Responses API, с
  `require_approval: "never"` (иначе вместо `mcp_call` приходит
  `mcp_approval_request`, и тикет не создаётся).
- **PII-маскирование** — [src/pii_mask.py](src/pii_mask.py), общий модуль
  для CF `ydb-tickets` и `email-poller`: email и телефоны в тексте
  заменяются на `[email]`/`[phone]` перед записью в YDB.
- **История и телеметрия** — пишет `email-poller`, не агент: перед
  вызовом LLM читает последние реплики пользователя из `messages` и
  передаёт их в запрос (multi-turn), сохраняет входящее письмо; после
  ответа сохраняет ответ агента с `model`, `tokens_in/out` (из `usage`
  ответа Responses API) и `latency_ms`. Если агент вызвал `create-ticket`,
  `ticket_id` из результата инструмента проставляется обеим репликам
  цикла.
- **Переменные окружения `email-poller`** (добавлены к перечисленным
  выше): `YDB_ENDPOINT`, `YDB_DATABASE` (секреты `ydb-endpoint`/
  `ydb-database`), `MCP_GATEWAY_URL` (SSE-адрес шлюза).

Проверка: `yc serverless function invoke ydb-tickets --data '{"action":"list-my-tickets","user_id":"..."}'`
и SQL-запрос к `messages` в консоли YDB.
