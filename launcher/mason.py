#!/usr/bin/env python3
r"""MASON desktop launcher.

Starts the MASON viewer, the opencode server with the MASON tools loaded, and the MASON workbench page
(conversation on the left, structure viewer on the right). On the first run, or with --setup, a wizard in the
terminal asks for a language model (and, optionally, a Materials Project API key), verifies each with a real
request, and writes ~/.mason/opencode.json (model, agent, MCP) plus ~/.mason/secrets.json (API keys, exported
as environment variables when the services start).

Layout of a bundle (all platforms):
  <root>/runtime/python/...      portable CPython with mason_mcp and its dependencies installed
  <root>/runtime/opencode[.exe]  opencode binary
  <root>/launcher/mason.py       this file
  <root>/launcher/workbench.py   the two-pane workbench page and its proxy
  <root>/docs/                   README and tutorial
  <root>/examples/               sample structure files copied into the workspace on first run

Commands:
  MASON                start (the wizard runs on the first launch)
  MASON --setup        choose the language-model provider and model, enter the keys (each is tested with a real request)
  MASON --test         send a test request to the configured model (and to the Materials Project) and show the answers
  MASON --config       show the current configuration (keys are masked)
  MASON --port 5000    preferred port for the workbench page (the next free port is used if it is taken)
  MASON --workspace D  use folder D for the structures (otherwise the folder chosen in the wizard, default ~/Documents/MASON)

Where MASON keeps its files:
  configuration  macOS ~/Library/Application Support/MASON, Windows %APPDATA%\MASON, Linux ~/.config/MASON
                 (opencode.json, secrets.json, settings.json, logs/, the agent's own state under xdg/)
  workspace      the folder chosen in the wizard (default ~/Documents/MASON): structures, renders, viewer pages
"""
import json, os, sys, subprocess, shutil, webbrowser, time, signal, platform, socket, urllib.request, urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from providers import PROVIDERS, LATER, find as find_provider, env_names

# Files and consoles are UTF-8 everywhere (Windows would otherwise use the legacy code page: Å, × and CJK text
# in reports, viewer pages and the console would fail or turn into '?').
os.environ.setdefault("PYTHONUTF8", "1"); os.environ.setdefault("PYTHONIOENCODING", "utf-8")
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

VERSION = "1.0.2"
ROOT = Path(os.environ.get("MASON_HOME") or Path(__file__).resolve().parent.parent)
WIN = platform.system() == "Windows"


def default_config_dir():
    """Per-user configuration directory following the platform conventions."""
    if WIN:
        return Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")) / "MASON"
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "MASON"
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "MASON"


HOME = Path(os.environ.get("MASON_USER_DIR") or default_config_dir())


def ensure_ca_bundle():
    """Portable Python has no root certificates on a minimal Linux: point TLS at certifi's bundle for this process
    and for the services it starts (SSL_CERT_FILE is honoured by Python, and by Node-style runtimes via NODE_EXTRA_CA_CERTS)."""
    if os.environ.get("SSL_CERT_FILE"):
        return
    try:
        import ssl, certifi
        if not ssl.create_default_context().get_ca_certs():
            os.environ["SSL_CERT_FILE"] = certifi.where()
            os.environ.setdefault("NODE_EXTRA_CA_CERTS", certifi.where())
    except Exception:
        pass


ensure_ca_bundle()
DEFAULT_WORKSPACE = Path.home() / "Documents" / "MASON"


def settings():
    try: return json.load(open(HOME / "settings.json"))
    except Exception: return {}


def workspace_dir():
    """The folder that holds the user's structures: --workspace, MASON_WORKSPACE, settings.json, or ~/Documents/MASON."""
    argv = sys.argv[1:]
    if "--workspace" in argv:
        return Path(argv[argv.index("--workspace") + 1]).expanduser()
    if os.environ.get("MASON_WORKSPACE"):
        return Path(os.environ["MASON_WORKSPACE"]).expanduser()
    return Path(settings().get("workspace") or DEFAULT_WORKSPACE).expanduser()


WORKSPACE = workspace_dir()
DEFAULT_PORT, DEFAULT_VIEWER_PORT, OPENCODE_OFFSET = 4096, 8931, 10

