import json
from pathlib import Path
from minio_client import build_public_url


NEWS_FILE = Path("news_data.json")


def update_audio_urls(news_file=NEWS_FILE, bucket="audio"):
    """Legacy helper to rebuild audio URLs in JSON."""
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
    print(f"Updated {count} audio URLs in {NEWS_FILE}")
