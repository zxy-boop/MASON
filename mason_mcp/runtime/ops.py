"""Structure operations. Each function: (Structure, params) -> (Structure, summary dict).

Backend per BACKENDS.md:
  make_supercell    — pymatgen  (§3)
  generate_slab     — pymatgen SlabGenerator  (§4)
  add_vacuum        — ASE Atoms.center via adaptor  (§5)
  make_vacancy      — pymatgen spglib symmetry  (§6)
"""

from __future__ import annotations

import math

import numpy as np
from pymatgen.core import Lattice, Structure
from pymatgen.core.surface import Slab, SlabGenerator
from pymatgen.symmetry.analyzer import SpacegroupAnalyzer

MIN_VACUUM_A = 10.0  # 2D guard: below this VASP slab images interact


# ---------------------------------------------------------------- supercell
def make_supercell(structure: Structure, scaling) -> tuple[Structure, dict]:
    """Supercell from a diagonal [na, nb, nc] or a full 3x3 integer matrix."""
    m = np.array(scaling, dtype=int)
    if m.shape == (3,):
        m = np.diag(m)
    if m.shape != (3, 3):
        raise ValueError("scaling must be [na, nb, nc] or a 3x3 integer matrix")
    det = int(round(abs(np.linalg.det(m))))
    if det < 1:
        raise ValueError("scaling matrix must have |det| >= 1")
    new = structure.make_supercell(m, in_place=False)
    return new, {
        "op": "make_supercell",
        "scaling": m.tolist(),
        "n_sites_before": len(structure),
        "n_sites_after": len(new),
        "volume_ratio": det,
    }


# --------------------------------------------------------------------- slab
def _standard_orientation(slab) -> Structure:
    """Rotate a pymatgen Slab into the conventional surface frame (ASE-style):
    a and b in the xy plane, c orthogonal to them along +z. pymatgen's
    SlabGenerator returns mathematically valid but tilted cells (c not normal
    to the surface, or in-plane vectors with z components)."""
    ortho = slab.get_orthogonal_c_slab()  # c ⊥ (a, b), lengths/angles of a,b kept
    m = ortho.lattice.matrix
    la = float(np.linalg.norm(m[0]))
    bx = float(np.dot(m[1], m[0]) / la)
    by = float(np.sqrt(np.linalg.norm(m[1]) ** 2 - bx**2))
    if float(np.linalg.det(m)) < 0:
        by = -by  # keep handedness: reusing frac coords in a flipped basis mirrors the structure
    h = float(np.linalg.norm(m[2]))
    new_lattice = [[la, 0.0, 0.0], [bx, by, 0.0], [0.0, 0.0, h]]
    # wrap in-plane fractional coords into the cell — stacking offsets otherwise
    # accumulate outside [0,1) and atoms render beside the box; keep the c
    # component untouched so the slab stays contiguous (never wrapped through vacuum)
    frac = np.array(ortho.frac_coords)
    frac[:, :2] %= 1.0
    return Structure(new_lattice, ortho.species, frac)


def _z_layers(structure: Structure, tol: float = 0.5) -> list[list[int]]:
    """Cluster atoms into z-layers: sorted by z, a gap > tol Å starts a new
    layer (rumpling within a layer stays below tol for typical slabs)."""
    order = sorted(range(len(structure)), key=lambda i: float(structure[i].coords[2]))
    layers: list[list[int]] = []
    prev_z = None
    for i in order:
        z = float(structure[i].coords[2])
        if prev_z is None or z - prev_z > tol:
            layers.append([])
        layers[-1].append(i)
        prev_z = z
    return layers


def generate_slab(
    bulk: Structure,
    miller: list[int],
    min_slab_size: float = 10.0,
    min_vacuum_size: float = 15.0,
    center_slab: bool = True,
    in_unit_planes: bool = False,
    termination: int = 0,
    symmetrize: bool = False,
    align: str = "bottom",
    fix_bottom_fraction: float = 0.5,
    n_layers: int | None = None,
) -> tuple[Structure, dict]:
    """Cut a slab from bulk. Enumerates all distinct terminations; picks `termination`.
    `n_layers`: keep only the top `n_layers` atomic planes (z-clustered, gap > 0.5 Å)
    of the generated slab, so that a layered material can be cut to a single layer
    (MoS2: 3 planes = one S-Mo-S layer; graphite: 1 plane = graphene).
    `align`: "bottom" shifts the slab to the base of the cell with all vacuum above
    (adsorbate convention); "center" keeps pymatgen's centered placement.
    `fix_bottom_fraction`: expert rule (bshao, 2026-07-15) — the bottom half of
    the slab's layers is frozen at bulk positions (selective dynamics F F F)
    so relaxations optimize only the surface side; 0.0 disables."""
    miller_t = tuple(int(x) for x in miller)
    if len(miller_t) != 3:
        raise ValueError("miller must be three integers, e.g. [1, 1, 1]")
    gen = SlabGenerator(
        bulk,
        miller_index=miller_t,
        min_slab_size=float(min_slab_size),
        min_vacuum_size=float(min_vacuum_size),
        center_slab=center_slab,
        in_unit_planes=in_unit_planes,
    )
    slabs = gen.get_slabs(symmetrize=symmetrize)
    if not slabs:
        raise ValueError(f"no valid slab terminations for miller={miller_t}")
    if not 0 <= termination < len(slabs):
        raise ValueError(f"termination={termination} out of range (found {len(slabs)})")
    if align not in ("bottom", "center"):
        raise ValueError('align must be "bottom" or "center"')
    slab = slabs[termination]
    # pymatgen-core >= 2026.4.16 keeps get_slab()'s output UNREDUCED whenever
    # the primitive oriented-unit-cell fails its new a/b-consistency check
    # (needed only for Slab.oriented_unit_cell, which we don't expose), so e.g.
    # Si(111) comes back as a 2x2 in-plane supercell (32 atoms instead of 8).
    # Restore the invariant every downstream consumer assumes: the returned
    # slab is the primitive in-plane cell. Vacuum makes any pure-z reduction
    # impossible, so get_primitive_structure can only fold in-plane repeats;
    # on older pymatgen the slab is already primitive and this is a no-op.
    prim = slab.get_primitive_structure(tolerance=0.1)
    if len(prim) < len(slab):
        slab = Slab(prim.lattice, prim.species_and_occu, prim.frac_coords,
                    miller_index=slab.miller_index,
                    oriented_unit_cell=slab.oriented_unit_cell,
                    shift=slab.shift, scale_factor=slab.scale_factor,
                    reorient_lattice=True)
    # Canonicalize the in-plane basis (Lagrange-Gauss reduction). The same
    # pymatgen versions also hand back valid but skewed primitive settings
    # (Si(001): 3.84x5.43 Å γ=135° instead of 3.84x3.84 Å γ=90°); a det=1
    # in-plane shear folds that to the reduced cell downstream geometry
    # expects. Strict-improvement-only steps (round(±0.5) -> 0) leave
    # already-reduced cells untouched, incl. the γ=120° hexagonal tie.
    m2 = np.array(slab.lattice.matrix[:2], dtype=float)
    t2 = np.eye(2, dtype=int)
    for _ in range(64):
        i, j = (0, 1) if np.linalg.norm(m2[1]) >= np.linalg.norm(m2[0]) else (1, 0)
        r = int(round(float(m2[j] @ m2[i]) / float(m2[i] @ m2[i])))
        if r == 0:
            break
        m2[j] -= r * m2[i]
        t2[j] -= r * t2[i]
    if not np.array_equal(t2, np.eye(2, dtype=int)):
        slab = slab.make_supercell(
            [[int(t2[0, 0]), int(t2[0, 1]), 0],
             [int(t2[1, 0]), int(t2[1, 1]), 0],
             [0, 0, 1]], in_place=False)
    out = _standard_orientation(slab)
    planes_summary = {}
    if n_layers is not None:
        planes = _z_layers(out)
        n = int(n_layers)
        if not 1 <= n <= len(planes):
            raise ValueError(f"n_layers must be in 1..{len(planes)} (the generated slab has {len(planes)} atomic planes; "
                             "raise min_slab_size for more)")
        if n < len(planes):
            # keep the n consecutive planes that span the smallest thickness (a complete layer group of a
            # layered material rather than a window cut through one); on a tie take the topmost window
            zs = [float(np.mean([out[i].coords[2] for i in layer])) for layer in planes]
            best = min(range(len(planes) - n + 1), key=lambda k: (round(zs[k + n - 1] - zs[k], 3), -k))
            keep = set(range(best, best + n))
            drop = [i for k, layer in enumerate(planes) if k not in keep for i in layer]
            out.remove_sites(drop)
            planes_summary = {"atomic_planes_kept": n, "atomic_planes_generated": len(planes),
                              "n_layers_note": f"kept {n} of {len(planes)} atomic planes (planes {best + 1}-{best + n} "
                                               f"counted from the bottom, the tightest {n}-plane group)"}
    # Expert ruling (step 128): pymatgen treats min_vacuum_size as a FLOOR
    # and its layer rounding routinely inflates it to ~17-20 Å; 12-15 Å is
    # enough. Normalize the TOTAL vacuum to exactly the requested value
    # (skip when in_unit_planes — the argument is in planes, not Å).
    vac_summary = None
    if not in_unit_planes:
        out, vac_summary = add_vacuum(out, float(min_vacuum_size),
                                      force=True, align=align)
    elif align == "bottom":
        z_min = min(site.coords[2] for site in out)
        out.translate_sites(range(len(out)), [0.0, 0.0, -z_min],
                            frac_coords=False, to_unit_cell=False)

    # Expert rule (bshao, 2026-07-15): freeze the bottom half of the slab's
    # layers at bulk positions (selective dynamics F F F) — relaxations then
    # optimize only the surface side. Applied last so every geometry op above
    # is done; floor() fixes LESS on odd layer counts (relax more, safer).
    sd_summary = {}
    if float(fix_bottom_fraction) > 0 and len(out):
        layers = _z_layers(out)
        n_fix = int(len(layers) * float(fix_bottom_fraction))
        frozen = {i for layer in layers[:n_fix] for i in layer}
        out.add_site_property("selective_dynamics",
                              [[False] * 3 if i in frozen else [True] * 3
                               for i in range(len(out))])
        sd_summary = {"n_layers": len(layers), "fixed_layers": n_fix,
                      "fixed_atoms": len(frozen),
                      "selective_dynamics": (
                          f"bottom {n_fix}/{len(layers)} layers frozen (expert rule: "
                          "fix the bottom half; fix_bottom_fraction=0 disables)")}

    return out, {
        "op": "generate_slab",
        "miller": list(miller_t),
        "n_terminations": len(slabs),
        "termination": termination,
        "shift": round(float(slab.shift), 4),
        "align": align,
        "orientation": "standard: a,b in xy plane, c perpendicular along z",
        "is_symmetric": bool(slab.is_symmetric()),
        "is_polar": bool(slab.is_polar()),
        "all_termination_shifts": [round(float(s.shift), 4) for s in slabs],
        **planes_summary,
        **({"vacuum_A": vac_summary["vacuum"],
            "slab_thickness_A": vac_summary["slab_thickness"]}
           if vac_summary else {}),
        **sd_summary,
    }


