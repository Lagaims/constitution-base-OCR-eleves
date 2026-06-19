from __future__ import annotations

import asyncio
import logging
import sys

from .config import load_config
from .pipeline import run
from .storage import save_metadata_parquet


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    config = load_config()
    records = asyncio.run(run(config))

    if not records:
        logging.error("No scans were downloaded. Check logs for details.")
        sys.exit(1)

    metadata_path = save_metadata_parquet(records, config)
    print(f"\nDone. {len(records)} scans uploaded.")
    print(f"Metadata: {metadata_path}")


if __name__ == "__main__":
    main()
