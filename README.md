# News Spider

通用新闻列表爬虫。抓取目标、路径、存储桶、数据库和服务端口都从 `config.json` 读取。

## 安装依赖

```bash
pip install -r requirements.txt
```

下载音频需要本机可执行 `ffmpeg`。

## 常用命令

完整流水线：

```bash
./start.sh
```

写入 PostgreSQL 时会先清空配置表并重置自增 `id`，再插入本次最新抓取结果。

只抓取并保存 JSON，不写 PostgreSQL：

```bash
./start.sh --skip-db
```

把已有新闻 JSON 导入 PostgreSQL：

```bash
python3 -m news_spider.clients.postgres -i <配置中指定的输出文件>
```

## 配置

项目必须读取 `config.json`。该文件被 `.gitignore` 忽略，适合放本地连接信息。

爬虫目标、输出文件、PostgreSQL、MinIO 和 API 服务配置都写在 `config.json`。生成到 JSON 里的资源访问地址由 `minio.public.scheme/host/port` 拼接。

## API 服务

启动本地接口：

```bash
python3 -m news_spider.api.server
```

接口：

```text
GET /news
```

## 代码结构

```text
news_spider/
  config.py              # 配置读取和校验
  pipeline.py            # 抓取、下载、上传、入库流水线
  api/server.py          # HTTP API
  clients/minio.py       # MinIO 客户端
  clients/postgres.py    # PostgreSQL 入库
  media/audio.py         # 音频下载和时长解析
  media/picture.py       # 图片下载和上传辅助
  media/process_audio.py # 音频 URL 处理辅助
```

所有逻辑都在 `news_spider/` 包内。根目录不保留重复功能脚本。