PROMPT = ("You are MASON, an assistant for building atomistic structures for first-principles calculations. "
          "Answer in the user's language. You have no shell and you cannot write files yourself: every crystallographic "
          "operation (reading, supercells, defects, slabs, adsorbates, interfaces, low-dimensional structures, rendering) "
          "must be done with the structure tools, which write the files. Never write atomic coordinates or scripts. "
          "Read the input first, validate the geometry after every building step, and finish with a short summary that "
          "lists the files written and the key numbers (layers, spacing, vacuum, strain, composition change). When a step "
          "has several legitimate choices (interface candidates, adsorption sites), list them and ask the user unless the "
          "request states a rule. After the final structure, always call render_structure with fmt='html' and give the "
          "returned interactive_html_url as a markdown link so that the structure opens in the viewer. "
          "Do not open structure files as text: the tool reports already contain the lattice, composition, layers and "
          "checks, and read_structure gives them for any file. The workspace folder examples/ holds ready-made inputs: "
          "si_bulk.vasp, si_diamond_primitive.vasp, pt_bulk.vasp, cu_bulk.vasp, ni_bulk.vasp, SiC_3C_bulk.vasp, "
          "graphene.vasp and mos2_monolayer.vasp; use them when the user names one of these materials without a file. "
          "To obtain an exact number of atomic planes, e.g. a single layer of a layered material, pass n_layers to "
          "generate_slab (MoS2 monolayer: n_layers=3; graphene from graphite: n_layers=1) instead of removing atoms "
          "one by one. Do not call set_selective_dynamics with an empty selection.")
AGENT_TOOLS = {"bash": False, "write": False, "edit": False, "patch": False, "multiedit": False, "webfetch": False,
               "todowrite": False, "todoread": False, "read": False}


def py_exe():
    p = ROOT / "runtime" / "python"
    for c in ([p / "python.exe"] if WIN else [p / "bin" / "python3", p / "bin" / "python3.12"]):
        if c.exists(): return str(c)
    return sys.executable


def opencode_exe():
    p = ROOT / "runtime" / ("opencode.exe" if WIN else "opencode")
    return str(p) if p.exists() else shutil.which("opencode")


def ask(prompt, default=None, secret=False):
    sys.stdout.flush()
    try:
        if secret:
            import getpass; v = getpass.getpass(prompt)
        else:
            v = input(prompt)
    except EOFError:
        v = ""
    v = v.strip()
    return v or (default or "")


def mask(key):
    return (key[:6] + "..." + key[-4:]) if key and len(key) > 12 else ("***" if key else "(none)")


def ask_secret(prompt):
    """Key entry that shows one * per character (Backspace deletes, Enter accepts, pasting works), so the user
    can see that typing arrives; plain input when there is no terminal (scripted runs)."""
    sys.stdout.flush()
    try:
        tty_in = sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        tty_in = False
    if not tty_in:
        return ask(prompt)
    chars = []
    sys.stdout.write(prompt); sys.stdout.flush()
    try:
        if WIN:
            import msvcrt
            while True:
                ch = msvcrt.getwch()
                if ch in ("\r", "\n"): break
                if ch == "\x03": raise KeyboardInterrupt
                if ch in ("\x00", "\xe0"): msvcrt.getwch(); continue        # function and arrow keys
                if ch in ("\x08", "\x7f"):
                    if chars: chars.pop(); sys.stdout.write("\b \b")
                elif ch >= " ":
                    chars.append(ch); sys.stdout.write("*")
                sys.stdout.flush()
        else:
            import termios, tty
            fd = sys.stdin.fileno(); old = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                while True:
                    ch = sys.stdin.read(1)
                    if ch in ("\r", "\n", ""): break
                    if ch == "\x03": raise KeyboardInterrupt
                    if ch in ("\x08", "\x7f"):
                        if chars: chars.pop(); sys.stdout.write("\b \b")
                    elif ch >= " ":
                        chars.append(ch); sys.stdout.write("*")
                    sys.stdout.flush()
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except (ImportError, OSError):
        sys.stdout.write("\n"); return ask(prompt, secret=True)
    sys.stdout.write("\n"); sys.stdout.flush()
    return "".join(chars).strip()


