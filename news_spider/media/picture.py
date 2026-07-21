import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import requests

from news_spider.clients.minio import upload_file
from news_spider.config import load_config

logger = logging.getLogger("pic_download")


def get_image_ext(url: str) -> str:
    path = urlparse(url).path
    suffix = Path(path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png"} else ".jpg"


def _get_item_value(item: Any, key: str) -> str:
    if isinstance(item, dict):
        return str(item.get(key, ""))
    return str(getattr(item, key, ""))


def _set_item_value(item: Any, key: str, value: str) -> None:
    if isinstance(item, dict):
        item[key] = value
    else:
        setattr(item, key, value)


def download_picture_files(
    items: list[Any],
    pic_dir: Path,
    user_agent: str,
    session: Optional[requests.Session] = None,
) -> tuple[int, int]:
    pic_dir.mkdir(parents=True, exist_ok=True)
    downloaded_count = 0
    skipped_count = 0
    http = session or requests.Session()

    for item in [i for i in items if _get_item_value(i, "image")]:
        img_url = _get_item_value(item, "image")
        pic_hash = hashlib.sha256(img_url.encode("utf-8")).hexdigest()
        ext = get_image_ext(img_url)
        output_file = pic_dir / f"{pic_hash}{ext}"

        if output_file.exists() and output_file.stat().st_size > 0:
            skipped_count += 1
            continue

        try:
            logger.info(f"Downloading: {img_url}")
            response = http.get(img_url, timeout=15, headers={"User-Agent": user_agent})
            response.raise_for_status()
            output_file.write_bytes(response.content)
            downloaded_count += 1
        except Exception as e:
            logger.error(f"Failed to download {img_url}: {e}")

    return downloaded_count, skipped_count


def main():
    config = load_config()
    json_file = Path(config["storage"]["default_output"])
    pic_dir = Path(config["storage"]["pic_dir"])
    img_bucket = config["storage"]["img_bucket"]
    user_agent = config["crawler"]["user_agent"]

    if not json_file.exists():
        logger.warning(f"{json_file} not found")
        return

    with json_file.open("r", encoding="utf-8") as f:
        items = json.load(f)

    downloaded, skipped = download_picture_files(items, pic_dir, user_agent)
    logger.info(f"Downloaded {downloaded} new pictures, skipped {skipped}")

    uploaded = 0
    for item in [i for i in items if i.get("image")]:
        img_url = item["image"]
        pic_hash = hashlib.sha256(img_url.encode("utf-8")).hexdigest()
        ext = get_image_ext(img_url)
        pic_file = pic_dir / f"{pic_hash}{ext}"

        if pic_file.exists():
            _set_item_value(item, "img", upload_file(pic_file, bucket=img_bucket, verbose=True))
            uploaded += 1

    json_file.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Uploaded {uploaded} pictures to MinIO")


if __name__ == "__main__":
    main()
