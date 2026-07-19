#!/usr/bin/env python3
"""通用新闻列表爬虫.

默认从配置文件读取抓取目标，不再依赖 Playwright，实现极速抓取。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import logging
import os
import re
import subprocess
import sys
import time
import warnings
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import Request, urlopen

from pydantic import BaseModel

# 忽略 urllib3 的 OpenSSL/LibreSSL 兼容性警告
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL 1.1.1+")

from audio_download import download_mp3, hash_m3u8_url, parse_duration
from minio_client import (
    build_public_url,
    bucket_exists,
    list_object_names,
    upload_file,
)

# Configuration loading
def load_config(config_path: str = "config.json") -> dict:
    default_config = {
        "m3u8_stream_base": "https://vod-stream.nhk.jp",
        "crawler": {
            "base_url": "https://www3.nhk.or.jp",
            "lang": "zh",
            "list_url": "https://www3.nhk.or.jp/nhkworld/zh/news/list/",
            "list_selector": "a",
            "json_endpoints": [
                "https://www3.nhk.or.jp/nhkworld/data/zh/news/all.json"
            ],
            "user_agent": "Mozilla/5.0",
            "patterns": {
                "news_url_regex": ".*",
                "news_id_url_template": "{news_id}",
                "inline_links_regex": ".*"
            }
        },
        "storage": {
            "default_output": "news_data.json",
            "audio_bucket": "audio",
            "img_bucket": "img",
            "mp3_dir": "mp3",
            "pic_dir": "pic"
        },
        "spider_name": "NewsSpider"
    }
    
    path = Path(config_path)
    if not path.exists():
        return default_config
    
    with path.open("r", encoding="utf-8") as f:
        user_config = json.load(f)
    return user_config

CONFIG = load_config()
CRAWLER_CFG = CONFIG["crawler"]
STORAGE_CFG = CONFIG["storage"]

BASE_URL = CRAWLER_CFG["base_url"]
LANG = CRAWLER_CFG["lang"]
LIST_URL = CRAWLER_CFG["list_url"]
JSON_ENDPOINTS = CRAWLER_CFG["json_endpoints"]
USER_AGENT = CRAWLER_CFG["user_agent"]
SPIDER_NAME = CONFIG.get("spider_name", "NewsSpider")
M3U8_STREAM_BASE = CONFIG.get("m3u8_stream_base", "https://vod-stream.nhk.jp")

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [%(name)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    stream=sys.stderr
)
logger = logging.getLogger(SPIDER_NAME)


class NewsItem(BaseModel):
    title: str
    url: str
    id: int = 0
    published_at: str = ""
    summary: str = ""
    image: str = ""  # 原始图片 URL
    img: str = ""    # 云端图片 URL
    duration: str = ""
    m3u8_url: str = ""
    mp3_hash: str = ""
    audio: str = ""


class NewsListParser(HTMLParser):
    """从新闻列表页 HTML 中提取链接、标题、时间和图片。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[NewsItem] = []
        self._current_href = ""
        self._current_text: list[str] = []
        self._current_time = ""
        self._current_image = ""
        self._in_link = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attr = {key: value or "" for key, value in attrs}
        if tag == "a":
            href = attr.get("href", "")
            if _looks_like_news_url(href):
                self._in_link = True
                self._current_href = href
                self._current_text = []
                self._current_time = ""
                self._current_image = ""
        elif self._in_link and tag == "time":
            self._current_time = attr.get("datetime", "")
        elif self._in_link and tag == "img":
            self._current_image = attr.get("src") or attr.get("data-src") or ""

    def handle_data(self, data: str) -> None:
        if self._in_link:
            text = data.strip()
            if text:
                self._current_text.append(text)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._in_link:
            return

        title = _clean_text(" ".join(self._current_text))
        url = urljoin(BASE_URL, self._current_href)
        if title and not any(item.url == url for item in self.items):
            self.items.append(
                NewsItem(
                    title=title,
                    url=url,
                    published_at=self._current_time,
                    image=urljoin(BASE_URL, self._current_image)
                    if self._current_image
                    else "",
                )
            )

        self._in_link = False
        self._current_href = ""
        self._current_text = []
        self._current_time = ""
        self._current_image = ""


def fetch_text(url: str, timeout: int = 20, retries: int = 2) -> str:
    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            logger.info(f"请求页面: {url} (attempt {attempt + 1}/{retries + 1})")
            request = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except (HTTPError, URLError, TimeoutError) as exc:
            last_error = exc
            logger.info(f"请求失败: {url} ({exc})")
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))

    error_msg = f"抓取失败: {url}"
    if last_error:
        error_msg += f" ({last_error})"
    raise RuntimeError(error_msg)


