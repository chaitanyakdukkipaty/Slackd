# Slackd 🔔

A macOS menu bar app that reads your Slack desktop notifications in real-time, uses an LLM to group them into conversation threads, and surfaces them ordered by latest activity with colour-coded urgency.

```
🔔 3
────────────────────────────────────────────────
● 🔴  [#team-qa-preprod-deploy]  Mayur: @pcp-ops server 500  ▶
● 🟠  [DM]  Alice: can we connect?                            ▶
○ ⚪   [#general]  Carol: lunch at 12pm                        ▶
────────────────────────────────────────────────
Mark all read
Delete all threads
⚙  Settings  ▶
────────────────────────────────────────────────
✗  Quit
```

---

## Quick Install

```bash
curl -fsSL https://raw.githubusercontent.com/chaitanyakdukkipaty/Slackd/main/install.sh | bash
```

Then start it:

```bash
slackd
```

> The install script clones the repo to `~/.slackd`, creates a virtualenv, installs all deps, and adds `slackd` to `~/.local/bin`.

---

## Requirements

| Requirement | Notes |
|---|---|
| macOS 13 (Ventura) or later | Uses macOS 15 Notification Center Accessibility API |
| Python 3.9+ | System Python on macOS works; `brew install python@3.12` recommended |
| [GitHub CLI](https://cli.github.com/) (`gh`) authenticated | Required for Copilot LLM backend (default) |
| Slack desktop app | Must be installed with Alert-style notifications |

---

## Required macOS Permissions

### 1. Accessibility (required)

Slackd reads notification banners from `NotificationCenter.app` via the Accessibility API.

1. **System Settings → Privacy & Security → Accessibility**
2. Click **+** and add your **Terminal** app (Terminal.app, iTerm2, Warp, etc.)
3. Enable the toggle

### 2. Slack notification style (required)

Slack must use **Alerts** (persistent banners), not Banners (auto-dismiss):

1. **System Settings → Notifications → Slack**
2. Set **Alert style** to **Alerts** (the square icon)

---

## Manual Installation

```bash
# Clone
git clone https://github.com/chaitanyakdukkipaty/Slackd.git ~/.slackd
cd ~/.slackd

# Virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Authenticate GitHub CLI (for Copilot LLM backend)
gh auth login

# Run
python main.py
```

---

## Usage

A **🔔** icon appears in your menu bar. Click it to see all tracked threads.

| Element | Meaning |
|---|---|
| **● 🔴** | Unread, high urgency |
| **● 🟠** | Unread, medium urgency |
| **● 🟡** | Unread, low urgency |
| **○ ⚪** | Read |
| Click thread | Opens accordion with all messages |
| **🔗 Open in Slack — exact thread** | AX-clicks the notification → Slack opens that exact message/thread |
| **🔗 Open in Slack — search in channel** | Navigates to channel + searches for message body (⌘F) |
| Click a message row | Searches for that specific message in Slack |
| **🗑 Delete thread** | Removes thread + all its messages |
| **Mark all read** | Clears unread badge |
| **Delete all threads** | Wipes the local DB |
| **⚙ Settings → Launch at Login** | Installs/removes `~/Library/LaunchAgents/com.slackorganizer.plist` |

---

## Configuration

Edit `~/.slackd/config.yaml` (or `config.yaml` in the project root):

| Key | Default | Description |
|---|---|---|
| `llm.backend` | `copilot` | LLM backend: `copilot`, `claude`, `openai` |
| `poll_interval` | `5` | Seconds between periodic polls |
| `urgency_keywords` | (list) | Keywords that boost urgency score |
| `scoring.dm_bonus` | `3` | Score bonus for direct messages |
| `scoring.mention_bonus` | `2` | Score bonus for @mentions |
| `scoring.keyword_bonus` | `2` | Score bonus per matched keyword |
| `scoring.llm_weight` | `1.0` | Multiplier applied to LLM urgency score |
| `priority_thresholds.red` | `8` | Minimum score for 🔴 |
| `priority_thresholds.orange` | `5` | Minimum score for 🟠 |
| `priority_thresholds.yellow` | `2` | Minimum score for 🟡 |
| `max_threads_displayed` | `20` | Max threads in menu |

---

## How It Works

```
Slack notification arrives
        │
        ▼
log stream (usernoted) ──triggers──▶ NotificationCenter.app AX tree
                                              │
                                    _parse_nc_group_desc()
                                    "Slack, workspace, channel, body"
                                              │
                                    _parse_sender()
                                    "Name: message" → sender + body
                                              │
                                    ThreadOrganizer
                                    ├── Rule scoring (DM/mention/keyword)
                                    ├── LLM clustering  (assigns thread_id,
                                    │   aware of existing threads)
                                    └── LLM urgency scoring (0–10 per thread)
                                              │
                                    SQLite (data/notifications.db)
                                              │
                                    rumps menu bar (refreshes every 5s)
```

**Two LLM calls per batch:**

1. **Thread clustering** — groups messages into conversation threads, optionally merging with existing ones
2. **Urgency scoring** — scores each thread 0–10 based on action items, mentions, outages, deadlines

---

## Adding a New LLM Backend

1. Copy `src/llm/claude.py` as a template
2. Implement `ask(self, prompt: str) -> str`
3. Register it with `@BackendFactory.register("my-backend")`
4. Set `llm.backend: my-backend` in `config.yaml`

---

## Troubleshooting

**No notifications appearing**
- Grant Accessibility to your terminal in System Settings → Privacy & Security → Accessibility
- Check NC process: `python3 -c "from src.notification_watcher import _find_nc_pid; print(_find_nc_pid())"` — should print a PID, not `None`
- Set Slack to Alert style (not Banner) in System Settings → Notifications → Slack

**LLM calls failing**
- `gh auth status` — must show authenticated
- `gh copilot explain "hello"` — test the Copilot CLI works

**Two instances running**
- A PID lock at `data/slack_organizer.pid` prevents this. If stale, delete it: `rm ~/.slackd/data/slack_organizer.pid`

**App doesn't appear in menu bar**
- Must run in the activated venv; `rumps` requires native macOS Python (not conda)

---

## Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/chaitanyakdukkipaty/Slackd/main/uninstall.sh | bash
```

Or manually:
```bash
launchctl unload -w ~/Library/LaunchAgents/com.slackorganizer.plist 2>/dev/null
rm -rf ~/.slackd ~/.local/bin/slackd ~/Library/LaunchAgents/com.slackorganizer.plist
```

---

## Data & Privacy

- All data is stored locally in `data/notifications.db` (SQLite)
- Notification content is sent only to the configured LLM CLI tool (local process)
- No telemetry, no network calls except LLM
