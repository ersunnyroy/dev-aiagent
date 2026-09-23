# dev-agent: Suni's AI developer on the Mac

An always-on daemon that works like a junior developer who reports to you on your phone:

```
 8 AM call ─┐                                   ┌─ ❓ questions ──► Telegram (you answer)
 1 PM call ─┼─► record ─► Whisper ─► Claude ─► tasks ─► branch/worktree ─► implement ─► tests
 any call  ─┘   (local)   (Hindi/En)  MoM + plan          (per repo, from origin/main)     │
                                                                                            ▼
 Teams chats ─► triage (Haiku) ─► 🚨/💬 alerts on phone          2 independent reviews + screenshots
                                                                                            │
                         Teams "v4-app" ◄── PR message ◄── Bitbucket PRs ◄── push ◄── ✅ your approval
```

**Two identities, never mixed:**
- This tool's own code → GitHub `ersunnyroy/dev-aiagent`. Set up by `scripts/setup-github.sh` with a repo-local identity and its own SSH key.
- Work done by the agent → your Bitbucket repos as **suni.kumar@qualibar.com**. Commits use `git.author_email` from config, and PRs use that account's API token.

**Guardrails in code:**
- Claude can't commit, push, merge, switch branches, edit `.env`, run destructive migrations, or call external APIs. A hook blocks these, and each block has been tested live.
- The orchestrator commits and pushes only after you tap **Approve**.
- It never pushes `main`/`master` and never merges PRs.

## 1. Install (≈15 min)

```bash
cd ~/Projects/dev-aiagent
./scripts/setup-github.sh        # this repo commits/pushes as ersunnyroy (not your Bitbucket identity)
./install.sh                     # brew deps, venv, whisper model, launchd agent
```

### Secrets (stored in the macOS Keychain, never in files)
| Secret | Where to get it |
|---|---|
| `telegram_bot_token` | Telegram → @BotFather → `/newbot` |
| `bitbucket_email` (`suni.kumar@qualibar.com`) + `bitbucket_api_token` | Signed in as suni.kumar@qualibar.com: id.atlassian.com → Security → *Create API token with scopes*. Choose Bitbucket with repository read/write and pull request read/write. |
| `anthropic_api_key` | console.anthropic.com. Recommended for an always-on agent. Alternatively run `claude login` once. |
| `teams_webhook_url` *(optional)* | Fallback if Graph posting is blocked. In Teams, open the chat → Workflows → "Post to a chat when a webhook request is received". |

```bash
devagent secrets set telegram_bot_token
devagent secrets set bitbucket_email
devagent secrets set bitbucket_api_token
devagent secrets set anthropic_api_key
```

### Config: `~/.devagent/config.yaml`
- `workspace.repos_root`: the folder containing your clones (`viam`, `vcommons`, `vmigration`, `vmanagement`, …).
- `repos`: one entry per repo, with its Bitbucket `workspace/slug`, setup commands, test command, and whether it has UI.
- `teams.pr_reviewers`: display names exactly as they appear in Teams. Check them with `devagent teams check`.
- `engine.models`: routing. Haiku does triage. Sonnet does planning and normal tasks. Opus does complex tasks and reviews.
- Copy `docs/repo-CLAUDE.md.example` into each work repo as `CLAUDE.md` and fill it in. It's the biggest quality lever.
- **Test DB:** make sure `.env.testing` points at a separate DB (e.g. `v4_testing` on XAMPP MySQL). The agent runs tests there, never on your dev data.

### Teams
```bash
devagent teams login     # device-code sign-in in the browser, as you
devagent teams check     # finds the "v4-app" chat and verifies reviewer names
```
If your tenant blocks the default public client, register your own app in Entra ID. Use delegated permissions `Chat.ReadWrite`, `ChatMessage.Send` and `User.Read`, and enable "Allow public client flows". Put its id in `teams.client_id`.

### Meeting audio (one-time, 3 min)
Recording happens locally. ffmpeg captures your mic plus what you hear. Nothing is added to the Teams call.
1. Open **Audio MIDI Setup** → **+** → **Create Multi-Output Device**. Tick your headphones/speakers **and** BlackHole 2ch. Set this device as the system (and Teams) speaker.
2. **+** → **Create Aggregate Device**. Tick the MacBook microphone (or your headset mic) **and** BlackHole 2ch. Rename it **`DevAgent Capture`**.
3. In Teams, keep your normal mic. Set the speaker to the Multi-Output device.
4. `devagent doctor` should show ✅ for `input device 'DevAgent Capture'`.

