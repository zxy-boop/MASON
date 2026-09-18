#!/bin/sh
# MASON installer for Linux: copies the unpacked bundle to a directory of your choice and adds a `MASON` command.
#   ./install.sh                 -> installs to ~/.local/opt/MASON and links ~/.local/bin/MASON
#   ./install.sh /opt/MASON      -> installs to /opt/MASON (run with sudo) and links /usr/local/bin/MASON
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="${1:-$HOME/.local/opt/MASON}"
if [ -w "$(dirname "$DEST")" ] 2>/dev/null || mkdir -p "$(dirname "$DEST")" 2>/dev/null; then :; else echo "cannot create $(dirname "$DEST"); run with sudo or choose another directory"; exit 1; fi
echo "Installing MASON to $DEST ..."
rm -rf "$DEST"; mkdir -p "$DEST"
cp -R "$SRC"/. "$DEST"/
chmod +x "$DEST/MASON" "$DEST/runtime/python/bin/"* "$DEST/runtime/opencode" 2>/dev/null || true
case "$DEST" in
  "$HOME"/*) BIN="$HOME/.local/bin";;
  *)         BIN="/usr/local/bin";;
esac
mkdir -p "$BIN" && printf '#!/bin/sh\nexec "%s/MASON" "$@"\n' "$DEST" > "$BIN/MASON" && chmod +x "$BIN/MASON"
APPS="$HOME/.local/share/applications"; mkdir -p "$APPS"
cat > "$APPS/mason.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=MASON
Comment=Atomistic structure builder operated in natural language
Exec=$DEST/MASON
Terminal=true
Categories=Science;Education;
DESK
echo "Installed. Command: $BIN/MASON   (make sure $BIN is on your PATH)"
echo "Uninstall: $DEST/uninstall.sh"
cat > "$DEST/uninstall.sh" <<UN
#!/bin/sh
rm -f "$BIN/MASON" "$APPS/mason.desktop"
rm -rf "$DEST"
echo "MASON removed from $DEST. Your configuration (~/.config/MASON) and structures (the workspace you chose) were kept."
UN
chmod +x "$DEST/uninstall.sh"
