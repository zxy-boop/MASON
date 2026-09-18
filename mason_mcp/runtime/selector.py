"""Interactive interface-match selector (DEVLOG step 60).

The user picks a ZSL candidate + strain method in a browser page (served by the
seed static server); the page calls GET /__seed_select on the same server,
which persists the choice under .seed_selections/<token>.json. The agent then
reads it back with the read_interface_selection tool and calls make_interface.
Pure MCP + static HTML — no opencode internals (CCD_PROGRAM §3).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
SELECTION_DIR = ".seed_selections"


def _selection_dir() -> Path:
    d = Path.cwd() / SELECTION_DIR
    d.mkdir(exist_ok=True)
    return d


def valid_token(token: str) -> bool:
    return bool(_TOKEN_RE.match(token or ""))


def record_selection(token: str, candidate: int, strain_method: str,
                     orthogonal_cell: bool) -> Path:
    """Persist a user's choice (called by the static-server endpoint)."""
    if not valid_token(token):
        raise ValueError("invalid selection token")
    if strain_method not in ("film", "substrate", "average"):
        raise ValueError("invalid strain_method")
    path = _selection_dir() / f"{token}.json"
    path.write_text(json.dumps({
        "token": token,
        "candidate": int(candidate),
        "strain_method": strain_method,
        "orthogonal_cell": bool(orthogonal_cell),
    }, indent=2) + "\n", encoding="utf-8")
    return path


def read_selection(token: str) -> dict | None:
    if not valid_token(token):
        raise ValueError("invalid selection token")
    path = _selection_dir() / f"{token}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def render_match_selector(survey: dict, token: str, out_path: Path,
                          title: str) -> Path:
    """Self-contained selector page: strain-vs-atoms scatter, candidate table,
    strain-method choice, confirm button posting to /__seed_select."""
    payload = json.dumps({
        "token": token,
        "title": title,
        "film": survey.get("film_formula", "film"),
        "substrate": survey.get("substrate_formula", "substrate"),
        "candidates": survey["candidates"],
        "termination": survey.get("termination"),
        "n_within": survey.get("n_candidates_within_strain"),
    }, ensure_ascii=False)
    html = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>__TITLE__ — interface match selector</title>
