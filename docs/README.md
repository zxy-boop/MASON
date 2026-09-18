# MASON — Materials Atomistic Structure builder Operated in Natural language

MASON is a toolkit of 39 deterministic structure-building tools for first-principles calculations, exposed to a
language-model agent through the Model Context Protocol (MCP). You describe the structure you need in natural
language; the agent calls the MASON tools step by step, every step is written to disk with a validation report,
and the result opens in the structure viewer next to the conversation.

Each package contains its own Python runtime, the MASON tools, the opencode agent and the viewer. Nothing else has
to be installed. The only thing you provide is access to a language model.

---

## 1. Install

| Platform | File | Install |
|---|---|---|
| macOS (Apple Silicon) | `MASON-1.0.2-macos-arm64.dmg` | Open the dmg and drag **MASON.app** to Applications (or to any other folder). Start it from there, not from the mounted image. The app is not notarized: if macOS blocks the first launch, right-click → Open, or allow it under System Settings → Privacy & Security. |
| Windows 10/11 (x64) | `MASON-1.0.2-windows-x64-setup.exe` | Run the installer and choose the installation folder (default `C:\Program Files\MASON`). It creates Start-menu entries (MASON, MASON --setup, README, Uninstall). |
| Windows, portable | `MASON-1.0.2-windows-x64.zip` | Unzip into any folder and double-click **MASON.exe** (or **MASON.bat**). No installation, no registry entries. |
| Linux (x64) | `MASON-1.0.2-linux-x64.tar.gz` | `tar xzf` the archive, then either run `./MASON` in place or `./install.sh [DIR]` to install to a folder of your choice (default `~/.local/opt/MASON`, adds a `MASON` command and a desktop entry). |

MASON never writes into its installation folder. Configuration and data live in per-user folders:

| | macOS | Windows | Linux |
|---|---|---|---|
| Configuration, keys, logs | `~/Library/Application Support/MASON` | `%APPDATA%\MASON` | `~/.config/MASON` |
| Workspace (your structures) | chosen in the wizard, default `~/Documents/MASON` | chosen in the wizard, default `Documents\MASON` | chosen in the wizard, default `~/Documents/MASON` |

Change the workspace any time with `MASON --setup` or `MASON --workspace DIR`. Uninstall: delete the app
(macOS), run *Uninstall MASON* from the Start menu (Windows) or `uninstall.sh` (Linux); the configuration
and workspace folders are kept unless you delete them yourself.

## 2. First run: configure a model in the terminal

The first launch opens a setup wizard in the terminal (three steps: model, Materials Project key, workspace folder).
Every menu accepts a number or a part of the name (for example `deepseek`), keys show one `*` per character while
you type (pasting works), and the wizard sends one real request to the model and prints the answer, so you can see
that the connection works before the interface opens. Ctrl-C cancels without changing anything. The providers offered are

| # | Provider | Key variable | Models offered (any other model id of the provider can be typed) |
|---|---|---|---|
| 1 | OpenAI | `OPENAI_API_KEY` | gpt-5.6, gpt-6-astra, gpt-5.5, gpt-5.4, gpt-5.4-mini |
| 2 | Anthropic (Claude) | `ANTHROPIC_API_KEY` | claude-sonnet-5, claude-opus-5, claude-fable-5-1, claude-opus-4-8, claude-sonnet-4-6, claude-haiku-4-5 |
| 3 | Google (Gemini) | `GEMINI_API_KEY` | gemini-3.8-flash, gemini-3.7-flash, gemini-3.1-pro-preview, gemini-2.5-pro |
| 4 | xAI (Grok) | `XAI_API_KEY` | grok-4.6, grok-4.5, grok-4.3 |
| 5 | Mistral | `MISTRAL_API_KEY` | mistral-large-latest, mistral-medium-latest, mistral-small-latest |
| 6 | DeepSeek | `DEEPSEEK_API_KEY` | deepseek-flash, deepseek-v4-pro, deepseek-v4-flash |
| 7 | Alibaba Cloud (Qwen), China or international endpoint | `DASHSCOPE_API_KEY` | qwen3.8-max, qwen3.8-flash, qwen3.7-plus, qwen3.7-max |
| 8 | Moonshot AI (Kimi), China or international endpoint | `MOONSHOT_API_KEY` | kimi-k3, kimi-k2.6 |
| 9 | Zhipu AI (GLM), China or international endpoint | `ZHIPU_API_KEY` | glm-5.3, glm-5.3-flash, glm-5.2 |
| 10 | MiniMax, China or international endpoint | `MINIMAX_API_KEY` | MiniMax-M3, MiniMax-M2.7 |
| 11 | ByteDance Volcengine Ark (Doubao) | `ARK_API_KEY` | doubao-seed-2-1-pro, doubao-seed-2-1-turbo, deepseek-v4-pro |
| 12 | OpenRouter (many models with one key) | `OPENROUTER_API_KEY` | openai/gpt-5.6, anthropic/claude-sonnet-5, google/gemini-3.8-flash, x-ai/grok-4.6, deepseek/deepseek-v4-pro, qwen/qwen3.8-flash, moonshotai/kimi-k3, z-ai/glm-5.3 |
| 13 | Ollama (models running on this computer) | none | any model pulled with `ollama pull`; 27B parameters or more with a 64k context recommended |
| 14 | Configure later in the web interface | | |

