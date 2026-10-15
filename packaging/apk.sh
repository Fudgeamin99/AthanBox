#!/usr/bin/env bash
# Puts the Android TV app (AzanTV.apk) at the given path, for a release and the Docker image:
#   packaging/apk.sh azan_sip/static/AzanTV.apk
# It's built from android/ (android/build.sh) and signed with the app's key, from the ANDROID_KEYSTORE
# and ANDROID_KEYSTORE_PASSWORD secrets. Android installs an update only over an app signed with the same
# key, so without them nothing is built: the copy attached to the newest AthanBox release that has one
# is used again (GH_TOKEN). A build that fails is an error; finding no copy at all is only a warning,
# and then no file is made (/apk sends screens to GitHub).
set -euo pipefail

dest="${1:?usage: packaging/apk.sh path/AzanTV.apk}"
here="$(cd "$(dirname "$0")/.." && pwd)"
repo="${GITHUB_REPOSITORY:-Fudgeamin99/azan-sip-box}"
mkdir -p "$(dirname "$dest")"

if [ -n "${ANDROID_KEYSTORE:-}" ]; then
  REQUIRE_KEY=1 "$here/android/build.sh" "$dest"
  exit 0
fi

echo "::warning::No ANDROID_KEYSTORE secret, so the Android app isn't built here: using the newest release's copy"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
prev=$(gh api "repos/$repo/releases?per_page=50" \
       --jq '[.[] | select(.draft | not) | select(any(.assets[]; .name == "AzanTV.apk"))][0].tag_name // ""' || true)
if [ -n "$prev" ] && gh release download "$prev" -R "$repo" -p AzanTV.apk -D "$tmp"; then
  mv "$tmp/AzanTV.apk" "$dest"
  echo "Using the AzanTV.apk attached to $prev"
else
  echo "::warning::No AthanBox release has AzanTV.apk yet, so there's no Android app to add"
fi