def choose(title, options, default=1, extra=None):
    """Numbered menu. Accepts the number or (part of) the name, case-insensitive; Enter takes the default.
    `extra` maps additional single-key answers (e.g. {"0": "another"}) to their return value."""
    print(f"\n{title}", flush=True)
    for i, o in enumerate(options, 1):
        print(f"  {i:2d}  {o}")
    for k, v in (extra or {}).items():
        print(f"  {k:>2}  {v}")
    while True:
        c = ask(f"Enter a number or a name [{default}]: ", str(default))
        if extra and c in extra:
            return c
        if c.isdigit() and 1 <= int(c) <= len(options):
            return int(c) - 1
        hits = [i for i, o in enumerate(options) if c.lower() in o.lower()]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            print("  Several entries match: " + ", ".join(options[i] for i in hits) + ". Please be more specific.")
        else:
            print(f"  '{c}' is not in the list. Enter a number from 1 to {len(options)}" + (" or 0" if extra else "") + ".")


def _normalized_path(path):
    """Return an absolute path without requiring it to exist."""
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _same_or_child(path, parent):
    """Case-insensitive containment check that also works across Windows drives."""
    try:
        p = os.path.normcase(os.fspath(_normalized_path(path)))
        root = os.path.normcase(os.fspath(_normalized_path(parent)))
        return os.path.commonpath((p, root)) == root
    except (OSError, ValueError):
        return False


def _workspace_policy_issue(path):
    """Reject OS-owned locations before an unhelpful PermissionError reaches the user."""
    try:
        path = _normalized_path(path)
    except (OSError, TypeError, ValueError) as exc:
        return f"the path is invalid ({exc})"
    if not os.fspath(path).strip():
        return "the path is empty"
    if path == Path(path.anchor):
        return "a drive or filesystem root cannot be used as a workspace"
    if WIN:
        # C:\Users itself caused a crash on Windows. Subfolders such as C:\Users\Alice\Documents\MASON remain valid.
        if path == _normalized_path(Path.home().parent):
            return "the Windows user-profile root cannot be used as a workspace"
        protected = [os.environ.get("SystemRoot"), os.environ.get("ProgramFiles"),
                     os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramData")]
        if any(value and _same_or_child(path, value) for value in protected):
            return "Windows system and program folders cannot be used as a workspace"
    return None


def _ensure_workspace(path):
    """Create and write-test a workspace; return (normalized path, error message)."""
    path = _normalized_path(path)
    issue = _workspace_policy_issue(path)
    if issue:
        return path, issue
    try:
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            return path, "the selected path is not a directory"
        probe = path / f".mason-write-test-{os.getpid()}-{time.time_ns()}"
        try:
            probe.write_text("MASON workspace write test", encoding="utf-8")
        finally:
            try: probe.unlink()
            except FileNotFoundError: pass
        return path, None
    except PermissionError:
        return path, "Windows denied write access" if WIN else "write access was denied"
    except OSError as exc:
        return path, str(exc)


def _save_workspace_setting(path):
    """Atomically persist a validated workspace path."""
    HOME.mkdir(parents=True, exist_ok=True)
    st = settings(); st["workspace"] = str(path)
    target = HOME / "settings.json"; temporary = HOME / "settings.json.tmp"
    temporary.write_text(json.dumps(st, indent=2), encoding="utf-8")
    os.replace(temporary, target)


def prepare_workspace():
    """Validate the configured workspace and repair stale saved settings safely."""
    global WORKSPACE
    selected, issue = _ensure_workspace(WORKSPACE)
    if issue is None:
        WORKSPACE = selected
        return True
    explicit = "--workspace" in sys.argv or bool(os.environ.get("MASON_WORKSPACE"))
    if explicit:
        print(f"ERROR: workspace '{selected}' cannot be used: {issue}.\nChoose a writable folder, for example:\n"
              f"  MASON --workspace \"{DEFAULT_WORKSPACE}\"", file=sys.stderr)
        return False
    for fallback in (DEFAULT_WORKSPACE, Path.home() / "MASON"):
        fallback, fallback_issue = _ensure_workspace(fallback)
        if fallback_issue is None:
            print(f"WARNING: saved workspace '{selected}' cannot be used: {issue}.\n"
                  f"MASON repaired the setting and will use '{fallback}'.", file=sys.stderr)
            WORKSPACE = fallback
            _save_workspace_setting(fallback)
            return True
    print(f"ERROR: workspace '{selected}' cannot be used: {issue}.\nMASON also could not create a fallback workspace "
          "in your profile. Run MASON --setup and choose a writable folder.", file=sys.stderr)
    return False


def free_port(start, host="127.0.0.1", tries=50):
    """First free TCP port at or above `start`."""
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if not WIN: s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, p)); return p
            except OSError:
                continue
    raise RuntimeError(f"no free port between {start} and {start + tries}")


