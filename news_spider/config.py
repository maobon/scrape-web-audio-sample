from __future__ import annotations

import json
from pathlib import Path
from typing import Any


REQUIRED_CONFIG_PATHS: tuple[tuple[str, ...], ...] = (
    ("m3u8_stream_base",),
    ("spider_name",),
    ("crawler", "base_url"),
    ("crawler", "list_url"),
    ("crawler", "json_endpoints"),
    ("crawler", "user_agent"),
    ("crawler", "patterns"),
    ("crawler", "patterns", "news_url_regex"),
    ("crawler", "patterns", "news_id_url_template"),
    ("crawler", "patterns", "inline_links_regex"),
    ("storage", "default_output"),
    ("storage", "audio_bucket"),
    ("storage", "img_bucket"),
    ("storage", "mp3_dir"),
    ("storage", "pic_dir"),
    ("storage", "minio_prefix"),
    ("database", "host"),
    ("database", "port"),
    ("database", "dbname"),
    ("database", "user"),
    ("database", "password"),
    ("database", "table"),
    ("minio", "endpoint"),
    ("minio", "secure"),
    ("minio", "access_key"),
    ("minio", "secret_key"),
    ("minio", "public_url"),
    ("server", "host"),
    ("server", "port"),
)


def _get_nested_value(config: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = config
    for key in path:
        if not isinstance(value, dict) or key not in value:
            raise KeyError(".".join(path))
        value = value[key]
    return value


def validate_config(config: dict[str, Any]) -> None:
    missing: list[str] = []
    empty: list[str] = []

    for path in REQUIRED_CONFIG_PATHS:
        try:
            value = _get_nested_value(config, path)
        except KeyError:
            missing.append(".".join(path))
            continue

        if value is None or value == "":
            if path not in {("minio", "public_url"), ("storage", "minio_prefix")}:
                empty.append(".".join(path))

    if missing or empty:
        messages = []
        if missing:
            messages.append(f"缺少配置项: {', '.join(missing)}")
        if empty:
            messages.append(f"配置项不能为空: {', '.join(empty)}")
        raise ValueError("; ".join(messages))


def load_config(config_path: str | Path = "config.json") -> dict[str, Any]:
    """Load project config from config.json."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")

    with path.open("r", encoding="utf-8") as file:
        config = json.load(file)

    if not isinstance(config, dict):
        raise ValueError(f"配置文件必须是 JSON object: {path}")

    validate_config(config)
    return config
