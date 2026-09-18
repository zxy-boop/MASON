"""Lightweight structure visualization (BACKENDS.md §9).

Two light outputs, zero new dependencies:
  PNG  — ASE's native writer (matplotlib Agg, headless): for multimodal agents.
  HTML — self-contained page loading 3Dmol.js from CDN: interactive, for humans.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from pymatgen.core import Structure

_HTML_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
<script src="{script_url}"></script>
<style>html,body{{margin:0;height:100%}}#view{{width:100%;height:100%;position:relative}}
#topleft{{position:absolute;top:8px;left:12px;z-index:10;display:flex;gap:10px;align-items:center}}
#backbtn{{font:12px monospace;color:#666;background:rgba(255,255,255,.75);border:1px solid #ddd;
 border-radius:8px;padding:3px 10px;cursor:pointer;user-select:none;display:none}}
#backbtn:hover{{background:rgba(255,255,255,.95);color:#333}}
#label{{font:13px monospace;color:#444}}
#legend{{position:absolute;top:8px;right:12px;z-index:10;background:rgba(255,255,255,.92);
 border:1px solid #ddd;border-radius:8px;padding:8px 12px;font:13px monospace;color:#333;
 display:none}}
#legend.open{{display:block}}
#lgbtn{{position:absolute;top:8px;right:12px;z-index:11;font:12px monospace;color:#666;
 background:rgba(255,255,255,.75);border:1px solid #ddd;border-radius:8px;
 padding:3px 10px;cursor:pointer;user-select:none}}
#lgbtn:hover{{background:rgba(255,255,255,.95);color:#333}}
#lgclose{{text-align:right;color:#888;font-size:12px;cursor:pointer;margin-bottom:4px}}
#lgclose:hover{{color:#333}}
#legend .row{{display:flex;align-items:center;gap:8px;margin:2px 0}}
#legend .sw{{width:14px;height:14px;border-radius:50%;border:1px solid #999;display:inline-block}}
#legend .hint{{color:#888;font-size:11px;margin-top:6px}}
#legend label{{display:flex;align-items:center;gap:6px;margin-top:6px;cursor:pointer;font-size:12px}}
#axes{{position:absolute;bottom:12px;left:12px;z-index:10;pointer-events:none}}</style>
</head>
<body>
<div id="view"><div id="topleft"><div id="backbtn">← Back</div><div id="label">{title}</div></div>
<div id="lgbtn">⚙ Settings</div><div id="legend"></div>
<svg id="axes" width="110" height="110"></svg></div>
<script>
// Back button: in a normal tab it always leads somewhere sane — history.back()
// when there is history, else the workbench (/shell). Hidden only inside the
// workbench preview iframe, which has its own chrome.
const backbtn = document.getElementById("backbtn");
if (window.self === window.top) backbtn.style.display = "block";
backbtn.addEventListener("click", () => {{
  if (history.length > 1) history.back(); else location.href = "/shell";
}});
const cif = `{cif}`;
const LATTICE = {lattice};
const LIFT = {lift};  // display-only slab lift in Å (0 = not applied)
if (typeof $3Dmol === "undefined") {{
  document.getElementById("label").textContent =
    "3Dmol.js failed to load — check network access to cdnjs.cloudflare.com";
}} else {{
  const viewer = $3Dmol.createViewer("view", {{backgroundColor: "white"}});
  const model = viewer.addModel(cif, "cif");
  // 3Dmol's built-in CIF bond detection uses a tight covalent-radius cutoff
  // that drops longer heavy-element bonds (e.g. Bi-Se ~3.0 Å in layered
  // Bi2Se3). Recompute connectivity from the drawn atoms with a per-element
  // covalent radius table + tolerance so such bonds are shown. Tolerance is
  // user-adjustable (legend slider, ATOMKIT-style) — layered/vdW systems may
  // want a looser or tighter cutoff.
  function rebond(TOL) {{
    const R = {{H:0.31,Li:1.28,Be:0.96,B:0.84,C:0.76,N:0.71,O:0.66,F:0.57,
      Na:1.66,Mg:1.41,Al:1.21,Si:1.11,P:1.07,S:1.05,Cl:1.02,K:2.03,Ca:1.76,
      Sc:1.70,Ti:1.60,V:1.53,Cr:1.39,Mn:1.39,Fe:1.32,Co:1.26,Ni:1.24,Cu:1.32,
      Zn:1.22,Ga:1.22,Ge:1.20,As:1.19,Se:1.20,Br:1.20,Rb:2.20,Sr:1.95,Y:1.90,
      Zr:1.75,Nb:1.64,Mo:1.54,Tc:1.47,Ru:1.46,Rh:1.42,Pd:1.39,Ag:1.45,Cd:1.44,
      In:1.42,Sn:1.39,Sb:1.39,Te:1.38,I:1.39,Cs:2.44,Ba:2.15,La:2.07,Ce:2.04,
      Hf:1.75,Ta:1.70,W:1.62,Re:1.51,Os:1.44,Ir:1.41,Pt:1.36,Au:1.36,Hg:1.32,
      Tl:1.45,Pb:1.46,Bi:1.48,Po:1.40,Th:2.06,U:1.96}};
    const DEF = 1.5;
    const at = model.selectedAtoms({{}});
    if (at.length > 2500) return;  // guard O(n^2) on huge cells
    at.forEach(a => {{ a.bonds = []; a.bondOrder = []; }});
    for (let i = 0; i < at.length; i++) {{
      for (let j = i + 1; j < at.length; j++) {{
        const dx = at[i].x - at[j].x, dy = at[i].y - at[j].y, dz = at[i].z - at[j].z;
        const d2 = dx*dx + dy*dy + dz*dz;
        const cut = ((R[at[i].elem] || DEF) + (R[at[j].elem] || DEF)) * TOL;
        if (d2 > 0.16 && d2 < cut*cut) {{
          at[i].bonds.push(at[j].index); at[i].bondOrder.push(1);
          at[j].bonds.push(at[i].index); at[j].bondOrder.push(1);
        }}
      }}
    }}
  }}
  rebond(1.30);
  // 3Dmol's defaultColors palette has no entry for many elements (Bi, Se,
  // ...) so they collapse to one magenta fallback. Use the Jmol scheme,
  // which colors the full periodic table distinctly, for spheres, sticks,
  // and the legend below.
  const CSCHEME = ($3Dmol.elementColors.Jmol) ? "Jmol" : "default";
  viewer.setStyle({{}}, {{sphere: {{scale: 0.35, colorscheme: CSCHEME}},
                       stick: {{radius: 0.12, colorscheme: CSCHEME}}}});
  // cell box only — the a/b/c triad lives in the corner compass (VESTA-style),
  // not drawn on the lattice
  viewer.addUnitCell(model, {{box: {{color: "#555"}},
    astyle: {{hidden: true}}, bstyle: {{hidden: true}}, cstyle: {{hidden: true}},
    alabel: "", blabel: "", clabel: ""}});

  // ---- corner axes compass (rotates with the view) ----
  const AXSVG = document.getElementById("axes");
  const AXC = 55, AXR = 38;
  const AXCOL = {{a: "#dc2626", b: "#16a34a", c: "#2563eb"}};
  const unit = v => {{ const n = Math.hypot(...v) || 1; return v.map(x => x / n); }};
  const AXV = {{a: unit(LATTICE[0]), b: unit(LATTICE[1]), c: unit(LATTICE[2])}};
  function rotq(v, q) {{  // rotate vector by quaternion [x,y,z,w]
    const [x, y, z] = v, [qx, qy, qz, qw] = q;
    const ux = qy * z - qz * y + qw * x, uy = qz * x - qx * z + qw * y,
          uz = qx * y - qy * x + qw * z, uw = -qx * x - qy * y - qz * z;
    return [ux * qw - uw * qx - uy * qz + uz * qy,
            uy * qw - uw * qy - uz * qx + ux * qz,
            uz * qw - uw * qz - ux * qy + uy * qx];
  }}
  function drawAxes() {{
    const view = viewer.getView();          // [..., qx, qy, qz, qw]
    const q = view.slice(4, 8);
    let out = `<circle cx="${{AXC}}" cy="${{AXC}}" r="3" fill="#888"/>`;
    const order = ["a", "b", "c"].sort(
      (p, r) => rotq(AXV[p], q)[2] - rotq(AXV[r], q)[2]);  // back-to-front
    for (const k of order) {{
      const r = rotq(AXV[k], q);
      const x2 = AXC + r[0] * AXR, y2 = AXC - r[1] * AXR;
      out += `<line x1="${{AXC}}" y1="${{AXC}}" x2="${{x2}}" y2="${{y2}}"` +
             ` stroke="${{AXCOL[k]}}" stroke-width="2.5"/>` +
             `<text x="${{AXC + r[0] * (AXR + 11)}}" y="${{AXC - r[1] * (AXR + 11) + 4}}"` +
             ` fill="${{AXCOL[k]}}" font-size="13" font-family="monospace"` +
             ` text-anchor="middle">${{k}}</text>`;
    }}
    AXSVG.innerHTML = out;
  }}
  if (viewer.setViewChangeCallback) viewer.setViewChangeCallback(drawAxes);
  drawAxes();

  // ---- element legend (color <-> element <-> count) ----
  // counts come from Python (true composition; boundary display images excluded)
  const atoms = model.selectedAtoms({{}});
  const counts = {counts};
  const cmap = $3Dmol.elementColors.Jmol || $3Dmol.elementColors.defaultColors;
  const css = c => (typeof c === "number")
    ? "#" + c.toString(16).padStart(6, "0") : (c || "#b0b0b0");
  const legend = document.getElementById("legend");
  // Collapsed by default (user ruling 2026-07-19): the panel was covering the
  // structure — the view should show ONLY the structure until settings are
  // wanted. A small ⚙ button opens it; × Hide collapses it again.
  const lgbtn = document.getElementById("lgbtn");
  const lgclose = document.createElement("div");
  lgclose.id = "lgclose";
  lgclose.textContent = "× Hide";
  lgclose.addEventListener("click", () => {{
    legend.classList.remove("open");
    lgbtn.style.display = "";
  }});
  legend.appendChild(lgclose);
  lgbtn.addEventListener("click", () => {{
    legend.classList.add("open");
    lgbtn.style.display = "none";
  }});
  Object.keys(counts).sort().forEach(el => {{
    const row = document.createElement("div");
    row.className = "row";
    row.innerHTML = `<span class="sw" style="background:${{css(cmap[el])}}"></span>` +
                    `<span>${{el}} × ${{counts[el]}}</span>`;
    legend.appendChild(row);
  }});
  const hint = document.createElement("div");
  hint.className = "hint";
  hint.textContent = "Hover for element/index, click to pin";
  legend.appendChild(hint);
  const toggle = document.createElement("label");
  toggle.innerHTML = '<input type="checkbox" id="alllab">All labels';
  legend.appendChild(toggle);
  // ---- projection: perspective (default) vs parallel/orthographic ----
  const projSel = document.createElement("label");
  projSel.innerHTML = 'Projection <select id="proj" style="font:12px monospace">' +
    '<option value="perspective">perspective</option>' +
    '<option value="orthographic">parallel</option></select>';
  legend.appendChild(projSel);
  document.getElementById("proj").addEventListener("change", e => {{
    viewer.setProjection(e.target.value);  // "perspective" | "orthographic"
    viewer.render();
  }});
  // ---- adjustable bond tolerance (ATOMKIT-style live cutoff) ----
  const tolRow = document.createElement("label");
  tolRow.innerHTML = 'Bond tolerance <input type="range" id="bondtol" min="1.00" max="1.60"' +
    ' step="0.05" value="1.30" style="width:90px;vertical-align:middle">' +
    ' <span id="bondtolv">1.30</span>';
  legend.appendChild(tolRow);
  document.getElementById("bondtol").addEventListener("input", e => {{
    const tol = parseFloat(e.target.value);
    document.getElementById("bondtolv").textContent = tol.toFixed(2);
    rebond(tol);
    viewer.setStyle({{}}, {{sphere: {{scale: 0.35, colorscheme: CSCHEME}},
                         stick: {{radius: 0.12, colorscheme: CSCHEME}}}});
    viewer.render();
  }});

  // ---- two-click distance measurement (user request 2026-07-14) ----
  // Reports the on-screen (direct) distance and, when different, the PBC
  // minimum-image distance — in slabs/interfaces the nearest periodic copy
  // can be closer than the drawn pair.
  // Measurement accent: bright rose + a cylinder THICKER than the bond
  // sticks (0.12) — the old thin amber dash (0.05) hid inside bonds
  // (user report 2026-07-19).
  const MC = "#e11d48", MR = 0.14;
  let measMode = "", mSel = [], mShapes = [], mLabels = [];
  const measRow = document.createElement("label");
  measRow.innerHTML = '<input type="checkbox" id="measure">Distance (click two atoms)';
  legend.appendChild(measRow);
  const angRow = document.createElement("label");
  angRow.innerHTML = '<input type="checkbox" id="measang">Angle (click three atoms, the second is the vertex)';
  legend.appendChild(angRow);
  const mOut = document.createElement("div");
  mOut.className = "hint";
  legend.appendChild(mOut);
  if (LIFT > 0) {{
    const ln = document.createElement("div");
    ln.className = "hint";
    ln.textContent = `Slab shifted up by ${{LIFT}} Å for display only; the file is unchanged`;
    legend.appendChild(ln);
  }}
  function clearMeasure(v) {{
    mShapes.forEach(s => v.removeShape(s));
    mLabels.forEach(l => v.removeLabel(l));
    mShapes = []; mLabels = []; mSel = [];
    mOut.textContent = "";
  }}
  function minImage(d) {{  // shortest |d + i·a + j·b + k·c| over i,j,k ∈ {{-1,0,1}}
    let best = Math.hypot(d[0], d[1], d[2]);
    for (let i = -1; i <= 1; i++) for (let j = -1; j <= 1; j++)
      for (let k = -1; k <= 1; k++) {{
        const dx = d[0] + i*LATTICE[0][0] + j*LATTICE[1][0] + k*LATTICE[2][0];
        const dy = d[1] + i*LATTICE[0][1] + j*LATTICE[1][1] + k*LATTICE[2][1];
        const dz = d[2] + i*LATTICE[0][2] + j*LATTICE[1][2] + k*LATTICE[2][2];
        best = Math.min(best, Math.hypot(dx, dy, dz));
      }}
    return best;
  }}
  function setMeasMode(mode) {{  // "" | "dist" | "angle" — modes are exclusive
    measMode = mode;
    document.getElementById("measure").checked = mode === "dist";
    document.getElementById("measang").checked = mode === "angle";
    clearMeasure(viewer);
    viewer.render();
  }}
  document.getElementById("measure").addEventListener("change", e => {{
    setMeasMode(e.target.checked ? "dist" : "");
  }});
  document.getElementById("measang").addEventListener("change", e => {{
    setMeasMode(e.target.checked ? "angle" : "");
  }});

  // ---- hover: element + site index; click: pin/unpin ----
  // Step 262 (qidi incident): the old label reused pymatgen's CIF tag
  // (element + GLOBAL 0-based index, e.g. "O37" for global site 37) which
  // READS like "37th O" — the ambiguity that replaced the wrong atom. Labels
  // now show BOTH numberings, 1-based, matching the tool site specs exactly:
  //   "O37 · #69"  = 37th O atom = global site 69.
  const _all = model.selectedAtoms({{}}).slice().sort((x, y) => x.serial - y.serial);
  const _cnt = {{}}, _dual = {{}};
  _all.forEach(at => {{
    _cnt[at.elem] = (_cnt[at.elem] || 0) + 1;
    _dual[at.serial] = `${{at.elem}}${{_cnt[at.elem]}} · #${{at.serial + 1}}`;
  }});
  const text = a => _dual[a.serial] || `${{a.elem}} #${{a.serial + 1}}`;
  const style = {{backgroundColor: "#222", fontColor: "white", fontSize: 12,
                  backgroundOpacity: 0.8}};
  viewer.setHoverable({{}}, true,
    (a, v) => {{ if (!a._hl && !a._pin) {{
      a._hl = v.addLabel(text(a), {{...style, position: a}}); v.render(); }} }},
    (a, v) => {{ if (a._hl) {{ v.removeLabel(a._hl); delete a._hl; v.render(); }} }});
  viewer.setClickable({{}}, true, (a, v) => {{
    if (a._hl) {{ v.removeLabel(a._hl); delete a._hl; }}
    if (measMode === "dist") {{
      if (mSel.length === 2) clearMeasure(v);
      mSel.push(a);
      mLabels.push(v.addLabel(text(a), {{...style, backgroundColor: MC,
                                         position: a}}));
      if (mSel.length === 2) {{
        const [p, q] = mSel;
        const d = [p.x - q.x, p.y - q.y, p.z - q.z];
        const direct = Math.hypot(d[0], d[1], d[2]);
        const mi = minImage(d);
        const msg = (direct - mi > 0.01)
          ? `${{direct.toFixed(3)}} Å (minimum image ${{mi.toFixed(3)}} Å)`
          : `${{direct.toFixed(3)}} Å`;
        mShapes.push(v.addCylinder({{start: {{x: p.x, y: p.y, z: p.z}},
                                     end: {{x: q.x, y: q.y, z: q.z}},
                                     radius: MR, color: MC,
                                     dashed: true, fromCap: 1, toCap: 1}}));
        mLabels.push(v.addLabel(msg, {{...style, backgroundColor: MC,
          position: {{x: (p.x + q.x) / 2, y: (p.y + q.y) / 2, z: (p.z + q.z) / 2}}}}));
        mOut.textContent = `${{text(p)}} – ${{text(q)}}: ${{msg}}`;
      }}
      v.render();
      return;
    }}
    if (measMode === "angle") {{
      if (mSel.length === 3) clearMeasure(v);
      mSel.push(a);
      mLabels.push(v.addLabel(text(a), {{...style, backgroundColor: MC,
                                         position: a}}));
      if (mSel.length === 3) {{
        const [p, c, q] = mSel;  // c = vertex (second click)
        const u = [p.x - c.x, p.y - c.y, p.z - c.z];
        const w = [q.x - c.x, q.y - c.y, q.z - c.z];
        const nu = Math.hypot(u[0], u[1], u[2]), nw = Math.hypot(w[0], w[1], w[2]);
        const cosA = (u[0]*w[0] + u[1]*w[1] + u[2]*w[2]) / (nu * nw);
        const ang = Math.acos(Math.min(1, Math.max(-1, cosA))) * 180 / Math.PI;
        mShapes.push(v.addCylinder({{start: {{x: c.x, y: c.y, z: c.z}},
                                     end: {{x: p.x, y: p.y, z: p.z}},
                                     radius: MR, color: MC,
                                     dashed: true, fromCap: 1, toCap: 1}}));
        mShapes.push(v.addCylinder({{start: {{x: c.x, y: c.y, z: c.z}},
                                     end: {{x: q.x, y: q.y, z: q.z}},
                                     radius: MR, color: MC,
                                     dashed: true, fromCap: 1, toCap: 1}}));
        // place the value along the angle bisector, clear of the vertex's
        // own atom label (which sits exactly at c)
        const bx = u[0]/nu + w[0]/nw, by = u[1]/nu + w[1]/nw, bz = u[2]/nu + w[2]/nw;
        const nb = Math.hypot(bx, by, bz) || 1, off = 0.9;
        mLabels.push(v.addLabel(`${{ang.toFixed(1)}}°`, {{...style,
          backgroundColor: MC,
          position: {{x: c.x + bx/nb*off, y: c.y + by/nb*off, z: c.z + bz/nb*off}}}}));
        mOut.textContent =
          `${{text(p)}} – ${{text(c)}} – ${{text(q)}}: ${{ang.toFixed(1)}}°`;
      }}
      v.render();
      return;
    }}
    if (a._pin) {{ v.removeLabel(a._pin); delete a._pin; }}
    else {{ a._pin = v.addLabel(text(a), {{...style, backgroundColor: "#1d4ed8",
                                           position: a}}); }}
    v.render();
  }});
  document.getElementById("alllab").addEventListener("change", e => {{
    atoms.forEach(a => {{
      if (e.target.checked) {{
        if (!a._all) a._all = viewer.addLabel(a.elem, {{fontSize: 10,
          backgroundOpacity: 0.55, backgroundColor: "#333", fontColor: "white",
          position: a}});
      }} else if (a._all) {{ viewer.removeLabel(a._all); delete a._all; }}
    }});
    viewer.render();
  }});

  viewer.zoomTo();
  viewer.render();
}}
</script>
</body>
</html>
"""


