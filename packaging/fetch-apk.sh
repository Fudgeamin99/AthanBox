#!/usr/bin/env bash
# Fetches the Android TV app (AzanTV.apk) for AthanBox's releases and Docker image:
#   packaging/fetch-apk.sh azan_sip/static/AzanTV.apk
# The app is built in Fudgeamin99/ATHAN-TO-SIP, which is private, so its newest release can only be read
# with a token: APP_TOKEN (the ATHAN_TO_SIP_TOKEN secret, a token that can read that repository's
# contents). Without one, it takes the copy attached to the newest AthanBox release that has it
# (GH_TOKEN, this repository), so a release never ends up without the app.
set -uo pipefail

dest="${1:?usage: packaging/fetch-apk.sh path/AzanTV.apk}"
repo="${GITHUB_REPOSITORY:-Fudgeamin99/azan-sip-box}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$(dirname "$dest")"

if [ -n "${APP_TOKEN:-}" ]; then
  if GH_TOKEN="$APP_TOKEN" gh release download -R Fudgeamin99/ATHAN-TO-SIP -p AzanTV.apk -D "$tmp/app"; then
    mv "$tmp/app/AzanTV.apk" "$dest"
    echo "Got AzanTV.apk from Fudgeamin99/ATHAN-TO-SIP's latest release"
    exit 0
  fi
  echo "::warning::The ATHAN_TO_SIP_TOKEN secret couldn't read Fudgeamin99/ATHAN-TO-SIP's latest release (expired, or no access to that repository?)"
else
  echo "::warning::No ATHAN_TO_SIP_TOKEN secret, so the newest Android app can't be fetched from Fudgeamin99/ATHAN-TO-SIP (private)"
fi

prev=$(gh api "repos/$repo/releases?per_page=50" \
       --jq '[.[] | select(.draft | not) | select(any(.assets[]; .name == "AzanTV.apk"))][0].tag_name // ""')
if [ -n "$prev" ] && gh release download "$prev" -R "$repo" -p AzanTV.apk -D "$tmp/box"; then
  mv "$tmp/box/AzanTV.apk" "$dest"
  echo "Using the AzanTV.apk attached to $prev"
  exit 0
fi
echo "::warning::Couldn't get AzanTV.apk at all - /apk will send screens to GitHub"
exit 1
