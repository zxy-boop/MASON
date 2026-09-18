"""Start a MASON bundle the way a user would (wizard with 'configure later', then launch) and check, without any language
model, that opencode serves, the MASON MCP server is connected and the viewer answers.
Usage: python smoke_launcher.py --launcher <MASON.command|MASON.bat|MASON> --python <bundled python> --home <dir> --port 4097"""
import argparse, json, os, platform, subprocess, sys, time, urllib.request, urllib.parse, base64, signal
ap = argparse.ArgumentParser(); ap.add_argument("--launcher", required=True); ap.add_argument("--python", required=True)
ap.add_argument("--home", required=True); ap.add_argument("--port", type=int, default=4097); a = ap.parse_args()
WIN = platform.system() == "Windows"
env = dict(os.environ, MASON_USER_DIR=a.home, MASON_WORKSPACE=os.path.join(a.home, "workspace"), BROWSER="true")
def launch(args, **kw):
    cmd = [a.launcher] + args
    if WIN and a.launcher.lower().endswith(".bat"):   # the .bat ends with `pause`; call the launcher module directly instead
        root = os.path.dirname(os.path.abspath(a.launcher)); env["MASON_HOME"] = root
        cmd = [a.python, os.path.join(root, "launcher", "mason.py")] + args
    return subprocess.Popen(cmd, env=env, **kw)
p = launch(["--setup-only"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
out, _ = p.communicate("14\n", timeout=120); print("wizard rc", p.returncode, out[-200:].replace("\n", " "))
cfg = json.load(open(os.path.join(a.home, "opencode.json"))); assert "structure" in cfg["mcp"], cfg
log = open(os.path.join(a.home, "launcher.log"), "w")
kw = dict(stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
if WIN: kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
else: kw["start_new_session"] = True
proc = launch(["--port", str(a.port)], **kw)
W = os.path.join(a.home, "workspace"); ok = False; B = WB = None
for _ in range(180):
    time.sleep(1)
    if proc.poll() is not None: break
    try:
        ports = json.load(open(os.path.join(a.home, "run.json")))
        B = f"http://127.0.0.1:{ports['opencode']}"; WB = f"http://127.0.0.1:{ports['workbench']}"
        urllib.request.urlopen(B + "/global/health", timeout=2); ok = True; break
    except Exception: pass
print("ports:", B, WB)
print("server up:", ok, "launcher alive:", proc.poll() is None)
result = {"up": ok}
try:
    if ok:
        mcp = None
        for _ in range(30):
            try:
                st = json.loads(urllib.request.urlopen(B + "/mcp?" + urllib.parse.urlencode({"directory": W}), timeout=20).read())
                mcp = {k: (v.get("status") if isinstance(v, dict) else v) for k, v in st.items()}
                if mcp.get("structure") == "connected": break
            except Exception as e: mcp = str(e)[:100]
            time.sleep(2)
        result["mcp"] = mcp; print("mcp:", mcp)
        try: result["viewer"] = urllib.request.urlopen(f"http://127.0.0.1:{ports['viewer']}/", timeout=5).status
        except Exception as e: result["viewer"] = str(e)[:80]
        print("viewer:", result["viewer"]); result["examples"] = sorted(os.listdir(os.path.join(W, "examples")))
        try:
            page = urllib.request.urlopen(WB + "/workbench", timeout=10).read().decode()
            result["workbench"] = 200 if "Structure viewer" in page else "page without viewer pane"
            result["workbench_proxy_health"] = json.loads(urllib.request.urlopen(WB + "/global/health", timeout=10).read()).get("healthy")
            result["workbench_proxy_viewer"] = urllib.request.urlopen(WB + "/viewer/", timeout=10).status
        except Exception as e: result["workbench"] = str(e)[:100]
        print("workbench:", result.get("workbench"), result.get("workbench_proxy_health"), result.get("workbench_proxy_viewer"))
finally:
    if WIN: subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        try: os.killpg(os.getpgid(proc.pid), signal.SIGTERM); time.sleep(2); os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception: pass
    log.close()
good = ok and isinstance(result.get("mcp"), dict) and result["mcp"].get("structure") == "connected" and result.get("viewer") == 200 and result.get("workbench") == 200 and result.get("workbench_proxy_health") is True
print("launcher.log tail:", open(os.path.join(a.home, "launcher.log"), errors="replace").read()[-400:].replace("\n", " | "))
print("SMOKE_LAUNCHER_OK" if good else "SMOKE_LAUNCHER_FAILED", json.dumps(result)); sys.exit(0 if good else 1)
