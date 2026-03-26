# Slack Notification Organizer 🔔

A macOS menu bar app that intercepts Slack desktop notifications, uses an LLM to organise them into conversation threads, and displays them ordered by urgency.

```
🔴 [#oncall] Alice: Production DB is down, need help ASAP
🟠 [DM] Bob: Can you review my PR before EOD?
🟡 [#general] Carol: Team lunch at 12pm today
────────────────────────────────────────────
⚙ Settings…   ✅ Mark all read   ✗ Quit
```

---

## Requirements

- macOS 12 or later
- Python 3.10+
- [GitHub CLI](https://cli.github.com/) (`gh`) authenticated — used for the Copilot LLM backend
- Slack desktop app installed

---

## Installation

```bash
# 1. Clone / enter the project directory
cd slack_notification

# 2. Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Authenticate with GitHub CLI (if not already done)
gh auth login
```

---

## Required macOS Permission — Accessibility

The app reads notification banners from NotificationCenter.app via the macOS Accessibility API. This requires **Accessibility** access:

1. Open **System Settings** → **Privacy & Security** → **Accessibility**
2. Click the **+** button
3. Navigate to your Terminal app (or the Python binary: `.venv/bin/python3`)
4. Enable the toggle

Also ensure Slack notifications are set to **Alerts** (persistent) style:

1. Open **System Settings** → **Notifications** → **Slack**
2. Set **Alert style** to **Alerts** (the square icon, not Banner)

> Without Accessibility permission, `_find_nc_pid()` will return a PID but window reads will return empty results.

---

## Running

```bash
python main.py
```

A 🔔 icon appears in your menu bar. Click it to see threads sorted by urgency.

---

## Configuration

Edit `config.yaml` to customise behaviour:

| Key | Default | Description |
|-----|---------|-------------|
| `llm.backend` | `copilot` | LLM backend: `copilot`, `claude`, or `openai` |
| `poll_interval` | `5` | Seconds between notification DB polls |
| `urgency_keywords` | (list) | Keywords that boost urgency score (+2 each) |
| `scoring.dm_bonus` | `3` | Score bonus for direct messages |
| `scoring.mention_bonus` | `2` | Score bonus for @mentions |
| `scoring.keyword_bonus` | `2` | Score bonus per matched keyword |
| `priority_thresholds.red` | `8` | Score threshold for 🔴 |
| `priority_thresholds.orange` | `5` | Score threshold for 🟠 |
| `priority_thresholds.yellow` | `2` | Score threshold for 🟡 |
| `max_threads_displayed` | `20` | Max threads shown in menu |

---

## Adding a New LLM Backend

1. Open `src/llm/` and copy `claude.py` as a template.
2. Implement the `ask(self, prompt: str) -> str` method.
3. Register it with `@BackendFactory.register("my-backend")`.
4. Set `llm.backend: my-backend` in `config.yaml`.

---

## Troubleshooting

**No notifications appearing:**
- Grant **Accessibility** permission to Terminal (or `.venv/bin/python3`):  
  `System Settings → Privacy & Security → Accessibility → add Terminal`
- Verify NotificationCenter.app is found:  
  `python -c "from src.notification_watcher import _find_nc_pid; print(_find_nc_pid())"`  
  Should print a PID (e.g. `780`), not `None`.
- The watcher reads from NotificationCenter.app's Accessibility tree — notifications must  
  appear as **alert-style** (persistent) banners. Confirm Slack is set to "Alerts" in  
  `System Settings → Notifications → Slack`.

**LLM calls failing:**
- Ensure `gh auth status` shows you are authenticated.
- Test manually: `gh copilot explain "hello"`.

**App doesn't appear in menu bar:**
- Make sure you are running in an activated venv and have installed `rumps`.
- On Apple Silicon, ensure you are using an arm64 Python.

---

## Data Storage

All data is stored locally in `data/notifications.db` (SQLite). No messages are sent to external servers except through the configured LLM CLI tool.

---

## Architecture

```
main.py
  ├── APScheduler (background)
  │     ├── NotificationWatcher  →  polls macOS Notification Center DB
  │     └── ThreadOrganizer      →  rule scoring + LLM clustering & urgency
  │           ├── storage.py     →  local SQLite store
  │           └── llm/           →  pluggable LLM backends
  └── SlackOrganizerApp (main thread, rumps)
        └── reads from storage.py every 5 s
```
