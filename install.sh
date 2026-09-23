#!/usr/bin/env bash
# dev-agent installer for macOS (Apple Silicon). Safe to re-run.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DA_HOME="${DEVAGENT_HOME:-$HOME/.devagent}"
VENV="$DA_HOME/venv"
PLIST="$HOME/Library/LaunchAgents/com.suni.devagent.plist"

say() { printf "\n\033[1;36m▶ %s\033[0m\n" "$*"; }

[[ "$(uname)" == "Darwin" ]] || { echo "This installer targets macOS. On Linux: python -m venv, pip install -e ., and use systemd (see README)."; exit 1; }
command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh"; exit 1; }

say "Installing system tools (python, ffmpeg, whisper.cpp, gh)…"
brew install python@3.12 ffmpeg whisper-cpp gh >/dev/null
if ! system_profiler SPAudioDataType 2>/dev/null | grep -q "BlackHole 2ch"; then
  say "Installing BlackHole 2ch (virtual audio device for meeting capture — may ask for your password)…"
  brew install --cask blackhole-2ch || echo "⚠️  BlackHole install failed — install it manually later."
fi

say "Creating Python environment in $VENV…"
mkdir -p "$DA_HOME" && chmod 700 "$DA_HOME"
"$(brew --prefix)/bin/python3.12" -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -e "$HERE"
ln -sf "$VENV/bin/devagent" "$(brew --prefix)/bin/devagent"

if [[ ! -f "$DA_HOME/config.yaml" ]]; then
  cp "$HERE/config.example.yaml" "$DA_HOME/config.yaml"
  say "Config created at $DA_HOME/config.yaml — edit repos_root and repo list."
fi

"$HERE/scripts/get-whisper-model.sh" "$DA_HOME/models"

say "Installing launchd agent (auto-start at login, keeps Mac awake while running)…"
mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s|__DEVAGENT__|$VENV/bin/devagent|g" \
    -e "s|__HOME__|$HOME|g" \
    -e "s|__DA_HOME__|$DA_HOME|g" \
    -e "s|__PATH__|$VENV/bin:$(brew --prefix)/bin:/Applications/XAMPP/xamppfiles/bin:$PATH|g" \
    "$HERE/launchd/com.suni.devagent.plist" > "$PLIST"

cat <<EOF

✅ Installed. Next steps (details in README.md):

  1. Secrets (stored in macOS Keychain):
       devagent secrets set telegram_bot_token     # from @BotFather
       devagent secrets set bitbucket_email        # your Atlassian account email
       devagent secrets set bitbucket_api_token    # Atlassian API token (repo + PR scopes)
       devagent secrets set anthropic_api_key      # or run \`claude login\` instead
  2. Edit  $DA_HOME/config.yaml  (repos_root, repos, reviewers)
  3. Teams sign-in:            devagent teams login && devagent teams check
  4. Audio: create the "DevAgent Capture" aggregate device (README → Meeting audio)
  5. Check everything:         devagent doctor
  6. First run in Terminal (grants mic permission), send /start to your bot:
                               devagent run
  7. Then run it in the background for good:
       launchctl load -w $PLIST
EOF
