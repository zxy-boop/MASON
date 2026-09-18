#!/bin/zsh
# Assemble MASON.app (arm64) and a dmg. Run on macOS. Requires: cache/ downloads, build/macos-arm64/runtime/python prepared.
set -e
APP=/Users/joyzhang/Projects/mason-app; B=$APP/build/macos-arm64; V=1.0.2
mkdir -p $B/runtime $B/dist
# opencode binary
if [ ! -x $B/runtime/opencode ]; then
  tmp=$(mktemp -d); tar xzf $APP/cache/opencode-darwin-arm64-1.18.26.tgz -C $tmp
  cp $tmp/package/bin/opencode $B/runtime/opencode; chmod +x $B/runtime/opencode; rm -rf $tmp
fi
# bundle skeleton
BUNDLE=$B/dist/MASON.app; rm -rf $BUNDLE; mkdir -p $BUNDLE/Contents/MacOS $BUNDLE/Contents/Resources
R=$BUNDLE/Contents/Resources
cp -R $B/runtime $R/runtime
cp -R $APP/launcher $R/launcher; cp -R $APP/docs $R/docs; cp -R $APP/examples $R/examples
# the .app executable opens a Terminal window running the launcher (the setup wizard needs a terminal)
cat > $BUNDLE/Contents/MacOS/MASON <<'EOF'
#!/bin/zsh
DIR="$(cd "$(dirname "$0")/../Resources" && pwd)"
open -a Terminal "$DIR/MASON.command"
EOF
chmod +x $BUNDLE/Contents/MacOS/MASON
cat > $R/MASON.command <<'EOF'
#!/bin/zsh
DIR="$(cd "$(dirname "$0")" && pwd)"
export MASON_HOME="$DIR"
exec "$DIR/runtime/python/bin/python3" "$DIR/launcher/mason.py" "$@"
EOF
chmod +x $R/MASON.command
cat > $BUNDLE/Contents/Info.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>MASON</string><key>CFBundleDisplayName</key><string>MASON</string>
<key>CFBundleIdentifier</key><string>org.mason.desktop</string><key>CFBundleVersion</key><string>$V</string>
<key>CFBundleShortVersionString</key><string>$V</string><key>CFBundleExecutable</key><string>MASON</string>
<key>CFBundlePackageType</key><string>APPL</string><key>LSMinimumSystemVersion</key><string>12.0</string>
<key>CFBundleIconFile</key><string>MASON.icns</string>
</dict></plist>
EOF
[ -f $APP/build/MASON.icns ] && cp $APP/build/MASON.icns $R/MASON.icns
# clean-up of the python runtime inside the bundle (tests, caches)
find $R/runtime/python -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
# dmg
DMG=$B/dist/MASON-$V-macos-arm64.dmg; rm -f $DMG
stage=$(mktemp -d); cp -R $BUNDLE $stage/; ln -s /Applications $stage/Applications
hdiutil create -volname "MASON $V" -srcfolder $stage -ov -format UDZO -quiet $DMG; rm -rf $stage
du -sh $BUNDLE $DMG