def set_selective_dynamics(
    structure: Structure,
    mode: str = "bottom_half",
    n_layers: int | None = None,
    z_window: list[float] | None = None,
    indices: list[int] | None = None,
) -> tuple[Structure, dict]:
    """Rewrite the slab's selective-dynamics flags from a named convention
    (step 211). Born from a live incident: a symmetric GaN slab needed its
    CENTRAL bilayer frozen — no primitive existed, the agent hand-wrote the
    POSCAR and scrambled the species blocks (job discarded). The frozen set
    is chosen here; grouping/ordering safety comes from sio.save as always.

    Modes (the selection is frozen F F F, everything else relaxes T T T):
      bottom_half   — floor(L/2) bottom layers (the step-195 default rule);
      bottom_layers — the bottom `n_layers` layers;
      center_layers — the central `n_layers` layers (default 2: the
                      symmetric-slab "freeze the central bilayer" convention);
      z_window      — atoms with cartesian z inside [zlo, zhi] Å;
      indices       — exact site indices (0-based, viewer/hover numbering).
    """
    if not len(structure):
        raise ValueError("empty structure")
    out = structure.copy()
    layers = _z_layers(out)
    L = len(layers)
    if mode == "bottom_half":
        pick = layers[: L // 2]
        frozen = {i for layer in pick for i in layer}
        desc = f"bottom {L // 2}/{L} layers"
    elif mode == "bottom_layers":
        n = int(n_layers if n_layers is not None else 1)
        if not 0 < n <= L:
            raise ValueError(f"n_layers must be in 1..{L} (slab has {L} layers)")
        frozen = {i for layer in layers[:n] for i in layer}
        desc = f"bottom {n}/{L} layers"
    elif mode == "center_layers":
        n = int(n_layers if n_layers is not None else 2)
        if not 0 < n <= L:
            raise ValueError(f"n_layers must be in 1..{L} (slab has {L} layers)")
        start = (L - n) // 2
        frozen = {i for layer in layers[start:start + n] for i in layer}
        desc = f"central layers {start + 1}-{start + n} of {L}"
    elif mode == "z_window":
        if not z_window or len(z_window) != 2:
            raise ValueError("z_window mode needs z_window=[zlo, zhi] in Å")
        zlo, zhi = sorted(float(v) for v in z_window)
        frozen = {i for i, s in enumerate(out) if zlo <= s.coords[2] <= zhi}
        desc = f"z ∈ [{zlo:.2f}, {zhi:.2f}] Å"
    elif mode == "indices":
        if not indices:
            raise ValueError("indices mode needs a non-empty indices list")
        bad = [i for i in indices if not 0 <= int(i) < len(out)]
        if bad:
            raise ValueError(f"indices out of range 0..{len(out) - 1}: {bad}")
        frozen = {int(i) for i in indices}
        desc = f"{len(frozen)} explicit sites"
    else:
        raise ValueError(
            'mode must be "bottom_half" | "bottom_layers" | "center_layers" '
            '| "z_window" | "indices"')
    if not frozen:
        raise ValueError(f"selection is empty ({mode}: {desc}) — nothing would be frozen")
    out.add_site_property("selective_dynamics",
                          [[False] * 3 if i in frozen else [True] * 3
                           for i in range(len(out))])
    return out, {
        "op": "set_selective_dynamics",
        "mode": mode,
        "n_layers_total": L,
        "frozen_atoms": len(frozen),
        "relaxing_atoms": len(out) - len(frozen),
        "frozen_selection": desc,
        "note": "frozen = F F F, rest = T T T; flags are REPLACED wholesale",
    }


def cap_slab_bottom(structure: Structure, layers: int = 1) -> tuple[Structure, dict]:
    """Bottom capping (group method, bshao 2026-07-17): extend the crystal
    BELOW the slab's bottom surface by `layers` bulk-registered layers — the
    group's "passivation" for metal slabs, aimed at decoupling the top and
    bottom surfaces. Placement is deterministic: the stacking translation T
    that maps layer k+1 onto the bottom layer (species-preserving, so fcc
    ABC stacking continues automatically) generates the new layer as
    (layer k) + T. NOT the dangling-bond passivate_surface — that tool's
    bond-vector engine rightly refuses close-packed metals (3+ missing
    bonds are geometrically ambiguous). Capping atoms are FROZEN (F F F)
    when the slab carries selective_dynamics — they stand in for bulk.
    Vacuum and the bottom-aligned convention are preserved (c grows by the
    added thickness; min z returns to 0). Validation protocol (pending the
    group's acceptance run): compare adsorption-energy/work-function
    convergence of N-layer, (N+1)-layer, and N-layer+cap slabs."""
    if int(layers) < 1:
        raise ValueError("layers must be >= 1")
    out = structure.copy()
    added_total = 0
    t_used = None
    for _ in range(int(layers)):
        zl = _z_layers(out)
        if len(zl) < 2:
            raise ValueError("need at least 2 resolvable z-layers to infer the stacking")
        frac = np.array([out[i].frac_coords for i in range(len(out))], dtype=float)

        def _maps(period: int) -> np.ndarray | None:
            """Species-preserving stacking translation mapping layer[period]
            onto layer[0] (in-plane PBC), or None."""
            l_low, l_high = zl[0], zl[period]
            for b in l_low:
                for a in l_high:
                    t = frac[b] - frac[a]
                    ok = True
                    for a2 in l_high:
                        target = frac[a2] + t
                        hit = False
                        for b2 in l_low:
                            d = frac[b2] - target
                            d[:2] -= np.round(d[:2])  # in-plane PBC
                            cart = out.lattice.get_cartesian_coords(d)
                            if np.linalg.norm(cart) < 0.3 and \
                                    str(out[b2].specie) == str(out[a2].specie):
                                hit = True
                                break
                        if not hit:
                            ok = False
                            break
                    if ok:
                        return t
            return None

        t = None
        period = None
        for k in (1, 2):  # k=1 covers elemental fcc/bcc/hcp; k=2 ABAB compounds
            if len(zl) > k and (t := _maps(k)) is not None:
                period = k
                break
        if t is None:
            raise ValueError(
                "could not infer a species-preserving stacking translation from the "
                "bottom layers — the slab is not bulk-truncated (relaxed/reconstructed?); "
                "cap before relaxing, or build a thicker slab instead")
        # new layer = (layer at index period-1) translated down by T
        src = zl[period - 1]
        dz_cart = abs(float(out.lattice.get_cartesian_coords(t)[2]))
        old_sd = out.site_properties.get("selective_dynamics")
        for i in src:
            out_frac = out[i].frac_coords + t
            out.append(out[i].species, out_frac, coords_are_cartesian=False,
                       validate_proximity=False)
        n_new = len(src)
        if old_sd is not None:
            out.add_site_property("selective_dynamics",
                                  [list(x) for x in old_sd] + [[False] * 3] * n_new)
        # restore conventions: grow c by the added thickness (vacuum unchanged),
        # then shift so the new bottom sits at z = 0
        grown = Structure(
            np.array(out.lattice.matrix, dtype=float)
            + np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 0.0],
                        [0.0, 0.0, dz_cart]]),
            [s.species for s in out],
            [s.coords for s in out], coords_are_cartesian=True,
            site_properties=out.site_properties)
        z_min = min(s.coords[2] for s in grown)
        grown.translate_sites(list(range(len(grown))), [0.0, 0.0, -z_min],
                              frac_coords=False, to_unit_cell=False)
        out = grown
        added_total += n_new
        t_used = [round(float(x), 4) for x in t]
    z = [s.coords[2] for s in out]
    return out, {
        "op": "cap_slab_bottom",
        "layers_added": int(layers),
        "atoms_added": added_total,
        "stacking_vector_frac": t_used,
        "capping_frozen": "selective_dynamics" in out.site_properties,
        "n_sites_after": len(out),
        "slab_thickness_A": round(max(z) - min(z), 3),
        "note": ("capping atoms sit at bulk-registered positions below the old "
                 "bottom layer (group decoupling method); validated on "
                 "Pt(111)+H (BACKENDS §19, 2026-07-19) — for hetero-capping "
                 "(e.g. PdO+Pd) run the same N vs N+1 vs N+cap check once"),
    }


# ------------------------------------------------------------------- vacuum
def add_vacuum(structure: Structure, vacuum: float, force: bool = False,
               align: str = "bottom") -> tuple[Structure, dict]:
    """Set the TOTAL vacuum along c to `vacuum` Å (c = slab_thickness + vacuum).

    ASE `center(vacuum=v, axis=2)` pads v per side → pass vacuum/2 for total semantics.
    `align`: "bottom" (default) then shifts the slab to the cell base with all vacuum
    above (adsorbate convention, same as generate_slab); "center" keeps ASE's centering.
    """
    if vacuum < MIN_VACUUM_A and not force:
        raise ValueError(
            f"vacuum={vacuum} Å is below the 2D guard ({MIN_VACUUM_A} Å); pass force=true to override"
        )
    if align not in ("bottom", "center"):
        raise ValueError('align must be "bottom" or "center"')
    from pymatgen.io.ase import AseAtomsAdaptor

    before_c = float(structure.lattice.c)
    # the ASE round trip must not drop selective_dynamics (frozen bottom
    # layers, expert rule): strip it before the adaptor (constraint-mapping
    # quirks) and re-apply after — atom order is preserved by the round trip
    sd = structure.site_properties.get("selective_dynamics")
    src = structure
    if sd is not None:
        src = structure.copy()
        src.remove_site_property("selective_dynamics")
    atoms = AseAtomsAdaptor.get_atoms(src)
    atoms.center(vacuum=vacuum / 2.0, axis=2)
    new = AseAtomsAdaptor.get_structure(atoms)
    if sd is not None:
        new.add_site_property("selective_dynamics", [list(x) for x in sd])
    if align == "bottom" and len(new):
        z_min = min(site.coords[2] for site in new)
        new.translate_sites(range(len(new)), [0.0, 0.0, -z_min],
                            frac_coords=False, to_unit_cell=False)
    z = [s.coords[2] for s in new]
    thickness = round(max(z) - min(z), 4) if z else 0.0
    return new, {
        "op": "add_vacuum",
        "vacuum": round(float(vacuum), 4),
        "before_c": round(before_c, 4),
        "after_c": round(float(new.lattice.c), 4),
        "slab_thickness": thickness,
        "align": align,
    }


# ------------------------------------------------------------- heterostructure
def _metric2d(vec_pair) -> tuple[float, float, float]:
    """2D lattice metric (|v1|, |v2|, angle°) — frame-invariant."""
    v1 = np.asarray(vec_pair[0], dtype=float)
    v2 = np.asarray(vec_pair[1], dtype=float)
    l1, l2 = float(np.linalg.norm(v1)), float(np.linalg.norm(v2))
    ang = float(np.degrees(np.arccos(np.clip(np.dot(v1, v2) / (l1 * l2), -1.0, 1.0))))
    return l1, l2, ang


def _side_strain(side: tuple, target: tuple) -> dict:
    """In-plane strain a side experiences when deformed onto the target metric."""
    return {
        "e11_pct": round((target[0] / side[0] - 1.0) * 100, 4),
        "e22_pct": round((target[1] / side[1] - 1.0) * 100, 4),
        "dgamma_deg": round(target[2] - side[2], 4),
    }


def _interface_candidates(
    film, substrate, film_miller, substrate_miller,
    film_thickness, substrate_thickness, gap, vacuum, max_area, max_strain, termination,
):
    """Shared ZSL candidate enumeration: deterministic sort + strain filter.
    The index into the returned list == `candidate` in make_interface."""
    from pymatgen.analysis.interfaces.coherent_interfaces import CoherentInterfaceBuilder
    from pymatgen.analysis.interfaces.zsl import ZSLGenerator

    cib = CoherentInterfaceBuilder(
        substrate_structure=substrate,
        film_structure=film,
        substrate_miller=tuple(int(x) for x in substrate_miller),
        film_miller=tuple(int(x) for x in film_miller),
        zslgen=ZSLGenerator(max_area=float(max_area)),
    )
    terms = cib.terminations
    if not terms:
        raise ValueError("no film/substrate termination pairs found")
    if not 0 <= termination < len(terms):
        raise ValueError(f"termination={termination} out of range (found {len(terms)}: {terms})")

    cands = list(cib.get_interfaces(
        termination=terms[termination], gap=float(gap),
        vacuum_over_film=float(vacuum),
        film_thickness=film_thickness, substrate_thickness=substrate_thickness,
        in_layers=True,
    ))
    cands.sort(key=lambda i: (round(float(i.interface_properties["von_mises_strain"]), 6),
                              len(i), round(i.lattice.a, 4)))
    # collapse symmetry-equivalent matches (identical strain/size/cell metric)
    seen: set = set()
    unique = []
    for c in cands:
        lat = c.lattice
        key = (round(float(c.interface_properties["von_mises_strain"]), 6), len(c),
               round(lat.a, 4), round(lat.b, 4), round(lat.angles[2], 2))
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)
    within = [c for c in unique
              if float(c.interface_properties["von_mises_strain"]) <= max_strain]
    if not within:
        best = float(cands[0].interface_properties["von_mises_strain"]) if cands else None
        best_txt = f"{best:.4f}" if best is not None else "none at all"
        raise ValueError(
            f"no coincidence lattice with von Mises strain <= {max_strain} "
            f"(best found: {best_txt} at max_area={max_area}; "
            f"raise max_area or max_strain)")
    return terms, within


def list_interface_matches(
    film: Structure,
    substrate: Structure,
    film_miller: list[int],
    substrate_miller: list[int],
    film_thickness: float = 3,
    substrate_thickness: float = 4,
    gap: float = 2.0,
    vacuum: float = 15.0,
    max_area: float = 200.0,
    max_strain: float = 0.05,
    termination: int = 0,
    limit: int = 20,
) -> dict:
    """Enumerate ZSL lattice-match candidates (QuantumATK-style strain-vs-size
    survey): per candidate the strain, atom count, in-plane cell and area.
    Row index k == `candidate=k` in make_interface (identical ordering)."""
    terms, within = _interface_candidates(
        film, substrate, film_miller, substrate_miller,
        film_thickness, substrate_thickness, gap, vacuum, max_area, max_strain, termination,
    )
    rows = []
    for k, cand in enumerate(within[: max(1, int(limit))]):
        p = cand.interface_properties
        sub_m = _metric2d(p["substrate_sl_vectors"][:2])
        film_m = _metric2d(p["film_sl_vectors"][:2])
        lat = cand.lattice
        rows.append({
            "candidate": k,
            "von_mises_strain_pct": round(float(p["von_mises_strain"]) * 100, 4),
            "n_atoms": len(cand),
            "in_plane_ab": [round(lat.a, 4), round(lat.b, 4)],
            "gamma_deg": round(lat.angles[2], 2),
            "area_A2": round(lat.a * lat.b * abs(np.sin(np.radians(lat.angles[2]))), 2),
            "film_vs_substrate_mismatch": _side_strain(film_m, sub_m),
        })
    return {
        "op": "list_interface_matches",
        "film_formula": film.composition.reduced_formula,
        "substrate_formula": substrate.composition.reduced_formula,
        "termination": list(terms[termination]),
        "n_terminations": len(terms),
        "n_candidates_within_strain": len(within),
        "shown": len(rows),
        "note": "candidate index here maps 1:1 to make_interface(candidate=...)",
        "candidates": rows,
    }


def make_interface(
    film: Structure,
    substrate: Structure,
    film_miller: list[int],
    substrate_miller: list[int],
    film_thickness: float = 3,
    substrate_thickness: float = 4,
    gap: float = 2.0,
    vacuum: float = 15.0,
    max_area: float = 200.0,
    max_strain: float = 0.05,
    termination: int = 0,
    candidate: int = 0,
    orthogonal_cell: bool = False,
    strain_method: str = "film",
) -> tuple[Structure, dict]:
    """Coherent film/substrate heterostructure via ZSL coincidence-lattice search
    (BACKENDS.md §10). Thicknesses in layers; gap/vacuum in Å.

    Candidates are sorted deterministically by (von Mises strain, n_atoms, a);
    `candidate` indexes that list (survey them with list_interface_matches),
    `max_strain` guards against bad matches.

    `strain_method` decides who absorbs the lattice mismatch (QuantumATK-style):
    "film" (default) strains the film onto the substrate lattice; "substrate"
    keeps the film natural and strains the substrate; "average" splits the
    mismatch evenly between both sides.

    All atoms are wrapped into the cell (project convention). Note that (111)
    and other hexagonal-plane interfaces are intrinsically non-orthogonal
    (γ=60°/120° — correct physics, same as our slab convention); pass
    `orthogonal_cell=True` to convert such cells to the rectangular a×√3a
    setting (doubles the atom count).
    """
    if strain_method not in ("film", "substrate", "average"):
        raise ValueError('strain_method must be "film", "substrate" or "average"')
    terms, within = _interface_candidates(
        film, substrate, film_miller, substrate_miller,
        film_thickness, substrate_thickness, gap, vacuum, max_area, max_strain, termination,
    )
    if not 0 <= candidate < len(within):
        raise ValueError(f"candidate={candidate} out of range ({len(within)} within max_strain)")

    iface = within[candidate]
    vm = float(iface.interface_properties["von_mises_strain"])
    # Wrap every atom into the cell (live-use finding, DEVLOG step 56: pymatgen
    # Interface objects carry fractional coords outside [0,1)).
    result = Structure.from_sites(iface.sites, to_unit_cell=True)
    for site in result:
        frac = site.frac_coords % 1.0
        frac[frac >= 1.0] = 0.0  # float modulo can land exactly on 1.0
        site.frac_coords = frac
    n_film = len(iface.film_indices)
    n_substrate = len(iface.substrate_indices)

    # ---- strain redistribution (metric swap; assembled in-plane == substrate
    # superlattice metric, verified exactly — DEVLOG step 59) ----
    p = iface.interface_properties
    sub_m = _metric2d(p["substrate_sl_vectors"][:2])
    film_m = _metric2d(p["film_sl_vectors"][:2])
    if strain_method == "film":
        target_m = sub_m
    elif strain_method == "substrate":
        target_m = film_m
    else:  # average — split the mismatch evenly
        target_m = tuple((a + b) / 2.0 for a, b in zip(sub_m, film_m))
    if strain_method != "film":
        l1, l2, ang = target_m
        rad = np.radians(ang)
        c_row = result.lattice.matrix[2]
        new_lattice = [
            [l1, 0.0, 0.0],
            [l2 * float(np.cos(rad)), l2 * float(np.sin(rad)), 0.0],
            [float(c_row[0]), float(c_row[1]), float(c_row[2])],
        ]
        result = Structure(new_lattice, [str(s.specie) for s in result], result.frac_coords)
    film_strain = _side_strain(film_m, target_m)
    substrate_strain = _side_strain(sub_m, target_m)
    film_f = film.composition.reduced_formula
    sub_f = substrate.composition.reduced_formula
    # user-facing: strain keyed by material formula, not by film/substrate role
    if film_f == sub_f:  # homo-interface: disambiguate
        film_key, sub_key = f"{film_f} (film)", f"{sub_f} (substrate)"
    else:
        film_key, sub_key = film_f, sub_f
    strain_by_material = {film_key: film_strain, sub_key: substrate_strain}

    gamma_note = None
    gamma = result.lattice.angles[2]
    if orthogonal_cell and abs(gamma - 90.0) > 1.0:
        a_len, b_len = result.lattice.a, result.lattice.b
        if abs(a_len - b_len) / max(a_len, b_len) < 0.02 and (
            abs(gamma - 60.0) < 1.0 or abs(gamma - 120.0) < 1.0
        ):
            # hexagonal in-plane cell -> rectangular a x sqrt(3)a supercell
            t = [[1, 0, 0], [-1, 2, 0], [0, 0, 1]] if abs(gamma - 60.0) < 1.0 \
                else [[1, 0, 0], [1, 2, 0], [0, 0, 1]]
            result.make_supercell(t, to_unit_cell=True)
            n_film *= 2
            n_substrate *= 2
            gamma_note = "hexagonal in-plane cell converted to the rectangular a x sqrt(3)a setting (atom count doubled)"
        else:
            gamma_note = (
                f"orthogonal_cell requested but gamma={gamma:.1f} deg is not a "
                "hexagonal setting this op can rectangularize; returning the "
                "primitive interface cell"
            )
    elif abs(gamma - 90.0) > 1.0:
        gamma_note = (
            f"gamma={gamma:.1f} deg is the correct primitive in-plane cell for "
            "this orientation (hexagonal-plane interfaces such as (111) are "
            "intrinsically non-orthogonal); pass orthogonal_cell=true for the "
            "rectangular setting at 2x atoms"
        )

    lat = result.lattice
    return result, {
        "op": "make_interface",
        "film_formula": film.composition.reduced_formula,
        "substrate_formula": substrate.composition.reduced_formula,
        "film_miller": [int(x) for x in film_miller],
        "substrate_miller": [int(x) for x in substrate_miller],
        "termination": list(terms[termination]),
        "n_terminations": len(terms),
        "n_candidates_within_strain": len(within),
        "candidate": int(candidate),
        "von_mises_strain_pct": round(vm * 100, 4),
        "strain_method": strain_method,
        "film_strain": film_strain,
        "substrate_strain": substrate_strain,
        "strain_by_material": strain_by_material,
        "in_plane_ab": [round(lat.a, 4), round(lat.b, 4)],
        "gamma_deg": round(lat.angles[2], 2),
        "gamma_note": gamma_note,
        "n_film_atoms": n_film,
        "n_substrate_atoms": n_substrate,
        "gap": gap,
        "vacuum": vacuum,
    }