def render_png(
    structure: Structure,
    path: str | Path,
    rotation: str = "10x,-80y",
    repeat: tuple[int, int, int] = (1, 1, 1),
    scale: int = 60,
) -> Path:
    """Static PNG via ASE's writer (headless). `rotation` e.g. '10x,-80y'; `repeat`
    tiles the cell for visual context without touching the structure itself."""
    os.environ.setdefault("MPLBACKEND", "Agg")
    import ase.io
    from pymatgen.io.ase import AseAtomsAdaptor

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atoms = AseAtomsAdaptor.get_atoms(structure) * tuple(int(x) for x in repeat)
    # format= must be explicit: ASE guesses by basename, so "POSCAR_x.png" would
    # otherwise be routed to the VASP writer.
    ase.io.write(str(path), atoms, format="png", rotation=rotation,
                 show_unit_cell=2, scale=scale)
    return path


def _slab_display_lift(structure: Structure, lift: float = 1.0,
                       min_vacuum: float = 6.0) -> tuple[Structure, float]:
    """Display-only slab lift (user ruling 2026-07-14, option B: viewer layer,
    POSCAR untouched). Slabs sit at the BOTTOM of the cell with all vacuum
    above (project convention), so the bottom layer sits AT z=0 — it renders
    half-clipped against the cell floor and spawns boundary images. Lift the
    display copy so the lowest atom sits `lift` Å above z=0. Applied only to
    slab-like cells (≥ `min_vacuum` Å of vacuum along c) whose bottom atom is
    below `lift` Å; returns (structure, applied_shift_Å)."""
    if len(structure) == 0:
        return structure, 0.0
    fz = [float(site.frac_coords[2]) for site in structure]
    c_len = float(structure.lattice.c)
    vacuum = c_len - (max(fz) - min(fz)) * c_len
    bottom = min(fz) * c_len
    if vacuum < min_vacuum or bottom >= lift:
        return structure, 0.0
    disp = structure.copy()
    disp.translate_sites(list(range(len(disp))), [0.0, 0.0, (lift - bottom) / c_len],
                         frac_coords=True, to_unit_cell=False)
    return disp, lift - bottom


