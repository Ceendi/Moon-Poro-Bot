from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path

from moon_poro.database import upgrade_database
from moon_poro.settings import Settings


async def run_migrations(
    env_file: Path | None = None,
    *,
    legacy_audit_channel_id: int | None = None,
) -> None:
    """Run project migrations with the same validated settings as the bot."""

    settings = Settings() if env_file is None else Settings(_env_file=env_file)
    await upgrade_database(
        settings,
        legacy_audit_channel_id=legacy_audit_channel_id,
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Uruchamia migracje bazy Moon Poro bez uruchamiania bota Discord."
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Plik środowiskowy bota, np. /etc/moon-poro/bot.env.",
    )
    parser.add_argument(
        "--legacy-audit-channel-id",
        type=int,
        help=(
            "Potwierdzony identyfikator kanału zawierającego wszystkie dotychczasowe "
            "wiadomości weryfikacyjne."
        ),
    )
    args = parser.parse_args(argv)
    asyncio.run(
        run_migrations(
            args.env_file,
            legacy_audit_channel_id=args.legacy_audit_channel_id,
        )
    )


if __name__ == "__main__":
    main()