# ------------------------------------------------------------------ vacancy
def list_inequivalent_sites(structure: Structure, symprec: float = 0.1) -> list[dict]:
    """Symmetry-inequivalent site classes (spglib), deterministically ordered."""
    sga = SpacegroupAnalyzer(structure, symprec=symprec)
    sym = sga.get_symmetrized_structure()
    classes = []
    for group in sym.equivalent_indices:
        rep = min(group)
        site = structure[rep]
        classes.append({
            "element": str(site.specie.symbol),
            "rep_index": rep,
            "multiplicity": len(group),
            "indices": sorted(group),
            "frac_coords": [round(float(x), 4) for x in site.frac_coords],
        })
    classes.sort(key=lambda c: (c["element"], c["rep_index"]))
    return classes


def make_vacancy(
    structure: Structure,
    element: str | None = None,
    index: int | None = None,
    symprec: float = 0.1,
) -> tuple[Structure, dict]:
    """Remove one site: by explicit `index`, or the representative of the first
    symmetry-inequivalent class of `element` (deterministic order)."""
    classes = list_inequivalent_sites(structure, symprec=symprec)
    if index is not None:
        if not 0 <= index < len(structure):
            raise ValueError(f"index={index} out of range (0..{len(structure) - 1})")
        chosen = index
        cls = next((c for c in classes if chosen in c["indices"]), None)
    else:
        if element is None:
            raise ValueError("provide either `element` or `index`")
        matching = [c for c in classes if c["element"] == element]
        if not matching:
            raise ValueError(f"no {element} sites in structure "
                             f"(elements: {sorted({c['element'] for c in classes})})")
        cls = matching[0]
        chosen = cls["rep_index"]
    removed = structure[chosen]
    new = structure.copy()
    new.remove_sites([chosen])
    return new, {
        "op": "make_vacancy",
        "removed_index": chosen,
        "removed_species": str(removed.specie.symbol),
        "removed_frac_coords": [round(float(x), 4) for x in removed.frac_coords],
        "wyckoff_multiplicity": cls["multiplicity"] if cls else None,
        "n_inequivalent_classes": len(classes),
        "n_sites_after": len(new),
    }


# ----------------------------------------------------------------- adsorption

# Step 245 (live gap): the ASE g2 set has NO hydroperoxyl — neither "OOH" nor
# "HO2" — so the standard OER/ORR intermediate chain (OH*, O*, OOH*) broke at
# OOH. Literature gas-phase HO2 geometry (r_OO = 1.331 Å, r_OH = 0.971 Å,
# ∠HOO = 104.3°, JANAF/experimental), laid out anchor-first: the H-free O is
# the surface-binding atom and sits lowest, so both the default lowest-atom
# anchor and anchor_element="O" grab it. A starting guess like every g2
# geometry — the relaxation refines it (BACKENDS §13 amendment).
_CUSTOM_ADSORBATES = {
    "OOH": (["O", "O", "H"],
            [[0.000, 0.000, 0.000],
             [0.000, 0.000, 1.331],
             [0.941, 0.000, 1.571]]),
    # Step 246 (tier B, geometries pending bshao physics review — same audit
    # convention as the che ZPE tables): the two entry intermediates of CO2RR,
    # absent from g2 under every spelling.
    # COOH* (carboxyl, trans-HOCO): binds through C -> C lowest. Gas-phase
    # r(C=O) 1.18 / r(C-OH) 1.34 / r(O-H) 0.97 A, OCO 127deg, COH 108deg.
    "COOH": (["C", "O", "O", "H"],
             [[0.000, 0.000, 0.000],
              [1.056, 0.000, 0.526],
              [-1.199, 0.000, 0.598],
              [-1.056, 0.000, 1.557]]),
    # HCOO* (formate, bidentate): both O DOWN (z=0, symmetric), C above,
    # H on top. Resonance-symmetric r(C-O) 1.25 A, OCO 130deg, C-H 1.10 A.
    "HCOO": (["O", "O", "C", "H"],
             [[-1.133, 0.000, 0.000],
              [1.133, 0.000, 0.000],
              [0.000, 0.000, 0.528],
              [0.000, 0.000, 1.628]]),
}
_CUSTOM_ADSORBATES["HO2"] = _CUSTOM_ADSORBATES["OOH"]

# Step 246 (tier A): common names whose g2 spelling differs — pure mapping,
# no new geometry. CH2 is deliberately NOT aliased: g2 splits it into
# CH2_s3B1d (triplet ground state) / CH2_s1A1d (singlet) and the choice is
# an expert call per use.
_G2_ALIASES = {"CHO": "HCO", "CH2O": "H2CO", "OCH2": "H2CO",
               "H2S": "SH2", "C2H5OH": "CH3CH2OH"}


def _load_adsorbate(adsorbate: str):
    """Molecule from an ASE g2 name (BACKENDS §13), a built-in species missing
    from g2 (OOH/HO2, step 245), or a structure file path."""
    from pathlib import Path

    from pymatgen.core import Molecule

    if Path(adsorbate).exists():
        return Molecule.from_file(adsorbate), f"file:{adsorbate}"
    if adsorbate in _CUSTOM_ADSORBATES:
        species, coords = _CUSTOM_ADSORBATES[adsorbate]
        return Molecule(species, coords), f"builtin:{adsorbate}"
    from ase.build import molecule as ase_molecule
    from ase.collections import g2
    from pymatgen.io.ase import AseAtomsAdaptor

    g2_name = _G2_ALIASES.get(adsorbate, adsorbate)
    if g2_name not in g2.names:
        raise ValueError(
            f"unknown adsorbate {adsorbate!r}: not a file, not a built-in "
            f"({', '.join(sorted(set(_CUSTOM_ADSORBATES)))}) and not in the ASE "
            f"g2 set (162 molecules, e.g. CO, OH, H2O, NH3, O2, N2, CH4)"
        )
    source = f"g2:{g2_name}" + ("" if g2_name == adsorbate else f"(alias:{adsorbate})")
    return AseAtomsAdaptor.get_molecule(ase_molecule(g2_name)), source


def make_molecule(molecule: str, box: float = 15.0) -> tuple[Structure, dict]:
    """Gas-phase molecule centered in a cubic vacuum box (step 277).

    The ONLY sanctioned route to a gas-reference POSCAR (E_O3, E_O2, …).
    Live incident: an agent hand-wrote three collinear O atoms as the O3 gas
    reference — a perfectly symmetric start has zero perpendicular force, so
    the relax converged to the linear SADDLE POINT and every adsorption
    energy referenced to it was ~1 eV too negative. Molecule sources are
    shared with add_adsorbate (BACKENDS §13: ASE g2 set, built-ins, file
    path), so the gas reference and the adsorbate carry the same geometry.
    """
    mol, source = _load_adsorbate(molecule)
    box = float(box)
    coords = np.array([site.coords for site in mol], dtype=float)
    extent = float(max(coords.max(axis=0) - coords.min(axis=0))) if len(mol) > 1 else 0.0
    # Same isolation logic as the adsorbate image rule: periodic images of
    # the molecule should not talk to each other.
    min_gap = box - extent
    if min_gap < ADSORBATE_IMAGE_SEPARATION:
        raise ValueError(
            f"box = {box:.1f} Å leaves only {min_gap:.1f} Å between periodic "
            f"images of {molecule!r} (extent {extent:.1f} Å) — use box >= "
            f"{extent + ADSORBATE_IMAGE_SEPARATION:.0f} Å.")
    lattice = Lattice.cubic(box)
    center_shift = np.array([box / 2] * 3) - coords.mean(axis=0)
    struct = Structure(
        lattice,
        [site.species_string for site in mol],
        coords + center_shift,
        coords_are_cartesian=True,
    )

    # Geometry summary so the caller can eyeball the starting shape against
    # the expected molecule BEFORE burning compute on it.
    bonds: list[dict] = []
    neighbors: dict[int, list[int]] = {}
    for i in range(len(mol)):
        for j in range(i + 1, len(mol)):
            d = float(np.linalg.norm(coords[i] - coords[j]))
            if d <= 2.0:
                bonds.append({"atoms": [f"{mol[i].species_string}{i}",
                                        f"{mol[j].species_string}{j}"],
                              "length": round(d, 3)})
                neighbors.setdefault(i, []).append(j)
                neighbors.setdefault(j, []).append(i)
    angles: list[dict] = []
    for i, nbrs in sorted(neighbors.items()):
        if len(nbrs) != 2:
            continue
        a, b = nbrs
        v1, v2 = coords[a] - coords[i], coords[b] - coords[i]
        cos = float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2)))
        angles.append({"vertex": f"{mol[i].species_string}{i}",
                       "atoms": [f"{mol[a].species_string}{a}",
                                 f"{mol[b].species_string}{b}"],
                       "degrees": round(math.degrees(math.acos(max(-1.0, min(1.0, cos)))), 1)})
    summary = {
        "op": "make_molecule",
        "molecule": molecule,
        "source": source,
        # composition.formula, NOT reduced_formula: pymatgen reduces
        # elemental O3 to "O2" (live display confusion, same session).
        "formula": mol.composition.formula.replace(" ", ""),
        "n_atoms": len(mol),
        "box": box,
        "image_separation": round(min_gap, 2),
        "bonds": bonds,
        "angles": angles,
        "kpoints_note": "Gamma-only (1x1x1) is the correct sampling for an isolated molecule.",
    }
    return struct, summary


def list_adsorption_sites(slab: Structure, distance: float = 2.0) -> dict:
    """Symmetry-distinct ontop/bridge/hollow sites (BACKENDS §12), deterministic order.

    Ordering (step 236): nearest to the in-plane cell CENTER first, previous
    (x, y, z) order as tie-break. The old x-then-y ascending order made
    site_index=0 — add_adsorbate's default — the cell CORNER, so every
    default build landed the molecule on the periodic boundary (user bug
    report: "molecules sit at the edges"). PBC-equivalent physics, but the
    centered choice renders whole and reads correctly."""
    from pymatgen.analysis.adsorption import AdsorbateSiteFinder

    finder = AdsorbateSiteFinder(slab)
    found = finder.find_adsorption_sites(distance=float(distance))
    center = slab.lattice.get_cartesian_coords([0.5, 0.5, 0.0])
    out = {}
    for kind in ("ontop", "bridge", "hollow"):
        rows = sorted(
            ([round(float(x), 4) for x in cart] for cart in found.get(kind, [])),
            key=lambda c: (round(float(np.hypot(c[0] - center[0], c[1] - center[1])), 6),
                           c[0], c[1], c[2]),
        )
        out[kind] = rows
    return {
        "op": "list_adsorption_sites",
        "distance": float(distance),
        "n_sites": {k: len(v) for k, v in out.items()},
        "sites_cartesian": out,
    }


# Expert rule (bshao, VASP authority, 2026-07-15): ~10 Å in-plane separation
# between periodic images of an adsorbate isolates the spurious image-image
# interaction PBC introduces — adsorption supercells should aim for in-plane
# lattice vectors around 10 Å.
ADSORBATE_IMAGE_SEPARATION = 10.0


def _min_inplane_image_distance(lattice) -> float:
    """Closest approach between periodic images of a single adsorbate: the
    shortest in-plane lattice translation |m·a + n·b| over (m, n) ≠ (0, 0)
    (search window ±1 suffices for reasonably reduced surface cells)."""
    a = np.array(lattice.matrix[0], dtype=float)
    b = np.array(lattice.matrix[1], dtype=float)
    return min(float(np.linalg.norm(m * a + n * b))
               for m in (-1, 0, 1) for n in (-1, 0, 1) if (m, n) != (0, 0))