<style>
 body { font-family: -apple-system, "PingFang SC", sans-serif; margin: 24px; background: #fafafa; color: #222; }
 h2 { margin: 0 0 4px; } .sub { color: #777; margin-bottom: 16px; }
 .wrap { display: flex; gap: 24px; flex-wrap: wrap; }
 svg { background: #fff; border: 1px solid #ddd; border-radius: 8px; }
 table { border-collapse: collapse; background: #fff; border: 1px solid #ddd; border-radius: 8px; font-size: 13px; }
 th, td { padding: 6px 10px; border-bottom: 1px solid #eee; text-align: right; }
 th { background: #f0f0f0; } td:first-child, th:first-child { text-align: center; }
 tr.sel { background: #e8f1ff; } tr { cursor: pointer; }
 .panel { background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 14px 18px; min-width: 260px; }
 .panel label { display: block; margin: 6px 0; cursor: pointer; }
 button { margin-top: 12px; padding: 8px 22px; font-size: 14px; border: none; border-radius: 6px;
          background: #2563eb; color: #fff; cursor: pointer; }
 button:disabled { background: #9ca3af; }
 #status { margin-top: 10px; font-size: 13px; }
 .ok { color: #047857; } .err { color: #b91c1c; }
 circle.pt { fill: #2563eb; opacity: .75; cursor: pointer; } circle.pt.sel { fill: #dc2626; r: 7; opacity: 1; }
</style>
</head>
<body>
<h2>__TITLE__</h2>
<div class="sub">termination: <span id="term"></span> · <span id="nw"></span> matches within the strain limit · click a point or a table row, choose the strain assignment on the right, then confirm</div>
<div class="sub" id="mmnote"></div>
<div class="wrap">
  <div>
    <svg id="plot" width="440" height="340"></svg>
  </div>
  <table id="tbl"><thead><tr>
    <th>#</th><th>strain %</th><th>atoms</th><th>a (Å)</th><th>b (Å)</th><th>γ°</th><th>area Å²</th><th>mismatch ε11 %</th><th>mismatch ε22 %</th>
  </tr></thead><tbody></tbody></table>
  <div class="panel">
    <b>Strain assignment: which side carries the mismatch</b>
    <label><input type="radio" name="sm" value="film" checked> <span class="smA"></span> carries all of the mismatch</label>
    <label><input type="radio" name="sm" value="substrate"> <span class="smB"></span> carries all of the mismatch</label>
    <label><input type="radio" name="sm" value="average"> shared equally by both sides</label>
    <b style="display:block;margin-top:10px">Cell shape</b>
    <label><input type="checkbox" id="ortho"> orthogonal_cell — convert the hexagonal cell to an a×√3a rectangular cell (atoms ×2)</label>
    <button id="go">Confirm selection</button>
    <div id="status"></div>
  </div>
</div>
<script>
const DATA = __PAYLOAD__;
let selected = 0;
document.getElementById('term').textContent = (DATA.termination || []).join(' / ');
document.getElementById('nw').textContent = DATA.n_within;
document.querySelector('.smA').textContent = DATA.film;
document.querySelector('.smB').textContent = DATA.substrate;
document.getElementById('mmnote').textContent =
  `Mismatch ε = lattice difference of ${DATA.film} relative to ${DATA.substrate}; the strain goes to the side you select`;
const tb = document.querySelector('#tbl tbody');
DATA.candidates.forEach((c, i) => {
  const tr = document.createElement('tr');
  tr.innerHTML = `<td>${c.candidate}</td><td>${c.von_mises_strain_pct}</td><td>${c.n_atoms}</td>`
    + `<td>${c.in_plane_ab[0]}</td><td>${c.in_plane_ab[1]}</td><td>${c.gamma_deg}</td><td>${c.area_A2}</td>`
    + `<td>${c.film_vs_substrate_mismatch.e11_pct}</td><td>${c.film_vs_substrate_mismatch.e22_pct}</td>`;
  tr.onclick = () => select(i);
  tb.appendChild(tr);
});
const svg = document.getElementById('plot');
const W = 440, H = 340, ML = 52, MR = 24, MT = 20, MB = 44;
const xs = DATA.candidates.map(c => Math.max(c.von_mises_strain_pct, 1e-3));
const ys = DATA.candidates.map(c => c.n_atoms);
const lx = v => Math.log10(v), xmin = Math.min(...xs.map(lx)) - .1, xmax = Math.max(...xs.map(lx)) + .1;
const ymin = Math.min(...ys.map(lx)) - .1, ymax = Math.max(...ys.map(lx)) + .1;
const X = v => ML + (lx(v) - xmin) / (xmax - xmin || 1) * (W - ML - MR);
const Y = v => H - MB - (lx(v) - ymin) / (ymax - ymin || 1) * (H - MT - MB);
svg.innerHTML = `<line x1="${ML}" y1="${H-MB}" x2="${W-MR}" y2="${H-MB}" stroke="#999"/>`
              + `<line x1="${ML}" y1="${MT}" x2="${ML}" y2="${H-MB}" stroke="#999"/>`
              + `<text x="${ML + (W-ML-MR)/2}" y="${H-8}" text-anchor="middle" font-size="13" fill="#556">von Mises strain % (log)</text>`
              + `<text x="16" y="${MT + (H-MT-MB)/2}" text-anchor="middle" font-size="13" fill="#556" transform="rotate(-90 16 ${MT + (H-MT-MB)/2})">atoms (log)</text>`;
DATA.candidates.forEach((c, i) => {
  const el = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
  el.setAttribute('cx', X(Math.max(c.von_mises_strain_pct, 1e-3)));
  el.setAttribute('cy', Y(c.n_atoms));
  el.setAttribute('r', 5);
  el.setAttribute('class', 'pt');
  el.addEventListener('click', () => select(i));
  svg.appendChild(el);
});
function select(i) {
  selected = i;
  document.querySelectorAll('#tbl tbody tr').forEach((tr, k) => tr.classList.toggle('sel', k === i));
  document.querySelectorAll('circle.pt').forEach((p, k) => p.classList.toggle('sel', k === i));
}
select(0);
document.getElementById('go').onclick = async () => {
  const sm = document.querySelector('input[name=sm]:checked').value;
  const ortho = document.getElementById('ortho').checked;
  const cand = DATA.candidates[selected].candidate;
  const q = `/__seed_select?token=${DATA.token}&candidate=${cand}&strain_method=${sm}&orthogonal_cell=${ortho ? 1 : 0}`;
  const st = document.getElementById('status');
  try {
    const r = await fetch(q);
    if (!r.ok) throw new Error(await r.text());
    st.className = 'ok';
    const who = sm === 'film' ? DATA.film : sm === 'substrate' ? DATA.substrate : 'both sides equally';
    st.textContent = `Recorded: candidate=${cand}, mismatch carried by ${who}${ortho ? ', orthogonal cell' : ''}. The agent is waiting and will continue automatically.`;
  } catch (e) {
    st.className = 'err';
    st.textContent = `Could not send the selection (${e}). Tell the agent directly: build the interface with candidate=${cand}, strain_method=${sm}${ortho ? ', orthogonal_cell=true' : ''}.`;
  }
};
</script>
</body>
</html>
"""
    html = html.replace("__TITLE__", title).replace("__PAYLOAD__", payload)
    out_path.write_text(html, encoding="utf-8")
    return out_path


# ------------------------------------------------------------ twist selector
def record_survey_meta(token: str, rows: list[dict]) -> Path:
    """Persist a survey's candidate rows beside the selection store so the
    read-back tool can map a picked candidate index to its physical params
    (e.g. twist (i, j)) without re-running the survey."""
    if not valid_token(token):
        raise ValueError("invalid selection token")
    path = _selection_dir() / f"{token}.meta.json"
    path.write_text(json.dumps({"token": token, "candidates": rows},
                               indent=2) + "\n", encoding="utf-8")
    return path


def read_survey_meta(token: str) -> list[dict] | None:
    if not valid_token(token):
        raise ValueError("invalid selection token")
    path = _selection_dir() / f"{token}.meta.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))["candidates"]


def render_twist_selector(survey: dict, token: str, out_path: Path,
                          title: str) -> Path:
    """Self-contained twist-pair selector page: angle-vs-atoms scatter +
    candidate table, confirm button posting to the same /__seed_select
    endpoint as the interface selector (strain_method is sent as the fixed
    dummy "film" — twists have no strain to distribute)."""
    payload = json.dumps({
        "token": token,
        "title": title,
        "formula": survey.get("formula", ""),
        "candidates": survey["candidates"],
    }, ensure_ascii=False)
    html = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>__TITLE__ — twist-angle selector</title>
<style>
 body { font-family: -apple-system, "PingFang SC", sans-serif; margin: 24px; background: #fafafa; color: #222; }
 h2 { margin: 0 0 4px; } .sub { color: #777; margin-bottom: 16px; }
 .wrap { display: flex; gap: 24px; flex-wrap: wrap; }
 svg { background: #fff; border: 1px solid #ddd; border-radius: 8px; }
 table { border-collapse: collapse; background: #fff; border: 1px solid #ddd; border-radius: 8px; font-size: 13px; }
 th, td { padding: 6px 10px; border-bottom: 1px solid #eee; text-align: right; }
 th { background: #f0f0f0; } td:first-child, th:first-child { text-align: center; }
 tr.sel { background: #e8f1ff; } tr { cursor: pointer; }
 .panel { background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 14px 18px; min-width: 240px; }
 button { margin-top: 12px; padding: 8px 22px; font-size: 14px; border: none; border-radius: 6px;
          background: #2563eb; color: #fff; cursor: pointer; }
 #status { margin-top: 10px; font-size: 13px; }
 .ok { color: #047857; } .err { color: #b91c1c; }
 circle.pt { fill: #2563eb; opacity: .75; cursor: pointer; } circle.pt.sel { fill: #dc2626; r: 7; opacity: 1; }
</style>
</head>
<body>
<h2>__TITLE__</h2>
<div class="sub">Twisted bilayer __FORMULA__ · <span id="nw"></span> commensurate angles · click a point or a table row, then confirm. Smaller angles give larger moiré cells; the atom count is the cost</div>
<div class="wrap">
  <div>
    <svg id="plot" width="460" height="340"></svg>
  </div>
  <table id="tbl"><thead><tr>
    <th>#</th><th>(i, j)</th><th>θ°</th><th>atoms</th><th>moiré constant Å</th>
  </tr></thead><tbody></tbody></table>
  <div class="panel">
    <b>After selection</b>
    <div style="font-size:13px;color:#555;margin-top:6px">The agent will call make_twisted_bilayer with this (i, j) to build the rigid twisted bilayer (no built-in strain; relaxation is left to the calculation).</div>
    <button id="go">Confirm selection</button>
    <div id="status"></div>
  </div>
</div>
<script>
const DATA = __PAYLOAD__;
let selected = 0;
document.getElementById('nw').textContent = DATA.candidates.length;
const tb = document.querySelector('#tbl tbody');
DATA.candidates.forEach((c, i) => {
  const tr = document.createElement('tr');
  tr.innerHTML = `<td>${c.candidate}</td><td>(${c.i}, ${c.j})</td><td>${c.angle_deg}</td>`
    + `<td>${c.n_atoms}</td><td>${c.moire_a_A}</td>`;
  tr.onclick = () => select(i);
  tb.appendChild(tr);
});
const svg = document.getElementById('plot');
const W = 460, H = 340, ML = 56, MR = 24, MT = 20, MB = 44;
const ys = DATA.candidates.map(c => c.n_atoms);
const ly = v => Math.log10(v);
const xmin = 0, xmax = 60;
const ymin = Math.min(...ys.map(ly)) - .1, ymax = Math.max(...ys.map(ly)) + .1;
const X = v => ML + (v - xmin) / (xmax - xmin) * (W - ML - MR);
const Y = v => H - MB - (ly(v) - ymin) / (ymax - ymin || 1) * (H - MT - MB);
svg.innerHTML = `<line x1="${ML}" y1="${H-MB}" x2="${W-MR}" y2="${H-MB}" stroke="#999"/>`
              + `<line x1="${ML}" y1="${MT}" x2="${ML}" y2="${H-MB}" stroke="#999"/>`
              + `<text x="${ML + (W-ML-MR)/2}" y="${H-8}" text-anchor="middle" font-size="13" fill="#556">twist angle θ (°)</text>`
              + `<text x="16" y="${MT + (H-MT-MB)/2}" text-anchor="middle" font-size="13" fill="#556" transform="rotate(-90 16 ${MT + (H-MT-MB)/2})">bilayer atoms (log)</text>`;
DATA.candidates.forEach((c, i) => {
  const el = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
  el.setAttribute('cx', X(c.angle_deg));
  el.setAttribute('cy', Y(c.n_atoms));
  el.setAttribute('r', 5);
  el.setAttribute('class', 'pt');
  el.addEventListener('click', () => select(i));
  svg.appendChild(el);
});
function select(i) {
  selected = i;
  document.querySelectorAll('#tbl tbody tr').forEach((tr, k) => tr.classList.toggle('sel', k === i));
  document.querySelectorAll('circle.pt').forEach((p, k) => p.classList.toggle('sel', k === i));
}
select(0);
document.getElementById('go').onclick = async () => {
  const c = DATA.candidates[selected];
  const q = `/__seed_select?token=${DATA.token}&candidate=${c.candidate}&strain_method=film&orthogonal_cell=0`;
  const st = document.getElementById('status');
  try {
    const r = await fetch(q);
    if (!r.ok) throw new Error(await r.text());
    st.className = 'ok';
    st.textContent = `Recorded: (i, j) = (${c.i}, ${c.j}), θ = ${c.angle_deg}°, ${c.n_atoms} atoms. The agent is waiting and will continue automatically.`;
  } catch (e) {
    st.className = 'err';
    st.textContent = `Could not send the selection (${e}). Tell the agent directly: build the twisted bilayer with i=${c.i}, j=${c.j}.`;
  }
};
</script>
</body>
</html>
"""
    html = (html.replace("__TITLE__", title)
                .replace("__FORMULA__", str(survey.get("formula", "")))
                .replace("__PAYLOAD__", payload))
    out_path.write_text(html, encoding="utf-8")
    return out_path


def render_twist_match_selector(survey: dict, token: str, out_path: Path,
                                title: str) -> Path:
    """Selector page for the general-lattice (ZSL) twist route: strain-vs-atoms
    scatter + candidate table with realized angle. Posts to the same
    /__seed_select endpoint (dummy strain_method, as render_twist_selector)."""
    payload = json.dumps({
        "token": token,
        "title": title,
        "formula": survey.get("formula", ""),
        "formula2": survey.get("layer2_formula", ""),
        "target": survey.get("target_angle_deg"),
        "candidates": survey["candidates"],
    }, ensure_ascii=False)
    html = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>__TITLE__ — twist ZSL match selector</title>
<style>
 body { font-family: -apple-system, "PingFang SC", sans-serif; margin: 24px; background: #fafafa; color: #222; }
 h2 { margin: 0 0 4px; } .sub { color: #777; margin-bottom: 16px; }
 .wrap { display: flex; gap: 24px; flex-wrap: wrap; }
 svg { background: #fff; border: 1px solid #ddd; border-radius: 8px; }
 table { border-collapse: collapse; background: #fff; border: 1px solid #ddd; border-radius: 8px; font-size: 13px; }
 th, td { padding: 6px 10px; border-bottom: 1px solid #eee; text-align: right; }
 th { background: #f0f0f0; } td:first-child, th:first-child { text-align: center; }
 tr.sel { background: #e8f1ff; } tr { cursor: pointer; }
 .panel { background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 14px 18px; min-width: 240px; }
 button { margin-top: 12px; padding: 8px 22px; font-size: 14px; border: none; border-radius: 6px;
          background: #2563eb; color: #fff; cursor: pointer; }
 #status { margin-top: 10px; font-size: 13px; }
 .ok { color: #047857; } .err { color: #b91c1c; }
 circle.pt { fill: #2563eb; opacity: .75; cursor: pointer; } circle.pt.sel { fill: #dc2626; r: 7; opacity: 1; }
</style>
</head>
<body>
<h2>__TITLE__</h2>
<div class="sub">Commensurate supercells near the target angle <span id="tg"></span>° · <span id="nw"></span> candidates · click a point or a table row, then confirm. The deviation from the target angle and the residual strain are both listed; both are costs of the construction</div>
<div class="wrap">
  <div>
    <svg id="plot" width="460" height="340"></svg>
  </div>
  <table id="tbl"><thead><tr>
    <th>#</th><th>θ°</th><th>Δθ°</th><th>strain %</th><th>atoms</th><th>a (Å)</th><th>b (Å)</th><th>γ°</th>
  </tr></thead><tbody></tbody></table>
  <div class="panel">
    <b>After selection</b>
    <div style="font-size:13px;color:#555;margin-top:6px">The agent will call make_twisted_bilayer_zsl with this candidate (by default layer 2 carries the residual strain; relaxation is left to the calculation).</div>
    <button id="go">Confirm selection</button>
    <div id="status"></div>
  </div>
</div>
<script>
const DATA = __PAYLOAD__;
let selected = 0;
document.getElementById('tg').textContent = DATA.target;
document.getElementById('nw').textContent = DATA.candidates.length;
const tb = document.querySelector('#tbl tbody');
DATA.candidates.forEach((c, i) => {
  const tr = document.createElement('tr');
  tr.innerHTML = `<td>${c.candidate}</td><td>${c.angle_deg}</td><td>${c.angle_offset_deg}</td>`
    + `<td>${c.von_mises_strain_pct}</td><td>${c.n_atoms}</td>`
    + `<td>${c.in_plane_ab[0]}</td><td>${c.in_plane_ab[1]}</td><td>${c.gamma_deg}</td>`;
  tr.onclick = () => select(i);
  tb.appendChild(tr);
});
const svg = document.getElementById('plot');
const W = 460, H = 340, ML = 56, MR = 24, MT = 20, MB = 44;
const xs = DATA.candidates.map(c => Math.max(c.von_mises_strain_pct, 1e-3));
const ys = DATA.candidates.map(c => c.n_atoms);
const lx = v => Math.log10(v);
const xmin = Math.min(...xs.map(lx)) - .1, xmax = Math.max(...xs.map(lx)) + .1;
const ymin = Math.min(...ys.map(lx)) - .1, ymax = Math.max(...ys.map(lx)) + .1;
const X = v => ML + (lx(v) - xmin) / (xmax - xmin || 1) * (W - ML - MR);
const Y = v => H - MB - (lx(v) - ymin) / (ymax - ymin || 1) * (H - MT - MB);
svg.innerHTML = `<line x1="${ML}" y1="${H-MB}" x2="${W-MR}" y2="${H-MB}" stroke="#999"/>`
              + `<line x1="${ML}" y1="${MT}" x2="${ML}" y2="${H-MB}" stroke="#999"/>`
              + `<text x="${ML + (W-ML-MR)/2}" y="${H-8}" text-anchor="middle" font-size="13" fill="#556">residual von Mises strain % (log)</text>`
              + `<text x="16" y="${MT + (H-MT-MB)/2}" text-anchor="middle" font-size="13" fill="#556" transform="rotate(-90 16 ${MT + (H-MT-MB)/2})">bilayer atoms (log)</text>`;
DATA.candidates.forEach((c, i) => {
  const el = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
  el.setAttribute('cx', X(Math.max(c.von_mises_strain_pct, 1e-3)));
  el.setAttribute('cy', Y(c.n_atoms));
  el.setAttribute('r', 5);
  el.setAttribute('class', 'pt');
  el.addEventListener('click', () => select(i));
  svg.appendChild(el);
});
function select(i) {
  selected = i;
  document.querySelectorAll('#tbl tbody tr').forEach((tr, k) => tr.classList.toggle('sel', k === i));
  document.querySelectorAll('circle.pt').forEach((p, k) => p.classList.toggle('sel', k === i));
}
select(0);
document.getElementById('go').onclick = async () => {
  const c = DATA.candidates[selected];
  const q = `/__seed_select?token=${DATA.token}&candidate=${c.candidate}&strain_method=film&orthogonal_cell=0`;
  const st = document.getElementById('status');
  try {
    const r = await fetch(q);
    if (!r.ok) throw new Error(await r.text());
    st.className = 'ok';
    st.textContent = `Recorded: candidate=${c.candidate}, θ=${c.angle_deg}°, strain ${c.von_mises_strain_pct}%, ${c.n_atoms} atoms. The agent is waiting and will continue automatically.`;
  } catch (e) {
    st.className = 'err';
    st.textContent = `Could not send the selection (${e}). Tell the agent directly: build the ZSL twisted bilayer with candidate=${c.candidate}.`;
  }
};
</script>
</body>
</html>
"""
    html = (html.replace("__TITLE__", title)
                .replace("__PAYLOAD__", payload))
    out_path.write_text(html, encoding="utf-8")
    return out_path


# ---------------------------------------------------------------- atom picker
# d-block set mirrors the anneal ruling (d-band default = atoms carrying d
# electrons); duplicated as a chemistry constant — plugins never import each
# other (CCD_PROGRAM §3).
_D_BLOCK = {
    "SC", "TI", "V", "CR", "MN", "FE", "CO", "NI", "CU", "ZN",
    "Y", "ZR", "NB", "MO", "TC", "RU", "RH", "PD", "AG", "CD",
    "HF", "TA", "W", "RE", "OS", "IR", "PT", "AU", "HG",
}


def record_atom_selection(token: str, indices: list[int]) -> Path:
    if not valid_token(token):
        raise ValueError("invalid selection token")
    clean = sorted({int(i) for i in indices})
    if not clean or any(i < 0 for i in clean):
        raise ValueError("indices must be a non-empty list of non-negative integers")
    path = _selection_dir() / f"{token}.json"
    path.write_text(json.dumps({"token": token, "kind": "atoms",
                                "indices": clean}, indent=2) + "\n",
                    encoding="utf-8")
    return path


def read_atom_selection(token: str) -> dict | None:
    choice = read_selection(token)
    if choice is not None and choice.get("kind") != "atoms":
        raise ValueError(f"selection {token} is not an atom selection")
    return choice


def render_atom_selector(structure, token: str, out_path: Path, title: str,
                         note: str = "", preselect: str | list[int] = "none",
                         script_url: str | None = None) -> Path:
    """Clickable 3D atom picker: click spheres to toggle, element quick-buttons,
    confirm posts to /__seed_select_atoms. `preselect`: "d_block" | "all" |
    "none" | explicit index list."""
    from . import render as _render

    sites = [{"i": i, "el": str(site.specie),
              "z": round(float(site.coords[2]), 3)}
             for i, site in enumerate(structure)]
    if preselect == "d_block":
        pre = [s["i"] for s in sites if s["el"].upper() in _D_BLOCK]
    elif preselect == "all":
        pre = [s["i"] for s in sites]
    elif preselect == "none":
        pre = []
    else:
        pre = sorted({int(i) for i in preselect})
    import warnings

    disp = _render._display_structure(structure)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Site labels are not unique")
        cif = disp.to(fmt="cif").replace("`", "'")
    payload = json.dumps({"token": token, "sites": sites, "preselected": pre,
                          "note": note}, ensure_ascii=False)
    html = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>__TITLE__ — atom selector</title>
<script src="__SCRIPT__"></script>
<style>
 body { font-family: -apple-system, "PingFang SC", sans-serif; margin: 0; display: flex; height: 100vh; }
 #view { flex: 1; position: relative; }
 #panel { width: 300px; padding: 16px; border-left: 1px solid #ddd; background: #fafafa;
          overflow-y: auto; font-size: 13px; }
 h3 { margin: 0 0 6px; } .note { color: #777; margin-bottom: 10px; }
 button { margin: 3px 4px 3px 0; padding: 5px 10px; border: 1px solid #cbd5e1; border-radius: 6px;
          background: #fff; cursor: pointer; font-size: 12px; }
 button.primary { background: #2563eb; color: #fff; border: none; padding: 8px 20px; font-size: 14px; }
 button.primary:disabled { background: #9ca3af; }
 #count { font-weight: 600; margin: 10px 0 4px; }
 #idx { font-family: monospace; font-size: 11px; color: #555; word-break: break-all;
        max-height: 120px; overflow-y: auto; background: #fff; border: 1px solid #eee;
        border-radius: 6px; padding: 6px; }
 #status { margin-top: 10px; } .ok { color: #047857; } .err { color: #b91c1c; }
</style>
</head>
<body>
<div id="view"></div>
<div id="panel">
  <h3>__TITLE__</h3>
  <div class="note" id="note"></div>
  <div class="note">Click atoms in the 3D view to select or deselect them; green = selected.</div>
  <div id="elbtns"></div>
  <div>
    <button id="selall">All</button><button id="selnone">None</button>
    <button id="selinv">Invert</button><button id="seldb">d-block elements</button>
  </div>
  <div id="count"></div>
  <div id="idx"></div>
  <button class="primary" id="go">Confirm selection</button>
  <div id="status"></div>
</div>
<script>
const DATA = __PAYLOAD__;
const cif = `__CIF__`;
document.getElementById('note').textContent = DATA.note || '';
const SEL = new Set(DATA.preselected);
const DBLOCK = new Set(__DBLOCK__);
const byEl = {};
DATA.sites.forEach(s => { (byEl[s.el] = byEl[s.el] || []).push(s.i); });
const viewer = $3Dmol.createViewer("view", {backgroundColor: "white"});
const model = viewer.addModel(cif, "cif");
viewer.addUnitCell(model, {box: {color: "#555"},
  astyle: {hidden: true}, bstyle: {hidden: true}, cstyle: {hidden: true},
  alabel: "", blabel: "", clabel: ""});
const atoms = model.selectedAtoms({});
const idxOf = a => { const m = (a.atom || "").match(/(\\d+)$/); return m ? parseInt(m[1]) : null; };
function restyle() {
  viewer.setStyle({}, {sphere: {scale: 0.35}, stick: {radius: 0.12}});
  const serials = atoms.filter(a => SEL.has(idxOf(a))).map(a => a.serial);
  if (serials.length) viewer.addStyle({serial: serials},
    {sphere: {scale: 0.5, color: "#16a34a"}});
  viewer.render();
  const arr = [...SEL].sort((a, b) => a - b);
  document.getElementById('count').textContent = `${arr.length} / ${DATA.sites.length} atoms selected`;
  document.getElementById('idx').textContent = arr.join(", ") || "(none)";
  document.getElementById('go').disabled = arr.length === 0;
  Object.keys(byEl).forEach(el => {
    const all = byEl[el].every(i => SEL.has(i));
    document.getElementById('el_' + el).textContent =
      `${el} (${byEl[el].length}) ${all ? '✓' : ''}`;
  });
}
Object.keys(byEl).sort().forEach(el => {
  const b = document.createElement('button');
  b.id = 'el_' + el;
  b.onclick = () => {
    const all = byEl[el].every(i => SEL.has(i));
    byEl[el].forEach(i => all ? SEL.delete(i) : SEL.add(i));
    restyle();
  };
  document.getElementById('elbtns').appendChild(b);
});
viewer.setClickable({}, true, a => {
  const i = idxOf(a);
  if (i === null) return;
  SEL.has(i) ? SEL.delete(i) : SEL.add(i);
  restyle();
});
document.getElementById('selall').onclick = () => { DATA.sites.forEach(s => SEL.add(s.i)); restyle(); };
document.getElementById('selnone').onclick = () => { SEL.clear(); restyle(); };
document.getElementById('selinv').onclick = () => {
  DATA.sites.forEach(s => SEL.has(s.i) ? SEL.delete(s.i) : SEL.add(s.i)); restyle(); };
document.getElementById('seldb').onclick = () => {
  SEL.clear(); DATA.sites.forEach(s => { if (DBLOCK.has(s.el.toUpperCase())) SEL.add(s.i); }); restyle(); };
document.getElementById('go').onclick = async () => {
  const arr = [...SEL].sort((a, b) => a - b);
  const st = document.getElementById('status');
  try {
    const r = await fetch(`/__seed_select_atoms?token=${DATA.token}&indices=${arr.join(',')}`);
    if (!r.ok) throw new Error(await r.text());
    st.className = 'ok';
    st.textContent = `Recorded ${arr.length} atoms. The agent is waiting and will continue automatically.`;
  } catch (e) {
    st.className = 'err';
    st.textContent = `Could not send the selection (${e}). Tell the agent directly: continue with atom indices ${arr.join(',')}.`;
  }
};
viewer.zoomTo();
restyle();
</script>
</body>
</html>
"""
    html = (html.replace("__TITLE__", title).replace("__PAYLOAD__", payload)
                .replace("__CIF__", cif)
                .replace("__SCRIPT__", script_url or _render.CDN_3DMOL)
                .replace("__DBLOCK__", json.dumps(sorted(_D_BLOCK))))
    out_path.write_text(html, encoding="utf-8")
    return out_path