# ----------------------------------------------------------------------------- configuration
def build_config(prov, region, model, apikey, baseurl=None):
    """opencode.json and the secrets for provider entry `prov` (see providers.py), region `region` (or None) and `model`."""
    pid = region["id"] if region else prov["id"]
    cfg = {"$schema": "https://opencode.ai/config.json", "default_agent": "MASON", "model": f"{pid}/{model}",
           "enabled_providers": [pid],
           "agent": {"MASON": {"description": "MASON structure-building assistant", "mode": "primary", "prompt": PROMPT, "tools": AGENT_TOOLS}},
           "mcp": {"structure": {"type": "local", "command": [py_exe(), "-m", "mason_mcp.server"], "enabled": True, "timeout": 120000,
                                 "environment": {"SEED_STATIC_PORT": str(DEFAULT_VIEWER_PORT), "SEED_STATIC_HOST": "127.0.0.1", "PYTHONUNBUFFERED": "1"}}}}
    secrets = {}
    if prov.get("local"):
        cfg["provider"] = {"ollama": {"npm": "@ai-sdk/openai-compatible", "name": "Ollama", "options": {"baseURL": baseurl or prov["base"]},
                                      "models": {model: {"name": model}}}}
    else:
        for name in env_names(prov):
            secrets[name] = apikey
        cfg["provider"] = {pid: {"models": {model: {"name": model}}}}      # make sure the interface offers exactly this id
    return cfg, secrets


def load_config():
    cfg = json.load(open(HOME / "opencode.json")) if (HOME / "opencode.json").exists() else None
    sec = {}
    if (HOME / "secrets.json").exists():
        try: sec = json.load(open(HOME / "secrets.json")).get("env", {})
        except Exception: sec = {}
    return cfg, sec


def save_config(cfg, secrets):
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    p = HOME / "opencode.json"; p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    sp = HOME / "secrets.json"; sp.write_text(json.dumps({"env": {k: v for k, v in secrets.items() if v}}, indent=2))
    try: os.chmod(sp, 0o600)
    except Exception: pass
    (HOME / "configured").write_text(time.strftime("%Y-%m-%d %H:%M"))
    return p, sp


def endpoint_of(cfg, sec):
    """(kind, base URL, model id, API key) for the configured model."""
    pid, mid = cfg["model"].split("/", 1)
    opts = ((cfg.get("provider") or {}).get(pid, {})).get("options") or {}
    prov, region = find_provider(pid)
    if prov is None:                                   # a hand-edited configuration: assume an OpenAI-style endpoint
        return "openai", opts.get("baseURL") or "http://127.0.0.1:11434/v1", mid, sec.get("OPENAI_API_KEY", "") or "none"
    base = opts.get("baseURL") or (region["base"] if region else prov["base"])
    key = next((sec[n] for n in env_names(prov) if sec.get(n)), "") or ("ollama" if prov.get("local") else "")
    return prov["kind"], base, mid, key


