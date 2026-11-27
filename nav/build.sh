#!/bin/bash
# Builds and installs the floating Back button onto the USB-connected phone and
# turns its accessibility service on (tap = Back, long press = Home, drag to move).
set -euo pipefail
cd "$(dirname "$0")"
export JAVA_HOME="$(brew --prefix openjdk@17)/libexec/openjdk.jdk/Contents/Home"
SDK="$HOME/Library/Android/sdk"
BT="$SDK/build-tools/34.0.0"
JAR="$SDK/platforms/android-34/android.jar"
KS="$HOME/.android/smsbridge.keystore"
rm -rf build && mkdir -p build/classes build/dex
"$JAVA_HOME/bin/javac" --release 8 -nowarn -classpath "$JAR" -d build/classes src/local/navbutton/*.java
"$BT/d8" --min-api 24 --lib "$JAR" --output build/dex $(find build/classes -name '*.class')
"$BT/aapt2" link -I "$JAR" --manifest AndroidManifest.xml -o build/unsigned.apk
(cd build/dex && zip -q ../unsigned.apk classes.dex)
"$BT/zipalign" -f 4 build/unsigned.apk build/aligned.apk
"$BT/apksigner" sign --ks "$KS" --ks-pass pass:smsbridge --out build/navbutton.apk build/aligned.apk
adb install -r build/navbutton.apk
adb shell settings put secure enabled_accessibility_services local.navbutton/local.navbutton.NavService
adb shell settings put secure accessibility_enabled 1
