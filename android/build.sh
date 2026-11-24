#!/bin/bash
# Builds and installs the bridge APK onto the USB-connected phone, then grants its SMS permissions.
set -euo pipefail
cd "$(dirname "$0")"
export JAVA_HOME="$(brew --prefix openjdk@17)/libexec/openjdk.jdk/Contents/Home"
SDK="$HOME/Library/Android/sdk"
BT="$SDK/build-tools/34.0.0"
JAR="$SDK/platforms/android-34/android.jar"
KS="$HOME/.android/smsbridge.keystore"
rm -rf build && mkdir -p build/classes build/dex
"$JAVA_HOME/bin/javac" --release 8 -nowarn -classpath "$JAR" -d build/classes src/local/smsbridge/*.java
"$BT/d8" --min-api 24 --lib "$JAR" --output build/dex $(find build/classes -name '*.class')
"$BT/aapt2" link -I "$JAR" --manifest AndroidManifest.xml -o build/unsigned.apk
(cd build/dex && zip -q ../unsigned.apk classes.dex)
"$BT/zipalign" -f 4 build/unsigned.apk build/aligned.apk
[ -f "$KS" ] || "$JAVA_HOME/bin/keytool" -genkeypair -keystore "$KS" -storepass smsbridge \
  -alias smsbridge -keyalg RSA -keysize 2048 -validity 36500 -dname CN=smsbridge
"$BT/apksigner" sign --ks "$KS" --ks-pass pass:smsbridge --out build/smsbridge.apk build/aligned.apk
adb install -r build/smsbridge.apk
adb shell pm grant local.smsbridge android.permission.READ_SMS
adb shell pm grant local.smsbridge android.permission.SEND_SMS