def _center_site_image(work: Structure, target: list[float]) -> list[float]:
    """Translate a chosen adsorption site to its periodic image nearest the
    in-plane cell centre (step 247, user ruling "prefer the primitive-cell centre").

    Step 236 only reordered the SYMMETRY-DISTINCT list, but pymatgen's ASF
    returns one arbitrary representative per class (a 3x3 slab has nine
    equivalent ontop sites yet reports one, usually near a corner) — with a
    single candidate the ordering chose nothing. Translating by the slab's
    own in-plane PRIMITIVE mesh provably maps the surface onto itself, so
    the site class is preserved exactly (fcc hollows stay fcc). A 1x1 or
    already-decorated slab has no smaller mesh -> unchanged."""
    try:
        prim = work.get_primitive_structure(tolerance=0.1)
    except Exception:  # noqa: BLE001 — centering is cosmetic, never fatal
        return target
    if len(prim) >= len(work):
        return target
    a = np.array(prim.lattice.matrix[0], dtype=float)
    b = np.array(prim.lattice.matrix[1], dtype=float)
    if abs(a[2]) > 1e-6 or abs(b[2]) > 1e-6:  # mesh not purely in-plane
        return target
    center = work.lattice.get_cartesian_coords([0.5, 0.5, 0.0])
    span = max(np.linalg.norm(work.lattice.matrix[0]),
               np.linalg.norm(work.lattice.matrix[1]))
    step_len = min(np.linalg.norm(a), np.linalg.norm(b))
    reach = min(12, int(np.ceil(span / max(1e-6, step_len))) + 1)
    best, best_key = list(target), None
    t = np.array(target, dtype=float)
    for m in range(-reach, reach + 1):
        for n in range(-reach, reach + 1):
            cand = t + m * a + n * b
            frac = work.lattice.get_fractional_coords(cand)
            if not (-1e-9 <= frac[0] < 1 - 1e-9 and -1e-9 <= frac[1] < 1 - 1e-9):
                continue
            d = float(np.hypot(cand[0] - center[0], cand[1] - center[1]))
            key = (round(d, 6), round(float(cand[0]), 6), round(float(cand[1]), 6))
            if best_key is None or key < best_key:
                best_key, best = key, [float(x) for x in cand]
    return best


def add_adsorbate(
    slab: Structure,
    adsorbate: str,
    site_type: str = "ontop",
    site_index: int = 0,
    height: float = 2.0,
    anchor_element: str | None = None,
    custom_position: list[float] | None = None,
    repeat: list[int] | None = None,
    min_image_separation: float = ADSORBATE_IMAGE_SEPARATION,
    recenter: bool = True,
) -> tuple[Structure, dict]:
    """Place an adsorbate on a slab (BACKENDS §12/§13).

    `adsorbate`: ASE g2 name (e.g. "CO") or a molecule file path. The anchor
    atom (by element, else the lowest atom of the molecule) sits exactly
    `height` Å above the surface at the chosen site; the molecule is kept
    rigid and flipped upright if it points into the slab. `custom_position`
    = [fa, fb] in-plane fractional coords overrides site selection.
    `repeat` = [nx, ny] expands the slab first (coverage control).
    """
    mol, source = _load_adsorbate(adsorbate)

    work = slab
    if repeat:
        if len(repeat) != 2 or any(int(n) < 1 for n in repeat):
            raise ValueError("repeat must be [nx, ny] positive integers")
        work = slab.make_supercell([int(repeat[0]), int(repeat[1]), 1], in_place=False)

    top_z = max(site.coords[2] for site in work)
    if custom_position is not None:
        if len(custom_position) != 2:
            raise ValueError("custom_position must be [fa, fb] fractional in-plane coords")
        cart = work.lattice.get_cartesian_coords(
            [float(custom_position[0]) % 1.0, float(custom_position[1]) % 1.0, 0.0]
        )
        target = [float(cart[0]), float(cart[1]), top_z + float(height)]
        chosen = {"site_type": "custom", "position": [round(x, 4) for x in target]}
    else:
        sites = list_adsorption_sites(work, distance=float(height))["sites_cartesian"]
        if site_type not in sites:
            raise ValueError('site_type must be "ontop", "bridge" or "hollow"')
        pool = sites[site_type]
        if not pool:
            raise ValueError(f"no {site_type} sites found on this slab")
        if not 0 <= site_index < len(pool):
            raise ValueError(f"site_index={site_index} out of range ({len(pool)} {site_type} sites)")
        target = _center_site_image(work, list(pool[site_index]))
        chosen = {"site_type": site_type, "site_index": site_index,
                  "position": [round(float(x), 4) for x in target]}

    coords = np.array([s.coords for s in mol], dtype=float)
    species = [str(s.specie) for s in mol]
    if anchor_element:
        matches = [i for i, el in enumerate(species) if el == anchor_element]
        if not matches:
            raise ValueError(f"anchor_element {anchor_element!r} not in adsorbate {species}")
        anchor = matches[0]
    else:
        anchor = int(np.argmin(coords[:, 2]))
    coords -= coords[anchor]  # anchor at origin
    if len(mol) > 1 and float(coords[:, 2].min()) < -1e-6:
        # molecule extends below the anchor -> rotate 180° about x (keeps chirality)
        coords[:, 1] *= -1.0
        coords[:, 2] *= -1.0
    coords += np.array(target, dtype=float)

    vacuum_top = float(work.lattice.c)
    if float(coords[:, 2].max()) >= vacuum_top - 1.0:
        raise ValueError(
            f"adsorbate reaches {coords[:, 2].max():.2f} Å but the cell ends at "
            f"{vacuum_top:.2f} Å — increase the slab vacuum first (add_vacuum)"
        )

    all_species = [str(s.specie) for s in work] + species
    all_cart = np.vstack([np.array([s.coords for s in work]), coords])
    n_slab = len(work)
    frac = work.lattice.get_fractional_coords(all_cart)
    # Step 278 (user ruling; live wwch structures sat at the cell edge): by
    # default the WHOLE cell is rigidly shifted in-plane so the anchor lands
    # at the visual center (0.5, 0.5) — an exact PBC operation that keeps the
    # adsorbate-slab registry (site class, height, freeze flags) bit-identical
    # on ANY slab. The step-247 image centering (_center_site_image) silently
    # no-ops on RELAXED slabs: get_primitive_structure finds no smaller
    # in-plane mesh once surface atoms have moved, so production adsorbates
    # built on relaxed CONTCARs landed at the corner. The rigid shift cannot
    # fail that way; 247's centering is kept as a registry-nicety upstream.
    # custom_position is the user's explicit placement — never overridden.
    recentered = bool(recenter) and custom_position is None
    if recentered:
        frac[:, :2] += np.array([0.5, 0.5]) - frac[n_slab + anchor, :2]
    frac[:n_slab, :2] %= 1.0  # in-plane wrap; z stays contiguous (slab convention)
    # Adsorbate wraps as a RIGID UNIT keyed on the anchor atom (step 236):
    # per-atom wrapping split a boundary-straddling molecule across the cell
    # ("atoms scattered at the edges"). The anchor lands exactly where the
    # old per-atom modulo put it; the rest of the molecule rides along, so
    # non-anchor atoms may land slightly outside [0,1) — legal in POSCAR,
    # and the molecule stays contiguous.
    anchor_inplane = frac[n_slab + anchor, :2].copy()
    frac[n_slab:, :2] += (anchor_inplane % 1.0) - anchor_inplane
    new = Structure(work.lattice, all_species, frac)
    # carry the frozen-bottom flags through the rebuild (a PARTIAL property
    # crashes the POSCAR writer — adsorbate atoms are always free, T T T)
    slab_sd = work.site_properties.get("selective_dynamics")
    if slab_sd is not None:
        new.add_site_property("selective_dynamics",
                              [list(x) for x in slab_sd] + [[True] * 3] * len(species))

    # Image-separation physics check (expert rule above): warn — never block,
    # small cells are legitimate for high-coverage studies — and suggest the
    # supercell that reaches the target separation.
    separation = _min_inplane_image_distance(work.lattice)
    image_warning = None
    if separation < float(min_image_separation):
        base = slab.lattice
        na = int(np.ceil(float(min_image_separation) / float(np.linalg.norm(base.matrix[0]))))
        nb = int(np.ceil(float(min_image_separation) / float(np.linalg.norm(base.matrix[1]))))
        image_warning = (
            f"adsorbate periodic images are only {separation:.2f} Å apart — below the "
            f"~{float(min_image_separation):.0f} Å rule for isolating spurious image-image "
            "interactions (expert rule). Unless this coverage is intentional, rebuild with "
            f"repeat=[{na}, {nb}] on the original slab (or make_supercell first).")

    return new, {
        "op": "add_adsorbate",
        "adsorbate": source,
        "adsorbate_formula": mol.composition.reduced_formula,
        "anchor_element": species[anchor],
        **chosen,
        # `position` above is the SITE REGISTRY location (pre-shift, class-
        # distinct); when recentered, the anchor's final spot is (0.5, 0.5).
        "recentered": recentered,
        "height": float(height),
        "repeat": [int(repeat[0]), int(repeat[1])] if repeat else None,
        "n_slab_atoms": len(work),
        "n_adsorbate_atoms": len(mol),
        "n_sites_after": len(new),
        "image_separation": round(separation, 3),
        **({"image_separation_warning": image_warning} if image_warning else {}),
    }


# --------------------------------------------------------- substitution/doping
def substitute_atom(
    structure: Structure,
    new_element: str,
    index: int | None = None,
    element: str | None = None,
    inequivalent_class: int = 0,
    symprec: float = 0.1,
) -> tuple[Structure, dict]:
    """Replace one atom (BACKENDS §14): by site `index`, or the representative
    of the `inequivalent_class`-th symmetry class of `element` (§6 machinery)."""
    new = structure.copy()
    cls = None
    if index is None:
        if not element:
            raise ValueError("give either index or element")
        classes = [c for c in list_inequivalent_sites(structure, symprec) if c["element"] == element]
        if not classes:
            raise ValueError(f"no {element} sites found")
        if not 0 <= inequivalent_class < len(classes):
            raise ValueError(
                f"inequivalent_class={inequivalent_class} out of range ({len(classes)} {element} classes)"
            )
        cls = classes[inequivalent_class]
        index = cls["rep_index"]
    if not 0 <= index < len(new):
        raise ValueError(f"index={index} out of range ({len(new)} sites)")
    old_element = str(new[index].specie)
    if old_element == new_element:
        raise ValueError(f"site {index} is already {new_element}")
    new[index] = new_element
    return new, {
        "op": "substitute_atom",
        "index": int(index),
        "old_element": old_element,
        "new_element": new_element,
        "site_frac_coords": [round(float(x), 4) for x in new[index].frac_coords],
        "wyckoff_multiplicity": cls["multiplicity"] if cls else None,
        "n_sites": len(new),
    }


# ---------------------------------------------------------------- interstitial
def interstitial_candidates(structure: Structure, min_dist: float = 1.2, top: int = 10) -> list[dict]:
    """Voronoi-vertex interstitial candidates (BACKENDS §15), ranked by clearance."""
    from scipy.spatial import Voronoi

    frac_images = []
    for da in (-1, 0, 1):
        for db in (-1, 0, 1):
            for dc in (-1, 0, 1):
                frac_images.append(structure.frac_coords + [da, db, dc])
    cart = structure.lattice.get_cartesian_coords(np.vstack(frac_images))
    vor = Voronoi(cart)
    rows = []
    seen = set()
    for vertex in vor.vertices:
        f = structure.lattice.get_fractional_coords(vertex)
        if not all(-1e-6 <= x < 1.0 - 1e-6 for x in f):
            continue
        d = min(structure.lattice.get_all_distances([f % 1.0], structure.frac_coords)[0])
        if d < min_dist:
            continue
        key = tuple(round(float(x), 3) for x in f)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"frac_coords": [round(float(x), 4) for x in f],
                     "min_distance": round(float(d), 4)})
    rows.sort(key=lambda r: (-r["min_distance"], tuple(r["frac_coords"])))
    return rows[:top]


def make_interstitial(
    structure: Structure,
    element: str,
    frac_position: list[float] | None = None,
    candidate: int = 0,
    min_dist: float = 1.2,
) -> tuple[Structure, dict]:
    """Insert an interstitial atom (BACKENDS §15). Give `frac_position`
    explicitly (primary interface), or pick `candidate` from the Voronoi
    clearance ranking. `min_dist` guards against unphysical overlaps."""
    if frac_position is not None:
        if len(frac_position) != 3:
            raise ValueError("frac_position must be [fa, fb, fc]")
        f = [float(x) % 1.0 for x in frac_position]
        d = float(min(structure.lattice.get_all_distances([f], structure.frac_coords)[0]))
        if d < min_dist:
            raise ValueError(
                f"interstitial at {f} is only {d:.2f} Å from the nearest atom "
                f"(< {min_dist} Å); pick another position or lower min_dist"
            )
        source = "explicit"
    else:
        cands = interstitial_candidates(structure, min_dist=min_dist)
        if not cands:
            raise ValueError(f"no interstitial candidates with clearance >= {min_dist} Å")
        if not 0 <= candidate < len(cands):
            raise ValueError(f"candidate={candidate} out of range ({len(cands)} candidates)")
        f = [x % 1.0 for x in cands[candidate]["frac_coords"]]
        d = cands[candidate]["min_distance"]
        source = f"voronoi_candidate_{candidate}"
    new = structure.copy()
    new.append(element, f, coords_are_cartesian=False)
    return new, {
        "op": "make_interstitial",
        "element": element,
        "frac_coords": [round(float(x), 4) for x in f],
        "min_distance": round(float(d), 4),
        "position_source": source,
        "n_sites_after": len(new),
    }


# -------------------------------------------------------------- reconstruction
def reconstruct_surface(
    bulk: Structure,
    reconstruction: str,
    min_slab_size: float = 10.0,
    min_vacuum_size: float = 15.0,
    termination: int = 0,
) -> tuple[Structure, dict]:
    """Named surface reconstruction from pymatgen's curated archive
    (BACKENDS §16); output follows our slab conventions (standard orientation,
    bottom-aligned, atoms wrapped)."""
    import json as _json
    import os as _os

    import pymatgen.core.surface as _surf
    from pymatgen.core.surface import ReconstructionGenerator

    archive = _os.path.join(_os.path.dirname(_surf.__file__), "reconstructions_archive.json")
    names = sorted(_json.load(open(archive)))
    if reconstruction not in names:
        raise ValueError(f"unknown reconstruction {reconstruction!r}; available: {', '.join(names)}")
    gen = ReconstructionGenerator(bulk, float(min_slab_size), float(min_vacuum_size), reconstruction)
    slabs = gen.build_slabs()
    if not slabs:
        raise ValueError(f"reconstruction {reconstruction!r} produced no slab for this bulk")
    if not 0 <= termination < len(slabs):
        raise ValueError(f"termination={termination} out of range ({len(slabs)})")
    slab = slabs[termination]
    out = _standard_orientation(slab)
    z_min = min(site.coords[2] for site in out)
    out.translate_sites(range(len(out)), [0.0, 0.0, -z_min], frac_coords=False, to_unit_cell=False)
    return out, {
        "op": "reconstruct_surface",
        "reconstruction": reconstruction,
        "miller": [int(x) for x in slab.miller_index],
        "n_terminations": len(slabs),
        "termination": termination,
        "n_sites": len(out),
        "orientation": "standard: a,b in xy plane, c perpendicular along z, bottom-aligned",
        "available_reconstructions": names,
    }


