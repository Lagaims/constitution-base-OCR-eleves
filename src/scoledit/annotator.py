from __future__ import annotations

import asyncio
import json
import logging
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr, unescape

from tqdm.asyncio import tqdm

from .config import S3Config
from .storage import list_existing_keys, make_s3_client

logger = logging.getLogger(__name__)

# Niveaux Scoledit traités (les dossiers Resolco/Ecriscol sont ignorés).
SCOLEDIT_LEVELS = {"CP", "CE1", "CE2", "CM1", "CM2"}

# Dossier de niveau : "Grade_01_(CP)" -> "CP"
_LEVEL_DIR_RE = re.compile(r"Grade_\d+_\((\w+)\)$")
# Nom de fichier : "EC-CP-2014-102-D1-S199-V1.xml" -> id élève = 199 (le numéro après "S")
_XML_NAME_RE = re.compile(r"^EC-\w+-\d{4}-\d+-D\d+-S(\d+)-V\d+\.xml$")

# Longueur max d'un segment terminal (après le dernier <pb/>) pour le considérer
# comme un saut de page parasite (ex. « Fin », une signature courte).
_TERMINAL_PB_MAXLEN = 30
_END_MARKER_RE = re.compile(r"(?i)^(fin|the end)\b")


# ---------------------------------------------------------------------------
# Découverte des fichiers
# ---------------------------------------------------------------------------

def find_xml_files(corpus_path: Path) -> list[tuple[str, str, Path]]:
    """Retourne (student_id, level, xml_path) pour tous les XML Scoledit valides.

    Structure attendue : Corpus/Grade_NN_(NIVEAU)/Scoledit/EC-...-S<id>-V<n>.xml
    """
    results: list[tuple[str, str, Path]] = []

    for grade_dir in sorted(corpus_path.iterdir()):
        if not grade_dir.is_dir():
            continue
        m = _LEVEL_DIR_RE.match(grade_dir.name)
        if not m:
            continue
        level = m.group(1)
        if level not in SCOLEDIT_LEVELS:
            continue

        scoledit_dir = grade_dir / "Scoledit"
        if not scoledit_dir.is_dir():
            logger.debug("Pas de sous-dossier Scoledit/ dans %s", grade_dir)
            continue

        for xml_file in sorted(scoledit_dir.glob("*.xml")):
            mm = _XML_NAME_RE.match(xml_file.name)
            if not mm:
                logger.warning("Nom de fichier XML non conforme, ignoré : %s", xml_file.name)
                continue
            results.append((mm.group(1), level, xml_file))

    logger.info("Trouvé %d fichier(s) XML Scoledit à traiter", len(results))
    return results


def verify_corpus_dir(corpus_path: Path) -> None:
    """Lève une erreur claire si le Corpus est absent ou ne contient aucun XML Scoledit."""
    if not corpus_path.exists():
        raise FileNotFoundError(
            f"Répertoire Corpus introuvable : {corpus_path}\n"
            "Vérifiez le chemin passé en argument (--corpus)."
        )
    if not corpus_path.is_dir():
        raise NotADirectoryError(f"{corpus_path} n'est pas un répertoire.")
    if not find_xml_files(corpus_path):
        raise FileNotFoundError(
            f"Aucun fichier XML Scoledit trouvé sous {corpus_path}\n"
            "Structure attendue : Corpus/Grade_NN_(NIVEAU)/Scoledit/EC-...-S<id>-V<n>.xml"
        )


# ---------------------------------------------------------------------------
# Sérialisation TEI (sans perte) + découpage par page
# ---------------------------------------------------------------------------

def _open_tag(el: ET.Element) -> str:
    attrs = "".join(f" {k}={quoteattr(v)}" for k, v in el.attrib.items())
    return f"<{el.tag}{attrs}>"


def _self_closing(el: ET.Element) -> str:
    attrs = "".join(f" {k}={quoteattr(v)}" for k, v in el.attrib.items())
    return f"<{el.tag}{attrs}/>"


def _is_void(el: ET.Element) -> bool:
    return len(el) == 0 and not (el.text or "")


def _render(body: ET.Element, separator_pb: set[int]) -> list[str]:
    """Sérialise le contenu interne de <body> en TEI brut, en coupant aux <pb/>.

    `separator_pb` = indices (1-based, ordre document) des <pb/> qui agissent comme
    saut de page. Les autres <pb/> sont conservés en ligne. Renvoie une liste de
    pages (len = nombre de séparateurs + 1). Avec un set vide, renvoie le corps
    entier en un seul fragment.
    """
    pages: list[str] = [""]
    stack: list[ET.Element] = []
    pb_seen = 0

    def emit(s: str) -> None:
        pages[-1] += s

    def new_page() -> None:
        # Referme les balises ouvertes dans la page courante, puis les rouvre
        # dans la nouvelle page : chaque fragment reste bien formé.
        for el in reversed(stack):
            emit(f"</{el.tag}>")
        pages.append("")
        for el in stack:
            emit(_open_tag(el))

    def walk(el: ET.Element) -> None:
        nonlocal pb_seen
        emit(escape(el.text or ""))
        for child in el:
            if child.tag == "pb":
                pb_seen += 1
                if pb_seen in separator_pb:
                    new_page()
                else:
                    emit(_self_closing(child))
            elif _is_void(child):
                emit(_self_closing(child))
            else:
                emit(_open_tag(child))
                stack.append(child)
                walk(child)
                stack.pop()
                emit(f"</{child.tag}>")
            emit(escape(child.tail or ""))

    walk(body)
    return [p.strip() for p in pages]