Models that spend a long time thinking before every tool call (for example deepseek-v4-pro or the "pro" tiers of
other providers) make a structure take minutes; the non-thinking tiers (deepseek-flash, gpt-5.6, claude-sonnet-5,
gemini-3.8-flash, qwen3.8-flash, glm-5.3-flash) answer in seconds and are sufficient for structure building.

After the model, the wizard asks for an optional **Materials Project API key**. It is needed only by the two
database tools (`search_materials`, `get_material`), which look up known crystals online; the wizard tests the
key with one query and prints the entries it returns. Get a free key from https://next-materialsproject.org/api
(Dashboard → API key). Press Enter to skip; all other tools work without it.

The wizard writes the model configuration to `opencode.json` and keeps the keys in `secrets.json` (file mode 600),
both in the configuration folder listed above. They are exported to the services as environment variables
(`OPENAI_API_KEY`, `ANTHROPIC_API_KEY` or `DEEPSEEK_API_KEY`, and `MP_API_KEY`) when MASON starts; they are never
written into the configuration and never uploaded anywhere else.
Only the provider you configured is offered in the model list of the interface (`enabled_providers`).

Useful commands:

```
MASON --setup     choose the provider and model, enter the keys (each is tested with a real request)
MASON --test      send a test request to the configured model and to the Materials Project, print the answers
MASON --config    show the configuration file, the model, the endpoint, the masked keys and the ports of the last start
MASON --port 5000 preferred port for the workbench page (if a port is taken, the next free one is used and printed)
MASON --workspace DIR  use another folder for the structures
```

How to be sure that your model is the one answering: the wizard and `MASON --test` print the model name returned
by the API together with its reply; the model name is also shown under the input box of the conversation, and
your provider's usage page shows the requests.