# ------------------------------------------------------- stacking fault / twin
def make_stacking_fault(
    structure: Structure,
    shift: list[float],
    z_cut: float = 0.5,
) -> tuple[Structure, dict]:
    """Generalized stacking fault (BACKENDS §17): every atom with fractional
    z > `z_cut` is shifted by in-plane fractional `shift`=[fa, fb] (wrapped).
    Apply to a slab for a single fault, scan `shift` for a γ-surface."""
    if len(shift) != 2:
        raise ValueError("shift must be [fa, fb] in-plane fractional components")
    z_cut = float(z_cut)
    if not 0.0 < z_cut < 1.0:
        raise ValueError("z_cut must be inside (0, 1)")
    new = structure.copy()
    moved = [i for i, s in enumerate(new) if s.frac_coords[2] > z_cut]
    if not moved or len(moved) == len(new):
        raise ValueError(
            f"z_cut={z_cut} splits nothing ({len(moved)}/{len(new)} atoms above); "
            "choose a cut between two layers"
        )
    frac = np.array(new.frac_coords)
    frac[moved, 0] += float(shift[0])
    frac[moved, 1] += float(shift[1])
    frac[:, :2] %= 1.0
    result = Structure(new.lattice, [str(s.specie) for s in new], frac)
    return result, {
        "op": "make_stacking_fault",
        "shift": [round(float(shift[0]), 6), round(float(shift[1]), 6)],
        "z_cut": z_cut,
        "n_shifted": len(moved),
        "n_sites": len(result),
    }


def make_grain_boundary(
    bulk: Structure,
    rotation_axis: list[int],
    rotation_angle: float,
    plane: list[int] | None = None,
    expand_times: int = 2,
    vacuum_thickness: float = 0.0,
    ab_shift: list[float] | None = None,
    rm_ratio: float = 0.0,
) -> tuple[Structure, dict]:
    """Coincidence-site-lattice grain boundary / twin via pymatgen
    GrainBoundaryGenerator (BACKENDS §17). fcc coherent twin = Σ3:
    rotation_axis=[1,1,1], rotation_angle=60, plane=[1,1,1]."""
    from pymatgen.core.interface import GrainBoundaryGenerator

    gen = GrainBoundaryGenerator(bulk)
    gb = gen.gb_from_parameters(
        rotation_axis=[int(x) for x in rotation_axis],
        rotation_angle=float(rotation_angle),
        expand_times=int(expand_times),
        vacuum_thickness=float(vacuum_thickness),
        ab_shift=[float(ab_shift[0]), float(ab_shift[1])] if ab_shift else [0.0, 0.0],
        plane=[int(x) for x in plane] if plane else None,
        rm_ratio=float(rm_ratio),
    )
    frac = np.array(gb.frac_coords) % 1.0
    frac[frac >= 1.0] = 0.0
    result = Structure(gb.lattice, [str(s.specie) for s in gb], frac)
    return result, {
        "op": "make_grain_boundary",
        "sigma": int(gb.sigma),
        "rotation_axis": [int(x) for x in rotation_axis],
        "rotation_angle": float(rotation_angle),
        "plane": [int(x) for x in plane] if plane else None,
        "expand_times": int(expand_times),
        "vacuum_thickness": float(vacuum_thickness),
        "n_sites": len(result),
        "is_twin": abs(int(gb.sigma)) == 3,
    }


# ------------------------------------------------------------- passivation


def passivate_surface(
    structure: Structure,
    passivant: str = "H",
    tolerance: float = 1.30,
    which: str = "all",
    expected_coordination: dict | None = None,
) -> tuple[Structure, dict]:
    """Saturate dangling bonds with a monoatomic passivant (BACKENDS §18).

    Bonds are detected with ASE NeighborList over Cordero covalent radii x
    `tolerance` — the same criterion as the 3D viewer's rebond, so what gets
    saturated is what the user sees as missing bonds. Expected coordination
    per element defaults to the modal (most common) coordination of that
    element in the structure (interior atoms dominate in slabs thicker than
    ~4 layers); override with `expected_coordination={"Si": 4, ...}` for thin
    slabs. `which` limits passivation to the "top" / "bottom" face (by the
    z midpoint of the undercoordinated sites) or "all" (default: both faces
    and any internal undercoordinated site, e.g. around a vacancy).

    Placement (own logic, deterministic): 1 missing bond -> opposite of the
    existing-bond-vector sum; 2 missing -> the completion pair
    s/2 +- (sqrt(4-|s|^2)/2) * e_hat (s = -sum of existing unit bond vectors,
    e_hat along the cross product of two neighbor directions; exact for
    tetrahedral dihydrides like Si(001) SiH2). Sites with no neighbors, >= 3
    missing bonds, or degenerate geometry are reported unpassivated — the
    tool never guesses. Passivant-passivant/host overlaps < 0.7 x covalent
    cutoff are dropped and reported.
    """
    from ase.data import atomic_numbers, covalent_radii
    from ase.neighborlist import NeighborList, natural_cutoffs
    from pymatgen.io.ase import AseAtomsAdaptor

    if which not in ("all", "top", "bottom"):
        raise ValueError('which must be "all", "top" or "bottom"')
    if passivant not in atomic_numbers:
        raise ValueError(f"unknown passivant element {passivant!r}")

    atoms = AseAtomsAdaptor.get_atoms(structure)
    # natural_cutoffs(mult=m) scales each covalent radius by m; the pair
    # cutoff is r_i*m + r_j*m = (r_i + r_j)*m — the viewer's sum-of-radii
    # x tolerance criterion.
    nl = NeighborList(natural_cutoffs(atoms, mult=float(tolerance)),
                      skin=0.0, self_interaction=False, bothways=True)
    nl.update(atoms)

    n = len(atoms)
    cell = atoms.get_cell()
    bond_vecs: list[np.ndarray] = []
    cns = np.zeros(n, dtype=int)
    for i in range(n):
        idx, offsets = nl.get_neighbors(i)
        vecs = (atoms.positions[idx] + offsets @ cell) - atoms.positions[i]
        bond_vecs.append(vecs)
        cns[i] = len(idx)

    symbols = atoms.get_chemical_symbols()
    expected = {}
    for el in set(symbols):
        if expected_coordination and el in expected_coordination:
            expected[el] = int(expected_coordination[el])
        else:
            vals = [cns[i] for i in range(n) if symbols[i] == el]
            expected[el] = int(np.bincount(vals).argmax())  # modal CN

    under = [i for i in range(n) if cns[i] < expected[symbols[i]]]
    if which in ("top", "bottom") and under:
        z_mid = float(np.mean([atoms.positions[i][2] for i in under]))
        key = (lambda z: z >= z_mid) if which == "top" else (lambda z: z < z_mid)
        under = [i for i in under if key(atoms.positions[i][2])]

    r_pass = covalent_radii[atomic_numbers[passivant]]
    added: list[np.ndarray] = []
    report, skipped = [], []
    for i in under:
        el = symbols[i]
        n_missing = expected[el] - cns[i]
        vecs = bond_vecs[i]
        entry = {"site_index": i, "element": el, "coordination": int(cns[i]),
                 "expected": expected[el], "n_missing": int(n_missing)}
        if len(vecs) == 0:
            skipped.append({**entry, "reason": "no existing neighbors"})
            continue
        units = vecs / np.linalg.norm(vecs, axis=1)[:, None]
        s = -units.sum(axis=0)
        dirs: list[np.ndarray] = []
        if n_missing == 1:
            if np.linalg.norm(s) < 1e-3:
                skipped.append({**entry, "reason": "degenerate geometry (bond vectors cancel)"})
                continue
            dirs = [s / np.linalg.norm(s)]
            entry["method"] = "anti-bond-sum"
        elif n_missing == 2 and len(units) >= 2:
            e = np.cross(units[0], units[1])
            if np.linalg.norm(e) < 1e-3:
                skipped.append({**entry, "reason": "degenerate geometry (collinear neighbors)"})
                continue
            e /= np.linalg.norm(e)
            s_len2 = float(s @ s)
            if s_len2 >= 4.0:
                skipped.append({**entry, "reason": "degenerate geometry (|s| >= 2)"})
                continue
            half = np.sqrt(4.0 - s_len2) / 2.0
            dirs = [s / 2.0 + half * e, s / 2.0 - half * e]
            dirs = [d / np.linalg.norm(d) for d in dirs]
            entry["method"] = "tetrahedral completion pair"
        else:
            skipped.append({**entry, "reason": f"{n_missing} missing bonds with "
                            f"{len(units)} neighbors — ambiguous, not guessed"})
            continue
        d_xh = covalent_radii[atomic_numbers[el]] + r_pass
        placed = []
        for d in dirs:
            pos = atoms.positions[i] + d_xh * d
            clash = False
            for j in range(n):
                rj = np.linalg.norm(atoms.positions[j] - pos)
                lim = 0.7 * (covalent_radii[atomic_numbers[symbols[j]]] + r_pass)
                if rj < lim:
                    clash = True
                    break
            if not clash:
                for q in added:
                    if np.linalg.norm(q - pos) < 0.7 * 2 * r_pass:
                        clash = True
                        break
            if clash:
                skipped.append({**entry, "reason": "placement collides with existing atom"})
                continue
            added.append(pos)
            placed.append([round(float(x), 4) for x in pos])
        if placed:
            entry["passivant_positions"] = placed
            report.append(entry)

    all_species = [str(s.specie) for s in structure] + [passivant] * len(added)
    all_cart = np.vstack([np.array([s.coords for s in structure])] +
                         ([np.array(added)] if added else []))
    # Bottom-face passivants land below z=0; keep the slab bottom-aligned and
    # everything inside the cell with one rigid upward shift (vacuum is above).
    z_shift = float(max(0.0, -all_cart[:, 2].min()))
    all_cart[:, 2] += z_shift
    frac = structure.lattice.get_fractional_coords(all_cart)
    frac[:, :2] %= 1.0  # in-plane wrap; z stays contiguous (slab convention)
    new = Structure(structure.lattice, all_species, frac)
    # carry frozen-bottom flags through the rebuild; passivants are free
    # (a PARTIAL selective_dynamics property crashes the POSCAR writer)
    base_sd = structure.site_properties.get("selective_dynamics")
    if base_sd is not None:
        new.add_site_property("selective_dynamics",
                              [list(x) for x in base_sd]
                              + [[True] * 3] * (len(new) - len(structure)))
    return new, {
        "op": "passivate_surface",
        "passivant": passivant,
        "tolerance": float(tolerance),
        "which": which,
        "expected_coordination": expected,
        "n_undercoordinated": len(under),
        "n_passivated_sites": len(report),
        "n_passivant_added": len(added),
        "z_shift": round(z_shift, 4),
        "passivated": report,
        "unpassivated": skipped,
        "n_sites_after": len(new),
    }


# ------------------------------------------------------ solvation / stacking
AVOGADRO_PER_A3 = 0.602214076  # N = rho[g/cm^3] * V[A^3] * this / M[g/mol]


def _random_rotation(rng: np.random.Generator) -> np.ndarray:
    """Uniform random rotation matrix from a normalized quaternion drawn from
    `rng` (Shoemake/Marsaglia via 4 gaussians) — deterministic given the rng."""
    q = rng.normal(size=4)
    q /= np.linalg.norm(q)
    w, x, y, z = q
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
        [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
        [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
    ])


def add_solvent(
    structure: Structure,
    molecule: str = "H2O",
    count: int | None = None,
    density: float | None = None,
    z_range: list[float] | None = None,
    gap: float = 2.5,
    top_margin: float = 2.0,
    min_dist: float = 2.0,
    seed: int = 42,
    max_tries: int = 5000,
) -> tuple[Structure, dict]:
    """Pack rigid solvent molecules into a z-region of the cell (BACKENDS §21)
    — the liquid half of a solid–liquid interface model.

    Molecules (§13 g2 source, e.g. "H2O", or a file path) are inserted one at
    a time at rng-drawn positions and orientations (seeded → deterministic);
    a candidate is accepted only if every atom stays inside [z_lo, z_hi] and
    no distance to slab atoms or previously placed molecules falls below
    `min_dist` (PBC min-image, so contacts across the cell boundary count).

    Region: `z_range=[z_lo, z_hi]` in Å, or by default from `max_z + gap` up
    to `c - top_margin`. Amount: `count`, or `density` in g/cm³ (N = ρ·V·N_A/M
    over the region volume); neither given → density 1.0 (liquid water).
    The result is a STARTING configuration — equilibrate with AIMD/relaxation
    before extracting physics.
    """
    if count is not None and density is not None:
        raise ValueError("give count OR density, not both")
    if not len(structure):
        raise ValueError("empty structure")
    mol, source = _load_adsorbate(molecule)
    lat = structure.lattice
    c_len = float(lat.c)
    if z_range is not None:
        if len(z_range) != 2:
            raise ValueError("z_range must be [z_lo, z_hi] in Å")
        z_lo, z_hi = (float(v) for v in z_range)
        if not 0.0 <= z_lo < z_hi <= c_len:
            raise ValueError(f"z_range must satisfy 0 <= z_lo < z_hi <= c ({c_len:.2f} Å)")
    else:
        z_lo = float(max(s.coords[2] for s in structure)) + float(gap)
        z_hi = c_len - float(top_margin)
    if z_hi - z_lo < 2.0:
        raise ValueError(
            f"solvent region [{z_lo:.2f}, {z_hi:.2f}] Å is only {z_hi - z_lo:.2f} Å "
            "thick — open more room first (add_vacuum, or stack with vacuum_above)")

    # region volume: in-plane cell area x region thickness
    a_vec, b_vec = np.array(lat.matrix[0]), np.array(lat.matrix[1])
    area = float(np.linalg.norm(np.cross(a_vec, b_vec)))
    volume = area * (z_hi - z_lo)
    molar_mass = float(mol.composition.weight)
    if count is None:
        rho = 1.0 if density is None else float(density)
        count = int(round(rho * volume * AVOGADRO_PER_A3 / molar_mass))
        if count < 1:
            raise ValueError(
                f"density {rho} g/cm³ over a {volume:.1f} Å³ region rounds to 0 "
                f"molecules of {mol.composition.reduced_formula}")
    count = int(count)
    if count < 1:
        raise ValueError("count must be >= 1")

    rng = np.random.default_rng(int(seed))
    mol_coords = np.array([s.coords for s in mol], dtype=float)
    mol_coords -= mol_coords.mean(axis=0)  # centroid at origin
    mol_species = [str(s.specie) for s in mol]

    existing_frac = [np.array(structure.frac_coords)]
    placed_cart: list[np.ndarray] = []
    total_tries = 0
    for i in range(count):
        for _ in range(int(max_tries)):
            total_tries += 1
            cart = mol_coords @ _random_rotation(rng).T
            fa, fb = rng.random(2)
            zc = z_lo + (z_hi - z_lo) * rng.random()
            origin = fa * a_vec + fb * b_vec
            cand = cart + np.array([origin[0], origin[1], zc])
            if cand[:, 2].min() < z_lo or cand[:, 2].max() > z_hi:
                continue
            frac = lat.get_fractional_coords(cand)
            frac[:, :2] %= 1.0
            d = lat.get_all_distances(frac, np.vstack(existing_frac))
            if float(d.min()) >= float(min_dist):
                existing_frac.append(frac)
                placed_cart.append(cand)
                break
        else:
            raise ValueError(
                f"placed only {i}/{count} {mol.composition.reduced_formula} after "
                f"{max_tries} tries each — the region is too crowded; lower the "
                f"density/count or min_dist ({min_dist} Å), or widen the z region")

    all_species = [str(s.specie) for s in structure] + mol_species * count
    all_frac = np.vstack(existing_frac)
    new = Structure(lat, all_species, all_frac)
    base_sd = structure.site_properties.get("selective_dynamics")
    if base_sd is not None:
        new.add_site_property("selective_dynamics",
                              [list(x) for x in base_sd]
                              + [[True] * 3] * (len(mol_species) * count))
    achieved_rho = count * molar_mass / (volume * AVOGADRO_PER_A3)
    return new, {
        "op": "add_solvent",
        "molecule": source,
        "molecule_formula": mol.composition.reduced_formula,
        "n_molecules": count,
        "n_atoms_added": len(mol_species) * count,
        "z_region_A": [round(float(z_lo), 3), round(float(z_hi), 3)],
        "region_volume_A3": round(float(volume), 2),
        "density_g_cm3": round(float(achieved_rho), 4),
        "min_dist": float(min_dist),
        "seed": int(seed),
        "insertion_tries": total_tries,
        "n_sites_after": len(new),
        "note": ("random packing is a STARTING configuration — equilibrate "
                 "(AIMD / relaxation) before extracting physics"),
    }


