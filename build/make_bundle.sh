#!/bin/zsh
# Assemble the Linux (x64) tarball and the Windows (x64) zip on macOS:
# portable CPython (python-build-standalone) + wheels installed for the target platform via pip --platform.
set -e
export COPYFILE_DISABLE=1   # no AppleDouble ._ files in the archives
setopt nullglob
APP=/Users/joyzhang/Projects/mason-app; V=1.0.2; PY=$APP/build/macos-arm64/runtime/python/bin/python3
build_target() {   # $1 target name, $2 pbs tarball, $3 pip platform, $4 opencode tgz, $5 site-packages relpath
  T=$1; B=$APP/build/$T; rm -rf $B; mkdir -p $B/runtime $B/dist
  tar xzf $APP/cache/$2 -C $B/runtime           # -> $B/runtime/python
  SP=$B/runtime/python/$5; mkdir -p $SP
  # wheels for the target platform (binary only), plus the pure-python mason_mcp package
  $PY -m pip install -q --target "$SP" --platform $3 --python-version 3.12 --only-binary=:all: --implementation cp \
      "mcp>=1.0,<2" "pymatgen>=2024.1" "ase>=3.22" "numpy>=1.24" "scipy>=1.10" 2>&1 | grep -v "notice" || true
  $PY -m pip install -q --target "$SP" --no-deps $APP 2>&1 | grep -v notice || true
  tmp=$(mktemp -d); tar xzf $APP/cache/$4 -C $tmp
  if [ -f $tmp/package/bin/opencode.exe ]; then cp $tmp/package/bin/opencode.exe $B/runtime/opencode.exe; else cp $tmp/package/bin/opencode $B/runtime/opencode; chmod +x $B/runtime/opencode; fi
  rm -rf $tmp
  cp -R $APP/launcher $B/launcher; cp -R $APP/docs $B/docs; cp -R $APP/examples $B/examples
  find $B/runtime/python -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
}
# ---------------- Linux x64
build_target linux-x64 cpython-3.12.14+20260901-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz manylinux2014_x86_64 opencode-linux-x64-1.18.26.tgz lib/python3.12/site-packages
cat > $APP/build/linux-x64/MASON <<'EOF'
#!/bin/sh
# Resolve symlinks so that the command works from ~/.local/bin or /usr/local/bin as well.
SELF="$0"; while [ -L "$SELF" ]; do L="$(readlink "$SELF")"; case "$L" in /*) SELF="$L";; *) SELF="$(dirname "$SELF")/$L";; esac; done
DIR="$(cd "$(dirname "$SELF")" && pwd)"
export MASON_HOME="$DIR"
exec "$DIR/runtime/python/bin/python3" "$DIR/launcher/mason.py" "$@"
EOF
chmod +x $APP/build/linux-x64/MASON
cp $APP/build/linux/install.sh $APP/build/linux-x64/install.sh; chmod +x $APP/build/linux-x64/install.sh
(cd $APP/build && mkdir -p linux-x64/dist && rm -f linux-x64/dist/MASON-$V-linux-x64.tar.gz && tar czf linux-x64/dist/MASON-$V-linux-x64.tar.gz --exclude='linux-x64/dist' -s '|^linux-x64|MASON-'$V'-linux-x64|' linux-x64)
# ---------------- Windows x64
build_target windows-x64 cpython-3.12.14+20260901-x86_64-pc-windows-msvc-install_only_stripped.tar.gz win_amd64 opencode-windows-x64-1.18.26.tgz Lib/site-packages
cat > $APP/build/windows-x64/MASON.bat <<'EOF'
@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set MASON_HOME=%~dp0
"%~dp0runtime\python\python.exe" "%~dp0launcher\mason.py" %*
pause
EOF
(cd $APP/build/windows-x64 && mkdir -p dist && rm -f dist/MASON-$V-windows-x64.zip && mkdir -p build/windows && cp $APP/build/windows/make_exe.ps1 $APP/build/windows/MASON.iss build/windows/ && zip -q -r dist/MASON-$V-windows-x64.zip runtime launcher docs examples build MASON.bat)
du -sh $APP/build/linux-x64/dist/* $APP/build/windows-x64/dist/*
