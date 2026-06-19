from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from pathlib import Path

import asyncio
from tqdm.asyncio import tqdm

from .config import S3Config
from .storage import list_existing_keys, make_s3_client

logger = logging.getLogger(__name__)

KNOWN_LEVELS = {"CP", "CE1", "CE2", "CM1", "CM2"}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def verify_corpus_dir(corpus_path: Path) -> None:
    """Raise a clear error if the Corpus directory is missing or has no XML."""
    if not corpus_path.exists():
        raise FileNotFoundError(
            f"Répertoire Corpus introuvable : {corpus_path}\n"
            "Vérifiez le chemin passé en argument (--corpus)."
        )
    if not corpus_path.is_dir():
        raise NotADirectoryError(f"{corpus_path} n'est pas un répertoire.")

    xml_files = list(corpus_path.rglob("scoledit/*.xml"))
    if not xml_files:
        raise FileNotFoundError(
            f"Aucun fichier XML trouvé sous {corpus_path}/**/scoledit/\n"
            "Structure attendue : Corpus/{niveau}/scoledit/{id}.xml"
        )

    logger.info("Corpus OK — %d fichier(s) XML trouvé(s)", len(xml_files))


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def find_xml_files(corpus_path: Path) -> list[tuple[int, str, Path]]:
    """Return list of (student_id, level, xml_path) for all valid XML files."""
    results: list[tuple[int, str, Path]] = []

    for grade_dir in sorted(corpus_path.iterdir()):
        if not grade_dir.is_dir():
            continue

        level = grade_dir.name
        if level not in KNOWN_LEVELS:
            logger.debug("Dossier ignoré (niveau inconnu) : %s", grade_dir)
            continue

        scoledit_dir = grade_dir / "scoledit"
        if not scoledit_dir.is_dir():
            logger.debug("Pas de sous-dossier scoledit/ dans %s", grade_dir)
            continue

        for xml_file in sorted(scoledit_dir.glob("*.xml")):
            try:
                student_id = int(xml_file.stem)
            except ValueError:
                logger.warning("Nom de fichier non numérique ignoré : %s", xml_file)
                continue
            results.append((student_id, level, xml_file))

    logger.info("Trouvé %d fichier(s) XML à traiter", len(results))
    return results


# ---------------------------------------------------------------------------
# XML parsing
# ---------------------------------------------------------------------------

def extract_texts(xml_path: Path) -> list[str]:
    """Extract all <text> elements, preserving internal newlines."""
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError as exc:
        logger.warning("XML invalide %s : %s", xml_path, exc)
        return []

    root = tree.getroot()
    texts: list[str] = []

    for text_elem in root.iter("text"):
        content = "".join(text_elem.itertext())
        # Normaliser les fins de ligne tout en préservant les sauts
        content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
        if content:
            texts.append(content)

    return texts


# ---------------------------------------------------------------------------
# S3 upload
# ---------------------------------------------------------------------------

def _build_s3_key(prefix: str, level: str, student_id: int) -> str:
    return f"{prefix}/{level}/{student_id}.json"


def _upload_annotation(s3_client, bucket: str, key: str, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    s3_client.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

async def _process_entry(
    s3_client,
    student_id: int,
    level: str,
    xml_path: Path,
    config: S3Config,
    existing_keys: set[str],
    semaphore: asyncio.Semaphore,
) -> dict | None:
    annotation_prefix = _annotation_prefix(config)
    s3_key = _build_s3_key(annotation_prefix, level, student_id)

    if s3_key in existing_keys:
        logger.debug("Déjà sur S3, ignoré : %s", s3_key)
        return {"student_id": student_id, "level": level, "s3_key": s3_key, "skipped": True}

    texts = extract_texts(xml_path)
    if not texts:
        logger.warning("Aucun <text> extrait de %s", xml_path)
        return None

    payload = {"student_id": student_id, "level": level, "texts": texts}

    async with semaphore:
        try:
            await asyncio.to_thread(_upload_annotation, s3_client, config.bucket, s3_key, payload)
        except Exception as exc:
            logger.warning("Upload échoué pour %s : %s", s3_key, exc)
            return None

    return {"student_id": student_id, "level": level, "s3_key": s3_key, "skipped": False}


def _annotation_prefix(config: S3Config) -> str:
    # scoledit/scans → scoledit/annotation
    base = config.prefix.rsplit("/", 1)[0]
    return f"{base}/annotation"


async def run(corpus_path: Path, config: S3Config) -> list[dict]:
    verify_corpus_dir(corpus_path)
    entries = find_xml_files(corpus_path)

    s3_client = make_s3_client(config)
    annotation_prefix = _annotation_prefix(config)

    # Réutiliser le même mécanisme de vérification S3
    existing_config_for_annotation = S3Config(
        bucket=config.bucket,
        prefix=annotation_prefix,
        endpoint_url=config.endpoint_url,
        access_key=config.access_key,
        secret_key=config.secret_key,
        session_token=config.session_token,
        region=config.region,
    )
    existing_keys = await asyncio.to_thread(list_existing_keys, s3_client, existing_config_for_annotation)

    to_upload = [
        e for e in entries
        if _build_s3_key(annotation_prefix, e[1], e[0]) not in existing_keys
    ]
    logger.info(
        "%d fichier(s) XML — %d déjà sur S3, %d à uploader",
        len(entries), len(entries) - len(to_upload), len(to_upload),
    )

    semaphore = asyncio.Semaphore(10)
    results = await tqdm.gather(
        *[
            _process_entry(s3_client, sid, level, path, config, existing_keys, semaphore)
            for sid, level, path in entries
        ],
        desc="Extraction & upload annotations",
        unit="fichier",
    )

    return [r for r in results if r is not None]
