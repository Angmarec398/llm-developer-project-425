# CLAUDE.md

Контекст для продолжения работы над проектом. Это учебный сквозной проект
Hexlet «AI-агент службы поддержки» (курс LLM-инженер, проект 425). Задания
шагов лежат в `docs/*.pdf` — читать их перед выполнением следующего шага,
там точная формулировка задач и критерии сдачи («Результат шага»).

## О проекте

Help Desk-агент — почтовый агент службы поддержки. Принимает обращение по
email, ищет ответ в корпоративной базе знаний (RAG), если ответа нет —
создаёт тикет и сохраняет переписку в БД. Работает целиком внутри контура
Yandex Cloud, без внешних трекеров.

Архитектура:

```
Timer (раз в минуту) ──► email-poller ──► IMAP fetch / SMTP send
                              │
                              ▼ текст обращения
                    ┌─────────────────────────────┐
                    │ Агент в Yandex AI Studio     │
                    │ YandexGPT (Responses API)    │
                    │ + RAG (Search Index)         │
                    └──────────────┬──────────────┘
                                   │ MCP Hub
                                   ▼
                        MCP-инструмент ydb-tickets
                                   │
                                   ▼
                            YDB Serverless
                            ├─ tickets  (обращения)
                            └─ messages (история диалога — пишет poller)

Cron 09:00 ──► Workflow daily-escalation ──► дайджест оператору по почте
```

Стек: YandexGPT через Responses API, YDB Serverless, инструменты через MCP
Hub, деплой через Cloud Functions и Yandex Workflows, секреты — в Lockbox.
Язык решения: Python 3.12 (примеры в шагах — под Python, но контракт сдачи
от языка не зависит).

## Прогресс по шагам

- [x] Шаг 1 — знакомство с задачей (пройден на Хекслете).
- [x] Шаг 2 — подготовка окружения: репозиторий, `yc`, каталог, YDB Serverless.
- [x] Шаг 3 — права и секреты: сервисный аккаунт, роли, Lockbox, агент в
      Agent Atelier. Детали — в [README.md](README.md).
- [x] Шаг 4 — базовый email-workflow (`docs/Шаг 4.pdf`). Функция
      `email-poller` работает по таймеру, цепочка `GOT_UNSEEN → MSG →
      AGENT_OK → SEND_OK` подтверждена в логах, ответ доходит до
      отправителя. Сдан (коммит «Выполнено 4 шага из 10»).
- [~] Шаг 5 — MCP-инструмент `ydb-tickets` + YDB (`docs/Шаг 5.pdf`):
      **готов к сдаче**. Таблицы `tickets`/`messages` созданы, CF
      `ydb-tickets` отвечает корректным JSON на `create-ticket` и
      `list-my-tickets`, MCP-шлюз `ydb-tickets-mcp` создан через CLI,
      `email-poller` вызывает агента с подключённым MCP-инструментом и
      пишет историю/телеметрию в `messages`. 2026-09-22 end-to-end
      проверено по почте (создание тикета + multi-turn история +
      PII-маскирование в логах и в YDB) — см. раздел «Шаг 5» ниже.
      Осталось: закоммитить и сдать шаг.
- [ ] Шаг 6 и далее — RAG, авто-эскалация тикетов, безопасность (prompt
      injection), наблюдаемость (трейсы), финальное ревью.

Формулировки задач каждого шага — в `docs/Шаг N.pdf`. Общий контекст и
критерии сдачи проекта — в `docs/Общие данные.pdf`.

## Облачные ресурсы (Yandex Cloud)

Это идентификаторы ресурсов, не секреты — их можно смело использовать в
командах и документации. Сами значения секретов — только в Lockbox и в
локальном `.env` (не в git).

- `cloud-id`: `b1g46i6gi5dt7s5etbvj`
- `folder-id`: `b1gtltvclf9uth3u1r37`
- YDB Serverless: база `help-desk-db`, статус `RUNNING`
  - `YDB_ENDPOINT=grpcs://ydb.serverless.yandexcloud.net:2135`
  - `YDB_DATABASE=/ru-central1/b1g46i6gi5dt7s5etbvj/etnieql31k4tpuj4hc0f`
- Сервисный аккаунт `ai-studio-sa` (`SA_ID=ajeot9iv90eij6tloqo0`), роли на
  каталоге: `functions.functionInvoker`, `serverless.mcpGateways.invoker`,
  `lockbox.payloadViewer`, `ai.languageModels.user`, `ydb.editor`.
