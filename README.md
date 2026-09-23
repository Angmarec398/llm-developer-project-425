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

### Авто-эскалация тикетов: workflow daily-escalation (шаг 6)

Раз в сутки по расписанию собирает просроченные открытые тикеты, просит
агента составить дайджест, переводит их в `escalated` и отправляет письмо
оператору — без участия email-poller.

```
Cron 06:00 UTC → select_overdue (SELECT) → check_overdue (switch)
              → build_digest (aiStudioAgent) → mark_escalated (UPDATE)
              → send_digest (httpCall → email-sender) → done
```

- **Спецификация** — [src/workflow.yaml](src/workflow.yaml) (YaWL),
  workflow `daily-escalation`, сервисный аккаунт — `ai-studio-sa`.
- **Cloud Function `email-sender`** ([src/email_sender.py](src/email_sender.py),
  точка входа `email_sender.handle`) — тонкая обёртка над SMTP, шлёт
  письмо на `OPERATOR_EMAIL` из своего окружения (не из тела запроса).
  Открыта на неаутентифицированный вызов — шаг `httpCall` в YaWL не
  передаёт IAM-токен.
- **`build_digest`** — не агент из Agent Atelier (`agent_id`, как в
  email-poller), а отдельный ресурс **Responses API** из консоли AI
  Studio (`promptTemplateId`, вида `fvt...`); промпт пустой, все
  инструкции передаются через `message` самого шага workflow.
- **Расписание** — cron 6 полей (`секунды минуты часы день месяц
  день-недели`), `0 0 6 * * *` = 06:00 UTC (09:00 МСК) ежедневно; символ
  `?` не поддерживается (не Quartz), «каждый день» — просто `*`.
- **Права workflow** — отдельно от ролей на каталоге:
  `yc serverless workflow add-access-binding` выдаёт SA
  `serverless.workflows.executor`/`viewer`, без них не сработает
  scheduled-trigger.

Проверка: `yc serverless workflow execution list --workflow-name
daily-escalation` и SQL `SELECT status, count(*) FROM tickets GROUP BY
status` в консоли YDB (эскалированные тикеты больше не попадают в
выборку — `select_overdue`/`mark_escalated` фильтруют `WHERE status =
'open'`).

### Подключение RAG: file_search (шаг 7)

Перед ответом агент ищет релевантные документы в корпоративной базе
знаний через `file_search`; если ответа нет — честно говорит «не знаю» и
сразу заводит тикет через уже существующий MCP-инструмент `ydb-tickets`.

```
вопрос → email-poller → Responses API (+ file_search, + MCP ydb-tickets)
              │                              │
              ▼ найден документ              ▼ не найдено
      ответ с цитатой и ссылкой      «не знаю» + create-ticket
```

- **Корпус** — [knowledge_base/](knowledge_base) (9 markdown-документов,
  HR/IT/администрирование), загружен в один search index через CLI
  `yandex-ai-studio vector-stores local` (пакет `yandex-ai-studio-sdk`).
  Один индекс — не по темам, т.к. Yandex Responses API поддерживает
  только один `vector_store_id` и один `file_search` tool за раз.
- **Подключение** — в [src/email_poller.py](src/email_poller.py) в
  `tools` добавлен `{"type": "file_search", "vector_store_ids":
  [SEARCH_INDEX_ID]}` рядом с MCP-инструментом; `SEARCH_INDEX_ID` —
  новая переменная окружения функции.
- **Промпт** — явно требует всегда обращаться к `file_search` перед
  содержательным ответом и использовать только факты из найденного
  документа; если ничего не найдено — вызвать `create-ticket` сразу, без
  уточняющего вопроса, и лишь затем сообщить «не знаю» с номером тикета.
- **Источник ответа** определяется не моделью, а кодом: `knowledge_base/`
  упакован в тот же zip, что и код функции, и `email-poller` сам
  сопоставляет текст ответа с документами корпуса (пересечение слов) —
  поля `results`/`annotations`, которые возвращает `file_search` в
  Responses API, оказались недостоверны (перечисляют вообще все файлы
  индекса, а не то, что реально процитировано).
- **Трейс** — `output[].type=="file_search_call"` (поля `queries`,
  `results`) логируется так же, как `mcp_call`.

Проверка: вопрос из базы знаний → в логах `file_search_call`, в ответе —
строка `Источник: <файл>`. Вопрос вне базы → «не знаю» + новый тикет,
видно через `yc serverless function invoke ydb-tickets --data
'{"action":"list-my-tickets","user_id":"..."}'`.

**Известное ограничение**: модель `yandexgpt/latest` не всегда надёжно
следует инструкциям промпта — иногда пропускает вызов `file_search` для
вопроса, явно покрытого базой знаний, иногда подмешивает в ответ факты,
которых нет в найденном документе. Сам RAG-контур (индекс, инструмент,
определение источника) на чистых тестах работает корректно; ненадёжность
— в инструкционной дисциплине модели, а не в реализации. Подробности и
конкретные примеры — в CLAUDE.md, раздел «Шаг 7».