def stack_structures(
    base: Structure,
    addon: Structure,
    gap: float = 2.0,
    vacuum_above: float | None = None,
) -> tuple[Structure, dict]:
    """Stack `addon` above `base` along z in the BASE cell (BACKENDS §22):
    the generic composition primitive make_interface (crystal↔crystal ZSL)
    cannot cover — e.g. a pre-equilibrated water box onto a slab.

    Addon fractional coords are re-expressed in the base lattice, so the two
    in-plane metrics must agree within 2 % / 2° (otherwise rebuild the addon
    in the base cell). The addon's bottom lands at `max_z(base) + gap`.
    `vacuum_above=None` keeps the base cell; a value re-sizes c to
    `addon_top + vacuum_above`. Base selective_dynamics flags are carried;
    addon atoms relax free (T T T)."""
    if not len(base) or not len(addon):
        raise ValueError("base and addon must both be non-empty")
    bm = _metric2d(base.lattice.matrix[:2])
    am = _metric2d(addon.lattice.matrix[:2])
    if (abs(am[0] - bm[0]) / bm[0] > 0.02 or abs(am[1] - bm[1]) / bm[1] > 0.02
            or abs(am[2] - bm[2]) > 2.0):
        raise ValueError(
            f"in-plane lattices differ beyond 2 %/2°: base (a={bm[0]:.3f}, "
            f"b={bm[1]:.3f}, γ={bm[2]:.1f}°) vs addon (a={am[0]:.3f}, "
            f"b={am[1]:.3f}, γ={am[2]:.1f}°) — rebuild the addon in the base cell")

    base_top = max(s.coords[2] for s in base)
    # addon geometry in the base in-plane metric: reuse fractional a/b, keep
    # cartesian z shape (liquids/amorphous have no c-periodicity to preserve)
    addon_frac = np.array(addon.frac_coords)
    addon_z = np.array([s.coords[2] for s in addon], dtype=float)
    inplane = addon_frac[:, :2] @ np.array(base.lattice.matrix[:2])[:, :]
    addon_cart = np.column_stack([
        inplane[:, 0], inplane[:, 1],
        addon_z - addon_z.min() + base_top + float(gap)])
    addon_top = float(addon_cart[:, 2].max())

    lattice = np.array(base.lattice.matrix, dtype=float)
    if vacuum_above is not None:
        new_c = addon_top + float(vacuum_above)
        lattice[2] = [0.0, 0.0, new_c] if abs(lattice[2][0]) < 1e-9 and \
            abs(lattice[2][1]) < 1e-9 else lattice[2] * (new_c / float(base.lattice.c))
    elif addon_top >= float(base.lattice.c) - 1.0:
        raise ValueError(
            f"addon reaches {addon_top:.2f} Å but the base cell ends at "
            f"{base.lattice.c:.2f} Å — pass vacuum_above to grow c, or "
            "add_vacuum on the base first")

    from pymatgen.core import Lattice

    all_species = [str(s.specie) for s in base] + [str(s.specie) for s in addon]
    all_cart = np.vstack([np.array([s.coords for s in base]), addon_cart])
    new_lat = Lattice(lattice)
    frac = new_lat.get_fractional_coords(all_cart)
    frac[:, :2] %= 1.0
    new = Structure(new_lat, all_species, frac)
    base_sd = base.site_properties.get("selective_dynamics")
    if base_sd is not None:
        new.add_site_property("selective_dynamics",
                              [list(x) for x in base_sd] + [[True] * 3] * len(addon))
    return new, {
        "op": "stack_structures",
        "n_base_atoms": len(base),
        "n_addon_atoms": len(addon),
        "gap": float(gap),
        "interface_z_A": round(float(base_top) + float(gap), 3),
        "addon_top_A": round(addon_top, 3),
        "c_after": round(float(new.lattice.c), 4),
        "vacuum_above": None if vacuum_above is None else float(vacuum_above),
        "n_sites_after": len(new),
    }


# ------------------------------------------------------- nano track (N1/N2)
def _require_2d_layer(structure: Structure, min_vacuum: float = 5.0) -> float:
    """Guard for the nano primitives (BACKENDS §23–24): the input must be a 2D
    layer in the standard frame — a, b in the xy plane, c along z, vacuum
    along c. Returns the c length."""
    m = structure.lattice.matrix
    if abs(m[0][2]) > 0.05 or abs(m[1][2]) > 0.05:
        raise ValueError("input is not in the standard 2D frame: a/b must lie "
                         "in the xy plane (z components < 0.05 Å)")
    if abs(m[2][0]) > 0.05 or abs(m[2][1]) > 0.05:
        raise ValueError("input is not in the standard 2D frame: c must be "
                         "orthogonal to the layer (along z)")
    z = [s.coords[2] for s in structure]
    vacuum = float(structure.lattice.c) - (max(z) - min(z))
    if vacuum < min_vacuum:
        raise ValueError(
            f"input does not look like a 2D layer: vacuum along c is "
            f"{vacuum:.2f} Å (< {min_vacuum} Å) — build/convert a monolayer "
            "first (e.g. generate_slab + add_vacuum)")
    return float(structure.lattice.c)


def _hexagonal_edge_direction(structure: Structure, edge: str) -> list[int]:
    """Map edge=zigzag/armchair to a lattice direction — hexagonal cells only
    (a ≈ b, γ ≈ 60° or 120°). zigzag runs along a lattice vector; armchair
    along the in-plane diagonal whose length is √3·a."""
    a, b = structure.lattice.a, structure.lattice.b
    gamma = structure.lattice.gamma
    if abs(a - b) > 0.01 * a or not (abs(gamma - 60.0) < 1.5 or abs(gamma - 120.0) < 1.5):
        raise ValueError(
            f"edge={edge!r} needs a hexagonal cell (a≈b, γ≈60°/120°); this cell "
            f"has a={a:.3f}, b={b:.3f}, γ={gamma:.1f}° — pass direction=[u,v] "
            "explicitly instead")
    if edge == "zigzag":
        return [1, 0]
    if edge == "armchair":
        return [1, 1] if abs(gamma - 60.0) < 1.5 else [1, -1]
    raise ValueError('edge must be "zigzag" or "armchair"')


def make_nanoribbon(
    structure: Structure,
    direction: list[int] | None = None,
    edge: str | None = None,
    width: int = 4,
    vacuum_transverse: float = 15.0,
) -> tuple[Structure, dict]:
    """Cut a nanoribbon from a 2D layer (BACKENDS §23): periodic along the
    lattice direction `direction`=[u,v] (or `edge`=zigzag/armchair for
    hexagonal cells), `width` transverse repeats, vacuum in the two aperiodic
    directions. Own re-basis construction: [u,v] reduced by gcd, completed to
    a unimodular pair via the extended Euclid identity u·q − v·p = 1, supercell
    [[u,v,0],[w·p,w·q,0],[0,0,1]] (pymatgen core), then rotated to a
    rectangular frame (ribbon axis ∥ x) with the transverse vacuum added.
    Edge saturation is NOT done here — compose with passivate_surface (§18).
    """
    from math import gcd

    c_len = _require_2d_layer(structure)
    if (direction is None) == (edge is None):
        raise ValueError("pass exactly one of direction=[u,v] or edge=zigzag/armchair")
    if edge is not None:
        direction = _hexagonal_edge_direction(structure, edge)
    u, v = (int(x) for x in direction)
    if u == 0 and v == 0:
        raise ValueError("direction must be a nonzero [u, v]")
    g = gcd(abs(u), abs(v))
    u, v = u // g, v // g
    width = int(width)
    if width < 1:
        raise ValueError("width must be >= 1 transverse repeat")

    # unimodular completion: q, p with u·q − v·p = 1 (extended Euclid)
    old_r, r = u, v
    old_s, s = 1, 0
    old_t, t = 0, 1
    while r != 0:
        quot = old_r // r
        old_r, r = r, old_r - quot * r
        old_s, s = s, old_s - quot * s
        old_t, t = t, old_t - quot * t
    # old_r = ±1 = u·old_s + v·old_t → q = old_s·sign, p = −old_t·sign
    sign = 1 if old_r > 0 else -1
    q, p = sign * old_s, -sign * old_t

    m = np.array([[u, v, 0], [width * p, width * q, 0], [0, 0, 1]], dtype=int)
    sup = structure.make_supercell(m, in_place=False)

    lat = np.array(sup.lattice.matrix, dtype=float)
    a_vec = lat[0]
    la = float(np.linalg.norm(a_vec))
    x_hat = a_vec / la
    y_hat = np.cross([0.0, 0.0, 1.0], x_hat)
    if float(lat[1] @ y_hat) < 0:
        y_hat = -y_hat

    cart = np.array([s_.coords for s_ in sup], dtype=float)
    x = cart @ x_hat
    y = cart @ y_hat
    z = cart[:, 2]
    y_span = float(y.max() - y.min())
    lb = y_span + float(vacuum_transverse)
    new_lat = [[la, 0.0, 0.0], [0.0, lb, 0.0], [0.0, 0.0, c_len]]
    pos = np.column_stack([x % la, y - y.min() + float(vacuum_transverse) / 2.0, z])
    new = Structure(new_lat, [str(s_.specie) for s_ in sup], pos,
                    coords_are_cartesian=True)
    for prop, values in sup.site_properties.items():
        new.add_site_property(prop, list(values))
    return new, {
        "op": "make_nanoribbon",
        "direction": [u, v],
        "edge": edge,
        "width_repeats": width,
        "period_A": round(la, 4),
        "ribbon_width_A": round(y_span, 4),
        "vacuum_transverse": float(vacuum_transverse),
        "n_sites_before": len(structure),
        "n_sites_after": len(new),
        "note": "edges are unsaturated — use passivate_surface to H-terminate",
    }


def make_nanotube(
    structure: Structure,
    n: int,
    m: int,
    repeat: int = 1,
    vacuum: float = 15.0,
    max_search: int = 20,
    max_atoms: int = 4000,
) -> tuple[Structure, dict]:
    """Roll a 2D layer into a nanotube by chiral indices (n, m) (BACKENDS §24).
    Chiral vector C = n·a1 + m·a2 becomes the circumference (R = |C|/2π); the
    tube axis is the shortest in-plane lattice vector T = p·a1 + q·a2 with
    T·C = 0, found by integer search — lattices with no commensurate T within
    `max_search` are REFUSED, never approximated. Finite-thickness layers
    (e.g. TMD S–Mo–S) map z-offsets to radial offsets ρ = R + (z − z_center).
    The geometry is the ideal rolled construction: relax with Anneal before
    extracting physics; below R ≈ 3 Å curvature makes it a rough start."""
    _require_2d_layer(structure)
    n, m = int(n), int(m)
    if n == 0 and m == 0:
        raise ValueError("chirality (n, m) must be nonzero")
    if n < 0 or (n == 0 and m < 0):
        n, m = -n, -m  # (n,m) and (−n,−m) are the same tube
    repeat = int(repeat)
    if repeat < 1:
        raise ValueError("repeat must be >= 1")

    lat2 = np.array(structure.lattice.matrix, dtype=float)[:2, :2]
    c_cart = np.array([n, m]) @ lat2
    c_norm = float(np.linalg.norm(c_cart))
    radius = c_norm / (2.0 * np.pi)

    # shortest commensurate translation vector T ⟂ C (integer search)
    best = None
    for pp in range(-max_search, max_search + 1):
        for qq in range(-max_search, max_search + 1):
            if pp == 0 and qq == 0:
                continue
            t_cart = np.array([pp, qq]) @ lat2
            t_norm = float(np.linalg.norm(t_cart))
            if abs(float(t_cart @ c_cart)) > 1e-6 * c_norm * t_norm:
                continue
            key = (round(t_norm, 8), abs(pp) + abs(qq), pp, qq)
            if best is None or key < best[0]:
                best = (key, pp, qq, t_norm)
    if best is None:
        raise ValueError(
            f"no commensurate translation vector T ⟂ C within ±{max_search}: "
            "this lattice/chirality does not admit a periodic tube — "
            "not approximating")
    _, p, q, t_norm = best
    det = n * q - m * p
    if det < 0:
        p, q, det = -p, -q, -det

    n_atoms = det * repeat * len(structure)
    if n_atoms > max_atoms:
        raise ValueError(
            f"tube would have {n_atoms} atoms (> max_atoms={max_atoms}) — "
            "reduce (n, m)/repeat or raise max_atoms explicitly")

    sup = structure.make_supercell(
        np.array([[n, m, 0], [repeat * p, repeat * q, 0], [0, 0, 1]], dtype=int),
        in_place=False)
    frac = np.array(sup.frac_coords, dtype=float)
    z = np.array([s_.coords[2] for s_ in sup], dtype=float)
    z_center = (float(z.max()) + float(z.min())) / 2.0
    rho = radius + (z - z_center)
    if float(rho.min()) < 0.5:
        raise ValueError(
            f"layer thickness does not fit the tube: inner radius would be "
            f"{float(rho.min()):.2f} Å — increase (n, m)")

    theta = 2.0 * np.pi * (frac[:, 0] % 1.0)
    axial = (frac[:, 1] % 1.0) * (repeat * t_norm)
    box = 2.0 * float(rho.max()) + float(vacuum)
    pos = np.column_stack([rho * np.cos(theta) + box / 2.0,
                           rho * np.sin(theta) + box / 2.0,
                           axial])
    new = Structure([[box, 0.0, 0.0], [0.0, box, 0.0], [0.0, 0.0, repeat * t_norm]],
                    [str(s_.specie) for s_ in sup], pos, coords_are_cartesian=True)
    for prop, values in sup.site_properties.items():
        new.add_site_property(prop, list(values))
    summary = {
        "op": "make_nanotube",
        "chirality": [n, m],
        "radius_A": round(radius, 4),
        "diameter_A": round(2 * radius, 4),
        "translation_indices": [p, q],
        "period_A": round(t_norm, 4),
        "repeat": repeat,
        "n_sites_after": len(new),
        "box_A": round(box, 4),
        "note": "ideal rolled geometry — relax before extracting physics",
    }
    if radius < 3.0:
        summary["warning"] = (f"small radius ({radius:.2f} Å): strong curvature, "
                              "the rolled-geometry approximation is rough")
    return new, summary


