"""Exercise the MASON MCP server over stdio without a language model: list the tools, then read, analyse, cut a slab,
validate and render.  Usage: python smoke_mcp.py --python <bundled python> --examples <examples dir>"""
import argparse, asyncio, json, os, shutil, sys, tempfile
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

def payload(res):
    txt = "\n".join(c.text for c in res.content if getattr(c, "type", "") == "text")
    try: return json.loads(txt)
    except Exception: return {"text": txt[:500]}

async def main(a):
    work = tempfile.mkdtemp(prefix="mason-smoke-"); si = os.path.join(work, "si_bulk.vasp")
    shutil.copy(os.path.join(os.path.abspath(a.examples), "si_bulk.vasp"), si)
    env = dict(os.environ, PYTHONUNBUFFERED="1", SEED_STATIC_HOST="127.0.0.1", SEED_STATIC_PORT="0")
    params = StdioServerParameters(command=os.path.abspath(a.python), args=["-m", "mason_mcp.server"], env=env, cwd=work)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools; names = sorted(t.name for t in tools)
            print("tools:", len(names)); assert len(names) == 39, names
            for need in ("read_structure", "analyze_symmetry", "generate_slab", "validate_geometry", "render_structure"): assert need in names, need
            rd = payload(await s.call_tool("read_structure", {"path": si})); print("read:", json.dumps(rd)[:200])
            sym = payload(await s.call_tool("analyze_symmetry", {"path": si})); print("symmetry:", json.dumps(sym)[:200])
            assert "227" in json.dumps(sym) or "Fd-3m" in json.dumps(sym), sym
            slab = os.path.join(work, "si111.vasp")
            sl = payload(await s.call_tool("generate_slab", {"input_path": si, "miller": [1, 1, 1], "min_slab_size": 4.0, "min_vacuum_size": 15.0, "output_path": slab}))
            print("slab:", json.dumps(sl)[:300]); assert os.path.exists(slab), "slab file not written"
            va = payload(await s.call_tool("validate_geometry", {"path": slab, "calc_type": "slab"})); print("validate:", json.dumps(va)[:300])
            assert "error" not in json.dumps(va).lower()[:20], va
            html = os.path.join(work, "si111.html")
            rn = payload(await s.call_tool("render_structure", {"input_path": slab, "fmt": "html", "output_path": html})); print("render:", json.dumps(rn)[:200])
            assert os.path.exists(html) and os.path.getsize(html) > 1000, "html not written"
            if os.environ.get("MP_API_KEY"):   # optional: the online Materials Project tools
                sr = payload(await s.call_tool("search_materials", {"formula": "Si", "energy_above_hull": [0, 0.001], "limit": 2}))
                print("search_materials:", json.dumps(sr)[:200]); assert sr.get("n_results", 0) >= 1, sr
                out = os.path.join(work, "mp-149.vasp")
                gm = payload(await s.call_tool("get_material", {"material_id": "mp-149", "output_path": out}))
                print("get_material:", json.dumps(gm)[:200]); assert os.path.exists(out), "structure not written"
                print("MP_TOOLS_OK")
    shutil.rmtree(work, ignore_errors=True); print("SMOKE_MCP_OK")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--python", default=sys.executable); ap.add_argument("--examples", required=True)
    asyncio.run(main(ap.parse_args()))
