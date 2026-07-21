from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from news_spider.config import load_config
from news_spider.media.audio import parse_duration

logger = logging.getLogger("postgres_client")


def _postgres_dsn(config: dict[str, Any] | None = None) -> str:
    """Build a PostgreSQL DSN from config.json data."""
    config = config or load_config()
    database_config = config["database"]

    if database_config.get("dsn"):
        return str(database_config["dsn"])

    host = str(database_config["host"])
    port = str(database_config["port"])
    dbname = str(database_config["dbname"])
    user = str(database_config["user"])
    password = str(database_config["password"])

    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@"
        f"{host}:{port}/{quote(dbname, safe='')}"
    )


def _table_name(config: dict[str, Any] | None = None) -> str:
    config = config or load_config()
    table_name = str(config["database"]["table"])
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name):
        raise ValueError(f"非法 PostgreSQL 表名: {table_name}")
    return table_name


def get_news_audio_table_name(config: dict[str, Any] | None = None) -> str:
    return _table_name(config)


def _normalize_published_at(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def _item_to_row(item: Any) -> dict[str, Any]:
    data = item.model_dump() if hasattr(item, "model_dump") else dict(item)
    return {
        "title": data.get("title", ""),
        "url": data.get("url", ""),
        "published_at": _normalize_published_at(data.get("published_at")),
        "summary": data.get("summary", ""),
        "image": data.get("image", ""),
        "img": data.get("img", ""),
        "duration": data.get("duration", ""),
        "duration_seconds": parse_duration(str(data.get("duration", ""))),
        "m3u8_url": data.get("m3u8_url", ""),
        "mp3_hash": data.get("mp3_hash", ""),
        "audio": data.get("audio", ""),
        "raw_data": data,
    }


def save_news_audio_to_postgres(
    items: Iterable[Any],
    config: dict[str, Any] | None = None,
) -> int:
    """Replace all rows in news_audio with the latest news items."""
    try:
        import psycopg
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb
    except ImportError as exc:
        raise RuntimeError(
            "缺少 PostgreSQL 依赖，请先执行: pip install -r requirements.txt"
        ) from exc

    rows = [_item_to_row(item) for item in items]
    rows = [row for row in rows if row["url"]]
    for row in rows:
        row["raw_data"] = Jsonb(row["raw_data"])

    dsn = _postgres_dsn(config)
    table_name = _table_name(config)

    create_table_sql = f"""
    CREATE TABLE IF NOT EXISTS {table_name} (
        id BIGSERIAL PRIMARY KEY,
        title TEXT NOT NULL,
        url TEXT NOT NULL UNIQUE,
        published_at BIGINT,
        summary TEXT,
        image TEXT,
        img TEXT,
        duration TEXT,
        duration_seconds INTEGER,
        m3u8_url TEXT,
        mp3_hash TEXT,
        audio TEXT,
        raw_data JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """

    upsert_sql = f"""
    INSERT INTO {table_name} (
        title, url, published_at, summary, image, img,
        duration, duration_seconds, m3u8_url, mp3_hash, audio, raw_data
    )
    VALUES (
        %(title)s, %(url)s, %(published_at)s, %(summary)s,
        %(image)s, %(img)s, %(duration)s, %(duration_seconds)s,
        %(m3u8_url)s, %(mp3_hash)s, %(audio)s, %(raw_data)s
    );
    """

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cursor:
            cursor.execute(create_table_sql)
            cursor.execute(f"ALTER TABLE {table_name} DROP COLUMN IF EXISTS source_id;")
            cursor.execute(f"TRUNCATE TABLE {table_name} RESTART IDENTITY;")
            if rows:
                cursor.executemany(upsert_sql, rows)

    logger.info(f"PostgreSQL 替换完成: table={table_name}, rows={len(rows)}")
    return len(rows)


def save_news_audio_file_to_postgres(
    input_path: Path,
    config: dict[str, Any] | None = None,
) -> int:
    with input_path.open("r", encoding="utf-8") as file:
        items = json.load(file)

    if not isinstance(items, list):
        raise ValueError(f"新闻数据文件必须是 JSON array: {input_path}")

    return save_news_audio_to_postgres(items, config)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="导入新闻 JSON 到 PostgreSQL")
    parser.add_argument(
        "-i",
        "--input",
        type=Path,
        default=None,
        help="新闻 JSON 文件路径",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("config.json"),
        help="配置文件路径",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    args = parse_args()
    config = load_config(args.config)
    if args.input is None:
        args.input = Path(config["storage"]["default_output"])
    saved_rows = save_news_audio_file_to_postgres(args.input, config)
    logger.info(f"导入完成: {saved_rows} 条 -> {get_news_audio_table_name(config)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
