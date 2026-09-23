#!/usr/bin/env bash
# Makes THIS repo commit/push as ersunnyroy on GitHub, without touching your global
# (Bitbucket) git identity. Uses a dedicated SSH key + host alias.
set -euo pipefail
cd "$(dirname "$0")/.."
KEY="${HOME}/.ssh/id_ed25519_ersunnyroy"
if [[ ! -f "${KEY}" ]]; then
  ssh-keygen -t ed25519 -C "er.sunnyroy@gmail.com" -f "${KEY}" -N ""
  echo; echo "Add this public key at https://github.com/settings/ssh/new :"; cat "${KEY}.pub"; echo
fi
if ! grep -q "Host github-ersunnyroy" "${HOME}/.ssh/config" 2>/dev/null; then
  cat >> "${HOME}/.ssh/config" <<CFG

Host github-ersunnyroy
  HostName github.com
  User git
  IdentityFile ${KEY}
  IdentitiesOnly yes
CFG
fi
[[ -d .git ]] || git init -b main
git config --local user.name "ersunnyroy"
git config --local user.email "er.sunnyroy@gmail.com"
git remote remove origin 2>/dev/null || true
git remote add origin git@github-ersunnyroy:ersunnyroy/dev-aiagent.git
echo "✅ Local identity = ersunnyroy; origin = github-ersunnyroy:ersunnyroy/dev-aiagent.git"
echo "   After adding the key on GitHub:  ssh -T git@github-ersunnyroy  &&  git push -u origin main"
