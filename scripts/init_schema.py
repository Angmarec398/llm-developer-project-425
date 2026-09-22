"""Инициализация схемы YDB из SQL-файла (см. src/ydb_tickets/schema.sql).

Использование:
    source .venv/bin/activate
    export YDB_ENDPOINT=grpcs://ydb.serverless.yandexcloud.net:2135
    export YDB_DATABASE=/ru-central1/<cloud-id>/<db-id>
    export YC_IAM_TOKEN=$(~/yandex-cloud/bin/yc iam create-token)
    python scripts/init_schema.py src/ydb_tickets/schema.sql

Комментарии (-- и всё до конца строки, включая инлайновые) вырезаются перед
разбиением на statement'ы — иначе ";" внутри текста комментария ломает
разбиение по ";", затем выполняет файл по одному statement'у через
session.execute_scheme().
"""
import os
import sys

import ydb


def _statements(sql_text: str) -> list[str]:
    lines = [line.split("--", 1)[0] for line in sql_text.splitlines()]
    cleaned = "\n".join(lines)
    return [stmt.strip() for stmt in cleaned.split(";") if stmt.strip()]


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python scripts/init_schema.py <path-to-schema.sql>")
        sys.exit(1)

    schema_path = sys.argv[1]
    endpoint = os.environ["YDB_ENDPOINT"]
    database = os.environ["YDB_DATABASE"]
    iam_token = os.environ["YC_IAM_TOKEN"]

    with open(schema_path, "r", encoding="utf-8") as f:
        sql_text = f.read()

    driver_config = ydb.DriverConfig(
        endpoint,
        database,
        credentials=ydb.AccessTokenCredentials(iam_token),
    )
    with ydb.Driver(driver_config) as driver:
        driver.wait(timeout=10, fail_fast=True)
        with ydb.SessionPool(driver) as pool:
            for statement in _statements(sql_text):
                print(f"EXEC: {statement.splitlines()[0][:60]}...")
                pool.retry_operation_sync(
                    lambda session, stmt=statement: session.execute_scheme(stmt)
                )
    print("DONE")


if __name__ == "__main__":
    main()
