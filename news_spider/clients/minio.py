import logging
from pathlib import Path
from typing import List, Optional, Union
from urllib.parse import quote

from news_spider.config import load_config

logger = logging.getLogger("minio_client")


def _minio_config() -> dict:
    return load_config()["minio"]


def _get_minio_client():
    from minio import Minio
    config = _minio_config()
    return Minio(
        str(config["endpoint"]),
        access_key=str(config["access_key"]),
        secret_key=str(config["secret_key"]),
        secure=bool(config["secure"]),
    )


def build_public_url(bucket: str, object_name: str) -> str:
    """Build a public URL for a MinIO object."""
    config = _minio_config()
    encoded_name = "/".join(quote(part, safe="") for part in object_name.split("/"))
    public_url = str(config["public_url"]).rstrip("/")

    if public_url:
        return f"{public_url}/{bucket}/{encoded_name}"

    scheme = "https" if config["secure"] else "http"
    return f"{scheme}://{config['endpoint']}/{bucket}/{encoded_name}"


def _content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".mp3": "audio/mpeg",
    }
    return types.get(suffix, "application/octet-stream")


def _ensure_public_bucket(client, bucket: str, verbose: bool = False):
    if not client.bucket_exists(bucket):
        if verbose:
            logger.info(f"Bucket does not exist, creating: {bucket}")
        client.make_bucket(bucket)

    policy = f"""{{
      "Version": "2012-10-17",
      "Statement": [
        {{
          "Effect": "Allow",
          "Principal": {{"AWS": ["*"]}},
          "Action": ["s3:GetObject"],
          "Resource": ["arn:aws:s3:::{bucket}/*"]
        }}
      ]
    }}"""
    client.set_bucket_policy(bucket, policy)


def upload_file(
        path: Union[Path, str],
        object_name: Optional[str] = None,
        bucket: Optional[str] = None,
        verbose: bool = False
) -> str:
    if not bucket:
        raise ValueError("bucket 必须从 config.json 读取后显式传入")

    file_path = Path(path)
    obj_name = object_name or file_path.name
    client = _get_minio_client()

    if verbose:
        logger.info(f"Uploading: {file_path} -> {bucket}/{obj_name}")

    _ensure_public_bucket(client, bucket, verbose=verbose)
    client.fput_object(
        bucket,
        obj_name,
        str(file_path),
        content_type=_content_type(file_path),
    )
    return build_public_url(bucket, obj_name)


def list_object_names(bucket: str) -> List[str]:
    client = _get_minio_client()
    if not client.bucket_exists(bucket):
        return []
    return [item.object_name for item in client.list_objects(bucket, recursive=True)]


def bucket_exists(bucket: str) -> bool:
    client = _get_minio_client()
    return client.bucket_exists(bucket)


def clear_bucket(bucket: str, verbose: bool = False) -> int:
    client = _get_minio_client()
    if not client.bucket_exists(bucket):
        return 0

    deleted = 0
    for item in client.list_objects(bucket, recursive=True):
        client.remove_object(bucket, item.object_name)
        deleted += 1
    return deleted
