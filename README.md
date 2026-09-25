# MASON — Materials Atomistic Structure builder Operated in Natural language

MASON is a toolkit of 39 deterministic structure-building tools for first-principles
calculations, exposed to a language-model agent through the Model Context Protocol (MCP).
You describe the structure you need in natural language; the agent calls the MASON tools
step by step, every step is written to disk with a validation report, and the result opens
in the structure viewer next to the conversation.

## Download

Ready-to-run packages (each bundles its own Python runtime, the MASON tools, the agent
front end and the structure viewer; only access to a language model is required) are on the
[Releases](https://github.com/zxy-boop/MASON/releases) page:

| Platform | File |
|---|---|
| macOS (Apple Silicon) | `MASON-1.0.2-macos-arm64.dmg` |
| Windows 10/11 (x64), portable | `MASON-1.0.2-windows-x64.zip` |
| Linux (x64) | `MASON-1.0.2-linux-x64.tar.gz` |

SHA-256 checksums are listed in `SHA256SUMS.txt` attached to each release.

## Documentation

- [User manual](docs/README.md): installation, first-run setup (model provider and
  Materials Project key), usage, troubleshooting, the full tool list, and running from source.
- [Getting started](docs/tutorial.html): the three-step tutorial shown on the first launch.

## Run from source

```bash
pip install .                  # or pip install -e .
python launcher/mason.py       # needs opencode on PATH, or MASON_HOME pointing to a directory that contains runtime/
```

## Tools

- Reading and analysis (9): read, inequivalent sites, validate, symmetry, interpolate, convert, render, search database, get entry
- Bulk and molecules (7): supercell, vacancy, molecule, substitute, interstitial, stacking fault, grain boundary
- Surfaces and 2D (15): nanoribbon, nanotube, twist pairs, twisted bilayer, twist matches, bilayer (ZSL), stacking, slab, cap bottom, freeze atoms, vacuum, adsorption sites, adsorbate, passivate, reconstruct
- Interfaces and stacking (4): interface matches, interface, solvent, stack
- Human selection (4): twist pick, select atoms, atom pick, interface pick

## Citation

If you use MASON in your research, please cite:

> X. Zhang, J. Li, B. Shao, B. Yang, Z. Liu, W. Wang, MASON: Materials Atomistic Structure
> builder Operated in Natural language, *Computational Materials Science* (submitted).

See [CITATION.cff](CITATION.cff) for a machine-readable citation.

## License

MASON is released under the [MIT License](LICENSE).
