# Moon Poro Bot

A modular Discord bot for a League of Legends community, written in Python. It provides Riot
account verification, rank synchronization, configurable roles, moderation warnings, and
moderator statistics.

> Moon Poro Bot isn't endorsed by Riot Games and doesn't reflect the views or opinions of Riot
> Games. Riot Games and related properties are trademarks of Riot Games, Inc.

## Features

- Riot ID verification and automatic Solo/Duo rank synchronization;
- persistent role selection panels;
- versioned moderation warnings with escalation, rollback, and automatic expiration;
- monthly moderation statistics;
- audited administrator lookups for Discord–Riot account links;
- optional account-age protection, member logs, and message filters.

Features, channels, and roles are configured through environment variables. The bot does not
request the privileged `message_content` intent unless optional message filters are enabled.

## Setup

Python 3.12 or 3.13 and PostgreSQL 17+ are required.

```bash
python -m pip install uv==0.12.3
uv sync --frozen
cp .env.example .env
uv run python main.py
```

Complete `.env` before starting the bot. Alembic applies pending migrations during startup, so
create a database backup before the first production deployment. On Linux, keep the secrets file
readable only by the service account (`chmod 600 .env`). The supplied systemd unit writes logs to
`/var/log/moon-poro` and keeps the application directory read-only.

## Development

```bash
uv sync --frozen --extra dev
uv run ruff format --check .
uv run ruff check .
uv run pytest --cov=moon_poro --cov-branch
uv run mypy moon_poro
uv run bandit -r moon_poro
uv run pylint --disable=all --enable=duplicate-code moon_poro
uv run detect-secrets-hook $(git ls-files)
uv run pip-audit --skip-editable
```

Application code is located in `moon_poro/`, database migrations in `alembic/`, and tests in
`tests/`.

## Rights and Riot Games

This repository is not an open-source project. All rights are reserved; see `LICENSE` for details.
This project is not an official Riot Games product. Usage of the Riot API must comply with the
current [Riot Developer Policies](https://developer.riotgames.com/policies/general).
