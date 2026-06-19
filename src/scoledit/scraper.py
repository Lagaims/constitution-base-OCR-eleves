from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import parse_qs, urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup

from .config import BASE_URL, CORPUS_URL
from .models import ScanInfo, StudentEntry

logger = logging.getLogger(__name__)

_PRODUCTION_HREF_RE = re.compile(r"production\.php")


async def fetch_corpus_entries(session: aiohttp.ClientSession) -> list[StudentEntry]:
    """Fetch corpus.php and return all (student_id, academy, level) combinations that have scans."""
    async with session.get(CORPUS_URL) as resp:
        resp.raise_for_status()
        html = await resp.text()

    entries = _parse_corpus_entries(html)
    logger.info("Found %d (student, level) combinations", len(entries))
    return entries


def _parse_corpus_entries(html: str) -> list[StudentEntry]:
    soup = BeautifulSoup(html, "html.parser")
    entries: list[StudentEntry] = []
    seen: set[tuple[int, str]] = set()

    for a_tag in soup.find_all("a", href=_PRODUCTION_HREF_RE):
        href = a_tag.get("href", "")
        qs = parse_qs(urlparse(href).query)

        if "id" not in qs or "niv" not in qs:
            continue

        try:
            student_id = int(qs["id"][0])
        except (ValueError, IndexError):
            continue

        level = qs["niv"][0]
        key = (student_id, level)
        if key in seen:
            continue

        tr = a_tag.find_parent("tr")
        if tr is None:
            continue

        tds = tr.find_all("td")
        # Some rows are missing the academy cell: td[3] jumps straight to the CP
        # transcription column (identifiable by containing a production.php link).
        td3 = tds[3] if len(tds) > 3 else None
        if td3 and td3.find("a", href=_PRODUCTION_HREF_RE):
            academy = ""
        else:
            academy = td3.get_text(strip=True) if td3 else ""

        seen.add(key)
        entries.append(StudentEntry(student_id=student_id, academy=academy, level=level))

    return entries


async def fetch_scan_infos(
    session: aiohttp.ClientSession,
    entry: StudentEntry,
) -> list[ScanInfo]:
    """Fetch a production page and return all scan image infos for this student/level."""
    from .config import BACKOFF_BASE, MAX_RETRIES

    page_url = f"{BASE_URL}/production.php?id={entry.student_id}&niv={entry.level}"

    for attempt in range(MAX_RETRIES + 1):
        try:
            async with session.get(page_url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 404:
                    return []
                if resp.status == 429:
                    retry_after = resp.headers.get("Retry-After", "")
                    wait = float(retry_after) if retry_after.isdigit() else BACKOFF_BASE * (2 ** attempt)
                    logger.warning("429 sur %s — attente %.1fs (tentative %d/%d)", page_url, wait, attempt + 1, MAX_RETRIES)
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                html = await resp.text()
        except Exception as exc:
            logger.warning("Failed to fetch %s: %s", page_url, exc)
            return []
        return _parse_scan_infos(html, entry, page_url)

    logger.error("Abandon après %d tentatives : %s", MAX_RETRIES, page_url)
    return []


def _parse_scan_infos(html: str, entry: StudentEntry, page_url: str) -> list[ScanInfo]:
    soup = BeautifulSoup(html, "html.parser")
    pattern = re.compile(
        rf"scans/{re.escape(entry.level)}/{re.escape(str(entry.student_id))}\w*\.(jpe?g|png)",
        re.IGNORECASE,
    )

    scans: list[ScanInfo] = []
    seen_filenames: set[str] = set()

    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not pattern.search(src):
            continue

        filename = src.rsplit("/", 1)[-1]
        if filename in seen_filenames:
            continue

        seen_filenames.add(filename)
        scans.append(
            ScanInfo(
                student_id=entry.student_id,
                academy=entry.academy,
                level=entry.level,
                filename=filename,
                source_url=urljoin(page_url, src),
            )
        )

    return scans
