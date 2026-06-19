from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from .annotator import run
from .config import load_config


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Extrait les transcriptions TEI des copies Scoledit (CP-CM2), les découpe "
            "par page (<pb/>) pour s'aligner sur les scans et upload une annotation "
            "JSON par scan sur S3."
        )
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=Path("Corpus"),
        help="Répertoire racine du corpus (structure Grade_NN_(NIVEAU)/Scoledit/). Défaut : ./Corpus",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
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
    results = asyncio.run(run(args.corpus, config))

    uploaded = sum(1 for r in results if not r.get("skipped"))
    skipped = sum(1 for r in results if r.get("skipped"))

    print(f"\nTerminé. {uploaded} annotation(s) uploadée(s), {skipped} déjà présente(s) sur S3.")


if __name__ == "__main__":
    main()
