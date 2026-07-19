#!/usr/bin/env python3
"""通用新闻列表爬虫.

默认从配置文件读取抓取目标，
自动向下滚动直到页面不再加载新内容，然后输出 JSON/CSV。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from urllib.request import Request, urlopen

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
        "crawler": {
            "base_url": "",
            "lang": "zh",
            "list_url": "",
            "list_selector": "a",
            "json_endpoints": [],
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
        "spider_name": "Spider"
    }
    
    path = Path(config_path)
    if not path.exists():
        return default_config
    
    with path.open("r", encoding="utf-8") as f:
        user_config = json.load(f)
        
    # Deep merge or simple update? Let's do a basic update for now.
    return user_config

CONFIG = load_config()
CRAWLER_CFG = CONFIG["crawler"]
STORAGE_CFG = CONFIG["storage"]

BASE_URL = CRAWLER_CFG["base_url"]
LANG = CRAWLER_CFG["lang"]
LIST_URL = CRAWLER_CFG["list_url"]
LIST_SELECTOR = CRAWLER_CFG["list_selector"]
JSON_ENDPOINTS = CRAWLER_CFG["json_endpoints"]
USER_AGENT = CRAWLER_CFG["user_agent"]
SPIDER_NAME = CONFIG.get("spider_name", "Spider")


def log_status(message: str) -> None:
    print(f"[{SPIDER_NAME}] {message}", file=sys.stderr, flush=True)


@dataclass
class NewsItem:
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

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
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
            log_status(f"请求页面: {url} (attempt {attempt + 1}/{retries + 1})")
            request = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except (HTTPError, URLError, TimeoutError) as exc:
            last_error = exc
            log_status(f"请求失败: {url} ({exc})")
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
        source: str = "browser",
        max_idle_scrolls: int = 8,
) -> list[NewsItem]:
    log_status(f"开始抓取新闻列表，source={source}, limit={limit or 'all'}")
    if source == "browser":
        items = fetch_from_browser(max_idle_scrolls=max_idle_scrolls)
    elif source == "html":
        items = fetch_from_list_page()
    elif source == "json":
        items = fetch_from_json_endpoints()
    else:
        items = fetch_from_browser(max_idle_scrolls=max_idle_scrolls)
        if not items:
            items = fetch_from_json_endpoints()
        if not items:
            items = fetch_from_list_page()

    log_status(f"新闻列表解析完成: {len(items)} 条")
    if include_detail:
        detail_items = items[:limit] if limit > 0 else items
        log_status(f"开始补充详情: {len(detail_items)} 条")
        for index, item in enumerate(detail_items, start=1):
            log_status(f"[{index}/{len(detail_items)}] 补充详情: {item.title}")
            enrich_detail(item)
        log_status("详情补充完成")

    result_items = items[:limit] if limit > 0 else items
    if limit > 0:
        log_status(f"应用 limit 后保留: {len(result_items)} 条")
    if include_m3u8:
        log_status(f"开始抓取 m3u8: {len(result_items)} 条")
        enrich_m3u8_urls_with_browser(result_items)
        success_count = sum(1 for item in result_items if item.m3u8_url)
        log_status(
            f"m3u8 抓取完成: 成功 {success_count} 条，缺失 {len(result_items) - success_count} 条")

    return result_items


def fetch_from_browser(max_idle_scrolls: int = 8) -> list[NewsItem]:
    """使用真实浏览器滚动页面，抓取截图中新闻列表区域的所有已加载条目。"""

    log_status("启动 Playwright 浏览器")
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "缺少 Playwright。请先运行: "
            "python3 -m pip install playwright && python3 -m playwright install chromium"
        ) from exc

    with sync_playwright() as playwright:
        browser = launch_browser(playwright, PlaywrightError)
        page = browser.new_page(
            viewport={"width": 1440, "height": 1200},
            user_agent=USER_AGENT,
            locale="zh-CN",
        )
        log_status(f"打开新闻列表页: {LIST_URL}")
        page.goto(LIST_URL, wait_until="domcontentloaded", timeout=60000)

        try:
            log_status(f"等待新闻列表区域: {LIST_SELECTOR}")
            page.wait_for_selector(LIST_SELECTOR, timeout=30000)
        except PlaywrightTimeoutError as exc:
            browser.close()
            raise RuntimeError(f"未找到新闻列表区域: {LIST_SELECTOR}") from exc

        scroll_until_loaded(page, max_idle_scrolls=max_idle_scrolls)
        log_status("开始从页面 DOM 提取新闻条目")
        raw_items = page.eval_on_selector_all(LIST_SELECTOR, BROWSER_EXTRACT_SCRIPT)
        items = build_news_items(raw_items)
        log_status(f"页面 DOM 提取完成: raw={len(raw_items)}, parsed={len(items)}")
        browser.close()
        log_status("Playwright 浏览器已关闭")

    return items


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


def enrich_m3u8_urls(context: Any, items: list[NewsItem]) -> None:
    for index, item in enumerate(items, start=1):
        log_status(f"[{index}/{len(items)}] 抓取 m3u8: {item.title}")
        item.m3u8_url = find_m3u8_url(context, item.url)
        if item.m3u8_url:
            log_status(f"[{index}/{len(items)}] m3u8 成功: {item.m3u8_url}")
        else:
            log_status(f"[{index}/{len(items)}] m3u8 未找到: {item.url}")


def enrich_m3u8_urls_with_browser(items: list[NewsItem]) -> None:
    log_status("启动浏览器上下文用于 m3u8 抓取")
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "缺少 Playwright。请先运行: "
            "python3 -m pip install playwright && python3 -m playwright install chromium"
        ) from exc

    with sync_playwright() as playwright:
        browser = launch_browser(playwright, PlaywrightError)
        context = browser.new_context(
            viewport={"width": 1440, "height": 1200},
            user_agent=USER_AGENT,
            locale="zh-CN",
        )
        enrich_m3u8_urls(context, items)
        context.close()
        browser.close()
        log_status("m3u8 浏览器上下文已关闭")


def find_m3u8_url(context: Any, url: str) -> str:
    page = context.new_page()
    found_urls: list[str] = []

    def handle_response(response: Any) -> None:
        m3u8_url = normalize_m3u8_url(response.url)
        if m3u8_url and m3u8_url not in found_urls:
            found_urls.append(m3u8_url)

    page.on("response", handle_response)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)

        direct_url = find_m3u8_in_page(page)
        if direct_url:
            return direct_url

        click_audio_buttons(page)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not found_urls:
            page.wait_for_timeout(500)
    finally:
        try:
            page.remove_listener("response", handle_response)
        finally:
            page.close()

    return found_urls[0] if found_urls else ""


def find_m3u8_in_page(page: Any) -> str:
    patterns = (
        r"https?://[^'\"<>\s]+index\.m3u8[^'\"<>\s]*",
        r"//[^'\"<>\s]+index\.m3u8[^'\"<>\s]*",
    )
    content = page.content()
    for pattern in patterns:
        match = re.search(pattern, content)
        if match:
            value = normalize_m3u8_url(match.group(0))
            if value:
                return value
    return ""


def normalize_m3u8_url(url: str) -> str:
    if not url:
        return ""

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("data_hlsurl", "hlsurl", "url"):
        for value in query.get(key, []):
            decoded = unquote(value)
            if "index.m3u8" in decoded:
                return decoded

    decoded_url = unquote(url)
    match = re.search(r"https?://[^'\"<>\s&]+index\.m3u8[^'\"<>\s&]*", decoded_url)
    if match:
        return match.group(0)
    if decoded_url.startswith("//") and "index.m3u8" in decoded_url:
        return "https:" + decoded_url
    return ""


def click_audio_buttons(page: Any) -> None:
    selectors = (
        "button[aria-label*='播放']",
        "button[aria-label*='Play']",
        ".p-audio button",
        ".c-audio button",
        "audio",
    )
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() == 0:
                continue
            locator.click(timeout=2000, force=True)
            return
        except Exception:
            continue


def launch_browser(playwright: Any, playwright_error: type[Exception]) -> Any:
    try:
        return playwright.chromium.launch(headless=True)
    except playwright_error:
        chrome_path = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        if chrome_path.exists():
            return playwright.chromium.launch(
                headless=True,
                executable_path=str(chrome_path),
            )
        raise RuntimeError(
            "Playwright 浏览器内核未安装。请运行: python3 -m playwright install chromium"
        )


def scroll_until_loaded(page: Any, max_idle_scrolls: int = 8) -> None:
    previous_count = -1
    previous_height = -1
    idle_scrolls = 0
    scroll_count = 0

    while idle_scrolls < max_idle_scrolls:
        scroll_count += 1
        current_count = page.locator(LIST_SELECTOR).count()
        current_height = page.evaluate("document.documentElement.scrollHeight")
        log_status(
            f"滚动加载: round={scroll_count}, items={current_count}, "
            f"idle={idle_scrolls}/{max_idle_scrolls}"
        )

        page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        page.wait_for_timeout(1200)

        next_count = page.locator(LIST_SELECTOR).count()
        next_height = page.evaluate("document.documentElement.scrollHeight")

        if next_count == current_count == previous_count and next_height == current_height == previous_height:
            idle_scrolls += 1
        else:
            idle_scrolls = 0

        previous_count = next_count
        previous_height = next_height

    log_status(f"滚动结束: rounds={scroll_count}, items={previous_count}")


BROWSER_EXTRACT_SCRIPT = r"""
(nodes) => nodes.map((node) => {
  const titleLink = node.querySelector('.c-item__title a[href]');
  const link = titleLink || node.querySelector('a[href]');
  const image = node.querySelector('img');
  const title =
    titleLink?.textContent ||
    node.querySelector('.c-item__title, .c-title, h2, h3')?.textContent ||
    link?.textContent ||
    '';
  const publishedAt =
    node.querySelector('.c-item__time')?.textContent ||
    node.querySelector('time')?.getAttribute('datetime') ||
    node.querySelector('time')?.textContent ||
    '';
  const infoText = Array.from(node.querySelectorAll('.c-item__info span'))
    .map((element) => element.textContent.trim())
    .filter(Boolean);
  const duration = infoText.find((value) => /^\d+\s*分\s*\d+\s*秒$/.test(value)) || '';

  return {
    title,
    url: link?.href || '',
    published_at: publishedAt,
    image: image?.currentSrc || image?.src || image?.dataset?.src || '',
    duration,
  };
})
"""


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
    candidates = _find_news_dicts(payload)
    items: list[NewsItem] = []
    
    id_template = CRAWLER_CFG["patterns"].get("news_id_url_template", "")

    for raw_item in candidates:
        title = _first_text(raw_item, ("title", "headline", "name"))
        url = _first_text(raw_item, ("link", "url", "href"))
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

        items.append(
            NewsItem(
                title=_clean_text(title),
                url=full_url,
                published_at=_first_text(
                    raw_item,
                    ("pubDate", "published_at", "publishedAt", "date", "datetime"),
                ),
                summary=_clean_text(
                    _first_text(raw_item, ("description", "summary", "lead", "body"))
                ),
                image=urljoin(
                    BASE_URL,
                    _first_text(raw_item, ("image", "image_url", "imageUrl", "thumbnail")),
                )
                if _first_text(raw_item, ("image", "image_url", "imageUrl", "thumbnail"))
                else "",
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
        title = _clean_text(re.sub(r"<[^>]+>", " ", match.group("body")))
        url = urljoin(BASE_URL, html.unescape(match.group("href")))
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
    log_status(f"保存结果: {len(items)} 条 -> {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    for index, item in enumerate(items, start=1):
        item.id = index

    if output.suffix.lower() == ".csv":
        with output.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=list(asdict(items[0]).keys()))
            writer.writeheader()
            writer.writerows(asdict(item) for item in items)
        return

    with output.open("w", encoding="utf-8") as file:
        json.dump([asdict(item) for item in items], file, ensure_ascii=False, indent=2)
    log_status(f"JSON 保存完成: {output}")


def clear_local_outputs(mp3_dir: Path, output: Path) -> None:
    mp3_dir.mkdir(parents=True, exist_ok=True)
    # 不再清空 mp3 文件夹，实现增量存储
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("", encoding="utf-8")
    log_status(f"本地初始化完成: 保留原有资源文件，清空结果文件 {output}")


def download_audio_files(
        items: list[NewsItem],
        mp3_dir: Path,
) -> tuple[int, int, list[tuple[str, str, int]]]:
    mp3_dir.mkdir(parents=True, exist_ok=True)
    downloaded_count = 0
    skipped_count = 0
    failures: list[tuple[str, str, int]] = []

    audio_items = [item for item in items if item.m3u8_url]
    log_status(f"开始处理音频下载: 可下载 {len(audio_items)} 条，目录={mp3_dir}")
    for index, item in enumerate(audio_items, start=1):
        item.mp3_hash = hash_m3u8_url(item.m3u8_url)
        output_file = mp3_dir / f"{item.mp3_hash}.mp3"

        if output_file.exists() and output_file.stat().st_size > 0:
            skipped_count += 1
            log_status(f"[{index}/{len(audio_items)}] 音频已存在: {output_file}")
            continue

        log_status(f"[{index}/{len(audio_items)}] 下载音频: {item.title}")
        try:
            total_sec = parse_duration(item.duration)
            download_mp3(item.m3u8_url, output_file, total_seconds=total_sec)
            downloaded_count += 1
        except subprocess.CalledProcessError as exc:
            failures.append((item.title, item.m3u8_url, exc.returncode))
            log_status(
                f"[{index}/{len(audio_items)}] 下载失败: returncode={exc.returncode}, "
                f"url={item.m3u8_url}"
            )
            if output_file.exists() and output_file.stat().st_size == 0:
                output_file.unlink()

    log_status(
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
    log_status(f"开始处理图片下载: {len(pic_items)} 条")

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
            log_status(f"图片下载失败: {item.image} ({exc})")

    log_status(f"图片下载结束: 下载 {downloaded_count} 条，跳过 {skipped_count} 条")
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
        log_status(f"同步音频到 MinIO: bucket={audio_bucket}")
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
        log_status(f"同步图片到 MinIO: bucket={img_bucket}")
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
        log_status(f"MinIO 连接成功: bucket={bucket}, exists={exists}, count={len(objs)}")
        return True
    except Exception as exc:
        log_status(f"MinIO 连接失败: {exc}")
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
    parser.add_argument("--source", choices=("browser", "html", "json", "auto"), default="browser")
    parser.add_argument("--max-idle-scrolls", type=int, default=8)
    parser.add_argument("--test-minio", action="store_true", help="测试 MinIO")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.full_pipeline:
        args.detail = True
        args.m3u8 = True
        args.download_audio = True
        args.download_pic = True
        args.upload_minio = True

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
            max_idle_scrolls=args.max_idle_scrolls,
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
            log_status(f"MinIO 同步完成: 上传 {up}，跳过 {sk}")

        save_items(items, args.output)
        log_status("任务完成")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
