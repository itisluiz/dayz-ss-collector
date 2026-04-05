# dayz-logs skill

Use this skill when the user wants to read, search, list, or follow DayZ script logs.

Trigger phrases: "show logs", "check logs", "search logs", "follow logs", "tail logs", "latest log", "server log", "client log", "list logs", "/dayz-logs".

## How to use

Run `python scripts/logs.py` from the project root (`P:\dayz-ss-collector`) with the appropriate flags.

### Commands

| Goal | Command |
|---|---|
| Show latest log (auto-detect type) | `python scripts/logs.py` |
| Show latest server log | `python scripts/logs.py --server` |
| Show latest client log | `python scripts/logs.py --client` |
| Print log file path only | `python scripts/logs.py --path` |
| Search with regex | `python scripts/logs.py --search "SSCollector"` |
| OR search | `python scripts/logs.py --search "error\|warning"` |
| Follow (tail -f) latest log | `python scripts/logs.py --follow` |
| Follow server log | `python scripts/logs.py --server --follow` |
| Follow server log, filtered | `python scripts/logs.py --server --follow --search "SSCollector"` |
| List recent logs with types | `python scripts/logs.py --list` |

### Notes

- Logs are detected as `server` or `client` by content (not filename). Server logs contain `"Server mission initialized"`; client logs contain `"MissionGameplay initialized"` or `"Creating Mission:"`.
- The logs directory is read from `scripts/settings.json` → `logs_dir`.
- `--follow` streams new lines in real time (0.25 s poll); press Ctrl+C to stop.
- `--search` is always case-insensitive regex.
- `--list` shows the 20 most-recent logs with their detected type and modification time.

## When invoked

Run the appropriate command via the Bash tool and display the output to the user. For `--follow`, note that interactive tail is not suitable in non-interactive contexts — instead run a bounded read (e.g., `--search` without `--follow`) and inform the user if they need real-time streaming.
