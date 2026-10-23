# Moon Poro Bot

A Discord bot for a Polish League of Legends community server. Players link their Riot account
through Riot Sign On, the bot keeps their rank roles up to date, and moderators get warnings and
monthly statistics. Website: [moonporo.pl](https://moonporo.pl).

> Moon Poro Bot isn't endorsed by Riot Games and doesn't reflect the views or opinions of Riot
> Games. Riot Games and related properties are trademarks of Riot Games, Inc.

## Features

- Riot account verification through Riot Sign On (RSO);
- automatic Solo/Duo rank roles, refreshed in the background;
- a private Riot account profile through `/profil` and the verification panel;
- role selection panels;
- moderation warnings with escalation, rollback and automatic expiry;
- monthly moderator statistics;
- logged administrator lookups of Discord-Riot account links;
- optional member logs, account-age protection and message filters.

Features, channels and roles are set with environment variables. The bot asks for the privileged
`message_content` intent only when message filters are enabled.

## How it works

- `main.py` starts the bot (discord.py). It runs database migrations on startup.
- `rso_main.py` starts the Riot Sign On callback, a small aiohttp app behind Caddy. RSO client
  credentials live only in its own settings, never in the bot's.
- Both use the same PostgreSQL database with separate logins.
- `site/` is a static Astro website (Polish and English) with the privacy policy and terms.

| Path | Contents |
|---|---|
| `moon_poro/` | bot, commands (`cogs/`) and the RSO callback |
| `alembic/` | database migrations |
| `site/` | website |
| `deploy/` | systemd units, Caddyfile, deployment and backup scripts |
| `docs/` | production runbook and design notes (in Polish) |
| `tests/` | tests |

## Running locally

You need Python 3.12-3.14, PostgreSQL 17 or newer, a Discord bot token and a Riot API key.

```bash
python -m pip install uv==0.12.3
uv sync --frozen --extra dev
cp .env.example .env
uv run python main.py
```

Fill in `.env` first. `RSO_PUBLIC_BASE_URL` is required while verification is enabled. To run the
callback, copy `.env.rso.example` to `.env.rso` and start `uv run python rso_main.py`.

The website needs Node.js only on the machine that builds it:

```bash
cd site
npm ci
npm run dev
```

`npm run build` also needs every `PUBLIC_*` value from `site/.env.example`.

## Checks

CI runs the same commands:

```bash
bash -n deploy/*.sh
uv run ruff format --check .
uv run ruff check .
uv run mypy moon_poro
uv run pytest --cov=moon_poro --cov-branch
uv run bandit -r moon_poro
uv run pylint --disable=all --enable=duplicate-code moon_poro
uv run detect-secrets-hook $(git ls-files)
uv run pip-audit --skip-editable
```

PostgreSQL integration tests run only when the `TEST_POSTGRES_*` variables are set.

## Documentation

- [`docs/PRODUCTION.md`](docs/PRODUCTION.md): the production server, deployments, backups (with
  the monthly restore test and failure alerts) and rebuilding the VM;
- [`docs/RANKS_AND_VERIFICATION.md`](docs/RANKS_AND_VERIFICATION.md): how rank refresh and
  verification behave;
- [`docs/LEGACY_ARCHIVE_REVIEW.md`](docs/LEGACY_ARCHIVE_REVIEW.md): checking the old verification
  archive.

## Rights and Riot Games

This repository is not an open-source project. All rights are reserved; see `LICENSE` for details.
This project is not an official Riot Games product. Usage of the Riot API must comply with the
current [Riot Developer Policies](https://developer.riotgames.com/policies/general).