# ------------------------------------------------------- nano track (N3/N4)
def _hexagonal_60_frame(structure: Structure) -> Structure:
    """Re-express a hexagonal 2D layer in the γ=60° convention (a ≈ b required).
    γ=120° cells are re-based via the unimodular a2' = a1 + a2 (|a2'| = a,
    ∠(a1, a2') = 60°, det = 1 — same atoms, same lattice); anything
    non-hexagonal is refused (the closed-form twist arithmetic below is
    hexagonal-only; the general-lattice case goes through the ZSL machinery
    of make_interface, not this op)."""
    a, b = structure.lattice.a, structure.lattice.b
    gamma = structure.lattice.gamma
    if abs(a - b) > 0.01 * a or not (abs(gamma - 60.0) < 1.5 or abs(gamma - 120.0) < 1.5):
        raise ValueError(
            f"twist/stacking registries need a hexagonal cell (a≈b, γ≈60°/120°); "
            f"this cell has a={a:.3f}, b={b:.3f}, γ={gamma:.1f}°")
    if abs(gamma - 120.0) < 1.5:
        return structure.make_supercell([[1, 0, 0], [1, 1, 0], [0, 0, 1]],
                                        in_place=False)
    return structure


def _normalize_twist_pair(i: int, j: int) -> tuple[int, int]:
    """Canonical (i, j): positive, coprime, i < j. (j, i) is the mirror twist
    (−θ) of (i, j) — the same bilayer up to reflection — and a common factor
    only replicates the commensurate cell without changing the angle."""
    from math import gcd

    i, j = int(i), int(j)
    if i < 1 or j < 1:
        raise ValueError("twist indices (i, j) must be positive integers")
    if i == j:
        raise ValueError("i == j gives twist angle 0° — that is a plain "
                         "bilayer; use make_stacking instead")
    g = gcd(i, j)
    i, j = i // g, j // g
    if i > j:
        i, j = j, i
    return i, j


def _twist_angle_deg(i: int, j: int) -> float:
    """Closed-form commensurate twist angle for hexagonal lattices:
    cos θ = (i² + 4ij + j²) / (2(i² + ij + j²))."""
    num = i * i + 4 * i * j + j * j
    den = 2 * (i * i + i * j + j * j)
    return float(np.degrees(np.arccos(np.clip(num / den, -1.0, 1.0))))


def list_twist_pairs(
    structure: Structure,
    max_index: int = 12,
    max_atoms: int = 3000,
) -> dict:
    """Enumerate commensurate twist pairs (i, j) for a hexagonal 2D layer:
    per pair the twist angle, bilayer atom count and moiré lattice constant —
    the same "angle vs system size" trade-off survey as list_interface_matches.
    Row index k == `candidate=k`; rows are sorted by (n_atoms, angle)."""
    _require_2d_layer(structure)
    work = _hexagonal_60_frame(structure)
    max_index = int(max_index)
    if max_index < 2:
        raise ValueError("max_index must be >= 2")
    from math import gcd

    lat2 = np.array(work.lattice.matrix, dtype=float)[:2, :2]
    rows = []
    for i in range(1, max_index):
        for j in range(i + 1, max_index + 1):
            if gcd(i, j) != 1:
                continue
            cells = i * i + i * j + j * j
            n_atoms = 2 * cells * len(work)
            if n_atoms > max_atoms:
                continue
            moire_a = float(np.linalg.norm(np.array([i, j], dtype=float) @ lat2))
            rows.append({
                "i": i, "j": j,
                "angle_deg": round(_twist_angle_deg(i, j), 4),
                "n_atoms": n_atoms,
                "cells_per_layer": cells,
                "moire_a_A": round(moire_a, 4),
            })
    rows.sort(key=lambda r: (r["n_atoms"], r["angle_deg"]))
    for k, r in enumerate(rows):
        r["candidate"] = k
    return {
        "op": "list_twist_pairs",
        "formula": structure.composition.reduced_formula,
        "max_index": max_index,
        "max_atoms": int(max_atoms),
        "n_candidates": len(rows),
        "note": "candidate index maps 1:1 to (i, j) for make_twisted_bilayer",
        "candidates": rows,
    }


def make_twisted_bilayer(
    structure: Structure,
    i: int,
    j: int,
    gap: float = 3.35,
    vacuum: float = 15.0,
    slide: list[float] | None = None,
    max_atoms: int = 4000,
) -> tuple[Structure, dict]:
    """Commensurate twisted homobilayer of a hexagonal 2D layer (BACKENDS §25).
    The twist pair (i, j) fixes the angle via cos θ = (i²+4ij+j²)/(2(i²+ij+j²));
    layer 1 is the supercell [[i,j],[−j,i+j]] and layer 2 the congruent
    supercell [[j,i],[−i,i+j]] of the SAME layer — both span one moiré cell of
    identical metric, so transplanting layer 2's fractional coordinates into
    layer 1's lattice applies the twist as an exact rigid rotation
    (zero numerical strain by construction, no ZSL search needed).
    `slide`=[fa, fb] (primitive-cell fractional) shifts layer 2 in-plane to
    move the registry origin (default [0, 0]: AA-stacked at the origin).
    The geometry is the rigid ideal construction — relax with Anneal before
    extracting physics."""
    _require_2d_layer(structure)
    work = _hexagonal_60_frame(structure)
    i, j = _normalize_twist_pair(i, j)
    cells = i * i + i * j + j * j
    n_atoms = 2 * cells * len(work)
    if n_atoms > int(max_atoms):
        raise ValueError(
            f"bilayer would have {n_atoms} atoms (> max_atoms={max_atoms}) — "
            "pick a larger-angle pair or raise max_atoms explicitly")

    sup1 = work.make_supercell([[i, j, 0], [-j, i + j, 0], [0, 0, 1]],
                               in_place=False)
    sup2 = work.make_supercell([[j, i, 0], [-i, i + j, 0], [0, 0, 1]],
                               in_place=False)
    m1 = _metric2d(sup1.lattice.matrix[:2])
    m2 = _metric2d(sup2.lattice.matrix[:2])
    if (abs(m1[0] - m2[0]) > 1e-6 * m1[0] or abs(m1[1] - m2[1]) > 1e-6 * m1[1]
            or abs(m1[2] - m2[2]) > 1e-4):
        raise ValueError("internal error: the two supercell metrics differ — "
                         "twist construction invalid for this cell")
    # numerical twist angle: t1(layer1) = i·a1 + j·a2 vs t1(layer2) = j·a1 + i·a2
    lat2 = np.array(work.lattice.matrix, dtype=float)[:2, :2]
    t1a = np.array([i, j], dtype=float) @ lat2
    t1b = np.array([j, i], dtype=float) @ lat2
    cosang = float(t1a @ t1b) / (float(np.linalg.norm(t1a)) * float(np.linalg.norm(t1b)))
    angle = float(np.degrees(np.arccos(np.clip(cosang, -1.0, 1.0))))

    # assemble in layer-1's moiré lattice; z: layer 1 keeps its internal shape,
    # layer 2's bottom lands at layer-1 top + gap (stack_structures convention)
    lat_ab = np.array(sup1.lattice.matrix, dtype=float)[:2]
    z1 = np.array([s_.coords[2] for s_ in sup1], dtype=float)
    z2 = np.array([s_.coords[2] for s_ in sup2], dtype=float)
    xy1 = np.array(sup1.frac_coords, dtype=float)[:, :2] @ lat_ab
    xy2 = np.array(sup2.frac_coords, dtype=float)[:, :2] @ lat_ab
    if slide is not None:
        if len(slide) != 2:
            raise ValueError("slide must be [fa, fb] primitive-cell fractional")
        # slide is fractional in the INPUT cell's own basis (not the internal
        # γ=60° working frame), so [1/3, 2/3] etc. mean what the user's cell says
        in_rows = np.array(structure.lattice.matrix, dtype=float)[:2]
        xy2 = xy2 + np.array([float(slide[0]), float(slide[1])]) @ in_rows
    z1 = z1 - z1.min()
    z2 = z2 - z2.min() + z1.max() + float(gap)
    span = float(z2.max())
    c_len = span + float(vacuum)
    cart = np.vstack([
        np.column_stack([xy1[:, 0], xy1[:, 1], z1 + float(vacuum) / 2.0]),
        np.column_stack([xy2[:, 0], xy2[:, 1], z2 + float(vacuum) / 2.0]),
    ])
    species = [str(s_.specie) for s_ in sup1] + [str(s_.specie) for s_ in sup2]
    from pymatgen.core import Lattice

    new_lat = Lattice([[lat_ab[0][0], lat_ab[0][1], 0.0],
                       [lat_ab[1][0], lat_ab[1][1], 0.0],
                       [0.0, 0.0, c_len]])
    frac = new_lat.get_fractional_coords(cart)
    frac[:, :2] %= 1.0
    new = Structure(new_lat, species, frac)
    return new, {
        "op": "make_twisted_bilayer",
        "twist_pair": [i, j],
        "twist_angle_deg": round(angle, 4),
        "cells_per_layer": cells,
        "moire_a_A": round(m1[0], 4),
        "gap": float(gap),
        "vacuum": float(vacuum),
        "slide": None if slide is None else [round(float(slide[0]), 6),
                                             round(float(slide[1]), 6)],
        "n_sites_after": len(new),
        "note": "rigid ideal twist (zero built-in strain) — relax before "
                "extracting physics",
    }


def _twist_zsl_candidates(
    layer1: Structure,
    layer2: Structure,
    angle_deg: float,
    angle_tol: float,
    max_area: float,
    max_strain: float,
) -> list[dict]:
    """Shared enumerator for the general-lattice twist route (BACKENDS §25b).

    pymatgen's ZSL matches superlattice METRICS only — it is orientation-blind
    (rotating one lattice changes nothing), so "rotate then ZSL" is wrong.
    Instead: enumerate coincidence supercell pairs (T1, T2) on the UNROTATED
    bases; each pair implies a twist angle — the rotation part (closed-form
    2x2 polar decomposition) of the map between the matched reduced bases —
    and a residual metric mismatch (von Mises strain). Pairs whose map is
    improper (det < 0, a mirror pairing) are retried with the substrate basis
    rows swapped; if the swap breaks the metric tolerance the pair is only
    realizable as a FLIPPED bilayer and is dropped (documented limitation).
    Candidates are filtered to |θ − angle_deg| <= angle_tol and strain <=
    max_strain, deterministically sorted by (strain, atoms, |θ − target|)."""
    from pymatgen.analysis.elasticity.strain import Deformation
    from pymatgen.analysis.interfaces.zsl import ZSLGenerator

    v1 = np.array(layer1.lattice.matrix, dtype=float)[:2]
    v2 = np.array(layer2.lattice.matrix, dtype=float)[:2]
    cell_area1 = abs(float(np.linalg.det(v1[:, :2])))
    cell_area2 = abs(float(np.linalg.det(v2[:, :2])))
    gen = ZSLGenerator(max_area=float(max_area))
    seen: set = set()
    rows = []
    for m in gen(v2, v1, lowest=False):  # film = layer2, substrate = layer1
        F = np.array(m.film_sl_vectors, dtype=float)
        S = np.array(m.substrate_sl_vectors, dtype=float)
        key = tuple(np.round(np.concatenate([F.ravel(), S.ravel()]), 6))
        if key in seen:
            continue
        seen.add(key)
        if np.linalg.det(F[:, :2]) * np.linalg.det(S[:, :2]) < 0:
            # mirror pairing: swapping the substrate rows flips orientation;
            # only valid if the crossed lengths still match within tolerance
            S = S[::-1].copy()
            lf = [float(np.linalg.norm(F[0])), float(np.linalg.norm(F[1]))]
            ls = [float(np.linalg.norm(S[0])), float(np.linalg.norm(S[1]))]
            if abs(ls[0] / lf[0] - 1) > 0.03 or abs(ls[1] / lf[1] - 1) > 0.03:
                continue  # flipped-bilayer-only match — out of scope
        # canonicalize handedness: negating the second row of BOTH bases
        # preserves the map while making the target-frame transplant proper
        if np.linalg.det(S[:, :2]) < 0:
            F, S = F.copy(), S.copy()
            F[1] *= -1.0
            S[1] *= -1.0
        F2, S2 = F[:, :2], S[:, :2]
        M = S2.T @ np.linalg.inv(F2.T)  # vector map: M · f_i = s_i
        theta = float(np.degrees(np.arctan2(M[1, 0] - M[0, 1],
                                            M[0, 0] + M[1, 1])))
        theta = abs(theta)  # ±θ are mirror twins; canonical θ ∈ [0, 180]
        if abs(theta - float(angle_deg)) > float(angle_tol):
            continue
        F3 = np.eye(3)
        F3[:2, :2] = M
        vm = float(Deformation(F3).green_lagrange_strain.von_mises_strain)
        if vm > float(max_strain):
            continue
        # integer supercell matrices back out exactly (reduced vectors are
        # integer combinations by construction; verify defensively)
        C_s = S2 @ np.linalg.inv(v1[:, :2])
        C_f = F2 @ np.linalg.inv(v2[:, :2])
        if (np.abs(C_s - np.round(C_s)).max() > 1e-5
                or np.abs(C_f - np.round(C_f)).max() > 1e-5):
            continue
        C_s = np.round(C_s).astype(int)
        C_f = np.round(C_f).astype(int)
        cells1 = int(round(abs(np.linalg.det(S2)) / cell_area1))
        cells2 = int(round(abs(np.linalg.det(F2)) / cell_area2))
        # collapse symmetry-equivalent pairs (identical angle/strain/size/metric)
        ms = _metric2d(S[:2])
        phys_key = (round(theta, 4), round(vm, 6), cells1, cells2,
                    round(ms[0], 4), round(ms[1], 4), round(ms[2], 2))
        if phys_key in seen:
            continue
        seen.add(phys_key)
        rows.append({
            "angle_deg": round(theta, 4),
            "von_mises_strain_pct": round(vm * 100, 4),
            "n_atoms": cells1 * len(layer1) + cells2 * len(layer2),
            "cells_per_layer": [cells1, cells2],
            "area_A2": round(abs(float(np.linalg.det(S2))), 2),
            "_S": S, "_F": F, "_C_s": C_s, "_C_f": C_f,
        })
    rows.sort(key=lambda r: (r["von_mises_strain_pct"], r["n_atoms"],
                             round(abs(r["angle_deg"] - float(angle_deg)), 4),
                             r["area_A2"]))
    for k, r in enumerate(rows):
        r["candidate"] = k
    return rows


