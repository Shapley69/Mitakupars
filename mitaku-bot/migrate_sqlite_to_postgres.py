#!/usr/bin/env python3
"""Migrate the local library.db into the PostgreSQL database in DATABASE_URL."""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import psycopg
from dotenv import load_dotenv

from mitaku_parser import COLS, ensure_postgres_schema


def main():
    load_dotenv()
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        sys.exit("Задайте DATABASE_URL в окружении или файле .env")

    sqlite_path = Path(os.getenv("MITAKU_OUT", "mitaku_data")) / "library.db"
    if not sqlite_path.is_file():
        sys.exit(f"Локальная база не найдена: {sqlite_path}")

    with tempfile.TemporaryDirectory() as temporary_directory:
        snapshot_path = Path(temporary_directory) / "library.db"
        source = sqlite3.connect(sqlite_path)
        snapshot = sqlite3.connect(snapshot_path)
        try:
            source.backup(snapshot)
        finally:
            snapshot.close()
            source.close()

        source = sqlite3.connect(snapshot_path)
        source.row_factory = sqlite3.Row
        target = psycopg.connect(database_url)
        try:
            ensure_postgres_schema(target)
            source_columns = {
                row["name"] for row in source.execute("PRAGMA table_info(posts)")
            }
            post_columns = [column for column in COLS if column in source_columns]
            columns = ["id", *post_columns]
            placeholders = ", ".join(["%s"] * len(columns))
            updates = ", ".join(
                f"{column}=EXCLUDED.{column}" for column in post_columns if column != "slug")
            insert_post = (
                f"INSERT INTO posts ({', '.join(columns)}) VALUES ({placeholders}) "
                f"ON CONFLICT (slug) DO UPDATE SET {updates}"
            )
            insert_kv = (
                "INSERT INTO kv(key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value"
            )

            posts_migrated = 0
            kv_migrated = 0
            with target.transaction():
                has_posts = target.execute(
                    "SELECT EXISTS (SELECT 1 FROM posts LIMIT 1)").fetchone()[0]
                has_settings = target.execute(
                    "SELECT EXISTS (SELECT 1 FROM kv LIMIT 1)").fetchone()[0]
                if has_posts or has_settings:
                    raise RuntimeError(
                        "Таблицы posts или kv в Supabase не пусты; "
                        "остановил перенос, чтобы не смешать базы"
                    )

                cursor = target.cursor()
                rows = source.execute(
                    f"SELECT rowid AS id, {', '.join(post_columns)} FROM posts"
                )
                while batch := rows.fetchmany(500):
                    values = []
                    for row in batch:
                        values.append([
                            row["id"],
                            *(row[column] if column in source_columns else None
                              for column in post_columns),
                        ])
                    cursor.executemany(insert_post, values)
                    posts_migrated += len(values)

                if source.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kv'"
                ).fetchone():
                    kv_rows = source.execute("SELECT key, value FROM kv")
                    while batch := kv_rows.fetchmany(500):
                        cursor.executemany(insert_kv, [(row["key"], row["value"])
                                                       for row in batch])
                        kv_migrated += len(batch)

                target_posts = target.execute(
                    "SELECT COUNT(*) FROM posts").fetchone()[0]
                target_settings = target.execute(
                    "SELECT COUNT(*) FROM kv").fetchone()[0]
                if target_posts != posts_migrated or target_settings != kv_migrated:
                    raise RuntimeError(
                        "Проверка после переноса не прошла; транзакция будет отменена"
                    )

                target.execute(
                    "SELECT setval(pg_get_serial_sequence('posts', 'id'), "
                    "COALESCE(MAX(id), 1), COUNT(*) > 0) FROM posts")

            print(f"Перенос завершён и проверен: {posts_migrated} posts, {kv_migrated} kv.")
        finally:
            target.close()
            source.close()


if __name__ == "__main__":
    main()
