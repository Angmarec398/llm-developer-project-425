-- DROP'ы в начале — пере-инициализация схемы (данные теряются; для учебного проекта ок).
-- Если таблицы уже созданы и данные нужны — закомментируйте DROP-блок.
DROP TABLE IF EXISTS messages;
DROP TABLE IF EXISTS tickets;

CREATE TABLE tickets (
    id          Utf8,       -- UUID
    user_id     Utf8,       -- email отправителя
    category    Utf8,       -- bug | docs | feature | access
    status      Utf8,       -- open | answered | escalated | closed
    text        Utf8,       -- текст обращения (после PII-маскирования)
    created_at  Timestamp,
    updated_at  Timestamp,
    PRIMARY KEY (id),
    INDEX tickets_by_user GLOBAL ON (user_id)  -- вторичный индекс для «мои заявки»
);

-- История переписки ведётся ПОЛЬЗОВАТЕЛЕМ (PK по user_id), а не тикетом:
-- почтовый диалог начинается до создания тикета (вопрос → RAG-ответ → «создай тикет»).
CREATE TABLE messages (
    id          Utf8,       -- UUID
    user_id     Utf8,       -- email отправителя / TG chat id
    ticket_id   Utf8?,      -- ссылка на tickets.id; NULL, если реплика вне тикета
    role        Utf8,       -- user | agent
    text        Utf8,       -- текст сообщения (после PII-маскирования)
    model       Utf8?,      -- какая модель отвечала (если role=agent) — заполняет poller
    tokens_in   Uint64,     -- из usage ответа Responses API — пишет poller
    tokens_out  Uint64,
    latency_ms  Uint32,     -- замер poller'а
    created_at  Timestamp,
    PRIMARY KEY (user_id, id)
);
