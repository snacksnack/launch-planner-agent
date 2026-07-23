"""``python -m app`` — print a credential-free config sanity check and exit.

Used as a smoke check in CI and by developers to confirm the app is wired up
correctly without needing any secrets present.
"""

from __future__ import annotations

import json

from app import __version__
from app.config import get_settings


def main() -> None:
    settings = get_settings()
    print(f"launch-planner-agent api v{__version__}")
    print("config sanity check (no credentials required):")
    print(json.dumps(settings.sanity_check(), indent=2))


if __name__ == "__main__":
    main()