def crawl_news(
        limit: int = 0,
        include_detail: bool = False,
        include_m3u8: bool = False,
        source: str = "auto",
) -> list[NewsItem]:
    logger.info(f"开始抓取新闻列表，source={source}, limit={limit or 'all'}")
    
    # 不再支持 browser 模式，强制使用 json/html
    if source == "json":
        items = fetch_from_json_endpoints()
    elif source == "html":
        items = fetch_from_list_page()
    else:
        # auto 模式优先使用 json，因为它包含最全的元数据（如音频路径）
        items = fetch_from_json_endpoints()
        if not items:
            items = fetch_from_list_page()

    logger.info(f"新闻列表解析完成: {len(items)} 条")
    
    # 应用 limit
    result_items = items[:limit] if limit > 0 else items
    if limit > 0:
        logger.info(f"应用 limit 后保留: {len(result_items)} 条")

    if include_detail:
        logger.info(f"开始补充详情: {len(result_items)} 条")
        for index, item in enumerate(result_items, start=1):
            logger.info(f"[{index}/{len(result_items)}] 补充详情: {item.title}")
            enrich_detail(item)
        logger.info("详情补充完成")

    if include_m3u8:
        # 在 auto/json 模式下，m3u8 可能已经在 extract_items_from_json 中解析出来了
        logger.info(f"开始检查/补充 m3u8: {len(result_items)} 条")
        for index, item in enumerate(result_items, start=1):
            if not item.m3u8_url:
                item.m3u8_url = find_m3u8_url(item.url)
        
        success_count = sum(1 for item in result_items if item.m3u8_url)
        logger.info(f"m3u8 补全完成: 成功 {success_count} 条")

    return result_items


def build_news_items(raw_items: list[dict[str, Any]]) -> list[NewsItem]:
    items: list[NewsItem] = []
    for raw_item in raw_items:
        title = _clean_text(str(raw_item.get("title", "")))
        url = str(raw_item.get("url", ""))
        if not title or not url:
            continue

        full_url = urljoin(BASE_URL, url)
        if any(item.url == full_url for item in items):
            continue

        image = str(raw_item.get("image", ""))
        items.append(
            NewsItem(
                title=title,
                url=full_url,
                published_at=_clean_text(str(raw_item.get("published_at", ""))),
                image=urljoin(BASE_URL, image) if image else "",
                duration=_clean_text(str(raw_item.get("duration", ""))),
            )
        )
    return items


def find_m3u8_url(url: str) -> str:
    """轻量化寻找 m3u8，直接请求详情页并用正则查找。"""
    try:
        content = fetch_text(url, timeout=10)
        # 匹配多种可能的 m3u8 格式
        patterns = (
            r"https?://[^'\"<>\s]+index\.m3u8[^'\"<>\s]*",
            r"//[^'\"<>\s]+index\.m3u8[^'\"<>\s]*",
        )
        for pattern in patterns:
            match = re.search(pattern, content)
            if match:
                m3u8 = match.group(0)
                if m3u8.startswith("//"):
                    m3u8 = "https:" + m3u8
                return unquote(m3u8)
    except Exception:
        pass
    return ""


def fetch_from_json_endpoints() -> list[NewsItem]:
    for endpoint in JSON_ENDPOINTS:
        try:
            payload = json.loads(fetch_text(endpoint))
        except (RuntimeError, json.JSONDecodeError):
            continue

        items = extract_items_from_json(payload)
        if items:
            return items
    return []


