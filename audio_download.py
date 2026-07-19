import hashlib
import json
import re
import subprocess
from pathlib import Path


def log_status(message: str) -> None:
    print(f"[audio_download] {message}", flush=True)


def hash_m3u8_url(m3u8_url: str) -> str:
    return hashlib.sha256(m3u8_url.encode("utf-8")).hexdigest()


def parse_duration(duration_str: str) -> int:
    """将 '1 分 30 秒' 或 '90秒' 转换为秒数。"""
    if not duration_str:
        return 0
    # 提取所有数字
    parts = re.findall(r"(\d+)", duration_str)
    if len(parts) == 2:  # X 分 Y 秒
        return int(parts[0]) * 60 + int(parts[1])
    if len(parts) == 1:  # X 秒
        return int(parts[0])
    return 0


def download_mp3(m3u8_url: str, output_file: Path, total_seconds: int = 0) -> None:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-reconnect", "1",
        "-reconnect_at_eof", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "2",
        "-fflags", "+genpts+discardcorrupt",
        "-err_detect", "ignore_err",
        "-i", m3u8_url,
        "-stats",
        str(output_file),
    ]

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    try:
        buffer = ""
        while True:
            char = process.stderr.read(1)
            if not char and process.poll() is not None:
                break

            if char in ("\r", "\n"):
                if "time=" in buffer:
                    time_match = re.search(r"time=\s*(\d{2}):(\d{2}):(\d{2})\.\d{2}", buffer)
                    if time_match:
                        h, m, s = map(int, time_match.groups())
                        current_seconds = h * 3600 + m * 60 + s

                        if total_seconds > 0:
                            percent = min(100, int((current_seconds / total_seconds) * 100))
                            dots = percent // 5
                            bar = "." * dots
                            spaces = " " * (20 - dots)
                            print(f"\r    进度: [{bar}{spaces}] {percent}%", end="", flush=True)
                        else:
                            print(f"\r    进度: 已下载 {h:02}:{m:02}:{s:02} ...", end="",
                                  flush=True)
                buffer = ""
            else:
                buffer += char

        process.wait()
        if total_seconds > 0 and process.returncode == 0:
            print(f"\r    进度: [{'.' * 20}] 100% ", end="")
        print()

        if process.returncode != 0:
            stderr_out = process.stderr.read()
            if stderr_out:
                print(f"\n[FFMPEG ERROR] {stderr_out}")
            raise subprocess.CalledProcessError(process.returncode, command)
    except Exception:
        process.kill()
        raise


def main() -> None:
    news_file = Path("news_data.json")
    if not news_file.exists():
        print(f"{news_file} not found")
        return

    items = json.loads(news_file.read_text(encoding="utf-8"))
    mp3_dir = Path("mp3")
    mp3_dir.mkdir(exist_ok=True)

    for index, item in enumerate(items, start=1):
        m3u8 = item.get("m3u8_url")
        if not m3u8:
            continue

        mp3_hash = hash_m3u8_url(m3u8)
        output = mp3_dir / f"{mp3_hash}.mp3"
        if output.exists() and output.stat().st_size > 0:
            continue

        print(f"[{index}/{len(items)}] Downloading: {item.get('title')}")
        total_sec = parse_duration(item.get("duration", ""))
        try:
            download_mp3(m3u8, output, total_seconds=total_sec)
        except Exception as e:
            print(f"Failed: {e}")


if __name__ == "__main__":
    main()
