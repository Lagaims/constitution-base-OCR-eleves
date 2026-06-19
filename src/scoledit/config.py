from __future__ import annotations

import os
from dataclasses import dataclass

BASE_URL = "https://scoledit.org/scoledition"
CORPUS_URL = f"{BASE_URL}/corpus.php"
LEVELS = ("CP", "CE1", "CE2", "CM1", "CM2")

MAX_CONCURRENT_PAGES = 2
MAX_CONCURRENT_DOWNLOADS = 3
REQUEST_DELAY = 0.5   # secondes entre chaque requête dans le semaphore
MAX_RETRIES = 5
BACKOFF_BASE = 2.0    # secondes (doublé à chaque retry)


@dataclass(frozen=True)
class S3Config:
    bucket: str
    prefix: str
    endpoint_url: str | None
    access_key: str | None
    secret_key: str | None
    session_token: str | None
    region: str


def load_config() -> S3Config:
    return S3Config(
        bucket=os.environ.get("S3_BUCKET", "projet-production-ecrits-depp"),
        prefix=os.environ.get("S3_PREFIX", "scoledit/scans"),
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or os.environ.get("AWS_S3_ENDPOINT"),
        access_key=os.environ.get("AWS_ACCESS_KEY_ID"),
        secret_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        session_token=os.environ.get("AWS_SESSION_TOKEN"),
        region=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
    )
