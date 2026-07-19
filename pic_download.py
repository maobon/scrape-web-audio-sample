import hashlib
import json
import logging
from pathlib import Path
from urllib.parse import urlparse
import ssl

import requests
import certifi
from requests.adapters import HTTPAdapter

from minio_client import upload_file

logger = logging.getLogger("pic_download")


class SSLAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        context = ssl.create_default_context()
        try:
            context.set_ciphers('DEFAULT@SECLEVEL=1')
        except ssl.SSLError:
            pass
            
        try:
            context.minimum_version = ssl.TLSVersion.TLSv1_2
        except AttributeError:
            pass
        kwargs['ssl_context'] = context
        return super().init_poolmanager(*args, **kwargs)


# Global session for pic downloads
session = requests.Session()
session.mount("https://", SSLAdapter())
session.verify = certifi.where()


JSON_FILE = Path("news_data.json")
PIC_DIR = Path("pic")
IMG_BUCKET = "img"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def _get_image_ext(url: str) -> str:
    path = urlparse(url).path
    suffix = Path(path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png"} else ".jpg"


def download_pic_files(items: list, pic_dir: Path):
    pic_dir.mkdir(parents=True, exist_ok=True)
    downloaded = 0

    for item in [i for i in items if i.get("image")]:
        img_url = item["image"]
        pic_hash = hashlib.sha256(img_url.encode("utf-8")).hexdigest()
        ext = _get_image_ext(img_url)
        output_file = pic_dir / f"{pic_hash}{ext}"

        if output_file.exists():
            continue

        try:
            logger.info(f"Downloading: {img_url}")
            response = session.get(img_url, timeout=15, headers={"User-Agent": USER_AGENT})
            response.raise_for_status()
            output_file.write_bytes(response.content)
            downloaded += 1
        except Exception as e:
            logger.error(f"Failed to download {img_url}: {e}")

    return downloaded


def main():
    if not JSON_FILE.exists():
        logger.warning(f"{JSON_FILE} not found")
        return

    with JSON_FILE.open("r", encoding="utf-8") as f:
        items = json.load(f)

    downloaded = download_pic_files(items, PIC_DIR)
    logger.info(f"Downloaded {downloaded} new pictures")

    uploaded = 0
    for item in [i for i in items if i.get("image")]:
        img_url = item["image"]
        pic_hash = hashlib.sha256(img_url.encode("utf-8")).hexdigest()
        ext = _get_image_ext(img_url)
        pic_file = PIC_DIR / f"{pic_hash}{ext}"

        if pic_file.exists():
            item["img"] = upload_file(pic_file, bucket=IMG_BUCKET, verbose=True)
            uploaded += 1

    JSON_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Uploaded {uploaded} pictures to MinIO")


if __name__ == "__main__":
    main()
