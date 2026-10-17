from collections.abc import Callable
from pathlib import Path

from moon_poro.settings import Settings

ROOT = Path(__file__).resolve().parent.parent


def test_database_pool_defaults_fit_concurrent_workers(
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory()

    assert (settings.database_pool_size, settings.database_max_overflow) == (5, 5)


def test_env_example_matches_the_production_defaults(
    settings_factory: Callable[..., Settings],
) -> None:
    values = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    defaults = settings_factory()

    assert values["VERIFICATION_MODE"] == defaults.verification_mode == "rso"
    assert int(values["DATABASE_POOL_SIZE"]) == defaults.database_pool_size
    assert int(values["DATABASE_MAX_OVERFLOW"]) == defaults.database_max_overflow
