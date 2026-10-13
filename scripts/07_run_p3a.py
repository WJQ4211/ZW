"""Deprecated compatibility entry point.

The old P3a pilot used deterministic hardcoded candidate payloads and is
retained only as a historical output. New runs must use the independent
P3a-R prepare, execute-model, and validate commands.
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "This historical entry point is disabled. Use scripts/07_prepare_p3ar.py, "
        "scripts/08_execute_model_p3ar.py, and scripts/09_validate_p3ar.py."
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
