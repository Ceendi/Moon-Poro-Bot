# Moon-Poro-Bot

Discord bot for League of Legends community server. Handles user verification via Riot API, role management, moderation warnings, and ticket system.

## Features

- **Verification** - Links Discord account with Riot account, auto-updates rank roles every 24h
- **Role Management** - Self-assignable server/rank/position roles via buttons
- **Warn System** - Tiered warnings with automatic expiration
- **Tickets** - User reports with moderator assignment
- **Mod Stats** - Monthly statistics for moderator activity

## Project Structure

```
Moon-Poro-Bot/
├── cogs/           # Command modules
├── utils/          # Helper functions
├── config.py       # Configuration loader
├── functions.py    # Role checking utilities
├── main.py         # Entry point
└── requirements.txt
```

## Commands

| Command | Description | Permission |
|---------|-------------|------------|
| /w | Issue a warning | Moderacja |
| /cw | Revert a warning | Moderacja |
| /dr | Add role to user | Moderacja |
| /ur | Remove role from user | Moderacja |
| /usun_weryfikacje | Remove own verification | Everyone |

## License

Private project.