def list_twist_matches(
    structure: Structure,
    angle_deg: float,
    layer2: Structure | None = None,
    angle_tol: float = 0.5,
    max_area: float = 400.0,
    max_strain: float = 0.05,
    limit: int = 20,
) -> dict:
    """Survey approximate-coincidence twist supercells for ANY 2D lattice pair
    near a target angle (BACKENDS §25b): per candidate the realized angle,
    von Mises strain and atom count — the strain-vs-size trade-off of
    list_interface_matches, at a chosen twist. Row index k == `candidate=k`
    in make_twisted_bilayer_zsl (identical deterministic ordering).
    `layer2` selects a heterobilayer (e.g. graphene on h-BN); default is the
    same layer twisted on itself."""
    _require_2d_layer(structure)
    if layer2 is not None:
        _require_2d_layer(layer2)
    other = structure if layer2 is None else layer2
    rows = _twist_zsl_candidates(structure, other, angle_deg, angle_tol,
                                 max_area, max_strain)
    shown = []
    for r in rows[: max(1, int(limit))]:
        pub = {k: v for k, v in r.items() if not k.startswith("_")}
        m = _metric2d(r["_S"][:2])
        pub["in_plane_ab"] = [round(m[0], 4), round(m[1], 4)]
        pub["gamma_deg"] = round(m[2], 2)
        pub["angle_offset_deg"] = round(r["angle_deg"] - float(angle_deg), 4)
        shown.append(pub)
    return {
        "op": "list_twist_matches",
        "formula": structure.composition.reduced_formula,
        "layer2_formula": other.composition.reduced_formula,
        "target_angle_deg": float(angle_deg),
        "angle_tol": float(angle_tol),
        "max_area": float(max_area),
        "max_strain": float(max_strain),
        "n_candidates": len(rows),
        "shown": len(shown),
        "note": "candidate index maps 1:1 to make_twisted_bilayer_zsl"
                "(candidate=...); flipped (mirror-only) pairings are excluded",
        "candidates": shown,
    }


def make_twisted_bilayer_zsl(
    structure: Structure,
    angle_deg: float,
    layer2: Structure | None = None,
    candidate: int = 0,
    angle_tol: float = 0.5,
    max_area: float = 400.0,
    max_strain: float = 0.05,
    strain_method: str = "layer2",
    gap: float = 3.35,
    vacuum: float = 15.0,
    max_atoms: int = 4000,
) -> tuple[Structure, dict]:
    """General-lattice twisted bilayer via approximate coincidence supercells
    (BACKENDS §25b) — any 2D lattice, hetero pairs included. Unlike the
    hexagonal closed-form route (make_twisted_bilayer: exact, zero strain),
    the realized angle is the nearest coincidence angle within `angle_tol`
    and a small residual strain closes the cell (both reported).
    `strain_method` decides who absorbs it: "layer2" (default; layer1 keeps
    its natural metric), "layer1", or "average". Candidates are sorted
    deterministically by (strain, atoms, |θ − target|); survey them with
    list_twist_matches. Both layers are transplanted into the common cell in
    the standard orientation (a ∥ x). Rigid ideal geometry — relax (Anneal)
    before extracting physics."""
    if strain_method not in ("layer1", "layer2", "average"):
        raise ValueError('strain_method must be "layer1", "layer2" or "average"')
    _require_2d_layer(structure)
    if layer2 is not None:
        _require_2d_layer(layer2)
    other = structure if layer2 is None else layer2
    rows = _twist_zsl_candidates(structure, other, angle_deg, angle_tol,
                                 max_area, max_strain)
    if not rows:
        raise ValueError(
            f"no coincidence supercell within {angle_tol}° of {angle_deg}° "
            f"with von Mises strain <= {max_strain} at max_area={max_area} — "
            "raise angle_tol/max_strain/max_area (or use make_twisted_bilayer "
            "for exact hexagonal pairs)")
    if not 0 <= int(candidate) < len(rows):
        raise ValueError(f"candidate={candidate} out of range ({len(rows)} found)")
    r = rows[int(candidate)]
    if r["n_atoms"] > int(max_atoms):
        raise ValueError(
            f"bilayer would have {r['n_atoms']} atoms (> max_atoms={max_atoms}) "
            "— pick another candidate or raise max_atoms explicitly")

    S, F = r["_S"], r["_F"]
    sup1 = structure.make_supercell(
        [[r["_C_s"][0][0], r["_C_s"][0][1], 0],
         [r["_C_s"][1][0], r["_C_s"][1][1], 0], [0, 0, 1]], in_place=False)
    sup2 = other.make_supercell(
        [[r["_C_f"][0][0], r["_C_f"][0][1], 0],
         [r["_C_f"][1][0], r["_C_f"][1][1], 0], [0, 0, 1]], in_place=False)

    m_s = _metric2d(S[:2])
    m_f = _metric2d(F[:2])
    if strain_method == "layer2":
        tgt = m_s
    elif strain_method == "layer1":
        tgt = m_f
    else:
        tgt = tuple((a + b) / 2.0 for a, b in zip(m_s, m_f))
    rad = np.radians(tgt[2])
    tgt_rows = np.array([[tgt[0], 0.0, 0.0],
                         [tgt[1] * float(np.cos(rad)), tgt[1] * float(np.sin(rad)), 0.0]])

    def _transplant(sup: Structure, basis_rows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """In-plane cart positions in the target frame + original z shape.
        frac coords are computed against the MATCHED basis (basis_rows), not
        the supercell's own lattice rows, so the pairing correspondence (incl.
        the handedness fix) carries through exactly."""
        b3 = np.vstack([basis_rows, [0.0, 0.0, float(sup.lattice.c)]])
        frac = np.array([s_.coords for s_ in sup]) @ np.linalg.inv(b3)
        xy = frac[:, :2] @ tgt_rows
        z = np.array([s_.coords[2] for s_ in sup], dtype=float)
        return xy, z

    xy1, z1 = _transplant(sup1, S)
    xy2, z2 = _transplant(sup2, F)
    z1 = z1 - z1.min()
    z2 = z2 - z2.min() + z1.max() + float(gap)
    c_len = float(z2.max()) + float(vacuum)
    cart = np.vstack([
        np.column_stack([xy1[:, 0], xy1[:, 1], z1 + float(vacuum) / 2.0]),
        np.column_stack([xy2[:, 0], xy2[:, 1], z2 + float(vacuum) / 2.0]),
    ])
    from pymatgen.core import Lattice

    new_lat = Lattice(np.vstack([tgt_rows, [0.0, 0.0, c_len]]))
    frac = new_lat.get_fractional_coords(cart)
    frac[:, :2] %= 1.0
    species = [str(s_.specie) for s_ in sup1] + [str(s_.specie) for s_ in sup2]
    new = Structure(new_lat, species, frac)

    f1 = structure.composition.reduced_formula
    f2 = other.composition.reduced_formula
    key1, key2 = (f"{f1} (layer1)", f"{f2} (layer2)") if f1 == f2 else (f1, f2)
    return new, {
        "op": "make_twisted_bilayer_zsl",
        "target_angle_deg": float(angle_deg),
        "angle_deg": r["angle_deg"],
        "angle_offset_deg": round(r["angle_deg"] - float(angle_deg), 4),
        "candidate": int(candidate),
        "n_candidates": len(rows),
        "von_mises_strain_pct": r["von_mises_strain_pct"],
        "strain_method": strain_method,
        "strain_by_layer": {key1: _side_strain(m_s, tgt),
                            key2: _side_strain(m_f, tgt)},
        "cells_per_layer": r["cells_per_layer"],
        "in_plane_ab": [round(tgt[0], 4), round(tgt[1], 4)],
        "gamma_deg": round(tgt[2], 2),
        "gap": float(gap),
        "vacuum": float(vacuum),
        "n_sites_after": len(new),
        "note": "nearest-coincidence twist (residual strain reported) — relax "
                "before extracting physics; exact hexagonal pairs: "
                "make_twisted_bilayer",
    }


_NAMED_STACKINGS = ("AA", "AB", "ABC")


def _hollow_shift(structure: Structure) -> list[float]:
    """High-symmetry interlayer shift for hexagonal cells (the Bernal/bond
    vector): γ≈60° → [1/3, 1/3]; γ≈120° → [1/3, 2/3]."""
    a, b = structure.lattice.a, structure.lattice.b
    gamma = structure.lattice.gamma
    if abs(a - b) > 0.01 * a or not (abs(gamma - 60.0) < 1.5 or abs(gamma - 120.0) < 1.5):
        raise ValueError(
            f'named stackings need a hexagonal cell (a≈b, γ≈60°/120°); this '
            f'cell has a={a:.3f}, b={b:.3f}, γ={gamma:.1f}° — pass explicit '
            f'shifts=[[fa,fb], ...] instead')
    return [1 / 3, 1 / 3] if abs(gamma - 60.0) < 1.5 else [1 / 3, 2 / 3]


def make_stacking(
    structure: Structure,
    n_layers: int = 2,
    stacking: str | None = "AB",
    shifts: list[list[float]] | None = None,
    gap: float = 3.0,
    vacuum: float = 15.0,
) -> tuple[Structure, dict]:
    """Stack a 2D layer into an N-layer slab with a chosen registry
    (BACKENDS §26). Pass exactly one of:
      - `stacking`: "AA" (all layers eclipsed), "AB" (alternating hollow
        shift — Bernal for graphene), "ABC" (cumulative hollow shift —
        rhombohedral); hexagonal cells only;
      - `shifts`: explicit per-layer in-plane shifts [[fa, fb], ...] in
        fractional cell coordinates (one entry per layer, layer 1 usually
        [0, 0]) — any cell, arbitrary slip stackings.
    `gap` is the vertical spacing between adjacent layers' atomic extents
    (stack_structures §22 convention) — a STARTING GUESS to be relaxed, not a
    prediction. Total vacuum along c is exactly `vacuum`, split evenly."""
    _require_2d_layer(structure)
    n_layers = int(n_layers)
    if n_layers < 2:
        raise ValueError("n_layers must be >= 2 (a single layer is the input)")
    if (stacking is None) == (shifts is None):
        raise ValueError("pass exactly one of stacking=AA/AB/ABC or "
                         "shifts=[[fa,fb], ...]")
    if stacking is not None:
        if stacking not in _NAMED_STACKINGS:
            raise ValueError(f'stacking must be one of {"/".join(_NAMED_STACKINGS)} '
                             "(or pass explicit shifts)")
        if stacking == "AA":
            resolved = [[0.0, 0.0] for _ in range(n_layers)]
        else:
            s = _hollow_shift(structure)
            if stacking == "AB":
                resolved = [[0.0, 0.0] if k % 2 == 0 else list(s)
                            for k in range(n_layers)]
            else:  # ABC
                resolved = [[(k * s[0]) % 1.0, (k * s[1]) % 1.0]
                            for k in range(n_layers)]
    else:
        if len(shifts) != n_layers:
            raise ValueError(f"shifts must have one [fa, fb] entry per layer "
                             f"({n_layers} layers, got {len(shifts)})")
        resolved = [[float(x[0]), float(x[1])] for x in shifts]

    def _shifted(shift: list[float]) -> Structure:
        frac = np.array(structure.frac_coords, dtype=float)
        frac[:, 0] += shift[0]
        frac[:, 1] += shift[1]
        frac[:, :2] %= 1.0
        return Structure(structure.lattice,
                         [str(s_.specie) for s_ in structure], frac)

    acc = _shifted(resolved[0])
    for k in range(1, n_layers):
        # reuse the §22 primitive per added layer (identical cell → exact)
        acc, _ = stack_structures(acc, _shifted(resolved[k]), gap=float(gap),
                                  vacuum_above=float(vacuum))
    # re-center: total vacuum exactly `vacuum`, split evenly below and above
    z = np.array([s_.coords[2] for s_ in acc], dtype=float)
    span = float(z.max() - z.min())
    c_len = span + float(vacuum)
    cart = np.array([s_.coords for s_ in acc], dtype=float)
    cart[:, 2] = z - z.min() + float(vacuum) / 2.0
    from pymatgen.core import Lattice

    m = np.array(acc.lattice.matrix, dtype=float)
    new_lat = Lattice([m[0], m[1], [0.0, 0.0, c_len]])
    frac = new_lat.get_fractional_coords(cart)
    frac[:, :2] %= 1.0
    new = Structure(new_lat, [str(s_.specie) for s_ in acc], frac)
    return new, {
        "op": "make_stacking",
        "n_layers": n_layers,
        "stacking": stacking if stacking is not None else "custom",
        "shifts": [[round(x, 6) for x in s_] for s_ in resolved],
        "gap": float(gap),
        "vacuum": float(vacuum),
        "n_sites_after": len(new),
        "note": "rigid stacking; gap is a starting guess — relax before "
                "extracting physics",
    }


def interpolate_structures(
    s_start: Structure,
    s_end: Structure,
    n_images: int = 9,
    extend: float = 0.2,
) -> tuple[list[tuple[float, Structure]], dict]:
    """Linear geometry path between two structures of the SAME system
    (configuration-coordinate diagrams, step 301; also usable as NEB seeds).

    Same lattice, same species order required (two relaxed charge states of
    one defect satisfy this — VASP preserves atom order). Interpolation runs
    x = -extend … 1+extend (x=0 -> s_start, x=1 -> s_end); the extension
    beyond the endpoints is what lets a parabola fit see both sides of each
    minimum. PBC min-image handling comes from pymatgen
    Structure.interpolate (BACKENDS §29); the mass-weighted path length

        dQ = sqrt( sum_a m_a |R_end,a - R_start,a|^2 )   [amu^1/2 Å]

    is the configuration coordinate span (NKU radiation doc Eq. 16); each
    image sits at Q = x·dQ.
    """
    if len(s_start) != len(s_end):
        raise ValueError(f"atom counts differ: {len(s_start)} vs {len(s_end)}")
    for a, b in zip(s_start, s_end):
        if a.specie.symbol != b.specie.symbol:
            raise ValueError("species order differs between the endpoints — "
                             "interpolation needs identical atom ordering")
    if not np.allclose(s_start.lattice.matrix, s_end.lattice.matrix, atol=1e-4):
        raise ValueError("lattices differ — interpolate fixed-cell structures")
    if n_images < 3:
        raise ValueError("n_images must be >= 3")
    xs = [round(float(x), 6) for x in
          np.linspace(-extend, 1.0 + extend, n_images)]
    images = s_start.interpolate(s_end, nimages=xs, pbc=True,
                                 interpolate_lattices=False)
    # mass-weighted displacement via the SAME min-image convention
    d_frac = np.array(s_end.frac_coords) - np.array(s_start.frac_coords)
    d_frac -= np.round(d_frac)
    d_cart = s_start.lattice.get_cartesian_coords(d_frac)
    masses = np.array([site.specie.atomic_mass for site in s_start])
    dq = float(np.sqrt((masses * (d_cart ** 2).sum(axis=1)).sum()))
    top = np.argsort(masses * (d_cart ** 2).sum(axis=1))[::-1][:5]
    summary = {
        "op": "interpolate_structures",
        "n_images": n_images, "x_values": xs,
        "dq_amu_ang": round(dq, 4),
        "q_values_amu_ang": [round(x * dq, 4) for x in xs],
        "max_atom_displacement_ang": round(
            float(np.linalg.norm(d_cart, axis=1).max()), 4),
        "top_moving_atoms": [int(i) for i in top],
    }
    return list(zip(xs, images)), summary
