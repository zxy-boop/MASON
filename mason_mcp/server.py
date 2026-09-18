"""MASON — MCP server for deterministic atomistic structure-building tools.

Tools exchange structures as POSCAR file paths (deterministic on-disk artifacts).
Backends are pymatgen + ASE, chosen per function in BACKENDS.md.
Run: `mason-mcp` (stdio transport).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import time
import urllib.parse
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .runtime import io as sio
from .runtime import matdb, ops, render as srender, selector as sselector, validate as sval, mpapi
from .runtime import sites as rt_sites

mcp = FastMCP(
    "mason",
    instructions=(
        "Deterministic atomic-structure editing primitives (MASON toolkit). "
        "Structures are exchanged as file paths (POSCAR by default). "
        "Compose primitives to build surfaces, defects and supercells; "
        "always finish an editing chain with validate_geometry. "
        "Structure-producing tools return interactive_html_url — show it to the "
        "user as a clickable link (it opens the 3D viewer in their browser). "
        "For heterostructures, prefer the interactive flow: "
        "list_interface_matches gives the user a selector_url page (candidate "
        "scatter/table + strain-method choice); after they confirm, "
        "read_interface_selection(selection_token) returns their choice for "
        "make_interface. When an analysis needs a site subset the USER "
        "should decide (d-band sites, atoms to fix, fatband selections), "
        "use select_atoms -> selector_url -> read_atom_selection."
    ),
)

# Local static file server so interactive HTML artifacts are clickable http://
# links in chat clients (plain file paths get shown as text, not rendered).
# Container-friendly (platform W1): bind host, port range, and the public URL
# base come from the environment; the default is the historical localhost
# behavior so dev Macs are unchanged.
_STATIC_HOST = os.environ.get("SEED_STATIC_HOST", "127.0.0.1")
_STATIC_PUBLIC_BASE = os.environ.get("SEED_PUBLIC_BASE_URL", "").rstrip("/")
# Shared viewer directory. In a container the always-on daemon (serves cwd) and
# the MCP process are separate: renders that land outside cwd (e.g. a /tmp
# scratch dir) are unreachable. When SEED_VIEWER_ROOT is set, out-of-cwd
# artifacts are copied here and the daemon serves them by basename. Unset on
# dev Macs → historical cwd-only behavior.
_VIEWER_ROOT = os.environ.get("SEED_VIEWER_ROOT", "").strip()


def _static_port_range() -> range:
    p = os.environ.get("SEED_STATIC_PORT", "").strip()
    if p.isdigit():
        return range(int(p), int(p) + 1)  # a fixed port (reverse-proxy target)
    return range(8931, 8942)


# 3Dmol.js is vendored (platform W1: no cdnjs at runtime in a container); the
# static server serves it at a stable path so render pages load it offline.
_ASSET_ROUTE = "/__seed_asset/3Dmol-min.js"
_ASSET_PATH = Path(__file__).resolve().parent / "assets" / "3Dmol-min.js"
_static_port: int | None = None
_static_tried = False
_static_lock = __import__("threading").Lock()


def _ensure_static_server() -> int | None:
    """Start (once) a quiet static server for cwd; scan ports so a foreign
    process on 8931 serving some other directory can't hijack our links.
    Lock-guarded: parallel first tool calls (agents fetch artifacts
    concurrently) must not race the once-flag and double-bind (step 134)."""
    global _static_port, _static_tried
    with _static_lock:
        if _static_tried:
            return _static_port
        _static_tried = True
        return _start_static_server()


def _make_static_handler():
    import http.server

    class _QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):  # stdout/stderr silence: stdio is MCP transport
            pass

        def guess_type(self, path):
            # VASP artifacts are extension-less text (INCAR/KPOINTS/POSCAR/
            # OUTCAR/OSZICAR…): the stdlib guess of application/octet-stream
            # makes browsers DOWNLOAD them. Serve unknowns as inline text so
            # the workbench right pane can display them directly.
            guessed = super().guess_type(path)
            if guessed == "application/octet-stream":
                return "text/plain; charset=utf-8"
            return guessed

        def _reply(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 (stdlib naming)
            # Identity route: lets another process (MCP vs always-on daemon)
            # recognize and adopt an existing seed static server.
            if urllib.parse.urlparse(self.path).path == "/__seed_ping":
                self._reply(200, b'{"service": "mason-static"}')
                return
            # Selection callbacks for the selector pages (runtime/selector.py).
            # Same-origin GET keeps them dependency-free.
            if self.path.startswith("/__seed_select_atoms"):
                from .runtime import selector as _selector

                try:
                    qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    indices = [int(x) for x in qs.get("indices", [""])[0].split(",") if x != ""]
                    _selector.record_atom_selection(qs.get("token", [""])[0], indices)
                except (ValueError, KeyError) as exc:
                    self._reply(400, str(exc).encode())
                else:
                    self._reply(200, b'{"status": "recorded"}')
                return
            if self.path.startswith("/__seed_select"):
                from .runtime import selector as _selector

                try:
                    qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    _selector.record_selection(
                        token=qs.get("token", [""])[0],
                        candidate=int(qs.get("candidate", ["0"])[0]),
                        strain_method=qs.get("strain_method", ["film"])[0],
                        orthogonal_cell=qs.get("orthogonal_cell", ["0"])[0] in ("1", "true"),
                    )
                except (ValueError, KeyError) as exc:
                    self._reply(400, str(exc).encode())
                else:
                    self._reply(200, b'{"status": "recorded"}')
                return
            super().do_GET()

        def send_head(self):  # noqa: N802 (stdlib naming)
            # Vendored 3Dmol.js (offline in a container).
            if urllib.parse.urlparse(self.path).path == _ASSET_ROUTE and _ASSET_PATH.is_file():
                body = _ASSET_PATH.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return io.BytesIO(body)
            # Shared viewer-root fallback: artifacts rendered outside the
            # daemon's cwd (e.g. under /tmp) are copied into SEED_VIEWER_ROOT;
            # serve them here so their links work (this process serves cwd only).
            served = _viewer_root_file(self.path)
            if served is not None:
                body = served.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", self.guess_type(str(served)))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return io.BytesIO(body)
            # Basename fallback: agents sometimes retype links and drop the
            # directory part — resolve known emitted pages by filename.
            target = _registry_resolve(self.path)
            if target is not None:
                self.path = "/" + urllib.parse.quote(
                    target.relative_to(Path.cwd().resolve()).as_posix()
                )
            return super().send_head()

    return _QuietHandler


def _bind_static(host: str, ports) -> tuple:
    import socketserver
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    for port in ports:
        try:
            httpd = socketserver.ThreadingTCPServer((host, port), _make_static_handler())
        except OSError:
            continue
        return httpd, int(httpd.server_address[1])
    return None, None


def _probe_seed_static(port: int) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/__seed_ping", timeout=2) as r:
            return b"mason-static" in r.read(200)
    except Exception:
        return False


def _start_static_server() -> int | None:
    global _static_port
    import threading

    httpd, port = _bind_static(_STATIC_HOST, _static_port_range())
    if httpd is not None:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        _static_port = port
        return port
    # ports taken — adopt an existing seed static server (the container's
    # always-on daemon, or an earlier MCP process) instead of failing.
    for port in _static_port_range():
        if _probe_seed_static(port):
            _static_port = port
            return port
    return None


def static_main(argv=None) -> int:
    """Foreground always-on daemon for deployments (container entrypoint):
    structure/selector pages must be servable with zero agent interaction."""
    host = os.environ.get("SEED_STATIC_HOST", "127.0.0.1")
    httpd, port = _bind_static(host, _static_port_range())
    if httpd is None:
        print("mason-static: no free port in range", flush=True)
        return 1
    print(f"mason-static: serving {Path.cwd()} on {host}:{port}", flush=True)
    httpd.serve_forever()
    return 0
    return None


_PAGE_REGISTRY: dict[str, str] = {}


def _registry_resolve(url_path: str) -> Path | None:
    """Map a bare root-level filename to a known emitted page (agents
    sometimes retype URLs and drop the directory part). Returns None when
    the path has directories, exists under cwd anyway, or is unknown."""
    name = urllib.parse.unquote(urllib.parse.urlparse(url_path).path).strip("/")
    if not name or "/" in name:
        return None
    if (Path.cwd() / name).exists():
        return None
    hit = _PAGE_REGISTRY.get(name)
    if not hit:
        return None
    target = Path(hit)
    try:
        target.resolve().relative_to(Path.cwd().resolve())
    except ValueError:
        return None
    return target.resolve() if target.is_file() else None


def _static_base() -> str | None:
    """Public URL base for served files. Behind a reverse proxy this is
    SEED_PUBLIC_BASE_URL; otherwise http://127.0.0.1:<port> after the local
    server starts."""
    if _STATIC_PUBLIC_BASE:
        _ensure_static_server()  # still need the server running behind the proxy
        return _STATIC_PUBLIC_BASE
    port = _ensure_static_server()
    return None if port is None else f"http://127.0.0.1:{port}"


def _viewer_root_file(url_path: str) -> Path | None:
    """A copied artifact under SEED_VIEWER_ROOT, when the request doesn't resolve
    to a real file under cwd (renders can live outside the daemon's cwd)."""
    if not _VIEWER_ROOT:
        return None
    name = urllib.parse.unquote(urllib.parse.urlparse(url_path).path).strip("/")
    if not name or "__seed" in name:
        return None
    if (Path.cwd() / name).exists():        # a real cwd file always wins
        return None
    cand = Path(_VIEWER_ROOT) / Path(name).name   # flat dir, basename only
    return cand if cand.is_file() else None


def _static_url(path: Path) -> str | None:
    """http URL for a rendered artifact. Files under cwd are served directly;
    files elsewhere (e.g. a /tmp scratch dir the always-on daemon can't reach)
    are copied into the shared SEED_VIEWER_ROOT so the daemon can serve them."""
    base = _static_base()
    if base is None:
        return None
    resolved = path.resolve()
    _PAGE_REGISTRY[path.name] = str(resolved)
    try:
        rel = resolved.relative_to(Path.cwd().resolve())
        return f"{base}/{urllib.parse.quote(rel.as_posix())}"
    except ValueError:
        pass  # outside cwd — fall through to the shared viewer root
    if not _VIEWER_ROOT:
        return None
    try:
        dest = Path(_VIEWER_ROOT)
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(resolved, dest / path.name)
    except OSError:
        return None
    return f"{base}/{urllib.parse.quote(path.name)}"


def _asset_url() -> str | None:
    """URL of the vendored 3Dmol.js served by the static server, or None
    when the server can't start (then render pages fall back to the CDN)."""
    base = _static_base()
    return None if (base is None or not _ASSET_PATH.is_file()) else base + _ASSET_ROUTE


def _out(input_path: str, op: str, output_path: str | None) -> Path:
    if output_path:
        return Path(output_path)
    p = Path(input_path)
    return p.with_name(f"{p.stem}_{op}.vasp")


def _artifacts(structure, out: Path) -> dict:
    """Every structure-producing tool also emits an interactive HTML twin
    (3Dmol viewer) next to the POSCAR, so humans can always inspect the result."""
    html = srender.render_html(structure, out.with_suffix(".html"), title=out.stem,
                               script_url=_asset_url())
    result = {"artifact": str(out.resolve()), "interactive_html": str(html.resolve())}
    url = _static_url(html)
    if url:
        result["interactive_html_url"] = url
    return result



def _mp_backend(db_path):
    """'local' when a snapshot is configured (SEED_MP_DB or db_path), 'api' when MP_API_KEY is set, else an error."""
    import os as _os
    if db_path or _os.environ.get(matdb.ENV_VAR):
        return "local"
    if mpapi.api_key():
        return "api"
    raise RuntimeError("Materials Project access is not configured. Run `MASON --setup` and enter a Materials Project "
                       "API key (from https://next-materialsproject.org/api), or set SEED_MP_DB to a local snapshot.")

@mcp.tool()
def read_structure(path: str) -> dict:
    """Read a structure file (POSCAR/CIF/xyz/…; ~100 formats) and summarize it:
    formula, sites, lattice, slab/vacuum extent."""
    s = sio.load(path)
    return {"path": str(Path(path).resolve()), **sio.describe(s)}


@mcp.tool()
def make_supercell(input_path: str, scaling: list, output_path: str | None = None) -> dict:
    """Build a supercell. `scaling` = [na, nb, nc] or full 3x3 integer matrix.
    Writes a POSCAR next to the input (or to output_path)."""
    s = sio.load(input_path)
    new, summary = ops.make_supercell(s, scaling)
    out = sio.save(new, _out(input_path, "super", output_path))
    return {**summary, **_artifacts(new, out), **sio.describe(new)}


@mcp.tool()
def make_nanoribbon(
    input_path: str,
    direction: list[int] | None = None,
    edge: str | None = None,
    width: int = 4,
    vacuum_transverse: float = 15.0,
    output_path: str | None = None,
) -> dict:
    """Cut a nanoribbon from a 2D layer (monolayer with vacuum along c):
    periodic along ONE in-plane lattice direction, vacuum in the other two.
    Pass exactly one of `direction`=[u,v] (lattice direction of the ribbon
    axis) or `edge`="zigzag"/"armchair" (hexagonal cells only — graphene,
    h-BN, TMDs). `width` counts transverse repeats; the result reports the
    ribbon width in Å. Edges come back UNSATURATED — run passivate_surface
    afterwards to H-terminate (its missing-bond machinery is direction-
    agnostic, so ribbon edges work like any surface)."""
    s = sio.load(input_path)
    new, summary = ops.make_nanoribbon(
        s, direction=direction, edge=edge, width=width,
        vacuum_transverse=vacuum_transverse)
    out = sio.save(new, _out(input_path, "ribbon", output_path))
    return {**summary, **_artifacts(new, out), **sio.describe(new)}


@mcp.tool()
def make_nanotube(
    input_path: str,
    n: int,
    m: int,
    repeat: int = 1,
    vacuum: float = 15.0,
    output_path: str | None = None,
) -> dict:
    """Roll a 2D layer (monolayer with vacuum along c) into a nanotube with
    chiral indices (n, m) — (n,0) zigzag, (n,n) armchair. Works for any
    layer whose lattice admits a commensurate tube (graphene, h-BN, TMDs;
    finite-thickness layers map to inner/outer radial shells). The tube axis
    is periodic along c inside a square vacuum box; the result reports
    radius, period and atom count. Refuses non-commensurate lattices and
    tubes too narrow for the layer thickness rather than guessing. The
    geometry is the ideal rolled construction — relax it (Anneal) before
    extracting physics; radii below ~3 Å are flagged as strongly curved."""
    s = sio.load(input_path)
    new, summary = ops.make_nanotube(s, n, m, repeat=repeat, vacuum=vacuum)
    out = sio.save(new, _out(input_path, "tube", output_path))
    return {**summary, **_artifacts(new, out), **sio.describe(new)}


@mcp.tool()
def list_twist_pairs(
    input_path: str,
    max_index: int = 12,
    max_atoms: int = 3000,
) -> dict:
    """Survey commensurate twist angles for a hexagonal 2D layer BEFORE
    building a twisted bilayer: per (i, j) pair the twist angle θ, bilayer
    atom count and moiré lattice constant — the smaller the angle, the larger
    the moiré cell. Returns selector_url (angle-vs-atoms scatter + table):
    give it to the user to pick, then call read_twist_selection
    (selection_token) and pass the returned i/j into make_twisted_bilayer.
    If the user already names an angle or (i, j) in chat, skip the survey and
    call make_twisted_bilayer directly — (i, j) are physically meaningful."""
    import hashlib

    s = sio.load(input_path)
    survey = ops.list_twist_pairs(s, max_index=max_index, max_atoms=max_atoms)
    key = f"twist|{input_path}|{max_index}|{max_atoms}"
    token = "twsel_" + hashlib.sha1(key.encode()).hexdigest()[:12]
    p = Path(input_path)
    page = p.with_name(f"{p.stem}_twist_pairs.html")
    sselector.render_twist_selector(survey, token, page,
                                    title=f"{p.stem} — twist-angle pair selection")
    sselector.record_survey_meta(token, survey["candidates"])
    survey["selection_token"] = token
    survey["selector_html"] = str(page.resolve())
    url = _static_url(page)
    if url:
        survey["selector_url"] = url
    survey["next_step"] = (
        "Give the user the selector_url link to pick a twist pair in the "
        "browser; when they confirm, call read_twist_selection"
        "(selection_token) and pass i/j into make_twisted_bilayer. If the "
        "user prefers, they can also just answer in chat."
    )
    return survey


@mcp.tool()
def read_twist_selection(selection_token: str, wait_seconds: float = 0) -> dict:
    """Read the twist pair the user picked in the list_twist_pairs selector
    page. Returns status=selected with i/j/angle for make_twisted_bilayer, or
    status=pending if the user has not confirmed yet. wait_seconds > 0 blocks
    server-side until the user confirms in the browser (1 s poll, capped
    600 s) — call right after presenting the selector_url."""
    deadline = time.monotonic() + min(max(float(wait_seconds or 0), 0.0), 600.0)
    while True:
        choice = sselector.read_selection(selection_token)
        if choice is not None:
            rows = sselector.read_survey_meta(selection_token) or []
            k = int(choice["candidate"])
            row = next((r for r in rows if r.get("candidate") == k), None)
            if row is None:
                return {"status": "error",
                        "next_step": "survey metadata missing for this token — "
                                     "re-run the survey tool and try again"}
            next_step = ("Call make_twisted_bilayer with these i/j."
                         if "i" in row else
                         "Call make_twisted_bilayer_zsl with this candidate "
                         "index (same angle/tol/area/strain params as the "
                         "survey).")
            return {"status": "selected", **row, "next_step": next_step}
        if time.monotonic() >= deadline:
            break
        time.sleep(1.0)
    return {
        "status": "pending",
        "selection_token": selection_token,
        "next_step": "The user has not confirmed yet — either call this again "
                     "with wait_seconds=180 (one retry max), or ask them to "
                     "open the selector page and press Confirm selection / state the "
                     "(i, j) pair in chat.",
    }


@mcp.tool()
def make_twisted_bilayer(
    input_path: str,
    i: int,
    j: int,
    gap: float = 3.35,
    vacuum: float = 15.0,
    slide: list[float] | None = None,
    max_atoms: int = 4000,
    output_path: str | None = None,
) -> dict:
    """Build a commensurate twisted homobilayer of a hexagonal 2D layer
    (graphene, h-BN, TMD monolayers). The twist pair (i, j) fixes the angle:
    cos θ = (i²+4ij+j²)/(2(i²+ij+j²)) — e.g. (1,2) → 21.79° with 28 atoms
    for graphene; smaller angles need larger pairs (survey the trade-off
    with list_twist_pairs, or take i/j from read_twist_selection). The
    construction is exact (zero built-in strain): both layers span one moiré
    cell, layer 2 rigidly rotated. `gap` = interlayer spacing between atomic
    extents (Å); `slide`=[fa,fb] (input-cell fractional) moves the registry
    origin. Rigid ideal geometry — relax (Anneal) before extracting physics.
    Refuses non-hexagonal cells and >max_atoms builds rather than guessing."""
    s = sio.load(input_path)
    new, summary = ops.make_twisted_bilayer(
        s, i, j, gap=gap, vacuum=vacuum, slide=slide, max_atoms=max_atoms)
    out = sio.save(new, _out(input_path, "twist", output_path))
    return {**summary, **_artifacts(new, out), **sio.describe(new)}


@mcp.tool()
def list_twist_matches(
    input_path: str,
    angle_deg: float,
    layer2_path: str | None = None,
    angle_tol: float = 0.5,
    max_area: float = 400.0,
    max_strain: float = 0.05,
    limit: int = 20,
) -> dict:
    """Survey twisted-bilayer supercells for ANY 2D lattice near a target
    angle — the general-lattice route (hexagonal cells have an exact
    closed-form alternative: list_twist_pairs). Enumerates approximate
    coincidence supercell pairs: per candidate the realized angle (within
    `angle_tol` of the target), residual von Mises strain, atom count and
    common cell. `layer2_path` makes it a HETEROBILAYER survey (e.g.
    graphene on h-BN — twisted or aligned, angle_deg=0 finds the aligned
    coincidence cells). Returns selector_url (strain-vs-atoms scatter);
    give it to the user, then call read_twist_selection(selection_token)
    and pass the returned candidate into make_twisted_bilayer_zsl with the
    SAME survey parameters."""
    import hashlib

    s = sio.load(input_path)
    l2 = sio.load(layer2_path) if layer2_path else None
    survey = ops.list_twist_matches(
        s, angle_deg, layer2=l2, angle_tol=angle_tol,
        max_area=max_area, max_strain=max_strain, limit=limit)
    key = f"twistzsl|{input_path}|{layer2_path}|{angle_deg}|{angle_tol}|{max_area}|{max_strain}"
    token = "twsel_" + hashlib.sha1(key.encode()).hexdigest()[:12]
    p = Path(input_path)
    page = p.with_name(f"{p.stem}_twist_matches.html")
    sselector.render_twist_match_selector(
        survey, token, page, title=f"{p.stem} — ZSL twist-match selection")
    sselector.record_survey_meta(token, survey["candidates"])
    survey["selection_token"] = token
    survey["selector_html"] = str(page.resolve())
    url = _static_url(page)
    if url:
        survey["selector_url"] = url
    survey["next_step"] = (
        "Give the user the selector_url link to pick a candidate; when they "
        "confirm, call read_twist_selection(selection_token) and pass the "
        "candidate index into make_twisted_bilayer_zsl (same survey params). "
        "If the user prefers, they can also just answer in chat."
    )
    return survey


@mcp.tool()
def make_twisted_bilayer_zsl(
    input_path: str,
    angle_deg: float,
    layer2_path: str | None = None,
    candidate: int = 0,
    angle_tol: float = 0.5,
    max_area: float = 400.0,
    max_strain: float = 0.05,
    strain_method: str = "layer2",
    gap: float = 3.35,
    vacuum: float = 15.0,
    max_atoms: int = 4000,
    output_path: str | None = None,
) -> dict:
    """Build a twisted bilayer for ANY 2D lattice (hetero pairs via
    `layer2_path`) from the nearest coincidence supercell to `angle_deg`.
    Unlike the exact hexagonal route (make_twisted_bilayer), the realized
    angle may differ from the target by up to `angle_tol` and a residual
    strain closes the common cell — both are reported, and `strain_method`
    decides who absorbs the strain: "layer2" (default; layer1 keeps its
    natural lattice), "layer1", or "average". `candidate` indexes the
    deterministic (strain, atoms, |θ−target|) ordering of
    list_twist_matches — candidate=0 is the auditable default (lowest
    strain, then fewest atoms); survey first when the user should weigh
    strain against system size. Refuses when no coincidence exists within
    the tolerances rather than approximating silently. Rigid ideal
    geometry — relax (Anneal) before extracting physics."""
    s = sio.load(input_path)
    l2 = sio.load(layer2_path) if layer2_path else None
    new, summary = ops.make_twisted_bilayer_zsl(
        s, angle_deg, layer2=l2, candidate=candidate, angle_tol=angle_tol,
        max_area=max_area, max_strain=max_strain, strain_method=strain_method,
        gap=gap, vacuum=vacuum, max_atoms=max_atoms)
    out = sio.save(new, _out(input_path, "twistzsl", output_path))
    return {**summary, **_artifacts(new, out), **sio.describe(new)}


@mcp.tool()
def make_stacking(
    input_path: str,
    n_layers: int = 2,
    stacking: str | None = "AB",
    shifts: list[list[float]] | None = None,
    gap: float = 3.0,
    vacuum: float = 15.0,
    output_path: str | None = None,
) -> dict:
    """Stack a 2D layer into an N-layer slab with a chosen registry. Pass
    exactly one of `stacking`="AA"/"AB"/"ABC" (named high-symmetry registries,
    hexagonal cells only: AA eclipsed, AB alternating hollow shift — Bernal
    graphite, ABC cumulative — rhombohedral) or `shifts`=[[fa,fb], ...] (one
    explicit in-plane fractional shift per layer, any cell — arbitrary slip
    stackings; layer 1 usually [0,0]). `gap` is the vertical spacing between
    adjacent layers' atomic extents in Å — a STARTING GUESS for relaxation
    (graphite ≈ 3.35, TMDs ≈ 3.0), not a prediction. Total vacuum along c is
    exactly `vacuum`. For two DIFFERENT layers use stack_structures; for a
    twisted bilayer use make_twisted_bilayer."""
    s = sio.load(input_path)
    new, summary = ops.make_stacking(
        s, n_layers=n_layers, stacking=stacking, shifts=shifts,
        gap=gap, vacuum=vacuum)
    out = sio.save(new, _out(input_path, "stack", output_path))
    return {**summary, **_artifacts(new, out), **sio.describe(new)}


@mcp.tool()
def generate_slab(
    input_path: str,
    miller: list[int],
    min_slab_size: float = 10.0,
    min_vacuum_size: float = 15.0,
    termination: int = 0,
    symmetrize: bool = False,
    align: str = "bottom",
    fix_bottom_fraction: float = 0.5,
    n_layers: int | None = None,
    output_path: str | None = None,
) -> dict:
    """Cut a surface slab from a bulk structure along `miller` (e.g. [1,1,1]).
    Enumerates all distinct terminations; `termination` selects one (see
    all_termination_shifts in the result). Sizes in Å.
    `n_layers` keeps only the top n atomic planes of the generated slab (planes
    are z-clustered): use it for an exact plane count, e.g. a single layer of a
    layered material (MoS2 monolayer: n_layers=3 for S-Mo-S; graphene from
    graphite: n_layers=1). The result reports n_layers (planes found).
    The TOTAL vacuum equals min_vacuum_size EXACTLY (expert rule: 12-15 Å
    suffices — don't ask for more without an explicit user request).
    Expert rule: the bottom half of the slab's layers comes back FROZEN
    (selective dynamics F F F) so relaxations optimize only the surface
    side — the result reports fixed_layers/fixed_atoms; pass
    fix_bottom_fraction=0 only when the USER explicitly wants a fully free
    slab (e.g. a symmetric two-sided relaxation).
    IMPORTANT: do NOT pass `align` — leave it at the default "bottom" (slab at the
    cell base, all vacuum above; this project's convention). Only pass
    align='center' if the USER explicitly asks for a centered slab."""
    bulk = sio.load(input_path)
    slab, summary = ops.generate_slab(
        bulk, miller,
        min_slab_size=min_slab_size, min_vacuum_size=min_vacuum_size,
        termination=termination, symmetrize=symmetrize, align=align,
        fix_bottom_fraction=fix_bottom_fraction, n_layers=n_layers,
    )
    out = sio.save(slab, _out(input_path, "slab", output_path))
    return {**summary, **_artifacts(slab, out), **sio.describe(slab)}


@mcp.tool()
def cap_slab_bottom(input_path: str, layers: int = 1,
                    output_path: str | None = None) -> dict:
    """Bottom capping (group method): extend the crystal BELOW the slab's
    bottom surface by `layers` bulk-registered layers — the group's
    "passivation" for metal slabs, meant to decouple the top and bottom
    surfaces. Deterministic: the stacking translation inferred from the
    bottom layers continues the bulk sequence (fcc ABC automatically), so
    use this INSTEAD of passivate_surface on metals (that tool refuses
    close-packed surfaces — multiple missing bonds are ambiguous). Capping
    atoms come back FROZEN when the slab carries selective_dynamics;
    vacuum and the bottom-aligned convention are preserved. Requires an
    UNRELAXED bulk-truncated slab (cap before relaxing). Validate against
    thicker uncapped slabs before production use (group acceptance)."""
    s = sio.load(input_path)
    new, summary = ops.cap_slab_bottom(s, layers=layers)
    out = sio.save(new, _out(input_path, "cap", output_path))
    return {**summary, **_artifacts(new, out), **sio.describe(new)}


@mcp.tool()
def set_selective_dynamics(input_path: str, mode: str = "bottom_half",
                           n_layers: int | None = None,
                           z_window: list[float] | None = None,
                           indices: list[str | int] | None = None,
                           output_path: str | None = None) -> dict:
    """Set which slab atoms are FROZEN (selective dynamics) by convention —
    NEVER hand-edit POSCAR text for this. Modes: "bottom_half" (step-195
    default), "bottom_layers"/"center_layers" (+n_layers; center default 2 =
    symmetric-slab central-bilayer convention), "z_window" ([zlo,zhi] Å),
    "indices" (site specs in the viewer's dual numbering, step 262, always
    1-based: "O37" = 37th O, "#69" or 69 = global site 69 — pass the user's own
    wording verbatim, never convert numberings yourself).
    The selection is frozen F F F, everything else relaxes T T T; existing
    flags are replaced wholesale. Saving goes through sio (species stay
    grouped, coordinates stay consistent)."""
    s = sio.load(input_path)
    indices0 = rt_sites.resolve_sites(s, indices) if indices else None
    new, summary = ops.set_selective_dynamics(
        s, mode=mode, n_layers=n_layers, z_window=z_window, indices=indices0)
    if indices0:
        summary = {**summary, "resolved_sites":
                   [rt_sites.site_label(s, i) for i in indices0[:50]]}
    out = sio.save(new, _out(input_path, "sd", output_path))
    return {**summary, **_artifacts(new, out), **sio.describe(new)}


@mcp.tool()
def add_vacuum(input_path: str, vacuum: float, force: bool = False,
               align: str = "bottom", output_path: str | None = None) -> dict:
    """Set the TOTAL vacuum along c to `vacuum` Å. The slab ends up at the BOTTOM
    of the cell with all vacuum above (this project's convention).
    IMPORTANT: do NOT pass `align` unless the USER explicitly asks for a centered
    slab. Guards against vacuum < 10 Å unless force=true."""
    s = sio.load(input_path)
    new, summary = ops.add_vacuum(s, vacuum, force=force, align=align)
    out = sio.save(new, _out(input_path, "vac", output_path))
    return {**summary, **_artifacts(new, out), **sio.describe(new)}


def _interface_token(film_path, substrate_path, film_miller, substrate_miller,
                     max_area, max_strain, termination) -> str:
    import hashlib

    key = f"{film_path}|{substrate_path}|{film_miller}|{substrate_miller}|{max_area}|{max_strain}|{termination}"
    return "ifsel_" + hashlib.sha1(key.encode()).hexdigest()[:12]


def _miller_tag(miller) -> str:
    """Filename-safe Miller tag: [0,0,1] -> '001', [1,-1,0] -> '1m10'."""
    return "".join(("m" + str(-int(x))) if int(x) < 0 else str(int(x)) for x in miller)


def _survey_page_path(film_path, substrate_path, film_miller, substrate_miller,
                      termination) -> Path:
    """The survey page is the gate's on-disk proof that the candidate options
    were shown to the user — it must be specific to the orientation pair
    (millers + termination), or a survey for one orientation would unlock
    explicit-candidate builds for every other (4th-gen gate leak, live retest
    after step 87)."""
    fp, sp = Path(film_path), Path(substrate_path)
    tag = f"{_miller_tag(film_miller)}_on_{_miller_tag(substrate_miller)}_t{int(termination)}"
    return fp.with_name(f"{fp.stem}_on_{sp.stem}_{tag}_matches.html")


def _interface_survey(film_path, substrate_path, film_miller, substrate_miller,
                      film_thickness, substrate_thickness, max_area, max_strain,
                      termination, limit) -> dict:
    film = sio.load(film_path)
    substrate = sio.load(substrate_path)
    survey = ops.list_interface_matches(
        film, substrate, film_miller, substrate_miller,
        film_thickness=film_thickness, substrate_thickness=substrate_thickness,
        max_area=max_area, max_strain=max_strain,
        termination=termination, limit=limit,
    )
    # Interactive selector page (deterministic token from the survey inputs).
    token = _interface_token(film_path, substrate_path, film_miller,
                             substrate_miller, max_area, max_strain, termination)
    fp, sp = Path(film_path), Path(substrate_path)
    page = _survey_page_path(film_path, substrate_path, film_miller,
                             substrate_miller, termination)
    sselector.render_match_selector(
        survey, token, page, title=f"{fp.stem} / {sp.stem} interface matches"
    )
    # Params sidecar: the survey's geometry parameters are the contract the
    # user saw (candidate ordering AND the n_atoms estimates depend on them).
    # make_interface inherits them by default so the built structure matches
    # the survey row the user picked (expert ruling, step 94).
    page.with_suffix(".params.json").write_text(json.dumps({
        "selection_token": token,
        "film_thickness": film_thickness,
        "substrate_thickness": substrate_thickness,
        "max_area": max_area,
        "max_strain": max_strain,
        "termination": termination,
        "film_miller": list(film_miller),
        "substrate_miller": list(substrate_miller),
    }, indent=2) + "\n", encoding="utf-8")
    survey["selection_token"] = token
    survey["selector_html"] = str(page.resolve())
    url = _static_url(page)
    if url:
        survey["selector_url"] = url
    survey["next_step"] = (
        "Give the user the selector_url link to pick a candidate and strain "
        "method in the browser; when they say they are done, call "
        "read_interface_selection(selection_token) and pass the returned "
        "candidate/strain_method/orthogonal_cell into make_interface. If the "
        "user prefers, they can also just answer in chat."
    )
    return survey


@mcp.tool()
def list_interface_matches(
    film_path: str,
    substrate_path: str,
    film_miller: list[int],
    substrate_miller: list[int],
    film_thickness: float = 3,
    substrate_thickness: float = 4,
    max_area: float = 200.0,
    max_strain: float = 0.05,
    termination: int = 0,
    limit: int = 20,
) -> dict:
    """Survey ZSL lattice-match candidates BEFORE building an interface:
    per candidate the von Mises strain, atom count, in-plane cell (a, b, gamma),
    area, and film-vs-substrate mismatch components (e11/e22/dgamma). Present
    the trade-off (small strain vs small cell) to the user and let them pick;
    the row's `candidate` index feeds make_interface(candidate=...) directly."""
    return _interface_survey(film_path, substrate_path, film_miller,
                             substrate_miller, film_thickness,
                             substrate_thickness, max_area, max_strain,
                             termination, limit)


@mcp.tool()
def select_atoms(
    structure_path: str,
    purpose: str = "",
    preselect: str = "none",
) -> dict:
    """Open a browser dialog for the USER to pick atoms of a structure by
    clicking spheres in 3D (element quick-buttons, select-all/invert, d-block
    preset). Use whenever an analysis needs a site subset and the user should
    decide — d-band center sites, fatband atom-orbital choices, atoms to fix,
    substitution targets. `preselect`: "none" | "all" | "d_block".
    Returns selector_url (give it to the user) + selection_token; after the
    user confirms, call read_atom_selection(selection_token)."""
    import hashlib

    s = sio.load(structure_path)
    if preselect not in ("none", "all", "d_block"):
        raise ValueError('preselect must be "none", "all" or "d_block"')
    key = f"atoms|{structure_path}|{purpose}"
    token = "atsel_" + hashlib.sha1(key.encode()).hexdigest()[:12]
    p = Path(structure_path)
    page = p.with_name(f"{p.stem}_atomsel.html")
    sselector.render_atom_selector(s, token, page,
                                   title=f"{p.stem} — atom selection",
                                   note=purpose, preselect=preselect,
                                   script_url=_asset_url())
    out = {
        "selection_token": token,
        "selector_html": str(page.resolve()),
        "n_sites": len(s),
        "next_step": (
            "Give the user the selector_url link; when they confirm, call "
            "read_atom_selection(selection_token) and use the returned indices."
        ),
    }
    url = _static_url(page)
    if url:
        out["selector_url"] = url
    return out


@mcp.tool()
def read_atom_selection(selection_token: str, wait_seconds: float = 0) -> dict:
    """Read the atom indices the user picked in the select_atoms dialog.
    Returns status=selected with indices, or status=pending if the user has
    not confirmed yet. wait_seconds > 0 blocks server-side until the user
    confirms in the browser (1 s poll, capped 600 s) — call right after
    presenting the dialog so the flow continues without a chat round-trip."""
    deadline = time.monotonic() + min(max(float(wait_seconds or 0), 0.0), 600.0)
    while True:
        choice = sselector.read_atom_selection(selection_token)
        if choice is not None:
            return {"status": "selected", **choice,
                    "next_step": "Use these site indices in the follow-up analysis "
                                 "(e.g. pass them as site_indices)."}
        if time.monotonic() >= deadline:
            break
        time.sleep(1.0)
    return {
        "status": "pending",
        "selection_token": selection_token,
        "next_step": "The user has not confirmed yet — either call this again "
                     "with wait_seconds=180 (one retry max), or ask them to "
                     "open the selector page and press Confirm selection / state the "
                     "indices in chat.",
    }


@mcp.tool()
def read_interface_selection(selection_token: str, wait_seconds: float = 0) -> dict:
    """Read the choice the user made in the interface-match selector page
    (list_interface_matches returns the selection_token). Returns the chosen
    candidate/strain_method/orthogonal_cell for make_interface, or
    status=pending if the user has not confirmed yet.

    wait_seconds > 0 BLOCKS server-side (1 s poll, no tokens burned) until
    the user presses Confirm selection in the browser or the wait expires — call this
    RIGHT AFTER presenting the selector_url (wait_seconds≈180) so the
    workflow continues the moment the user confirms, without them having to
    come back to the chat. Capped at 600 s."""
    deadline = time.monotonic() + min(max(float(wait_seconds or 0), 0.0), 600.0)
    while True:
        choice = sselector.read_selection(selection_token)
        if choice is not None:
            return {"status": "selected", **choice,
                    "next_step": "Call make_interface with these candidate/strain_method/"
                                 "orthogonal_cell values (plus the original miller/size params)."}
        if time.monotonic() >= deadline:
            break
        time.sleep(1.0)
    return {
        "status": "pending",
        "selection_token": selection_token,
        "next_step": "The user has not confirmed a selection yet — either call "
                     "this again with wait_seconds=180 (one retry max), or ask "
                     "them to open the selector page and press Confirm selection / state "
                     "the candidate number in chat.",
    }


@mcp.tool()
def make_interface(
    film_path: str,
    substrate_path: str,
    film_miller: list[int],
    substrate_miller: list[int],
    film_thickness: float = 3,
    substrate_thickness: float = 4,
    gap: float = 2.0,
    vacuum: float = 15.0,
    max_area: float = 200.0,
    max_strain: float = 0.05,
    termination: int = 0,
    candidate: int = -1,
    orthogonal_cell: bool = False,
    strain_method: str = "film",
    output_path: str | None = None,
    selection_policy: str = "ask",
    inherit_survey_params: bool = True,
    auto_atom_budget: int = 500,
) -> dict:
    """Build a film/substrate heterostructure with automatic coincidence-lattice
    (ZSL) matching — handles large unit-cell mismatch (e.g. SiC on Si).

    SELECTION GATE (unbypassable): the user must choose. This tool BUILDS
    only when one of three things is true:
      (1) the user confirmed a choice in the browser dialog (auto-applied);
      (2) `selection_policy="auto"` — the user opted into auto mode;
      (3) an explicit `candidate` index (>=0) is given AND the candidate
          survey page for this exact interface already exists on disk (proof
          the options were shown to the user first).
    In every other case — including a bare first call, or an explicit
    `candidate` when the survey was never generated — it does NOT build and
    returns status="needs_selection" with a selector_url dialog (strain-vs-
    size scatter + table + strain_method choice). Paste that link for the
    user. There is no boolean to skip this: you cannot pick a candidate the
    user never saw. Candidate indices come ONLY from a list_interface_matches
    survey the user has actually seen — never invent one.

    AUTO MODE: pass selection_policy="auto" ONLY after the user opted into
    auto mode for this session. The candidate is then picked by the fixed,
    auditable policy (expert-confirmed): among candidates with n_atoms <=
    `auto_atom_budget` (default 500) — lowest von Mises strain, tie-break by
    fewest atoms; if NO candidate fits the budget the same policy runs on
    the full pool and the result carries `atom_budget_exceeded`. The survey
    page is still generated and the result records the policy and the
    numbers behind the pick. Physics gates (verify_inputs etc.) are never
    bypassed by auto mode.

    SURVEY PARAM INHERITANCE (default on): when a survey exists for this
    orientation, the build inherits the survey's film/substrate thickness,
    max_area, max_strain and termination, so the structure matches the
    survey rows the user saw (candidate ordering AND atom counts depend on
    them). Pass inherit_survey_params=False only when the user explicitly
    asks to rebuild with different geometry — and prefer re-running
    list_interface_matches instead, so the user picks from the new pool.

    Thicknesses in layers, gap/vacuum in Å. `candidate` indexes the
    list_interface_matches ordering.
    `strain_method` decides who absorbs the mismatch: "film" (default),
    "substrate", or "average" (split evenly) — result reports the per-side
    strain components either way.
    All atoms come wrapped inside the cell. Hexagonal-plane orientations such as
    (111) give the correct primitive cell with gamma=60/120 deg; set
    orthogonal_cell=true for the rectangular a x sqrt(3)a setting (2x atoms)."""
    applied_selection = None
    survey_page = _survey_page_path(film_path, substrate_path, film_miller,
                                    substrate_miller, termination)
    inherited = None
    params_file = survey_page.with_suffix(".params.json")
    if inherit_survey_params and params_file.is_file():
        inherited = json.loads(params_file.read_text(encoding="utf-8"))
        film_thickness = inherited.get("film_thickness", film_thickness)
        substrate_thickness = inherited.get("substrate_thickness",
                                            substrate_thickness)
        max_area = inherited.get("max_area", max_area)
        max_strain = inherited.get("max_strain", max_strain)
        termination = inherited.get("termination", termination)
    token = _interface_token(film_path, substrate_path, film_miller,
                             substrate_miller, max_area, max_strain, termination)
    recorded = sselector.read_selection(token)

    def _gate():
        """Emit the survey + selector dialog; build nothing."""
        survey = _interface_survey(
            film_path, substrate_path, film_miller, substrate_miller,
            film_thickness, substrate_thickness, max_area, max_strain,
            termination, 20,
        )
        survey["status"] = "needs_selection"
        survey["next_step"] = (
            "Nothing was built yet (selection gate). Give the user the "
            "selector_url link to pick a candidate + strain method; after they "
            "confirm in the browser, call make_interface again with the same "
            "arguments (the recorded choice auto-applies). If the user names a "
            "candidate in chat, re-call with that explicit candidate index — "
            "the survey page now exists, so the gate will accept it. To run "
            "hands-off, the user must first opt into auto mode "
            "(selection_policy=\"auto\")."
        )
        return survey

    if recorded is not None:
        candidate = int(recorded.get("candidate", candidate))
        strain_method = recorded.get("strain_method", strain_method) or strain_method
        orthogonal_cell = bool(recorded.get("orthogonal_cell", orthogonal_cell))
        applied_selection = recorded
    elif selection_policy == "auto":
        survey = _interface_survey(
            film_path, substrate_path, film_miller, substrate_miller,
            film_thickness, substrate_thickness, max_area, max_strain,
            termination, 20,
        )
        pool = survey.get("candidates") or []
        if not pool:
            return {**survey, "status": "no_candidates",
                    "next_step": "No lattice match within max_strain/max_area; loosen them or change orientations."}
        within_budget = [c for c in pool if c["n_atoms"] <= auto_atom_budget]
        best = min(within_budget or pool,
                   key=lambda c: (c["von_mises_strain_pct"], c["n_atoms"]))
        candidate = int(best["candidate"])
        applied_selection = {
            "policy": ("auto: min von Mises strain, tie-break min atoms, "
                       f"among candidates <= {auto_atom_budget} atoms"),
            "candidate": candidate,
            "von_mises_strain_pct": best["von_mises_strain_pct"],
            "n_atoms": best["n_atoms"],
            "strain_method": strain_method,
            "selector_html": survey.get("selector_html"),
        }
        if not within_budget:
            applied_selection["atom_budget_exceeded"] = (
                f"no candidate <= {auto_atom_budget} atoms (smallest: "
                f"{min(c['n_atoms'] for c in pool)}); policy ran on the full "
                "pool — consider other orientations or a smaller max_area")
    elif candidate >= 0 and survey_page.is_file():
        # explicit chat choice, and the options were demonstrably shown first
        applied_selection = {"candidate": candidate, "strain_method": strain_method,
                             "source": "explicit candidate (survey already shown)"}
    else:
        # bare first call, or a candidate guessed before the survey existed
        return _gate()
    film = sio.load(film_path)
    substrate = sio.load(substrate_path)
    new, summary = ops.make_interface(
        film, substrate, film_miller, substrate_miller,
        film_thickness=film_thickness, substrate_thickness=substrate_thickness,
        gap=gap, vacuum=vacuum, max_area=max_area, max_strain=max_strain,
        termination=termination, candidate=candidate,
        orthogonal_cell=orthogonal_cell, strain_method=strain_method,
    )
    fp, sp = Path(film_path), Path(substrate_path)
    out = Path(output_path) if output_path else fp.with_name(
        f"{fp.stem}_on_{sp.stem}.vasp")
    sio.save(new, out)
    result = {**summary, **_artifacts(new, out), **sio.describe(new)}
    result["applied_selection"] = applied_selection
    if inherited is not None:
        result["survey_params_inherited"] = {
            k: inherited[k] for k in ("film_thickness", "substrate_thickness",
                                      "max_area", "max_strain", "termination")
            if k in inherited}
    if "policy" in applied_selection:
        result["selection_source"] = "auto policy (user opted into auto mode)"
    elif "source" in applied_selection:
        result["selection_source"] = "explicit candidate (survey already shown)"
    else:
        result["selection_source"] = "selector dialog (recorded choice)"
    return result


@mcp.tool()
def make_vacancy(input_path: str, element: str | None = None,
                 site: str | int | None = None,
                 symprec: float = 0.1, output_path: str | None = None) -> dict:
    """Remove one atom: by `site` spec, or the first symmetry-inequivalent site
    of `element` (spglib Wyckoff classes; deterministic).
    `site` accepts EXACTLY the viewer's dual numbering (step 262, always 1-based):
    "O37" = the 37th O atom, "#69" or 69 = global site 69. Pass the user's own
    wording verbatim — never convert between numberings yourself."""
    s = sio.load(input_path)
    index0 = rt_sites.resolve_site(s, site) if site is not None else None
    picked = rt_sites.site_label(s, index0) if index0 is not None else None
    new, summary = ops.make_vacancy(s, element=element, index=index0, symprec=symprec)
    out = sio.save(new, _out(input_path, "vacancy", output_path))
    if picked:
        summary = {**summary, "resolved_site": picked}
    return {**summary, **_artifacts(new, out), **sio.describe(new)}


@mcp.tool()
def list_adsorption_sites(slab_path: str, distance: float = 2.0) -> dict:
    """Enumerate symmetry-distinct adsorption sites (ontop/bridge/hollow) on a
    slab, with Cartesian positions `distance` Å above the surface.
    Use before add_adsorbate to see what is available."""
    s = sio.load(slab_path)
    return ops.list_adsorption_sites(s, distance=distance)


@mcp.tool()
def make_molecule(
    molecule: str,
    box: float = 15.0,
    output_path: str | None = None,
) -> dict:
    """Build a gas-phase molecule centered in a cubic vacuum box — THE way to
    make gas references (E_O3, E_O2, E_H2O, …) for adsorption and reaction
    energies. `molecule` = ASE g2 name (162 molecules: O3, O2, CO, H2O, NH3,
    …), a built-in (OOH/COOH/HCOO), or a molecule file path — the same
    sources as add_adsorbate, so gas reference and adsorbate share one
    geometry. Default 15 Å box; Gamma-only k-sampling.

    NEVER hand-write a molecule POSCAR instead: a hand-placed symmetric
    guess has zero symmetry-breaking force and relaxes to a saddle point,
    not the ground state (live incident: three collinear O as "O3" stayed
    linear through every relax — the g2 O3 is properly bent, 1.30 Å/116.3°).
    The result reports bond lengths and angles — check them against the
    expected molecular geometry before submitting."""
    new, summary = ops.make_molecule(molecule, box=box)
    out = sio.save(new, Path(output_path) if output_path
                   else Path("runs") / "molecules" / f"{molecule}_box.vasp")
    return {**summary, **_artifacts(new, out), **sio.describe(new)}


@mcp.tool()
def add_adsorbate(
    slab_path: str,
    adsorbate: str,
    site_type: str = "ontop",
    site_index: int = 0,
    height: float = 2.0,
    anchor_element: str | None = None,
    custom_position: list[float] | None = None,
    repeat: list[int] | None = None,
    recenter: bool = True,
    output_path: str | None = None,
) -> dict:
    """Place an adsorbate molecule on a slab. `adsorbate` = ASE g2 name
    (162 molecules: CO, OH, H2O, NH3, O2, N2, CH4, …) or a molecule file path.
    `anchor_element` fixes which atom binds the surface (e.g. "C" for CO on
    metals — chemistry matters, set it deliberately); default anchors the
    lowest atom. site_type ontop|bridge|hollow with site_index from
    list_adsorption_sites; custom_position=[fa,fb] overrides. repeat=[nx,ny]
    expands the slab first for coverage control.

    By default the finished cell is rigidly shifted in-plane so the adsorbate
    anchor sits at the visual center (0.5, 0.5) — an exact PBC operation that
    preserves the site registry on any slab, relaxed or not (user ruling,
    step 278). `recenter=False` keeps the raw site position; an explicit
    `custom_position` is never overridden.

    Physics rule (expert): adsorbate periodic images should sit ~10 Å apart
    to isolate spurious image-image interactions — the result reports
    `image_separation` and, when it falls short, an
    `image_separation_warning` with the repeat that fixes it. Heed the
    warning unless a high-coverage study is intended."""
    s = sio.load(slab_path)
    new, summary = ops.add_adsorbate(
        s, adsorbate, site_type=site_type, site_index=site_index, height=height,
        anchor_element=anchor_element, custom_position=custom_position, repeat=repeat,
        recenter=recenter,
    )
    out = sio.save(new, _out(slab_path, "ads", output_path))
    return {**summary, **_artifacts(new, out), **sio.describe(new)}


@mcp.tool()
def add_solvent(
    input_path: str,
    molecule: str = "H2O",
    count: int | None = None,
    density: float | None = None,
    z_range: list[float] | None = None,
    gap: float = 2.5,
    top_margin: float = 2.0,
    min_dist: float = 2.0,
    seed: int = 42,
    max_tries: int = 5000,
    output_path: str | None = None,
) -> dict:
    """Pack a liquid/solvent layer into the vacuum region of a slab — the
    liquid half of a solid–liquid interface model (e.g. Pt(111) + H2O).

    `molecule` = ASE g2 name (H2O, NH3, CH3OH, …) or a molecule file path.
    Amount: `count` molecules, or `density` in g/cm³ (default 1.0, liquid
    water); the tool converts density → count over the packing region.
    Region: `z_range=[z_lo, z_hi]` Å, or by default from `gap` Å above the
    topmost atom up to `top_margin` Å below the cell top — make sure the
    slab has enough vacuum first (add_vacuum).

    Packing is seeded rejection sampling (BACKENDS §21): random rigid
    positions/orientations, accepted only when every interatomic contact
    (slab + already-placed molecules, PBC min-image) stays >= `min_dist` Å.
    Same seed → identical structure (deterministic artifact). The result is
    a STARTING configuration — equilibrate with AIMD/relaxation before
    extracting physics. Typical flow: generate_slab → make_supercell →
    add_vacuum (room for the liquid) → add_solvent → validate_geometry."""
    s = sio.load(input_path)
    new, summary = ops.add_solvent(
        s, molecule=molecule, count=count, density=density, z_range=z_range,
        gap=gap, top_margin=top_margin, min_dist=min_dist, seed=seed,
        max_tries=max_tries,
    )
    out = sio.save(new, _out(input_path, "solv", output_path))
    return {**summary, **_artifacts(new, out), **sio.describe(new)}


@mcp.tool()
def stack_structures(
    base_path: str,
    addon_path: str,
    gap: float = 2.0,
    vacuum_above: float | None = None,
    output_path: str | None = None,
) -> dict:
    """Stack one structure above another along z in the BASE cell — the
    generic composition primitive for cases make_interface (crystal↔crystal
    ZSL matching) cannot cover, e.g. a pre-equilibrated water box or an
    amorphous film onto a slab.

    In-plane lattices must agree within 2 % / 2° (else rebuild the addon in
    the base cell). The addon's bottom lands `gap` Å above the base's top
    atom. `vacuum_above` grows c to fit (addon_top + vacuum_above); without
    it the base cell must already have room. Base selective_dynamics flags
    are kept; addon atoms relax free. Finish with validate_geometry."""
    b = sio.load(base_path)
    a = sio.load(addon_path)
    new, summary = ops.stack_structures(b, a, gap=gap, vacuum_above=vacuum_above)
    out = sio.save(new, _out(base_path, "stack", output_path))
    return {**summary, **_artifacts(new, out), **sio.describe(new)}


@mcp.tool()
def passivate_surface(
    input_path: str,
    passivant: str = "H",
    tolerance: float = 1.30,
    which: str = "all",
    expected_coordination: dict | None = None,
    output_path: str | None = None,
) -> dict:
    """Saturate dangling bonds with a monoatomic passivant (default H).

    Detects undercoordinated atoms (ASE NeighborList, Cordero covalent radii
    x `tolerance` — the SAME bond criterion as the 3D viewer, BACKENDS §18)
    against each element's modal coordination in the structure; override with
    `expected_coordination={"Si": 4}` for very thin slabs. `which` = "all"
    (default: both faces + internal defects) | "top" | "bottom". Placement:
    one missing bond -> opposite the existing-bond sum; two missing -> exact
    tetrahedral completion pair (Si(001) SiH2 dihydride). X-H distance =
    covalent radii sum. Sites the geometry cannot decide (no neighbors, >=3
    missing bonds, degenerate directions) are returned in `unpassivated` —
    the tool never guesses; report them to the user. Always follow with
    validate_geometry; positions are ideal starting points for relaxation,
    not relaxed geometry."""
    s = sio.load(input_path)
    new, summary = ops.passivate_surface(
        s, passivant=passivant, tolerance=tolerance, which=which,
        expected_coordination=expected_coordination,
    )
    out = sio.save(new, _out(input_path, "passivated", output_path))
    return {**summary, **_artifacts(new, out), **sio.describe(new)}


@mcp.tool()
def substitute_atom(
    input_path: str,
    new_element: str,
    site: str | int | None = None,
    element: str | None = None,
    inequivalent_class: int = 0,
    symprec: float = 0.1,
    output_path: str | None = None,
) -> dict:
    """Substitutional doping: replace one atom by `site` spec, or the
    representative of the `inequivalent_class`-th symmetry class of `element`
    (see list_inequivalent_sites). For dilute doping build a supercell first.
    `site` accepts EXACTLY the viewer's dual numbering (step 262, always 1-based):
    "O37" = the 37th O atom, "#69" or 69 = global site 69. Pass the user's own
    wording verbatim — never convert between numberings yourself; the result's
    `resolved_site` echoes both notations for the user to confirm."""
    s = sio.load(input_path)
    index0 = rt_sites.resolve_site(s, site) if site is not None else None
    picked = rt_sites.site_label(s, index0) if index0 is not None else None
    new, summary = ops.substitute_atom(
        s, new_element, index=index0, element=element,
        inequivalent_class=inequivalent_class, symprec=symprec,
    )
    out = sio.save(new, _out(input_path, "sub", output_path))
    if picked:
        summary = {**summary, "resolved_site": picked}
    return {**summary, **_artifacts(new, out), **sio.describe(new)}


@mcp.tool()
def make_interstitial(
    input_path: str,
    element: str,
    frac_position: list[float] | None = None,
    candidate: int = 0,
    min_dist: float = 1.2,
    output_path: str | None = None,
) -> dict:
    """Insert an interstitial atom. Give frac_position=[fa,fb,fc] explicitly,
    or omit it to use the `candidate`-th Voronoi-clearance candidate (ranked
    by distance to existing atoms). min_dist guards against overlaps."""
    s = sio.load(input_path)
    new, summary = ops.make_interstitial(
        s, element, frac_position=frac_position, candidate=candidate, min_dist=min_dist,
    )
    out = sio.save(new, _out(input_path, "inter", output_path))
    return {**summary, **_artifacts(new, out), **sio.describe(new)}


@mcp.tool()
def reconstruct_surface(
    input_path: str,
    reconstruction: str,
    min_slab_size: float = 10.0,
    min_vacuum_size: float = 15.0,
    termination: int = 0,
    output_path: str | None = None,
) -> dict:
    """Build a named surface reconstruction from bulk (pymatgen curated
    archive, e.g. fcc_110_missing_row_1x2, fcc_111_adatom_t_1x1,
    diamond_100_2x1). Wrong names error with the full available list.
    Output follows the project slab conventions (bottom-aligned, c ⊥ z)."""
    bulk = sio.load(input_path)
    new, summary = ops.reconstruct_surface(
        bulk, reconstruction, min_slab_size=min_slab_size,
        min_vacuum_size=min_vacuum_size, termination=termination,
    )
    out = sio.save(new, _out(input_path, "recon", output_path))
    return {**summary, **_artifacts(new, out), **sio.describe(new)}


@mcp.tool()
def make_stacking_fault(
    input_path: str,
    shift: list[float],
    z_cut: float = 0.5,
    output_path: str | None = None,
) -> dict:
    """Generalized stacking fault: shift every atom with fractional z > z_cut
    by in-plane fractional [fa, fb]. Apply to a slab; scan shift values for a
    γ-surface. fcc(111) intrinsic SF example: shift=[1/3, 1/3] variants."""
    s = sio.load(input_path)
    new, summary = ops.make_stacking_fault(s, shift, z_cut=z_cut)
    out = sio.save(new, _out(input_path, "sf", output_path))
    return {**summary, **_artifacts(new, out), **sio.describe(new)}


@mcp.tool()
def make_grain_boundary(
    input_path: str,
    rotation_axis: list[int],
    rotation_angle: float,
    plane: list[int] | None = None,
    expand_times: int = 2,
    vacuum_thickness: float = 0.0,
    ab_shift: list[float] | None = None,
    rm_ratio: float = 0.0,
    output_path: str | None = None,
) -> dict:
    """Coincidence-site-lattice grain boundary or twin from bulk. fcc coherent
    twin (Σ3): rotation_axis=[1,1,1], rotation_angle=60, plane=[1,1,1].
    Result reports the computed sigma. rm_ratio removes too-close boundary
    atoms if needed."""
    bulk = sio.load(input_path)
    new, summary = ops.make_grain_boundary(
        bulk, rotation_axis, rotation_angle, plane=plane,
        expand_times=expand_times, vacuum_thickness=vacuum_thickness,
        ab_shift=ab_shift, rm_ratio=rm_ratio,
    )
    out = sio.save(new, _out(input_path, "gb", output_path))
    return {**summary, **_artifacts(new, out), **sio.describe(new)}


@mcp.tool()
def list_inequivalent_sites(path: str, symprec: float = 0.1) -> dict:
    """List symmetry-inequivalent site classes (element, multiplicity, representative index)."""
    s = sio.load(path)
    classes = ops.list_inequivalent_sites(s, symprec=symprec)
    return {"n_classes": len(classes), "classes": classes}


@mcp.tool()
def validate_geometry(path: str, calc_type: str = "generic") -> dict:
    """Sanity-check a structure (overlaps, degenerate lattice, thin vacuum).
    calc_type: generic | slab. Returns {valid, issues[]}."""
    s = sio.load(path)
    return sval.validate_geometry(s, calc_type=calc_type)


@mcp.tool()
def analyze_symmetry(path: str, symprec: float = 0.1) -> dict:
    """Spacegroup symbol/number, crystal system, point group, inequivalent sites (spglib)."""
    s = sio.load(path)
    return sval.analyze_symmetry(s, symprec=symprec)


@mcp.tool()
def interpolate_structures(
    input_start: str,
    input_end: str,
    n_images: int = 9,
    extend: float = 0.2,
    output_dir: str | None = None,
) -> dict:
    """Linear geometry path between two relaxed structures of the SAME
    system — THE input for configuration-coordinate (CC) diagrams of
    carrier capture (two charge states of one defect; VASP CONTCARs keep
    atom order, so any two charge-state CONTCARs from a defect ensemble
    work directly). Also usable as NEB seeds. x runs -extend…1+extend
    (x=0 -> input_start, x=1 -> input_end); the extension lets parabola
    fits see both sides of each minimum. Writes image_00.vasp… plus
    cc_path.json (x values, mass-weighted dQ in amu^1/2·Å — the
    configuration-coordinate span, and per-image Q) into output_dir.
    Feed the directory to calc_defect_ensemble action="cc_plan"."""
    s_i = sio.load(input_start)
    s_f = sio.load(input_end)
    pairs, summary = ops.interpolate_structures(
        s_i, s_f, n_images=n_images, extend=extend)
    base = Path(output_dir) if output_dir else (
        Path(input_start).resolve().parent / "cc_images")
    base.mkdir(parents=True, exist_ok=True)
    files = []
    for k, (x, img) in enumerate(pairs):
        out = sio.save(img, base / f"image_{k:02d}.vasp")
        files.append(str(out))
    (base / "cc_path.json").write_text(json.dumps({
        **{k: v for k, v in summary.items() if k != "op"},
        "start": str(Path(input_start).resolve()),
        "end": str(Path(input_end).resolve()),
        "images": files}, indent=2))
    return {**summary, "output_dir": str(base.resolve()),
            "cc_path_json": str((base / "cc_path.json").resolve()),
            "note": ("images are single-point geometries — do NOT relax "
                     "them; run calc_defect_ensemble cc_plan next")}


@mcp.tool()
def convert_structure(input_path: str, output_path: str, fmt: str | None = None) -> dict:
    """Convert between structure formats (POSCAR/CIF native; ~100 formats via ASE).
    Format inferred from output_path extension unless `fmt` given."""
    s = sio.load(input_path)
    out = sio.save(s, output_path, fmt=fmt)
    return {"op": "convert_structure", "artifact": str(out.resolve()), **sio.describe(s)}


@mcp.tool()
def render_structure(
    input_path: str,
    fmt: str = "png",
    rotation: str = "10x,-80y",
    repeat: list[int] | None = None,
    output_path: str | None = None,
):
    """Visualize a structure IN the conversation. fmt='png' returns the rendered
    image inline (the model sees it directly — use this to self-check edits) and
    also writes it next to the input. fmt='html' writes an interactive 3Dmol.js
    page for humans. `repeat` (e.g. [2,2,1]) tiles the cell for visual context only."""
    from mcp.server.fastmcp import Image

    s = sio.load(input_path)
    p = Path(input_path)
    if fmt == "png":
        out = Path(output_path) if output_path else p.with_suffix(".png")
        srender.render_png(s, out, rotation=rotation,
                           repeat=tuple(repeat) if repeat else (1, 1, 1))
        summary = {"op": "render_structure", "fmt": fmt,
                   "artifact": str(out.resolve()), **sio.describe(s)}
        # viewer link for HUMANS (the workbench right pane) — without it the
        # agent only has a file path to show, which the chat UI can't linkify
        url = _static_url(out)
        if url:
            summary["viewer_url"] = url
        # inline image → multimodal agent sees the structure in the tool result
        return [Image(path=str(out)), summary]
    if fmt == "html":
        out = Path(output_path) if output_path else p.with_suffix(".html")
        srender.render_html(s, out, title=p.stem, script_url=_asset_url())
        result = {"op": "render_structure", "fmt": fmt,
                  "artifact": str(out.resolve()), **sio.describe(s)}
        url = _static_url(out)
        if url:
            result["interactive_html_url"] = url
        return result
    raise ValueError("fmt must be 'png' or 'html'")


@mcp.tool()
def search_materials(
    elements: list[str] | None = None,
    exclude_elements: list[str] | None = None,
    chemsys: str | None = None,
    formula: str | None = None,
    spacegroup_symbol: str | None = None,
    spacegroup_number: int | None = None,
    band_gap: list[float] | None = None,
    energy_above_hull: list[float] | None = None,
    nsites: list[int] | None = None,
    nelements: list[int] | None = None,
    is_metal: bool | None = None,
    theoretical: bool | None = None,
    limit: int = 20,
    db_path: str | None = None,
) -> dict:
    """Search the Materials Project (online API with MP_API_KEY, or a local snapshot) by
    composition and properties. `elements` = must contain at least these;
    `chemsys` (e.g. "Si-O") = exactly these elements; `formula` matches the
    reduced formula (e.g. "TiO2"). Ranges are inclusive [min, max]:
    band_gap / energy_above_hull in eV (e_hull [0, 0.05] ≈ stable), nsites,
    nelements. Returns compact summary rows — follow up with get_material to
    fetch a structure."""
    kw = dict(elements=elements, exclude_elements=exclude_elements, chemsys=chemsys,
              formula=formula, spacegroup_symbol=spacegroup_symbol,
              spacegroup_number=spacegroup_number, band_gap=band_gap,
              energy_above_hull=energy_above_hull, nsites=nsites, nelements=nelements,
              is_metal=is_metal, theoretical=theoretical, limit=limit)
    if _mp_backend(db_path) == "local":
        return matdb.search(db_path=db_path, **kw)
    return mpapi.search(**kw)


@mcp.tool()
def get_material(material_id: str, output_path: str | None = None,
                 db_path: str | None = None) -> dict:
    """Fetch one structure from the Materials Project (online API or local snapshot) by ID
    (e.g. "mp-149") and write it as a POSCAR artifact (default:
    ./<material_id>.vasp) — the path feeds directly into the other tools."""
    if _mp_backend(db_path) == "local":
        structure, summary = matdb.get_structure(material_id, db_path)
    else:
        structure, summary = mpapi.get_structure(material_id)
    out = sio.save(structure, Path(output_path) if output_path else Path(f"{material_id}.vasp"))
    return {**summary, **_artifacts(structure, out), **sio.describe(structure)}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