def _last_pb_is_terminal(body: ET.Element) -> bool:
    """Vrai si le dernier <pb/> n'est suivi que d'un marqueur de fin court."""
    full = _render(body, set())[0]
    idx = full.rfind("<pb")
    if idx < 0:
        return False
    after = unescape(re.sub(r"<[^>]*>", "", full[idx:])).strip()
    return len(after) <= _TERMINAL_PB_MAXLEN or bool(_END_MARKER_RE.match(after))


# ---------------------------------------------------------------------------
# Construction des annotations (logique pure, testable sans S3)
# ---------------------------------------------------------------------------

def get_body(xml_path: Path) -> ET.Element | None:
    """Retourne l'élément <body> du <text> de production, ou None."""
    root = ET.parse(xml_path).getroot()
    for text in root.iter("text"):
        body = text.find("body")
        if body is not None:
            return body
    return None


def build_annotations(
    student_id: str,
    body: ET.Element,
    letters: list[str],
) -> list[tuple[str, str]]:
    """Retourne une liste de (stem, tei) pour un élève donné.

    Règle de mapping vis-à-vis des scans (lettres a-e) :
      - si nb_pages == nb_scans (en ignorant un <pb/> terminal parasite) :
        une annotation par scan, stem = {id}{lettre} ;
      - sinon : une annotation groupée, stem = {id}{toutes les lettres}.
    """
    n = len(letters)
    pb_total = sum(1 for _ in body.iter("pb"))

    if pb_total + 1 == n:
        pages = _render(body, set(range(1, pb_total + 1)))
        return [(f"{student_id}{ltr}", tei) for ltr, tei in zip(letters, pages)]

    if pb_total == n and pb_total >= 1 and _last_pb_is_terminal(body):
        pages = _render(body, set(range(1, pb_total)))  # on ignore le dernier <pb/>
        return [(f"{student_id}{ltr}", tei) for ltr, tei in zip(letters, pages)]

    # Cas divergent : une seule annotation groupée couvrant tous les scans.
    tei = _render(body, set())[0]
    return [(f"{student_id}{''.join(letters)}", tei)]


# ---------------------------------------------------------------------------
# Index des scans présents sur S3 (pour connaître les lettres a-e par élève)
# ---------------------------------------------------------------------------

def list_scan_letters(s3_client, config: S3Config) -> dict[tuple[str, str], list[str]]:
    """Retourne {(level, student_id): [lettres triées]} d'après les scans sur S3."""
    pat = re.compile(
        rf"^{re.escape(config.prefix)}/([^/]+)/(\d+)([a-e])\.(?:jpe?g|png)$",
        re.IGNORECASE,
    )
    out: dict[tuple[str, str], set[str]] = defaultdict(set)
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=config.bucket, Prefix=config.prefix + "/"):
        for obj in page.get("Contents", []):
            m = pat.match(obj["Key"])
            if m:
                out[(m.group(1), m.group(2))].add(m.group(3).lower())
    return {k: sorted(v) for k, v in out.items()}


# ---------------------------------------------------------------------------
# S3 upload
# ---------------------------------------------------------------------------

def _annotation_prefix(config: S3Config) -> str:
    # scoledit/scans → scoledit/annotation
    base = config.prefix.rsplit("/", 1)[0]
    return f"{base}/annotation"


def _upload_annotation(s3_client, bucket: str, key: str, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    s3_client.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

async def _process_entry(
    s3_client,
    student_id: str,
    level: str,
    xml_path: Path,
    config: S3Config,
    annotation_prefix: str,
    scan_letters: dict[tuple[str, str], list[str]],
    existing_keys: set[str],
    semaphore: asyncio.Semaphore,
) -> list[dict]:
    letters = scan_letters.get((level, student_id))
    if not letters:
        logger.warning("Aucun scan S3 pour %s/%s (%s) — ignoré", level, student_id, xml_path.name)
        return []

    try:
        body = get_body(xml_path)
    except ET.ParseError as exc:
        logger.warning("XML invalide %s : %s", xml_path, exc)
        return []
    if body is None:
        logger.warning("Aucun <body> dans %s — ignoré", xml_path)
        return []

    results: list[dict] = []
    for stem, tei in build_annotations(student_id, body, letters):
        if not tei.strip():
            logger.debug("Page vide ignorée : %s", stem)
            continue

        key = f"{annotation_prefix}/{level}/{stem}.json"
        if key in existing_keys:
            results.append({"scan": stem, "level": level, "s3_key": key, "skipped": True})
            continue

        payload = {"student_id": int(student_id), "level": level, "scan": stem, "tei": tei}
        async with semaphore:
            try:
                await asyncio.to_thread(_upload_annotation, s3_client, config.bucket, key, payload)
            except Exception as exc:
                logger.warning("Upload échoué pour %s : %s", key, exc)
                continue
        results.append({"scan": stem, "level": level, "s3_key": key, "skipped": False})

    return results


async def run(corpus_path: Path, config: S3Config) -> list[dict]:
    verify_corpus_dir(corpus_path)
    entries = find_xml_files(corpus_path)

    s3_client = make_s3_client(config)
    annotation_prefix = _annotation_prefix(config)

    scan_letters = await asyncio.to_thread(list_scan_letters, s3_client, config)
    logger.info("Scans indexés sur S3 pour %d élève(s)", len(scan_letters))

    ann_config = replace(config, prefix=annotation_prefix)
    existing_keys = await asyncio.to_thread(list_existing_keys, s3_client, ann_config)

    semaphore = asyncio.Semaphore(10)
    nested = await tqdm.gather(
        *[
            _process_entry(
                s3_client, sid, level, path, config,
                annotation_prefix, scan_letters, existing_keys, semaphore,
            )
            for sid, level, path in entries
        ],
        desc="Extraction & upload annotations",
        unit="fichier",
    )

    return [r for sub in nested for r in sub]