def extract_items_from_json(payload: Any) -> list[NewsItem]:
    # 支持 {"data": [...]} 格式
    candidates = payload.get("data", []) if isinstance(payload, dict) else _find_news_dicts(payload)
    items: list[NewsItem] = []
    
    id_template = CRAWLER_CFG["patterns"].get("news_id_url_template", "")

    for raw_item in candidates:
        title = _first_text(raw_item, ("title", "headline", "name"))
        url = _first_text(raw_item, ("page_url", "url", "link", "href"))
        
        if not url and id_template:
            news_id = _first_text(raw_item, ("id", "news_id", "newsId"))
            if news_id:
                url = id_template.format(news_id=news_id)

        if not title or not url:
            continue

        full_url = urljoin(BASE_URL, url)
        if not _looks_like_news_url(full_url):
            continue
        if any(item.url == full_url for item in items):
            continue

        # 处理音频和时长
        m3u8_url = ""
        duration_str = ""
        audios = raw_item.get("audios")
        if isinstance(audios, dict):
            audio_path = audios.get("path", "")
            if audio_path:
                # 规律推导: /.../ID.m4a -> https://vod-stream.nhk.jp/.../ID/index.m3u8
                base_path = audio_path.rsplit(".", 1)[0]
                m3u8_url = f"{M3U8_STREAM_BASE}{base_path}/index.m3u8"
            
            raw_duration = audios.get("duration")
            if raw_duration is not None:
                try:
                    total_secs = int(float(str(raw_duration)))
                    mins, secs = divmod(total_secs, 60)
                    duration_str = f"{mins} 分 {secs} 秒" if mins > 0 else f"{secs} 秒"
                except (ValueError, TypeError):
                    duration_str = str(raw_duration)

        # 处理图片
        image_url = ""
        thumbnails = raw_item.get("thumbnails")
        if isinstance(thumbnails, dict):
            image_url = thumbnails.get("large") or thumbnails.get("middle") or ""
        if not image_url:
            image_url = _first_text(raw_item, ("image", "image_url", "imageUrl", "thumbnail"))

        items.append(
            NewsItem(
                title=_clean_text(title),
                url=full_url,
                published_at=_first_text(
                    raw_item,
                    ("pubDate", "published_at", "publishedAt", "date", "datetime", "public_at"),
                ),
                summary=_clean_text(
                    _first_text(raw_item, ("description", "summary", "lead", "body"))
                ),
                image=urljoin(BASE_URL, image_url) if image_url else "",
                duration=duration_str,
                m3u8_url=m3u8_url,
            )
        )

    return items


def fetch_from_list_page() -> list[NewsItem]:
    page = fetch_text(LIST_URL)
    parser = NewsListParser()
    parser.feed(page)
    if parser.items:
        return parser.items

    return extract_items_from_inline_links(page)


def extract_items_from_inline_links(page: str) -> list[NewsItem]:
    pattern_str = CRAWLER_CFG["patterns"].get("inline_links_regex", "")
    if not pattern_str:
        return []
        
    pattern = re.compile(pattern_str, re.IGNORECASE | re.DOTALL)
    items: list[NewsItem] = []
    for match in pattern.finditer(page):
        body = match.group("body")
        href = match.group("href")
        if isinstance(body, (str, bytes)) and isinstance(href, (str, bytes)):
            body_str = body.decode() if isinstance(body, bytes) else body
            href_str = href.decode() if isinstance(href, bytes) else href
            title = _clean_text(re.sub(r"<[^>]+>", " ", body_str))
            url = urljoin(BASE_URL, html.unescape(href_str))
            if title and not any(item.url == url for item in items):
                items.append(NewsItem(title=title, url=url))
    return items


def enrich_detail(item: NewsItem) -> None:
    try:
        page = fetch_text(item.url)
    except RuntimeError:
        return

    if not item.summary:
        item.summary = _meta_content(page, "description") or _meta_content(
            page, "og:description"
        )
    if not item.image:
        image = _meta_content(page, "og:image")
        item.image = urljoin(BASE_URL, image) if image else ""
    if not item.published_at:
        item.published_at = _meta_content(page, "article:published_time")


def save_items(items: list[NewsItem], output: Path) -> None:
    logger.info(f"保存结果: {len(items)} 条 -> {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    for index, item in enumerate(items, start=1):
        item.id = index

    if output.suffix.lower() == ".csv":
        with output.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=list(items[0].model_dump().keys()))
            writer.writeheader()
            writer.writerows(item.model_dump() for item in items)
        return

    with output.open("w", encoding="utf-8") as file:
        json.dump([item.model_dump() for item in items], file, ensure_ascii=False, indent=2)
    logger.info(f"JSON 保存完成: {output}")


def clear_local_outputs(mp3_dir: Path, output: Path) -> None:
    mp3_dir.mkdir(parents=True, exist_ok=True)
    # 不再清空 mp3 文件夹，实现增量存储
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("", encoding="utf-8")
    logger.info(f"本地初始化完成: 保留原有资源文件，清空结果文件 {output}")