def http_json(method, url, headers, body=None, timeout=60):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def verify_model(cfg, sec):
    """Send one real request to the configured model. Returns True on success."""
    kind, base, mid, key = endpoint_of(cfg, sec)
    base = base.rstrip("/")
    print(f"Testing {cfg['model']} at {base} ...", flush=True)
    try:
        if kind == "anthropic":
            hdr = {"x-api-key": key, "anthropic-version": "2023-06-01"}
            try:
                ids = [m.get("id") for m in http_json("GET", base + "/v1/models", hdr).get("data", [])]
                if ids and mid not in ids:
                    print(f"  Warning: model '{mid}' is not in the list returned by the API. Available: {', '.join(ids[:15])}")
            except Exception:
                pass
            r = http_json("POST", base + "/v1/messages", hdr,
                          {"model": mid, "max_tokens": 64, "messages": [{"role": "user", "content": "Reply with the single word OK."}]})
            text = "".join(p.get("text", "") for p in r.get("content", []))
        else:
            hdr = {"Authorization": f"Bearer {key}"}
            try:
                ids = [m.get("id") for m in http_json("GET", base + "/models", hdr).get("data", [])]
                if ids and mid not in ids:
                    print(f"  Warning: model '{mid}' is not in the list returned by the API. Available: {', '.join(ids[:15])}")
            except Exception:
                pass
            r = http_json("POST", base + "/chat/completions", hdr,
                          {"model": mid, "max_tokens": 64, "messages": [{"role": "user", "content": "Reply with the single word OK."}]})
            text = (r.get("choices") or [{}])[0].get("message", {}).get("content", "")
        usage = r.get("usage", {})
        shown = text.strip()[:80] or "(no text; reasoning models may spend the whole budget on thinking, the request itself succeeded)"
        print(f"  OK. The model '{r.get('model', mid)}' answered: {shown!r}" + (f"  (tokens: {usage})" if usage else ""), flush=True)
        return True
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        print(f"  FAILED: HTTP {e.code} from {base}. {detail}")
        if e.code in (401, 403): print("  The API key was rejected. Check the key, and for providers with two endpoints that the key belongs to the endpoint chosen.")
        if e.code == 404: print("  Not found: the base URL is probably wrong (it usually ends with /v1) or the model id does not exist.")
        return False
    except Exception as e:
        print(f"  FAILED: {e}")
        return False


