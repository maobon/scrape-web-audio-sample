import json
from pathlib import Path
from typing import Optional

from news_spider.clients.minio import build_public_url
from news_spider.config import load_config


def update_audio_urls(news_file: Optional[Path] = None, bucket: Optional[str] = None):
    """Legacy helper to rebuild audio URLs in JSON."""
    config = load_config()
    news_file = news_file or Path(config["storage"]["default_output"])
    bucket = bucket or config["storage"]["audio_bucket"]
    if not news_file.exists():
        return 0

    with news_file.open("r", encoding="utf-8") as f:
        items = json.load(f)

    updated = 0
    for item in items:
        mp3_hash = item.get("mp3_hash")
        if mp3_hash:
            item["audio"] = build_public_url(bucket, f"{mp3_hash}.mp3")
            updated += 1

    with news_file.open("w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

    return updated


if __name__ == "__main__":
    count = update_audio_urls()
    config = load_config()
    print(f"Updated {count} audio URLs in {config['storage']['default_output']}")