- Секреты в Lockbox (ключ payload везде называется `PAYLOAD_KEY`):
  | Секрет | secret_id | version_id |
  |---|---|---|
  | `ydb-endpoint` | `e6q31omfr2ss0jamt4qk` | `e6qcq2p1lki28fpbs626` |
  | `ydb-database` | `e6qbk0b28uapvo2ot5bt` | `e6qv6nqi5t87eak47nou` |
  | `ai-studio-api-key` | `e6q2t3hdojktbvrkp4ai` | `e6qivuk8cmb8ove6os3v` |
- Агент `help-desk` в AI Studio Agent Atelier: `agent_id=aactljdp4006i2nut1qo`,
  модель `yandexgpt`. SA к агенту не привязан — авторизация идёт через
  IAM-токен вызывающей Cloud Function (см. README «Как работает
  авторизация» / `docs/Шаг 3.pdf`).
- Прочие существующие SA в каталоге (не относятся к этому проекту, не
  трогать): `ai-studio-7c2d91`, `ai-studio-8557b7`, `hexlet-ai-agent`,
  `auto-generated-workflow-editor-493`.
- Cloud Function `ydb-tickets`: `function_id=d4eqej760k5k669iniqq`, код —
  `src/ydb_tickets/index.py`, точка входа `index.handle`.
- MCP Hub gateway `ydb-tickets-mcp`: `gateway_id=db8aoe7ahsu56rr7u571`,
  SSE-URL — `https://db8aoe7ahsu56rr7u571.zfnhylrb.mcpgw.serverless.yandexcloud.net/sse`
  (используется как `MCP_GATEWAY_URL` в `email-poller`).

Полное соответствие «секрет → зачем → куда положен» — в разделе
«Права и секреты» в [README.md](README.md).

## Шаг 4: email-workflow (текущее состояние)

- Почта — **не Яндекс**, а Reg.ru (ISPmanager): ящик
  `helpdesk_hexlet@cif-raz.ru`, IMAP и SMTP на одном хосте
  `mail.hosting.reg.ru` (993/465), вход по логину и обычному паролю ящика.
- Секрет Lockbox `email-credentials`: `secret_id=e6qtoe9qgsma9dp598h4`,
  `version_id=e6q0mrrnukuk4n6a9vfl`, ключ payload — `email_password`
  (не `password`, как в PDF; так решил пользователь).
- Cloud Function **`email-poller`** (`function_id=d4eplkj8g376i602ddpp`,
  код — `src/email_poller.py`, точка входа `email_poller.handle`). Раньше
  называлась `email-poller-v2`, потому что старая функция с этим именем
  (`d4e3222ra0sdp5ld4hqo`) зависла в `DELETING`; после её удаления
  переименована в `email-poller` (2026-09-21), как в задании.
- Timer-триггер `email-poller-trigger` (`a1s7d2d0ih0l3bnl2jur`), cron
  `0/1 * * * ? *`, вызывает `email-poller` с тегом `$latest` (привязан по ID,
  переименование его не затронуло).
- Переменные окружения функции: `YC_FOLDER_ID`, `IMAP_HOST`, `SMTP_HOST`,
  `IMAP_USER`, `SMTP_USER`, `HELPDESK_MAILBOX`, `OPERATOR_EMAIL` (последняя
  в коде `email_poller.py` пока не читается — нужна для дайджеста); секреты
  `IMAP_PASSWORD`/`SMTP_PASSWORD` из `email-credentials`. Команда деплоя —
  `yc serverless function version create` (см. `docs/Шаг 4.pdf`, задача 4).
- **Вызов агента через `prompt.id` не работает**: `agent_id` из Agent
  Atelier (`aactljdp...`) даёт `404 assistant with id ... not found`. Сейчас
  поллер обращается к модели напрямую (`model=gpt://<folder>/yandexgpt/latest`
  + `instructions`). Вернуться к агенту, когда понадобятся инструменты/RAG
  (в примере PDF `agent_id` выглядит как `fvtv...` — проверить, тот ли это ID).
- Для отладки ошибок API функция логирует тело HTTP-ответа; `print`
  переопределён с `flush=True` — без этого `SEND_OK` терялся в логах.
- Модель иногда «выдумывает» контекст (упоминала ЭДО Диадок без причины) —
  учесть в промпте/RAG на следующих шагах.
- `.env.example` содержит `MAIL`, `MAIL_PASSWORD`, `MAIL_SERVER` (правка
  пользователя) — они для локальной работы; в облаке функция читает
  `IMAP_*`/`SMTP_*`. Ключ пароля в Lockbox — `email_password` (в
  `.env.example` комментарий приведён в соответствие).