def _display_structure(structure: Structure, tol: float = 1e-3) -> Structure:
    """Display copy with VESTA-style boundary images: an atom on a cell face /
    edge / corner is shown on every equivalent boundary (corner atom appears at
    all 8 corners). Images reuse the ORIGINAL site label (El + site index), so
    hover still reports the real index. Display-only — artifacts untouched."""
    from itertools import combinations

    disp = structure.copy()
    for i, site in enumerate(disp):
        site.label = f"{site.specie}{i}"
    images = []
    for i, site in enumerate(structure):
        f = [float(x) for x in site.frac_coords]
        boundary_axes = []
        for ax in range(3):
            if abs(f[ax]) < tol or abs(f[ax] - 1.0) < tol:
                boundary_axes.append((ax, 1.0 if f[ax] < 0.5 else -1.0))
        for r in range(1, len(boundary_axes) + 1):
            for combo in combinations(boundary_axes, r):
                nf = list(f)
                for ax, shift in combo:
                    nf[ax] += shift
                images.append((site.species, nf, f"{site.specie}{i}"))
    for species, frac, label in images:
        disp.append(species, frac, coords_are_cartesian=False,
                    validate_proximity=False)
        disp[-1].label = label
    return disp


CDN_3DMOL = "https://cdnjs.cloudflare.com/ajax/libs/3Dmol/2.1.0/3Dmol-min.js"


