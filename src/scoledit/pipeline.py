from __future__ import annotations

import asyncio
import logging

import aiohttp
from tqdm.asyncio import tqdm

from .config import (
    BACKOFF_BASE,
    MAX_CONCURRENT_DOWNLOADS,
    MAX_CONCURRENT_PAGES,
    MAX_RETRIES,
    REQUEST_DELAY,
    S3Config,
)
from .models import ScanInfo, ScanRecord
from .scraper import fetch_corpus_entries, fetch_scan_infos
from .storage import list_existing_keys, make_s3_client, upload_image

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; scoledit-scraper/0.1; research use)"
}


async def _get_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    **kwargs,
) -> aiohttp.ClientResponse:
    """GET with exponential backoff on 429. Caller must use response as context manager."""
    for attempt in range(MAX_RETRIES):
        resp = await session.get(url, **kwargs)

        if resp.status != 429:
            return resp

        # Respecter le header Retry-After s'il est présent
        retry_after = resp.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            wait = float(retry_after)
        else:
            wait = BACKOFF_BASE * (2 ** attempt)

        logger.warning("429 sur %s — attente %.1fs (tentative %d/%d)", url, wait, attempt + 1, MAX_RETRIES)
        await resp.release()
        await asyncio.sleep(wait)

    # Dernière tentative sans intercepter le 429
    return await session.get(url, **kwargs)


async def _fetch_all_scan_infos(
    session: aiohttp.ClientSession,
    entries,
) -> list[ScanInfo]:
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_PAGES)

    async def bounded(entry):
        async with semaphore:
            result = await fetch_scan_infos(session, entry)
            await asyncio.sleep(REQUEST_DELAY)
            return result

    results = await tqdm.gather(
        *[bounded(e) for e in entries],
        desc="Fetching scan lists",
        unit="page",
    )
    return [scan for scans in results for scan in scans]


async def _process_scan(
    session: aiohttp.ClientSession,
    s3_client,
    scan: ScanInfo,
    config: S3Config,
    semaphore: asyncio.Semaphore,
    existing_keys: set[str],
) -> ScanRecord | None:
    s3_key = f"{config.prefix}/{scan.level}/{scan.filename}"
    s3_path = f"s3://{config.bucket}/{s3_key}"

    if s3_key in existing_keys:
        logger.debug("Déjà sur S3, ignoré : %s", s3_key)
        return ScanRecord(
            filename=scan.filename,
            student_id=scan.student_id,
            level=scan.level,
            academy=scan.academy,
            s3_path=s3_path,
        )

    async with semaphore:
        try:
            resp = await _get_with_retry(
                session,
                scan.source_url,
                timeout=aiohttp.ClientTimeout(total=60),
            )
            async with resp:
                if resp.status == 404:
                    logger.debug("Not found: %s", scan.source_url)
                    return None
                resp.raise_for_status()
                data = await resp.read()
        except Exception as exc:
            logger.warning("Download failed for %s: %s", scan.source_url, exc)
            return None
        finally:
            await asyncio.sleep(REQUEST_DELAY)

    try:
        await asyncio.to_thread(upload_image, s3_client, config.bucket, s3_key, data)
    except Exception as exc:
        logger.warning("S3 upload failed for %s: %s", s3_key, exc)
        return None

    return ScanRecord(
        filename=scan.filename,
        student_id=scan.student_id,
        level=scan.level,
        academy=scan.academy,
        s3_path=s3_path,
    )


async def run(config: S3Config) -> list[ScanRecord]:
    s3_client = make_s3_client(config)
    existing_keys = await asyncio.to_thread(list_existing_keys, s3_client, config)
    download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_DOWNLOADS + MAX_CONCURRENT_PAGES)
    async with aiohttp.ClientSession(headers=_HEADERS, connector=connector) as session:
        entries = await fetch_corpus_entries(session)
        all_scans = await _fetch_all_scan_infos(session, entries)

        to_download = [s for s in all_scans if f"{config.prefix}/{s.level}/{s.filename}" not in existing_keys]
        logger.info(
            "%d scans au total — %d déjà sur S3, %d à télécharger",
            len(all_scans), len(all_scans) - len(to_download), len(to_download),
        )

        results = await tqdm.gather(
            *[
                _process_scan(session, s3_client, scan, config, download_semaphore, existing_keys)
                for scan in all_scans
            ],
            desc="Downloading & uploading",
            unit="scan",
        )

    return [r for r in results if r is not None]