def verify_mp(key):
    """One request to the Materials Project API with the given key. Returns True on success."""
    print("Testing the Materials Project API key ...", flush=True)
    url = "https://api.materialsproject.org/materials/summary/?formula=Si&energy_above_hull_max=0.001&_fields=material_id,formula_pretty,symmetry&_limit=3"
    try:
        req = urllib.request.Request(url, headers={"X-API-KEY": key, "Accept": "application/json",
                                                   "User-Agent": "MASON/1.0 (+https://github.com/zxy-boop/MASON)"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
        rows = data.get("data", [])
        hits = ", ".join(f"{d.get('material_id')} ({d.get('formula_pretty')}, {(d.get('symmetry') or {}).get('symbol')})" for d in rows)
        print(f"  OK. Query 'formula=Si, stable' returned {len(rows)} entries: {hits}", flush=True)
        return True
    except urllib.error.HTTPError as e:
        print(f"  FAILED: HTTP {e.code}. {e.read().decode(errors='replace')[:200]}")
        if e.code in (401, 403): print("  The key was rejected. Copy it from https://next-materialsproject.org/api (Dashboard → API key).")
        return False
    except Exception as e:
        print(f"  FAILED: {e}")
        return False


def choose_model(prov):
    """The provider's model list; 0 lets the user type any model id of the provider."""
    r = choose(f"Model of {prov['name']} (a selection; any other model id of the provider can be typed):",
               prov["models"], 1, extra={"0": "another model id"})
    if r == "0":
        while True:
            m = ask("Model id: ")
            if m: return m
    return prov["models"][r]


def setup():
    line = "-" * 72
    print(f"\n{line}\n MASON {VERSION}  setup\n{line}\nMASON contains no language model. Choose the provider whose model it "
          "should use;\nthe key is tested with one real request before anything is saved. Ctrl-C cancels.", flush=True)
    _, old_secrets = load_config()
    names = [p["name"] for p in PROVIDERS] + [LATER]
    while True:
        r = choose("Step 1 of 3: language-model provider", names, 1)
        if r == len(PROVIDERS):
            cfg, secrets = build_config(PROVIDERS[0], None, PROVIDERS[0]["models"][0], "")[0], {}
            cfg.pop("model", None); cfg.pop("enabled_providers", None); cfg.pop("provider", None)
            break
        prov = PROVIDERS[r]
        region = None
        if prov.get("regions"):
            region = prov["regions"][choose(f"Endpoint of {prov['name']}", [x["label"] for x in prov["regions"]], 1)]
        apikey, baseurl = "", None
        if prov.get("local"):
            baseurl = ask(f"Ollama URL [{prov['base']}]: ", prov["base"])
            print("Run `ollama pull <model>` first; models of 27B parameters or more with a 64k context are recommended.")
        else:
            print(f"\nAPI key of {prov['name']} (create one at {prov['site']}). Typing shows one * per character;\n"
                  "pasting works, Backspace deletes.")
            current = next((old_secrets.get(n) for n in env_names(prov) if old_secrets.get(n)), "")
            while True:
                apikey = ask_secret(f"API key [{'keep ' + mask(current) if current else 'required'}]: ") or current
                if apikey: break
                print("  A key is needed for this provider.")
            print(f"  Key received: {mask(apikey)} ({len(apikey)} characters)")
        model = choose_model(prov)
        cfg, secrets = build_config(prov, region, model, apikey, baseurl)
        if verify_model(cfg, secrets):
            break
        again = choose("The test request failed. What next?", ["Try again (change provider, key or model)",
                                                                "Keep this configuration anyway"], 1)
        if again == 1:
            break
    # Materials Project (optional): needed only by the two database tools
    print(f"\nStep 2 of 3: Materials Project API key (optional). It enables the two database tools that look up known\n"
          "crystals online; get a free key at https://next-materialsproject.org/api. Press Enter to skip.", flush=True)
    current = (old_secrets or {}).get("MP_API_KEY", "")
    while True:
        mp = ask_secret(f"Materials Project API key [{'keep ' + mask(current) if current else 'press Enter to skip'}]: ")
        if not mp:
            mp = current; break
        if verify_mp(mp):
            break
        nxt = choose("The key did not work. What next?", ["Enter it again", "Keep it anyway", "Skip"], 1)
        if nxt == 1: break
        if nxt == 2: mp = ""; break
    if mp:
        secrets["MP_API_KEY"] = mp
    # Workspace: the folder for the structures
    global WORKSPACE
    if "--workspace" not in sys.argv and not os.environ.get("MASON_WORKSPACE"):
        cur = settings().get("workspace") or str(DEFAULT_WORKSPACE)
        if _workspace_policy_issue(cur):
            cur = str(DEFAULT_WORKSPACE)
        print(f"\nStep 3 of 3: folder for your structures (input files, results and viewer pages).", flush=True)
        while True:
            ws = ask(f"Workspace folder [{cur}]: ", cur)
            candidate, issue = _ensure_workspace(ws)
            if issue is None:
                WORKSPACE = candidate
                _save_workspace_setting(WORKSPACE)
                break
            print(f"  This folder cannot be used: {issue}.\n  Choose a writable folder such as {DEFAULT_WORKSPACE}.\n", flush=True)
            cur = str(DEFAULT_WORKSPACE)
    p, sp = save_config(cfg, secrets)
    print(f"\n{line}\n Summary\n{line}\n  Model             : {cfg.get('model') or '(choose one in the web interface)'}"
          f"\n  Materials Project : {'key ' + mask(mp) if mp else 'no key (database tools disabled)'}"
          f"\n  Workspace         : {WORKSPACE}\n  Configuration     : {p}\n  Keys              : {sp} (exported to the services as "
          f"environment variables only)\n{line}\nRun `MASON --setup` to change these, `MASON --test` to test them again.\n", flush=True)


def show_config():
    cfg, sec = load_config()
    if not cfg:
        print("No configuration yet. Run MASON --setup."); return 1
    print(f"Configuration file : {HOME / 'opencode.json'}\nSecrets file       : {HOME / 'secrets.json'}\nLog file           : {HOME / 'logs' / 'mason.log'}\nWorkspace          : {WORKSPACE}")
    print(f"Model              : {cfg.get('model') or '(not set: choose one in the web interface)'}")
    if cfg.get("model"):
        kind, base, mid, key = endpoint_of(cfg, sec)
        print(f"Endpoint           : {base}\nModel API key      : {mask(key)}")
    print(f"Materials Project  : {mask(sec.get('MP_API_KEY', '')) if sec.get('MP_API_KEY') else '(no key; the database tools are disabled)'}")
    run_file = HOME / "run.json"
    if run_file.exists():
        try:
            r = json.load(open(run_file)); print(f"Last start         : workbench http://127.0.0.1:{r['workbench']}/workbench, viewer port {r['viewer']}")
        except Exception: pass
    return 0


def test_config():
    cfg, sec = load_config()
    if not cfg or not cfg.get("model"):
        print("No model configured. Run MASON --setup."); return 1
    ok = verify_model(cfg, sec)
    if sec.get("MP_API_KEY"):
        ok = verify_mp(sec["MP_API_KEY"]) and ok
    else:
        print("Materials Project: no API key configured (search_materials and get_material are disabled).")
    return 0 if ok else 1


# ----------------------------------------------------------------------------- run
def first_run_files():
    ex = ROOT / "examples"
    if ex.exists():
        dst = WORKSPACE / "examples"; dst.mkdir(parents=True, exist_ok=True)
        for f in ex.iterdir():
            if f.is_file() and not (dst / f.name).exists(): shutil.copy2(f, dst / f.name)


_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never send local health checks through a system proxy


def wait_http(url, proc=None, tries=60):
    for _ in range(tries):
        time.sleep(0.5)
        if proc is not None and proc.poll() is not None: return False
        try:
            _LOCAL.open(url, timeout=1); return True
        except Exception:
            pass
    return False


def tail_log(path, n=40):
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def run(port=DEFAULT_PORT):
    oc = opencode_exe()
    if not oc:
        print("opencode binary not found; the bundle is incomplete.", file=sys.stderr); return 2
    global WORKSPACE
    if not prepare_workspace():
        return 2
    first_run_files()
    cfg, sec = load_config()
    if cfg is None:
        print("No configuration found; run MASON --setup first."); return 1
    # ports: the requested workbench port, opencode above it, the viewer at 8931 — each moved up if taken
    wb_port = free_port(port)
    oc_port = free_port(wb_port + OPENCODE_OFFSET)
    viewer_port = free_port(DEFAULT_VIEWER_PORT)
    if wb_port != port:
        print(f"Port {port} is in use; using {wb_port} for the workbench instead.")
    # The MCP server must always come from THIS installation: the wizard may have run from another copy (e.g. a
    # mounted disk image), so refresh the interpreter path and the environment on every start.
    st = cfg.setdefault("mcp", {}).setdefault("structure", {})
    st.update({"type": "local", "command": [py_exe(), "-m", "mason_mcp.server"], "enabled": True})
    st.setdefault("timeout", 120000)
    st.setdefault("environment", {}).update({"SEED_STATIC_HOST": "127.0.0.1", "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"})
    st["environment"]["SEED_STATIC_PORT"] = str(viewer_port)
    cfg["mcp"]["structure"]["environment"]["SEED_PUBLIC_BASE_URL"] = f"http://127.0.0.1:{wb_port}/viewer"
    (HOME / "opencode.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    (HOME / "run.json").write_text(json.dumps({"workbench": wb_port, "opencode": oc_port, "viewer": viewer_port}))
    env = dict(os.environ, SEED_STATIC_PORT=str(viewer_port), SEED_STATIC_HOST="127.0.0.1", PYTHONUNBUFFERED="1", PYTHONUTF8="1",
               SEED_PUBLIC_BASE_URL=f"http://127.0.0.1:{wb_port}/viewer", OPENCODE_CONFIG=str(HOME / "opencode.json"))
    for k in ("DATA", "CACHE", "CONFIG", "STATE"):
        env[f"XDG_{k}_HOME"] = str(HOME / "xdg" / k.lower()); Path(env[f"XDG_{k}_HOME"]).mkdir(parents=True, exist_ok=True)
    env.update({k: v for k, v in sec.items() if v})       # model key(s) and MP_API_KEY
    procs = []
    logs = HOME / "logs"; logs.mkdir(parents=True, exist_ok=True)
    log = open(logs / "mason.log", "a", buffering=1, encoding="utf-8", errors="replace")
    log.write(f"\n=== MASON {VERSION} start {time.strftime('%Y-%m-%d %H:%M:%S')} workbench={wb_port} opencode={oc_port} viewer={viewer_port}\n")

    def stop(*_):                      # Ctrl-C, SIGTERM or window close: stop every service we started
        raise KeyboardInterrupt
    for sg in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None), getattr(signal, "SIGBREAK", None)):
        if sg is not None:
            try: signal.signal(sg, stop)
            except Exception: pass
    try:
        procs.append(subprocess.Popen([py_exe(), "-c", "from mason_mcp.server import static_main; static_main()"], cwd=str(WORKSPACE), env=env,
                                      stdout=log, stderr=subprocess.STDOUT))
        procs.append(subprocess.Popen([oc, "serve", "--port", str(oc_port), "--hostname", "127.0.0.1"], cwd=str(WORKSPACE), env=env,
                                      stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT))
        procs.append(subprocess.Popen([py_exe(), str(ROOT / "launcher" / "workbench.py"), "--port", str(wb_port), "--opencode", str(oc_port),
                                       "--viewer", str(viewer_port), "--workspace", str(WORKSPACE)], cwd=str(WORKSPACE), env=env,
                                      stdout=log, stderr=subprocess.STDOUT))
        url = f"http://127.0.0.1:{wb_port}/workbench"
        up_oc = wait_http(f"http://127.0.0.1:{oc_port}/global/health", procs[1])
        up_wb = up_oc and wait_http(url, procs[2])
        up_view = wait_http(f"http://127.0.0.1:{viewer_port}/", procs[0], tries=20)
        model = cfg.get("model") or "(choose a model in the web interface)"
        print(f"\nMASON {VERSION}\n  Model             : {model}\n  Materials Project : {'API key configured' if sec.get('MP_API_KEY') else 'no key (database tools disabled)'}"
              f"\n  Workspace         : {WORKSPACE}\n  Configuration     : {HOME}\n  Log               : {logs / 'mason.log'}"
              f"\n  Workbench         : {url}\n  Viewer            : http://127.0.0.1:{viewer_port}\n"
              f"  Run `MASON --test` in another terminal to confirm that the model answers.\n  Press Ctrl-C to stop.\n", flush=True)
        if not (up_oc and up_wb and up_view):
            print("ERROR: a service did not start (opencode: %s, workbench: %s, viewer: %s). Last lines of %s:\n%s\n"
                  % (up_oc, up_wb, up_view, logs / "mason.log", tail_log(logs / "mason.log")), file=sys.stderr, flush=True)
            if procs[1].poll() is not None or procs[2].poll() is not None:
                print("A required service exited; MASON cannot start. Please report the lines above.", file=sys.stderr, flush=True)
                return 3
            print(f"The services are still running; try opening {url} in the browser.", file=sys.stderr, flush=True)
            webbrowser.open(url)
        else:
            if not (HOME / "tutorial_shown").exists():
                tut = ROOT / "docs" / "tutorial.html"
                if tut.exists(): webbrowser.open(tut.as_uri()); (HOME / "tutorial_shown").write_text("1"); time.sleep(1.0)
            webbrowser.open(url)
        procs[1].wait()
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            try: p.terminate()
            except Exception: pass
        deadline = time.time() + 5
        for p in procs:
            try: p.wait(timeout=max(0.1, deadline - time.time()))
            except Exception:
                try: p.kill()
                except Exception: pass
        log.write(f"=== MASON stop {time.strftime('%Y-%m-%d %H:%M:%S')}\n"); log.close()
        print("MASON stopped.")
    return 0


def main(argv):
    HOME.mkdir(parents=True, exist_ok=True)
    if "--version" in argv: print(VERSION); return 0
    if "--config" in argv: return show_config()
    if "--test" in argv: return test_config()
    if "--setup" in argv or not (HOME / "configured").exists(): setup()
    if "--setup-only" in argv: return 0
    port = DEFAULT_PORT
    if "--port" in argv: port = int(argv[argv.index("--port") + 1])
    return run(port)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\nCancelled. Nothing was changed; run MASON --setup to start the setup again.")
        sys.exit(130)