- В коде дефолты `IMAP_HOST`/`SMTP_HOST` — Яндекс, реальные значения
  (`mail.hosting.reg.ru`) приходят из окружения функции. `AI_STUDIO_AGENT_ID`
  читается, но пока не используется; модель — `MODEL` (по умолчанию
  `yandexgpt/latest`).

## Шаг 5: MCP ydb-tickets (текущее состояние)

- **Схема YDB** — `src/ydb_tickets/schema.sql` (таблицы `tickets`,
  `messages`, PK `messages` — `(user_id, id)`, `ticket_id` — Nullable).
  Применяется через `scripts/init_schema.py` (Python SDK, IAM-токен через
  `YC_IAM_TOKEN`) — второй способ из задачи 1, UI-консоль тоже подходит.
  Скрипт вырезает всё после `--` до конца строки перед разбиением на
  statement'ы по `;` (наивное разбиение ломалось на `;` внутри инлайн-
  комментария `-- ссылка на tickets.id; NULL, ...`).
- **Cloud Function `ydb-tickets`** (`src/ydb_tickets/index.py`) реализует
  `create-ticket` и `list-my-tickets`, диспетчеризация по трём контрактам
  вызова (`_resolve_action`): прямой invoke (`action` в event), HTTP через
  API Gateway (`httpMethod`+`body`), MCP Hub (аргументы инструмента как
  event напрямую, без `action` — определяется по набору ключей). PII
  текста тикета маскируется через `mask_pii` (см. ниже) перед записью.
  Особенность ydb-python-sdk: колонки `Timestamp` при чтении приходят как
  `int` (микросекунды от эпохи), а не `datetime` — конвертация в
  `_format_timestamp`.
- **PII-маскирование** — `src/pii_mask.py`, общий модуль для CF
  `ydb-tickets` и `email_poller.py` (упаковывается в оба zip). Простая
  regex-маскировка email и телефонов; текст сначала маскируется, потом
  пишется в YDB.
- **MCP Hub gateway `ydb-tickets-mcp`** создан через
  `yc serverless mcp-gateway create --tools-file src/ydb_tickets/mcp-tools.yaml`
  (см. ID выше). Оба инструмента в `mcp-tools.yaml` указывают на тот же
  `function_id` — диспетчеризация внутри CF.
- **`email-poller` доработан**: перед вызовом агента читает последние
  `HISTORY_LIMIT` (по умолчанию 10) реплик пользователя из `messages`
  (`ORDER BY created_at DESC`, разворачивает в хронологический порядок),
  пишет входящее письмо (`role=user`, PII замаскирован, `ticket_id=NULL`)
  до вызова LLM. Запрос к Responses API включает `tools: [{"type": "mcp",
  "server_url": MCP_GATEWAY_URL, "require_approval": "never"}]` и историю
  в `input` (role `agent`→`assistant`). После ответа: замеряет
  `latency_ms` (`time.monotonic()` вокруг вызова), достаёт `ticket_id` из
  `output[].type=="mcp_call"` с `name=="create-ticket"` (парсит JSON в
  поле `output` этого элемента), пишет ответ агента (`role=agent`,
  `model`, `tokens_in/out` из `usage`, `latency_ms`, тот же `ticket_id`) и
  UPDATE'ом проставляет `ticket_id` входящей реплике, если тикет заведён
  в этом же цикле. Системный промпт обновлён — описывает, когда вызывать
  `create-ticket`, когда `list-my-tickets`; email отправителя передаётся
  моделью как `user_id` через `instructions` (динамически, на каждый
  запрос).
  Деплой поллера теперь требует **все** переменные окружения разом
  (версия не наследует их от предыдущей): `YC_FOLDER_ID`, `IMAP_HOST`,
  `SMTP_HOST`, `IMAP_USER`, `SMTP_USER`, `HELPDESK_MAILBOX`,
  `OPERATOR_EMAIL`, `MCP_GATEWAY_URL` + секреты `IMAP_PASSWORD`/
  `SMTP_PASSWORD` (`email-credentials`) и новые `YDB_ENDPOINT`/
  `YDB_DATABASE` (секреты `ydb-endpoint`/`ydb-database`). SA — тот же
  `ai-studio-sa`, роль `ydb.editor` на запись в YDB уже была выдана на
  шаге 3 (отдельно выдавать не потребовалось). Таймаут увеличен с 30s до
  180s — запрос с MCP-инструментом занимает больше времени (в проверке —
  до ~9с на создание тикета, до ~24с когда агент сначала отвечает текстом
  и затем требуется второй tool-call для `list-my-tickets`).