def download_audio_files(
        items: list[NewsItem],
        mp3_dir: Path,
) -> tuple[int, int, list[tuple[str, str, int]]]:
    mp3_dir.mkdir(parents=True, exist_ok=True)
    downloaded_count = 0
    skipped_count = 0
    failures: list[tuple[str, str, int]] = []

    audio_items = [item for item in items if item.m3u8_url]
    logger.info(f"开始处理音频下载: 可下载 {len(audio_items)} 条，目录={mp3_dir}")
    for index, item in enumerate(audio_items, start=1):
        item.mp3_hash = hash_m3u8_url(item.m3u8_url)
        output_file = mp3_dir / f"{item.mp3_hash}.mp3"

        if output_file.exists() and output_file.stat().st_size > 0:
            skipped_count += 1
            logger.info(f"[{index}/{len(audio_items)}] 音频已存在: {output_file}")
            continue

        logger.info(f"[{index}/{len(audio_items)}] 下载音频: {item.title}")
        try:
            total_sec = parse_duration(item.duration)
            download_mp3(item.m3u8_url, output_file, total_seconds=total_sec)
            downloaded_count += 1
        except subprocess.CalledProcessError as exc:
            failures.append((item.title, item.m3u8_url, exc.returncode))
            logger.info(
                f"[{index}/{len(audio_items)}] 下载失败: returncode={exc.returncode}, "
                f"url={item.m3u8_url}"
            )
            if output_file.exists() and output_file.stat().st_size == 0:
                output_file.unlink()

    logger.info(
        f"音频下载结束: 下载 {downloaded_count} 条，跳过 {skipped_count} 条，"
        f"失败 {len(failures)} 条"
    )
    return downloaded_count, skipped_count, failures


def download_pic_files(
        items: list[NewsItem],
        pic_dir: Path,
) -> tuple[int, int]:
    pic_dir.mkdir(parents=True, exist_ok=True)
    downloaded_count = 0
    skipped_count = 0

    pic_items = [item for item in items if item.image]
    logger.info(f"开始处理图片下载: {len(pic_items)} 条")

    for index, item in enumerate(pic_items, start=1):
        # 使用 URL hash 作为文件名
        ext = _get_image_ext(item.image)
        pic_hash = hashlib.sha256(item.image.encode("utf-8")).hexdigest()
        output_file = pic_dir / f"{pic_hash}{ext}"

        if output_file.exists() and output_file.stat().st_size > 0:
            skipped_count += 1
            continue

        try:
            request = Request(item.image, headers={"User-Agent": USER_AGENT})
            with urlopen(request, timeout=15) as response:
                output_file.write_bytes(response.read())
            downloaded_count += 1
        except Exception as exc:
            logger.info(f"图片下载失败: {item.image} ({exc})")

    logger.info(f"图片下载结束: 下载 {downloaded_count} 条，跳过 {skipped_count} 条")
    return downloaded_count, skipped_count