def render_html(structure: Structure, path: str | Path, title: str = "structure",
                script_url: str | None = None) -> Path:
    """Interactive HTML (3Dmol.js). Self-contained file; structure embedded as
    CIF text — 3Dmol's VASP/POSCAR parser drops atoms (mode-line case,
    fractional conversion), its CIF parser is solid. Zero Python dependencies.
    `script_url` loads 3Dmol.js: pass the vendored/served URL for offline
    containers (platform W1); defaults to the cdnjs CDN."""
    import json
    import warnings

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lifted, lift = _slab_display_lift(structure)
    disp = _display_structure(lifted)
    with warnings.catch_warnings():
        # boundary images intentionally reuse the original site labels
        warnings.filterwarnings("ignore", message="Site labels are not unique")
        cif = disp.to(fmt="cif").replace("`", "'")
    lattice = json.dumps([[round(float(x), 6) for x in row]
                          for row in structure.lattice.matrix])
    # legend counts = the REAL composition (boundary images excluded)
    counts = json.dumps({el: int(n) for el, n in
                         sorted(structure.composition.get_el_amt_dict().items())})
    path.write_text(_HTML_TEMPLATE.format(title=title, cif=cif, lattice=lattice,
                                          counts=counts, lift=json.dumps(round(lift, 3)),
                                          script_url=script_url or CDN_3DMOL),
                    encoding="utf-8")
    return path
