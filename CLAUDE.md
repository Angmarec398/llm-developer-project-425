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
- [~] Шаг 4 — базовый email-workflow (`docs/Шаг 4.pdf`): **почти готов**.
      Функция `email-poller-v2` работает, письма обрабатываются и по таймеру,
      ответ от YandexGPT приходит. Осталось: (1) убедиться, что после
      последнего передеплоя (принудительный flush логов) в логах видна вся
      цепочка `GOT_UNSEEN → MSG → AGENT_OK → SEND_OK`; (2) обновить README;
      (3) сдать шаг. Детали и особенности — в разделе «Шаг 4» ниже.
- [ ] Шаг 5 и далее — MCP-инструмент `ydb-tickets` + YDB, RAG, авто-эскалация
      тикетов, безопасность (prompt injection, PII), наблюдаемость (трейсы,
      токены), финальное ревью.

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

Полное соответствие «секрет → зачем → куда положен» — в разделе
«Права и секреты» в [README.md](README.md).

## Шаг 4: email-workflow (текущее состояние)

- Почта — **не Яндекс**, а Reg.ru (ISPmanager): ящик
  `helpdesk_hexlet@cif-raz.ru`, IMAP и SMTP на одном хосте
  `mail.hosting.reg.ru` (993/465), вход по логину и обычному паролю ящика.
- Секрет Lockbox `email-credentials`: `secret_id=e6qtoe9qgsma9dp598h4`,
  `version_id=e6q0mrrnukuk4n6a9vfl`, ключ payload — `email_password`
  (не `password`, как в PDF; так решил пользователь).
- Cloud Function **`email-poller-v2`** (`function_id=d4eplkj8g376i602ddpp`,
  код — `src/email_poller.py`, точка входа `email_poller.handle`). Старая
  `email-poller` (`d4e3222ra0sdp5ld4hqo`) зависла в статусе `DELETING` —
  когда исчезнет, можно вернуть имя `email-poller` (задание называет её так).
- Timer-триггер `email-poller-trigger` (`a1s7d2d0ih0l3bnl2jur`), cron
  `0/1 * * * ? *`, вызывает `email-poller-v2` с тегом `$latest`.
- Переменные окружения функции: `YC_FOLDER_ID`, `IMAP_HOST`, `SMTP_HOST`,
  `IMAP_USER`, `SMTP_USER`, `HELPDESK_MAILBOX`, `OPERATOR_EMAIL`; секреты
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
  `IMAP_*`/`SMTP_*`.

## Локальное окружение

- `yc` CLI установлен, но **не в PATH** по умолчанию:
  `C:\Users\ASUS\yandex-cloud\bin\yc.exe`. В новой PowerShell-сессии:
  `$env:Path += ";C:\Users\ASUS\yandex-cloud\bin"`.
- Профиль `yc` уже авторизован и указывает на каталог `b1gtltvclf9uth3u1r37`.
- `.env` — реальные значения, в `.gitignore`, **инструменту Claude в этой
  среде запрещено читать/редактировать `.env` настройками разрешений** —
  все правки в него просит сделать пользователь сам (давать ему готовые
  строки для вставки).
- `.env.example` — шаблон переменных без значений, поддерживается в
  актуальном состоянии, отражает все переменные из `.env`.
- `requirements.txt` — есть пакет `ydb` (нужен для инициализации схемы на
  шаге про MCP/YDB). Пакет `yandex-ai-studio-sdk` понадобится на шаге про
  RAG — ещё не добавлен.

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