- **Деплой на Windows**: в PDF команда сборки архива — `zip -j` (Unix); в
  PowerShell аналог — `Compress-Archive` по файлам, скопированным в общую
  временную папку (чтобы пути внутри zip были плоские, как у `zip -j`).
- **End-to-end проверка** (2026-09-22, `director@cif-raz.ru`): письмо
  «Создай тикет, категория bug, текст: не открывается отчёт в личном
  кабинете» → в логах `GOT_UNSEEN=1 → MSG → REQUEST_PAYLOAD → AGENT_OK →
  mcp_call name=create-ticket → SEND_OK`; в YDB тикет и обе реплики с
  `ticket_id`, `model=yandexgpt/latest`, `tokens_in=515`,
  `tokens_out=93`, `latency_ms=6293` (проверено SQL-запросом в консоли
  YDB). Второе письмо «А какой статус у моего тикета?» — в
  `REQUEST_PAYLOAD` видна история из двух предыдущих реплик (multi-turn),
  сработал `list-my-tickets`. Ранее отмеченная «выдумка про ЭДО Диадок» —
  оказалась реальным текстом из подписи отправителя, а не галлюцинацией
  модели.

## Локальное окружение

- `yc` CLI установлен, но **не в PATH** по умолчанию (путь зависит от
  машины; на одной из них — `C:\Users\ASUS\yandex-cloud\bin\yc.exe`, на
  другой `yc` уже в PATH). В новой PowerShell-сессии:
  `$env:Path += ";C:\Users\ASUS\yandex-cloud\bin"`.
- Профиль `yc` уже авторизован и указывает на каталог `b1gtltvclf9uth3u1r37`.
- `.env` — реальные значения, в `.gitignore`, **инструменту Claude в этой
  среде запрещено читать/редактировать `.env` настройками разрешений** —
  все правки в него просит сделать пользователь сам (давать ему готовые
  строки для вставки).
- Права Claude в `.claude/settings.json` (общие, в git) и
  `.claude/settings.local.json` (личные, в `.gitignore`); пути в них
  относительные. `.env` — запрещены Read/Edit/Write и чтение через
  `cat`/`type`/`more`/`Get-Content`; `.env.example` — разрешён. Также закрыты
  `*authorized_key.json` и `pg_tunnel_user`.
- `.env.example` — шаблон переменных без значений, поддерживается в
  актуальном состоянии, отражает все переменные из `.env`.
- `requirements.txt` (корень, для локального `.venv`) — в кодировке
  UTF-16 (с BOM); пакет `ydb` и его зависимости. Пакет
  `yandex-ai-studio-sdk` понадобится на шаге про RAG — ещё не добавлен.
- Деплойные `requirements.txt` (`src/requirements.txt`,
  `src/ydb_tickets/requirements.txt`, обычный UTF-8, только `ydb`) — не
  для `.venv`, а чтобы Cloud Functions runtime сам поставил зависимость
  при сборке образа; упаковываются в zip вместе с кодом функции.
- `src/email_poller.py`, `src/pii_mask.py` — код `email-poller`
  (`pii_mask.py` общий с `ydb_tickets`). `src/ydb_tickets/index.py` — код
  CF `ydb-tickets`. Компоновка деплойных zip — через `Compress-Archive`
  из временной папки (см. раздел «Шаг 5»).

## Последний проверенный коммит

`0fadf10` («Выполнено 4 шага из 10») — на этом коммите README.md и
CLAUDE.md сверены с состоянием проекта (2026-09-21). Код шага 5
(`src/ydb_tickets/*`, `src/pii_mask.py`, `scripts/init_schema.py`,
доработка `src/email_poller.py`) на 2026-09-22 проверен end-to-end, но
ещё не закоммичен. При следующей актуализации смотреть изменения
`git diff 0fadf10..HEAD`.

## Как работаем (важно)

Пользователь проходит учебный курс и просит **пошаговые инструкции**,
которые выполняет сам в своём терминале/консоли — не выполнять команды за
него молча. Формат: команда → объяснение зачем → пользователь выполняет →
присылает вывод → проверяем и даём следующий шаг. Редактирование файлов
самого репозитория (README, конфиги, `.env.example`, код) можно делать
напрямую — это не то же самое, что действия в облачном аккаунте.

**Одно действие за одно сообщение**: не выдавать несколько шагов подряд —
пользователь просил решать одну задачу за один шаг.

Секреты (значения ключей, паролей) никогда не публикуются в чате — только
ID ресурсов (secret_id, version_id, agent_id, folder_id и т.п.).