### Run
```bash
devagent doctor          # everything ✅?
devagent run             # first time in Terminal → macOS asks for Microphone permission → Allow
# on Telegram: send /start to your bot (pairs your phone; only that chat is obeyed)
launchctl load -w ~/Library/LaunchAgents/com.suni.devagent.plist   # background, auto-start at login
```
Logs are in `~/.devagent/devagent.log`. To stop: `launchctl unload ~/Library/LaunchAgents/com.suni.devagent.plist`.

## 2. Daily flow

| Time | What happens |
|---|---|
| 07:58 | The recording window opens. When Teams starts the call, recording starts automatically. |
| After the 8 AM call | You get the MoM (.md file), a day plan and "Tasks for you" with buttons: **Add all & start**, **Pick by number** or **Ignore**. |
| During the day | Each task is analysed, then any questions come to your phone (business 💼 / technical 🔧). Tap an option or reply. The agent then branches, implements, tests and runs 2 independent reviews. You get screenshots, the diff and test results. |
| Your review | Tap **Approve**: commit → push → Bitbucket PR (…/pull-requests/N/diff) → Teams message preview → **Post**. Or tap **Request changes** and reply with feedback and code snippets. It reworks, re-tests, runs 1 more review and sends everything back. |
| 12:58 | The standup is recorded. You get proposed task-list changes, with an **Apply** button. |
| Any call | Recorded automatically (`auto_record_any_call`), or with `/rec title` and `/stop`. |
| All day | Teams messages are triaged. Only 🚨 urgent and 💬 needs-reply messages reach your phone, with a suggested reply you can send in one tap, or turn into a task. |

**Telegram commands:** `/task`, `/tasks`, `/status`, `/plan`, `/cost`, `/rec`, `/stop`, `/approve id`, `/changes id text`, `/retry id`, `/cancel id`, `/prio id n`, `/teamslogin`.

**Hotkey recording:** the daemon listens on `localhost:8765`. Create a macOS Shortcut with the "Run Shell Script" action: `curl -s localhost:8765/rec/start?title=Call`, and another for `/rec/stop`. Assign keyboard shortcuts to both.

**Test the meeting flow without a call:** `devagent transcribe some-notes.txt --kind priorities`

## 3. The Teams PR message
The template is `devagent/templates/pr_message.j2`. It reproduces your format:
```
Hi
@Puneet Chelani @Virendra Pratap Singh @Mithun M P

Please review and merge the following PR's related to <topic> :

1. https://bitbucket.org/qualibar/viam/pull-requests/249/diff
2. https://bitbucket.org/qualibar/vcommons/pull-requests/803/diff
```
Names become real @mentions when posted through Graph. Preview it with `devagent pr-message <task-id>`.

## 4. Layout
```
devagent/
  daemon.py        loops: pipeline worker, meeting scheduler, Teams watcher, localhost control
  pipeline.py      analyze → questions → implement → tests → 2 reviews → approval → PR → Teams
  engine.py        Claude Agent SDK wrapper + guardrail hook (deny list for Bash/Write)
  gitops.py        fetch/pull main, branch + worktree per task per repo, commit/push
  meetings.py      MoM/day plan, standup diff, auto-record windows
  capture/         ffmpeg recorder, call detection, whisper.cpp transcription
  bridges/         telegram_bot.py · teams.py (Graph) · prs.py (Bitbucket/GitHub) · havana.py (stub)
  prompts/*.md     editable instructions for each step
  templates/       PR message, Telegram review card, MoM
tests/             35 offline tests (git worktrees, full pipeline with fake Claude, bridges)
```

## 5. Havana (later)
`bridges/havana.py` holds the interface: `fetch_assigned()` and `update_status()`. The daemon already polls it every 5 minutes and pushes PR links back when `havana.enabled: true`. Only the two HTTP calls need filling in once the API docs are available.

## 6. Known limits
- **Call auto-detect** reads macOS power assertions (Teams holds one during calls). Recording starts after about 20 s in a call and stops about 60 s after the call ends. It's a heuristic, so `/rec` and `/stop` always work as overrides.
- **Microphone permission under launchd:** if background recordings come out silent, open System Settings → Privacy → Microphone and allow `ffmpeg`/`python`. Or run `devagent run` from Terminal once.
- **Teams triage** covers 1:1, group and meeting chats. Channel posts would need admin-consented permissions.
- **Mac sleep:** the lid must stay open, or run in clamshell mode with power connected. `caffeinate` prevents idle sleep only.
- **Cost:** each run is capped by `max_budget_usd_per_task`. Use `/cost` for today's spend.
