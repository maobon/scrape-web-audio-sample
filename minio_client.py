import os
from pathlib import Path
from typing import List, Optional, Union
from urllib.parse import quote

# Centralized Configuration
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "127.0.0.1:9000")
MINIO_SECURE = os.getenv("MINIO_SECURE", "false").lower() in {"1", "true", "yes"}
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin")
MINIO_PUBLIC_URL = os.getenv("MINIO_PUBLIC_URL", "").rstrip("/")


def _get_minio_client():
    from minio import Minio
    return Minio(
        MINIO_ENDPOINT,
        access_key=MINIO_ACCESS_KEY,
        secret_key=MINIO_SECRET_KEY,
        secure=MINIO_SECURE,
    )


def build_public_url(bucket: str, object_name: str) -> str:
    """Build a public URL for a MinIO object."""
    encoded_name = "/".join(quote(part, safe="") for part in object_name.split("/"))

    if MINIO_PUBLIC_URL:
        return f"{MINIO_PUBLIC_URL}/{bucket}/{encoded_name}"

    scheme = "https" if MINIO_SECURE else "http"
    return f"{scheme}://{MINIO_ENDPOINT}/{bucket}/{encoded_name}"


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
            print(f"[MINIO] Bucket does not exist, creating: {bucket}")
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
        bucket: str = "data",
        verbose: bool = False
) -> str:
    file_path = Path(path)
    obj_name = object_name or file_path.name
    client = _get_minio_client()

    if verbose:
        print(f"[MINIO] Uploading: {file_path} -> {bucket}/{obj_name}")

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
