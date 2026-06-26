"""CLI `scoledit-consignes` : classe les exercices du corpus d'après leur consigne.

Parcourt le corpus local, extrait la consigne (`<factuality>`) de chaque copie, en
déduit le type d'exercice (écriture libre / dictée / recopie) et écrit un manifeste
CSV. La répartition par type et par niveau est affichée en fin d'exécution.
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import sys
from collections import Counter
from pathlib import Path

from .config import load_config
from .consignes import classify_corpus
from .storage import make_s3_client

logger = logging.getLogger(__name__)

MANIFEST_KEY = "scoledit/consignes.csv"
CSV_HEADER = [
    "file", "subcorpus", "level", "student_id", "exercise_type", "derivation", "consigne",
]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Classe les exercices du corpus (écriture libre / dictée / recopie) "
            "d'après la consigne TEI <factuality>, et écrit un manifeste CSV."
        )
    )
    parser.add_argument(
        "--corpus", type=Path, default=Path("Corpus"),
        help="Répertoire racine du corpus. Défaut : ./Corpus",
    )
    parser.add_argument(
        "--out", type=Path, default=None,
        help="Chemin du manifeste CSV local à écrire (ex. consignes.csv).",
    )
    parser.add_argument(
        "--s3", action="store_true",
        help=f"Upload aussi le manifeste sur S3 ({MANIFEST_KEY}).",
    )
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    if not args.corpus.is_dir():
        print(f"Répertoire corpus introuvable : {args.corpus}")
        raise SystemExit(1)

    records = classify_corpus(args.corpus)
    logger.info("%d copie(s) classée(s)", len(records))

    # Manifeste CSV.
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(CSV_HEADER)
    for r in records:
        writer.writerow(
            [r.file, r.subcorpus, r.level, r.student_id, r.exercise_type,
             r.derivation, r.consigne]
        )
    csv_text = buf.getvalue()

    if args.out is not None:
        args.out.write_text(csv_text, encoding="utf-8")
        print(f"Manifeste écrit : {args.out}")
    if args.s3:
        config = load_config()
        make_s3_client(config).put_object(
            Bucket=config.bucket, Key=MANIFEST_KEY,
            Body=csv_text.encode("utf-8"), ContentType="text/csv",
        )
        print(f"Manifeste uploadé : s3://{config.bucket}/{MANIFEST_KEY}")

    # Récapitulatif.
    by_type = Counter(r.exercise_type for r in records)
    print("\nRépartition par type d'exercice :")
    for etype, n in by_type.most_common():
        print(f"  {etype:16} {n:5}")

    print("\nRépartition type × niveau :")
    by_type_level: Counter[tuple[str, str]] = Counter(
        (r.exercise_type, r.level) for r in records
    )
    for (etype, level), n in sorted(by_type_level.items()):
        print(f"  {etype:16} {level:6} {n:5}")

    if args.out is None and not args.s3:
        print("\n(Aucune sortie écrite : utiliser --out <fichier> et/ou --s3.)")


if __name__ == "__main__":
    main()
