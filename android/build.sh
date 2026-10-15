#!/usr/bin/env bash
# Builds the Android app (Azan SIP TV) without Gradle: it's a few small Java classes.
#   android/build.sh out/AzanTV.apk
# Needs a JDK (17) and the Android SDK (ANDROID_HOME, with build-tools and a platform), as on GitHub's
# ubuntu runners. The version is android:versionCode / versionName in AndroidManifest.xml.
#
# Signing: with the key in ANDROID_KEYSTORE (the keystore file, base64) and ANDROID_KEYSTORE_PASSWORD.
# Android installs an update only over an app signed with the same key, so every APK people get must
# be signed with that one key ("Update the app" relies on it). Without them the APK is signed with a
# new throwaway key, for trying a build out; REQUIRE_KEY=1 makes that an error instead.
set -euo pipefail

out="${1:?usage: android/build.sh out/AzanTV.apk}"
here="$(cd "$(dirname "$0")" && pwd)"
SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}"
[ -n "$SDK" ] || { echo "::error::ANDROID_HOME isn't set (the Android SDK)"; exit 1; }
BT="$SDK/build-tools/$(ls "$SDK/build-tools" | sort -V | tail -n1)"
PLAT="$(ls -d "$SDK"/platforms/android-* | grep -E 'android-[0-9]+$' | sort -V | tail -n1)"
JAR="$PLAT/android.jar"
code=$(sed -n 's/.*android:versionCode="\([0-9]*\)".*/\1/p' "$here/AndroidManifest.xml")
name=$(sed -n 's/.*android:versionName="\([^"]*\)".*/\1/p' "$here/AndroidManifest.xml")
echo "Building Azan SIP TV $name (version code $code) with $(basename "$BT") build tools and $(basename "$PLAT")"

build="$(mktemp -d)"
trap 'rm -rf "$build"' EXIT
mkdir -p "$build/gen" "$build/classes" "$build/dex" "$(dirname "$out")"

"$BT/aapt2" compile --dir "$here/res" -o "$build/res.zip"
"$BT/aapt2" link -o "$build/app.apk" -I "$JAR" --manifest "$here/AndroidManifest.xml" -R "$build/res.zip" \
  -A "$here/assets" --java "$build/gen" --auto-add-overlay --min-sdk-version 21 --target-sdk-version 34
javac -proc:none -source 8 -target 8 -bootclasspath "$JAR" -classpath "$JAR" -Xlint:-options \
  -d "$build/classes" $(find "$here/src" "$build/gen" -name '*.java')
"$BT/d8" --release --min-api 21 --lib "$JAR" --output "$build/dex" $(find "$build/classes" -name '*.class')
(cd "$build/dex" && zip -q -j ../app.apk classes.dex)
"$BT/zipalign" -p -f 4 "$build/app.apk" "$build/aligned.apk"

if [ -n "${ANDROID_KEYSTORE:-}" ]; then
  printf '%s' "$ANDROID_KEYSTORE" | base64 -d > "$build/release.jks"
  pass="${ANDROID_KEYSTORE_PASSWORD:?ANDROID_KEYSTORE_PASSWORD is needed with ANDROID_KEYSTORE}"
  echo "Signing with the ANDROID_KEYSTORE key"
elif [ "${REQUIRE_KEY:-0}" = 1 ]; then
  echo "::error::No ANDROID_KEYSTORE secret. Released apps must be signed with that key, or TVs can't update them."
  exit 1
else
  echo "::warning::No ANDROID_KEYSTORE secret, so this APK is signed with a throwaway key: fine for trying it out, but it won't install over another build."
  pass=$(openssl rand -hex 16)
  keytool -genkeypair -keystore "$build/release.jks" -storetype PKCS12 -storepass "$pass" -keypass "$pass" \
    -alias azantv -keyalg RSA -keysize 2048 -validity 12000 -dname "CN=Azan SIP TV (throwaway)" >/dev/null
fi
"$BT/apksigner" sign --ks "$build/release.jks" --ks-pass "pass:$pass" --key-pass "pass:$pass" \
  --out "$out" "$build/aligned.apk"

"$BT/apksigner" verify --print-certs "$out"
"$BT/aapt2" dump badging "$out" | head -n 1
ls -l "$out"