def _get_image_ext(url: str) -> str:
    path = urlparse(url).path
    suffix = Path(path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png"} else ".jpg"


def upload_resources_to_minio(
        items: list[NewsItem],
        mp3_dir: Path,
        pic_dir: Path,
        audio_bucket: str,
        img_bucket: str,
        object_prefix: str = "",
) -> tuple[int, int]:
    """同步音频和图片到 MinIO。"""
    try:
        # 获取云端列表
        existing_audio = set(list_object_names(audio_bucket))
        existing_img = set(list_object_names(img_bucket))

        uploaded_count = 0
        skipped_count = 0
        prefix = object_prefix.strip("/")

        # 1. 音频
        logger.info(f"同步音频到 MinIO: bucket={audio_bucket}")
        for item in [i for i in items if i.mp3_hash]:
            audio_file = mp3_dir / f"{item.mp3_hash}.mp3"
            obj_name = f"{prefix}/{audio_file.name}" if prefix else audio_file.name

            if obj_name in existing_audio:
                item.audio = build_public_url(audio_bucket, obj_name)
                skipped_count += 1
            elif audio_file.exists():
                item.audio = upload_file(audio_file, object_name=obj_name, bucket=audio_bucket)
                uploaded_count += 1

        # 2. 图片
        logger.info(f"同步图片到 MinIO: bucket={img_bucket}")
        for item in [i for i in items if i.image]:
            pic_hash = hashlib.sha256(item.image.encode("utf-8")).hexdigest()
            ext = _get_image_ext(item.image)
            pic_file = pic_dir / f"{pic_hash}{ext}"
            obj_name = f"{prefix}/{pic_file.name}" if prefix else pic_file.name

            if obj_name in existing_img:
                item.img = build_public_url(img_bucket, obj_name)
                skipped_count += 1
            elif pic_file.exists():
                item.img = upload_file(pic_file, object_name=obj_name, bucket=img_bucket)
                uploaded_count += 1

        return uploaded_count, skipped_count
    except Exception as exc:
        raise RuntimeError(f"MinIO 同步失败: {exc}") from exc


def test_minio_connection(bucket: str) -> bool:
    try:
        exists = bucket_exists(bucket=bucket)
        objs = list_object_names(bucket=bucket)
        logger.info(f"MinIO 连接成功: bucket={bucket}, exists={exists}, count={len(objs)}")
        return True
    except Exception as exc:
        logger.info(f"MinIO 连接失败: {exc}")
        return False


def _find_news_dicts(payload: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if any(key in value for key in ("title", "headline", "link", "url", "id")):
                found.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return found


def _first_text(data: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = _first_text(value, ("url", "src", "href", "text", "title"))
            if nested:
                return nested
    return ""


def _meta_content(page: str, name: str) -> str:
    escaped = re.escape(name)
    patterns = (
        rf'<meta[^>]+(?:name|property)=["\']{escaped}["\'][^>]+content=["\']([^"\']*)',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:name|property)=["\']{escaped}["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, page, re.IGNORECASE)
        if match:
            return _clean_text(match.group(1))
    return ""


def _looks_like_news_url(url: str) -> bool:
    pattern = CRAWLER_CFG["patterns"].get("news_url_regex", "")
    if not pattern:
        return True
    return bool(re.search(pattern, url))


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def str_to_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"抓取 {SPIDER_NAME} 新闻列表")
    parser.add_argument("-n", "--limit", type=int, default=0, help="最多抓取条数")
    parser.add_argument("-o", "--output", type=Path, 
                        default=Path(STORAGE_CFG.get("default_output", "news_data.json")),
                        help="输出 JSON")
    parser.add_argument("--detail", action="store_true", help="补充详情")
    parser.add_argument("--m3u8", action="store_true", help="抓取 m3u8")
    parser.add_argument("--download-audio", action="store_true", help="下载音频")
    parser.add_argument("--download-pic", action="store_true", help="下载图片")
    parser.add_argument("--mp3-dir", type=Path, 
                        default=Path(STORAGE_CFG.get("mp3_dir", "mp3")), help="音频目录")
    parser.add_argument("--pic-dir", type=Path, 
                        default=Path(STORAGE_CFG.get("pic_dir", "pic")), help="图片目录")
    parser.add_argument("--upload-minio", action="store_true", help="同步到 MinIO")
    parser.add_argument("--full-pipeline", action="store_true", help="执行完整流水线")
    parser.add_argument("--audio-bucket", 
                        default=STORAGE_CFG.get("audio_bucket", "audio"), help="音频 bucket")
    parser.add_argument("--img-bucket", 
                        default=STORAGE_CFG.get("img_bucket", "img"), help="图片 bucket")
    parser.add_argument("--minio-prefix", default=os.getenv("MINIO_PREFIX", ""), help="对象前缀")
    parser.add_argument("--source", choices=("html", "json", "auto"), default="auto")
    parser.add_argument("--test-minio", action="store_true", help="测试 MinIO")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # 如果没有指定任何特定操作，默认执行完整流水线
    if not any([args.detail, args.m3u8, args.download_audio, args.download_pic, args.upload_minio, args.test_minio]):
        args.full_pipeline = True

    if args.full_pipeline:
        args.detail = True
        args.m3u8 = True
        args.download_audio = True
        args.download_pic = True
        args.upload_minio = True

    logger.info(
        f"启动任务: source={args.source}, output={args.output}, "
        f"detail={args.detail}, m3u8={args.m3u8}, "
        f"download_audio={args.download_audio}, download_pic={args.download_pic}, "
        f"upload_minio={args.upload_minio}"
    )

    if args.test_minio:
        test_minio_connection(args.audio_bucket)
        test_minio_connection(args.img_bucket)
        return 0

    clear_local_outputs(args.mp3_dir, args.output)

    try:
        items = crawl_news(
            limit=args.limit,
            include_detail=args.detail,
            include_m3u8=args.m3u8,
            source=args.source,
        )
        if not items:
            return 2

        if args.download_audio:
            download_audio_files(items, args.mp3_dir)

        if args.download_pic:
            download_pic_files(items, args.pic_dir)

        if args.upload_minio:
            up, sk = upload_resources_to_minio(
                items, args.mp3_dir, args.pic_dir,
                args.audio_bucket, args.img_bucket,
                object_prefix=args.minio_prefix
            )
            logger.info(f"MinIO 同步完成: 上传 {up}，跳过 {sk}")

        save_items(items, args.output)
        logger.info("任务完成")
        return 0
    except Exception as exc:
        logger.error(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
