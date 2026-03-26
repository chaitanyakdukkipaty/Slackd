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

> The install script clones the repo to `~/.slackd`, creates a virtualenv, installs all deps, and adds `slackd` to `~/.local/bin`.

---

## Post-Install Setup

Complete these steps **once** after installing before running Slackd.

### Step 1 — Grant Accessibility Permission

Slackd reads notification banners via the macOS Accessibility API.

1. Open **System Settings → Privacy & Security → Accessibility**
2. Click **+** and add your terminal app (Terminal.app, iTerm2, Warp, etc.)
3. Toggle it **on**

> **If running via `slackd` command:** you may also need to add `/usr/bin/python3` or the `.venv/bin/python3` binary.

### Step 2 — Set Slack to Alert Style

Slackd can only read notifications that stay on screen long enough to inspect.

1. **System Settings → Notifications → Slack**
2. Set **Alert style** to **Alerts** (the square icon, not the pill-shaped Banner)

### Step 3 — Set Up Your LLM Backend

Choose **one** of the following:

---

#### Option A — GitHub Copilot (default)

Requires a GitHub account with [Copilot](https://github.com/features/copilot) access.

```bash
# 1. Install GitHub CLI
brew install gh

# 2. Authenticate (choose browser or token)
gh auth login

# 3. Install the Copilot CLI extension
gh extension install github/gh-copilot

# 4. Verify it works
gh copilot explain "hello"
```

`config.yaml` default is already `llm.backend: copilot` — no further change needed.

---

#### Option B — Claude (Anthropic API)

```bash
# 1. Install the Anthropic Python SDK inside the venv
~/.slackd/.venv/bin/pip install anthropic

# 2. Set your API key (add to ~/.zshrc or ~/.bash_profile to persist)
export ANTHROPIC_API_KEY="sk-ant-..."
```

Then open `~/.slackd/src/llm/claude.py` and replace the `NotImplementedError` body with:

```python
import anthropic
client = anthropic.Anthropic()
message = client.messages.create(
    model="claude-opus-4-5",
    max_tokens=1024,
    messages=[{"role": "user", "content": prompt}]
)
return message.content[0].text
```

Set the backend in `~/.slackd/config.yaml`:

```yaml
llm:
  backend: claude
```

---

#### Option C — OpenAI API

```bash
# 1. Install OpenAI SDK inside the venv
~/.slackd/.venv/bin/pip install openai

# 2. Set your API key
export OPENAI_API_KEY="sk-..."
```

Edit `~/.slackd/src/llm/openai_cli.py` to implement `ask()` using `openai.OpenAI().chat.completions.create(...)`, then set:

```yaml
llm:
  backend: openai
```

---

### Step 4 — (Optional) Enable Launch at Login

To have Slackd start automatically after every login:

```bash
slackd
```

Then in the menu bar: **⚙ Settings → Launch at Login**

---

## Running Slackd

### Foreground (interactive / debugging)

```bash
slackd
```

### Background (detached from terminal)

```bash
nohup slackd > ~/.slackd/data/slackd.log 2>&1 &
```

The process will survive closing your terminal. Check the log:

```bash
tail -f ~/.slackd/data/slackd.log
```

Stop it:

```bash
kill $(cat ~/.slackd/data/slack_organizer.pid)
```

### Via Launch Agent (recommended for daily use)

Enable **Launch at Login** from the Settings submenu in the menu bar. Slackd will automatically start on every login, with logs at:

- `~/.slackd/data/launchagent.stdout.log`
- `~/.slackd/data/launchagent.stderr.log`

---

## Requirements

| Requirement | Notes |
|---|---|
| macOS 13 (Ventura) or later | Uses macOS 15 Notification Center Accessibility API |
| Python 3.9+ | System Python on macOS works; `brew install python@3.12` recommended |
| [GitHub CLI](https://cli.github.com/) (`gh`) + Copilot extension | Required if using Copilot backend (default) |
| Slack desktop app | Must be installed with Alert-style notifications |

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

# Run
python main.py
```

---

## Usage

A **🔔** icon appears in your menu bar. Click it to see all tracked threads.

| Element | Meaning |
|---|---|
| **● 🔴** | Unread, high urgency (set by Score Priority) |
| **● 🟠** | Unread, medium urgency |
| **● 🟡** | Unread, low urgency |
| **○ ⚪** | Read or not yet scored |
| Click thread | Opens accordion with all messages |
| **🔗 Open in Slack — exact thread** | AX-clicks the notification → Slack opens that exact message/thread |
| **🔗 Open in Slack — search in channel** | Navigates to channel + searches for message body (⌘F) |
| Click a message row | Searches for that specific message in Slack |
| **🗑 Delete thread** | Removes thread + all its messages |
| **Cluster Threads** | LLM re-groups all messages into descriptive conversation threads |
| **Score Priority** | LLM scores all threads 0–10; urgency colours update |
| **Mark all read** | Clears unread badge |
| **Delete all threads** | Wipes the local DB |
| **⚙ Settings → Launch at Login** | Installs/removes `~/Library/LaunchAgents/com.slackorganizer.plist` |
| **⚙ Settings → Prevent Sleep** | Toggles `caffeinate -i` to keep Mac awake while service runs |
| **⚙ Settings → Auto-cluster** | Off / On notification / Every 15–60 min |
| **⚙ Settings → Auto-score** | Off / On notification / Every 15–60 min |

---

## Configuration

Edit `~/.slackd/config.yaml`:

| Key | Default | Description |
|---|---|---|
| `llm.backend` | `copilot` | LLM backend: `copilot`, `claude`, `openai` |
| `llm.score_weight` | `1.0` | Multiplier applied to LLM urgency score (0–10) |
| `poll_interval` | `5` | Seconds between periodic notification polls |
| `prevent_sleep` | `true` | Run `caffeinate -i` to prevent idle sleep |
| `cluster_interval` | `0` | When to auto-cluster: `0`=manual, `-1`=on every notification, `N`=every N minutes |
| `score_interval` | `0` | When to auto-score: `0`=manual, `-1`=on every notification, `N`=every N minutes |
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
                                    ThreadOrganizer.process()
                                    channel-slug grouping (no LLM)
                                    → stored in SQLite instantly
                                              │
                                    ┌─────────┴──────────┐
                             manual │                    │ scheduled
                             button │                    │ interval / on-notification
                                    ▼                    ▼
                            cluster_all()          score_all()
                            LLM re-clusters        LLM scores each
                            all messages into      thread 0–10,
                            descriptive threads    updates priority
                                    │
                              rumps menu bar (refreshes every 5s)
```

**LLM operations are on-demand (not automatic by default):**

- **Cluster Threads** — re-groups all messages into descriptive conversation threads
- **Score Priority** — assigns urgency 0–10 to each thread; colours update (🔴🟠🟡⚪)

**Auto-schedule options** (⚙ Settings → Auto-cluster / Auto-score):
- `Off` — manual only
- `On notification` — runs after every new notification batch
- `Every 15/30/60 min` — background APScheduler job

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
- Check `~/.slackd/data/slackd.log` or the launchagent logs for errors

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


