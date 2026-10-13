"""Create or migrate the Phase A SQLite database."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.storage import Store


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize/migrate the Phase A SQLite database")
    parser.add_argument("--workspace", default=".")
    args = parser.parse_args()
    database = Path(args.workspace).resolve() / "data" / "research.sqlite3"
    with Store(database):
        pass
    print(database)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
