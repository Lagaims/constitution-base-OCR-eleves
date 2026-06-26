"""CLI `scoledit-scorer` : note mot à mot les transcriptions et dépose la notation.

Lit les annotations TEI sous `s3://<bucket>/scoledit/annotation/<niveau>/<id>.json`,
produit pour chaque copie un JSON de notation sous `scoledit/notation/<niveau>/<id>.json`
(même arborescence) et un CSV agrégé `scoledit/notation/notation.csv` (format long,
une ligne par mot) directement consommable par le pipeline evaluation_dictee.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor

from .config import S3Config, load_config
from .scorer import (
    CSV_HEADER,
    FrenchLexicon,
    build_notation,
    notation_csv_rows,
    score_tei,
)
from .storage import make_s3_client

logger = logging.getLogger(__name__)

ANNOTATION_PREFIX = "scoledit/annotation"
NOTATION_PREFIX = "scoledit/notation"


def _annotation_to_notation_key(key: str) -> str:
    """`scoledit/annotation/CM2/100a.json` -> `scoledit/notation/CM2/100a.json`."""
    return key.replace(f"{ANNOTATION_PREFIX}/", f"{NOTATION_PREFIX}/", 1)


def _list_annotation_keys(s3, config: S3Config, levels: set[str] | None) -> list[str]:
    paginator = s3.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=config.bucket, Prefix=ANNOTATION_PREFIX + "/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".json"):
                continue
            if levels is not None:
                # .../annotation/<niveau>/<fichier>.json
                parts = key.split("/")
                if len(parts) < 4 or parts[2] not in levels:
                    continue
            keys.append(key)
    return sorted(keys)


def _load_extra_words(path: str | None) -> list[str]:
    if not path:
        return []
    with open(path, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Note « erreur / pas erreur » chaque mot des transcriptions SCOLEDIT "
            "(annotations TEI sur S3) et dépose une notation JSON par copie + un "
            "CSV agrégé sous scoledit/notation/."
        )
    )
    parser.add_argument(
        "--level", action="append", default=None,
        help="Niveau(x) à traiter (CP, CE1…). Répétable. Défaut : tous.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Nb max de copies (test).")
    parser.add_argument(
        "--names", default=None,
        help="Fichier de mots supplémentaires acceptés (noms propres, 1 par ligne).",
    )
    parser.add_argument(
        "--local-dir", default=None,
        help="Écrit aussi les notations dans ce dossier local (au lieu de/en plus de S3).",
    )
    parser.add_argument(
        "--no-s3", action="store_true",
        help="N'écrit rien sur S3 (utile avec --local-dir pour une exécution sans droits d'écriture).",
    )
    parser.add_argument("--workers", type=int, default=16, help="Threads de lecture S3.")
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    config = load_config()
    s3 = make_s3_client(config)
    levels = {lvl.upper() for lvl in args.level} if args.level else None

    keys = _list_annotation_keys(s3, config, levels)
    if args.limit is not None:
        keys = keys[: args.limit]
    logger.info("%d annotation(s) à noter", len(keys))
    if not keys:
        print("Aucune annotation trouvée — lancer scoledit-annotator d'abord ?")
        return

    logger.info("Chargement du lexique français…")
    lexicon = FrenchLexicon(extra_words=_load_extra_words(args.names))

    local_dir = None
    if args.local_dir:
        from pathlib import Path

        local_dir = Path(args.local_dir)
        local_dir.mkdir(parents=True, exist_ok=True)

    def fetch(key: str) -> dict | None:
        try:
            body = s3.get_object(Bucket=config.bucket, Key=key)["Body"].read()
            return json.loads(body)
        except Exception as exc:  # noqa: BLE001 — on continue malgré une copie illisible
            logger.warning("Lecture impossible %s : %s", key, exc)
            return None

    csv_rows: list[list[str]] = []
    uploaded = errors = 0

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for key, annotation in zip(keys, pool.map(fetch, keys)):
            if annotation is None or "tei" not in annotation:
                errors += 1
                continue

            tokens = score_tei(annotation["tei"], lexicon.is_correct)
            notation = build_notation(annotation, tokens)
            csv_rows.extend(notation_csv_rows(notation))

            payload = json.dumps(notation, ensure_ascii=False, indent=2).encode("utf-8")
            out_key = _annotation_to_notation_key(key)

            if local_dir is not None:
                dest = local_dir / out_key[len(NOTATION_PREFIX) + 1 :]
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(payload)

            if not args.no_s3:
                try:
                    s3.put_object(
                        Bucket=config.bucket, Key=out_key, Body=payload,
                        ContentType="application/json",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Upload échoué %s : %s", out_key, exc)
                    errors += 1
                    continue
            uploaded += 1

    # CSV agrégé (format long).
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(CSV_HEADER)
    writer.writerows(csv_rows)
    csv_bytes = buf.getvalue().encode("utf-8")

    csv_key = f"{NOTATION_PREFIX}/notation.csv"
    if local_dir is not None:
        (local_dir / "notation.csv").write_bytes(csv_bytes)
    if not args.no_s3:
        try:
            s3.put_object(
                Bucket=config.bucket, Key=csv_key, Body=csv_bytes, ContentType="text/csv"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Upload CSV échoué %s : %s", csv_key, exc)

    dest_msg = []
    if not args.no_s3:
        dest_msg.append(f"s3://{config.bucket}/{NOTATION_PREFIX}/")
    if local_dir is not None:
        dest_msg.append(str(local_dir))
    print(
        f"\nTerminé. {uploaded} copie(s) notée(s), {errors} en erreur, "
        f"{len(csv_rows)} mots dans le CSV agrégé.\n"
        f"Sorties : {', '.join(dest_msg) or '(aucune — voir options)'}"
    )


if __name__ == "__main__":
    main()
