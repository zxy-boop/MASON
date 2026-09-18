"""Tiny stub that PyInstaller turns into MASON.exe: sets MASON_HOME to the folder of the exe and runs the launcher
with the bundled Python, so the exe stays independent of the Python packaged inside."""
import os, sys, subprocess
root = os.path.dirname(os.path.abspath(sys.executable))
os.environ["MASON_HOME"] = root
os.environ["PYTHONUTF8"] = "1"
py = os.path.join(root, "runtime", "python", "python.exe")
sys.exit(subprocess.call([py, os.path.join(root, "launcher", "mason.py"), *sys.argv[1:]]))
