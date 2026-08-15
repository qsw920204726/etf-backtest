#!/bin/bash
# 手工打包 APK（无需 Gradle/Android Studio）
# 依赖: JDK17 + Android SDK (platforms;android-34, build-tools;34.0.0)
# 用法: bash app/build_apk.sh
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/app"
BUILD="$APP/build"
ANDROID_HOME="${ANDROID_HOME:-C:/android-build/sdk}"
BT="$ANDROID_HOME/build-tools/34.0.0"
PLATFORM="$ANDROID_HOME/platforms/android-34/android.jar"
JAVA_HOME="${JAVA_HOME:-C:/android-build/jdk-17.0.20+8}"
export JAVA_HOME
export PATH="$JAVA_HOME/bin:$PATH"

rm -rf "$BUILD"; mkdir -p "$BUILD/classes"

echo "== 1. javac 编译 =="
"$JAVA_HOME/bin/javac" --release 8 -classpath "$PLATFORM" \
  -d "$BUILD/classes" "$APP/src/com/qsw/etfbacktest/MainActivity.java"

echo "== 2. d8 转 dex =="
"$JAVA_HOME/bin/java" -cp "$BT/lib/d8.jar" com.android.tools.r8.D8 \
  --release --lib "$PLATFORM" --output "$BUILD" "$BUILD/classes/com/qsw/etfbacktest/"*.class

echo "== 3. aapt2 打包资源+assets =="
"$BT/aapt2.exe" link -o "$BUILD/app.base.apk" -I "$PLATFORM" \
  --manifest "$APP/AndroidManifest.xml" -A "$APP/assets" \
  --min-sdk-version 24 --target-sdk-version 34

echo "== 4. 注入 classes.dex =="
APK_WIN=$(cygpath -w "$BUILD/app.base.apk"); DEX_WIN=$(cygpath -w "$BUILD/classes.dex")
py -c "import zipfile,sys; z=zipfile.ZipFile(sys.argv[1],'a',zipfile.ZIP_DEFLATED); z.write(sys.argv[2],'classes.dex'); z.close()" "$APK_WIN" "$DEX_WIN"

echo "== 5. 对齐 =="
"$BT/zipalign.exe" -f 4 "$BUILD/app.base.apk" "$BUILD/app.aligned.apk"

echo "== 6. 签名 =="
KS="$APP/keystore.jks"
if [ ! -f "$KS" ]; then
  "$JAVA_HOME/bin/keytool" -genkeypair -keystore "$KS" -alias etf \
    -keyalg RSA -keysize 2048 -validity 10950 -storepass etf123456 -keypass etf123456 \
    -dname "CN=ETF Backtest, OU=Personal, O=qsw, C=CN"
fi
"$BT/apksigner.bat" sign --ks "$KS" --ks-pass pass:etf123456 --key-pass pass:etf123456 \
  --out "$ROOT/ETF回测.apk" "$BUILD/app.aligned.apk"
"$BT/apksigner.bat" verify "$ROOT/ETF回测.apk"
echo "== 完成: $ROOT/ETF回测.apk ($(du -h "$ROOT/ETF回测.apk" | cut -f1)) =="