Example `opencode.json` for DeepSeek:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "deepseek/deepseek-flash",
  "enabled_providers": ["deepseek"],
  "default_agent": "MASON",
  "provider": { "deepseek": { "models": { "deepseek-flash": { "name": "deepseek-flash" } } } },
  "agent": { "MASON": { "mode": "primary", "prompt": "...", "tools": { "bash": false, "write": false, "edit": false, "read": false } } },
  "mcp": {
    "structure": { "type": "local", "command": ["<bundle>/runtime/python/bin/python3", "-m", "mason_mcp.server"], "enabled": true }
  }
}
```

`secrets.json`:

```json
{ "env": { "DEEPSEEK_API_KEY": "sk-...", "MP_API_KEY": "..." } }
```

`model` is written as `<provider>/<model id>`; the provider ids are the ones opencode resolves itself
(`openai`, `anthropic`, `google`, `xai`, `mistral`, `deepseek`, `alibaba` / `alibaba-cn`, `moonshotai` /
`moonshotai-cn`, `zai` / `zhipuai`, `minimax` / `minimax-cn`, `volcengine`, `openrouter`) plus the local `ollama` entry.

## 3. Use

After the start-up the terminal prints the addresses and the browser opens the MASON workbench (by default **http://127.0.0.1:4096/workbench**; if that port is taken, the next free port is used and printed): the conversation
on the left, the structure viewer on the right. The MASON agent and your model are preselected under the input
box. The workspace folder you chose already contains sample structures in `examples/` (bulk Si, Pt, Cu, Ni and
3C-SiC, a MoS₂ monolayer and graphene).

Type requests in everyday language, for example:

- `Read examples/si_bulk.vasp and report its space group and inequivalent sites.`
- `From examples/pt_bulk.vasp cut a Pt(111) slab with 4 layers and 15 Å vacuum, fix the bottom two layers, and render it.`
- `Put CO (C end down, 2.0 Å) on the fcc hollow of that slab in a 3×3 cell.`
- `Match graphene (examples/graphene.vasp) on Ni(111) with strain below 3 % and build the interface.`

Every step calls a MASON tool and writes a file. Each structure the agent renders appears in the viewer pane
automatically; links in the answers open it as well, and interactive selection pages (interface candidates,
atoms) open there too. The agent has no shell and cannot write files itself: everything goes through the tools.

## 4. Troubleshooting

- **The model returns 401 or 403**: the API key is wrong, or (for providers with two endpoints) the key belongs to the other endpoint. Run `MASON --test` to see the exact error, or `MASON --setup` to enter them again.
- **A structure takes minutes**: the model is a long-thinking one; choose a non-thinking tier (see the table above). The MASON tools themselves take well under a second per step.
- **Windows: the console shows an error and closes**: the batch file keeps the window open (`pause`); read the last lines, they name the service that failed and show the log. A system proxy is not used for the local services since 1.0.1.
- **The saved workspace is missing, protected or read-only**: MASON tests the folder before starting. A stale saved setting is automatically repaired to `Documents\MASON` (or `~/Documents/MASON`) and reported in the terminal. An invalid explicit `--workspace` path is rejected with a readable error. Drive roots and Windows system/program folders cannot be selected as workspaces.
- **Something else fails**: the services write to `logs/mason.log` in the configuration folder.
- **A different model than the one you configured answers**: run `MASON --config`; if the model id is not offered by your provider, the interface falls back to another model. `MASON --setup` validates the model id against the provider's model list.
- **The page shows "Add project" instead of a conversation**: open the full workbench URL printed in the terminal.
- **Tool calls time out**: interface matching for large systems (more than 500 atoms) can take longer than two minutes; increase `mcp.structure.timeout` (milliseconds) in `opencode.json`.
- **The viewer pane is blank**: the viewer runs on port 8931 and falls back to 8932–8941 if that port is taken.
- **`search_materials` and `get_material` fail with "Materials Project access is not configured"**: run `MASON --setup` and enter a Materials Project API key (free at https://next-materialsproject.org/api). Alternatively set `SEED_MP_DB` to a local snapshot file. All other tools work without either.
- **The interface does not open, or opens a different application**: another program is using the port. MASON picks the next free port and prints the address; use the address from the terminal (or `MASON --config` shows the ports of the last start).

## 5. Tool list (39)

Reading and analysis (9): read, inequivalent sites, validate, symmetry, interpolate, convert, render, search database, get entry
Bulk and molecules (7): supercell, vacancy, molecule, substitute, interstitial, stacking fault, grain boundary
Surfaces and 2D (15): nanoribbon, nanotube, twist pairs, twisted bilayer, twist matches, bilayer (ZSL), stacking, slab, cap bottom, freeze atoms, vacuum, adsorption sites, adsorbate, passivate, reconstruct
Interfaces and stacking (4): interface matches, interface, solvent, stack
Human selection (4): twist pick, select atoms, atom pick, interface pick

## 6. Run from source

```bash
pip install .                  # or pip install -e .
python launcher/mason.py       # needs opencode on PATH, or MASON_HOME pointing to a directory that contains runtime/
```

## 7. Building MASON.exe on Windows

`MASON.bat` works by double-click. For a single `MASON.exe` with the same behaviour, run once in PowerShell on Windows:

```powershell
cd <unpacked folder>\build\windows
.\make_exe.ps1
```

The script installs PyInstaller into the bundled Python and builds `launcher\mason_exe_stub.py` into `MASON.exe`
in the bundle root. The GitHub Actions workflow `.github/workflows/windows.yml` assembles and tests the Windows
bundle and builds the same executable automatically.

## 8. Security notes

All services listen on 127.0.0.1 only; nothing is reachable from other machines. API keys are stored in
`secrets.json` with owner-only permissions and are passed to the model provider and to the Materials Project only.
The macOS app and the Windows executable are not code-signed; both operating systems therefore ask for
confirmation on the first launch.
