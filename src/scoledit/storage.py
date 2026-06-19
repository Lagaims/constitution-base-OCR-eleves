from __future__ import annotations

import io
import logging
from typing import Any

import boto3
import pandas as pd

from .config import S3Config
from .models import ScanRecord

logger = logging.getLogger(__name__)


def make_s3_client(config: S3Config):
    kwargs: dict[str, Any] = {"region_name": config.region}
    if config.endpoint_url:
        kwargs["endpoint_url"] = config.endpoint_url
    if config.access_key and config.secret_key:
        kwargs["aws_access_key_id"] = config.access_key
        kwargs["aws_secret_access_key"] = config.secret_key
    if config.session_token:
        kwargs["aws_session_token"] = config.session_token
    return boto3.client("s3", **kwargs)


def list_existing_keys(s3_client, config: S3Config) -> set[str]:
    """Return the set of S3 keys already present under the scans prefix."""
    paginator = s3_client.get_paginator("list_objects_v2")
    keys: set[str] = set()
    for page in paginator.paginate(Bucket=config.bucket, Prefix=config.prefix + "/"):
        for obj in page.get("Contents", []):
            keys.add(obj["Key"])
    logger.info("Trouvé %d fichier(s) déjà sur S3", len(keys))
    return keys


def upload_image(s3_client, bucket: str, key: str, data: bytes) -> None:
    s3_client.put_object(Bucket=bucket, Key=key, Body=data, ContentType="image/jpeg")


def save_metadata_parquet(records: list[ScanRecord], config: S3Config) -> str:
    """Build a DataFrame from scan records and upload it as Parquet to S3."""
    df = pd.DataFrame([r.to_dict() for r in records])
    df = df[["filename", "student_id", "level", "academy", "s3_path"]]

    buf = io.BytesIO()
    df.to_parquet(buf, index=False, engine="pyarrow")

    s3_client = make_s3_client(config)
    key = "scoledit/metadata.parquet"
    s3_client.put_object(Bucket=config.bucket, Key=key, Body=buf.getvalue())

    path = f"s3://{config.bucket}/{key}"
    logger.info("Metadata saved to %s (%d rows)", path, len(df))
    return path
