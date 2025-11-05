# -*- coding: utf-8 -*-
"""
Minimal EnergyPlus-MCP probe:
- Loads an IDF
- (Optionally) fetches zones (best-effort)
- Prints ONLY the output of inspect_schedules

Run from project root (.../05_src/assignment_chat):
  python -m assignment_chat.tests.mcp_zones_and_schedules --idf data/idfs/YourModel.idf
  # or:
  python -m assignment_chat.tests.mcp_zones_and_schedules --use-sample
"""

import argparse, asyncio, os, shlex, shutil, subprocess, sys
from pathlib import Path
from typing import Optional, List

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# ---- EDIT THESE IF NEEDED ----
MCP_REPO = r"C:\Users\scliu\EnergyPlus-MCP"   # your cloned EnergyPlus-MCP repo
IMAGE    = "energyplus-mcp-dev"               # docker image tag you built
# --------------------------------

HERE = Path(__file__).resolve().parent
ROOT = (HERE / "..").resolve()
IDFS_DIR = (ROOT / "data" / "idfs").resolve()

def ok(m):  print(f"[OK]  {m}")
def info(m):print(f"[i]   {m}")
def err(m): print(f"[ERR] {m}", file=sys.stderr)
def die(m): err(m); raise SystemExit(1)

def prechecks(idf_host: Optional[Path], use_sample: bool) -> str:
    info("=== Pre-checks ===")
    if not shutil.which("docker"):
        die("Docker CLI not found. Start Docker Desktop; ensure 'docker version' works.")
    ok("docker CLI found")

    rv = subprocess.run(["docker","version","--format","{{.Server.Version}}"], capture_output=True, text=True)
    if rv.returncode != 0 or not rv.stdout.strip():
        die("Docker engine not reachable. Wait for 'Engine running' in Docker Desktop.")
    ok(f"Docker Engine version: {rv.stdout.strip()}")

    rv = subprocess.run(["docker","image","inspect", IMAGE], capture_output=True, text=True)
    if rv.returncode != 0:
        die(f"Image '{IMAGE}' not found. Build it:\n  cd {MCP_REPO}\\.devcontainer && docker build -t {IMAGE} .")
    ok(f"Docker image present: {IMAGE}")

    repo = Path(MCP_REPO)
    if not (repo / "energyplus-mcp-server").exists():
        die(f"Missing {repo}\\energyplus-mcp-server (MCP_REPO wrong?)")
    ok(f"MCP repo OK: {repo}")

    IDFS_DIR.mkdir(parents=True, exist_ok=True)
    ok(f"IDF mount dir exists: {IDFS_DIR}")

    if use_sample:
        target = "sample_files/1ZoneUncontrolled.idf"
        info(f"Using built-in sample: {target}")
        return target

    if idf_host is None:
        die("Provide --idf under data/idfs or use --use-sample.")
    idf_host = idf_host.resolve()
    info(f"IDF host path: {idf_host}")
    if not idf_host.exists():
        die(f"IDF not found: {idf_host}")
    try:
        idf_host.relative_to(IDFS_DIR)
    except ValueError:
        die(f"IDF must be inside {IDFS_DIR}. Move it there.")
    ok(f"IDF file OK: {idf_host.name}")
    target = f"/idfs/{idf_host.name}"
    info(f"Container will read: {target}")
    return target

def confirm_container_sees_idf(image: str, host_idfs_dir: Path, rel_name: Optional[str]):
    info("Checking /idfs mount inside container…")
    cmd = [
        "docker","run","--rm",
        "-v", f"{host_idfs_dir}:/idfs",
        image,"bash","-lc",
        "ls -l /idfs; echo '---'; " +
        (f"if [ -f /idfs/{shlex.quote(rel_name)} ]; then echo FOUND; else echo MISSING; fi"
         if rel_name else "echo SKIP_FILE_CHECK")
    ]
    out = subprocess.run(cmd, capture_output=True, text=True)
    print(out.stdout, end="")
    if rel_name and "FOUND" not in out.stdout:
        die(f"Container does NOT see /idfs/{rel_name}. Fix mount or filename.")
    ok("Container /idfs check passed")

def print_content(label: str, tool_result):
    print(f"\n=== {label} ===")
    if getattr(tool_result, "structuredContent", None) is not None:
        print(tool_result.structuredContent)
        return
    printed = False
    for c in getattr(tool_result, "content", []) or []:
        if hasattr(c, "text") and c.text:
            print(c.text); printed = True
        elif getattr(c, "type", None) == "text" and hasattr(c, "text"):
            print(c.text); printed = True
        elif hasattr(c, "data"):
            print(c.data); printed = True
    if not printed:
        print("(no printable content)")

def find_zone_tool(tool_names: List[str]) -> Optional[str]:
    # Best-effort discovery across possible names
    candidates = [
        "inspect_zones", "get_zones", "list_zones",
        "inspect_zone_summary", "get_zone_summary"
    ]
    lower = {n.lower(): n for n in tool_names}
    for c in candidates:
        if c in lower:
            return lower[c]
    # Fallback: any tool containing 'zone'
    for n in tool_names:
        if "zone" in n.lower():
            return n
    return None

async def main_async(idf_arg: Optional[str], use_sample: bool, show_zones: bool):
    target_idf = prechecks(Path(idf_arg) if idf_arg else None, use_sample)
    rel = None if use_sample else Path(idf_arg).name
    confirm_container_sees_idf(IMAGE, IDFS_DIR, rel)

    server = StdioServerParameters(
        command="docker",
        args=[
            "run","--rm","-i",
            "-v", f"{MCP_REPO}:/workspace",
            "-v", f"{IDFS_DIR}:/idfs",
            "-w","/workspace/energyplus-mcp-server",
            IMAGE,
            "uv","run","python","-m","energyplus_mcp_server.server",
        ],
    )

    info("Starting stdio client & initializing MCP session…")
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            info(f"Tools available (first 12): {names[:12]}")

            # Attach model (some servers use this to cache/validate path)
            info(f"Loading model: {target_idf}")
            try:
                await session.call_tool("load_idf_model", {"idf_path": target_idf})
            except Exception as e:
                err(f"load_idf_model error (continuing): {e}")

            # (Optional) Zones
            if show_zones:
                zone_tool = find_zone_tool(names)
                if zone_tool:
                    try:
                        r = await session.call_tool(zone_tool, {"idf_path": target_idf})
                        print_content(zone_tool, r)
                    except Exception as e:
                        err(f"{zone_tool} error: {e}")
                else:
                    err("No zone-inspection tool found (looked for inspect_zones/get_zones/list_zones).")

            # Schedules (the only required printed output)
            try:
                r = await session.call_tool("inspect_schedules", {"idf_path": target_idf})
                print_content("inspect_schedules", r)
            except Exception as e:
                err(f"inspect_schedules error: {e}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--idf", type=str, help="Host path under data/idfs (e.g., data/idfs/YourModel.idf)")
    ap.add_argument("--use-sample", action="store_true", help="Use built-in sample IDF")
    ap.add_argument("--show-zones", action="store_true", help="Also print zones (if a zone tool exists)")
    args = ap.parse_args()
    asyncio.run(main_async(args.idf, args.use_sample, args.show_zones))

if __name__ == "__main__":
    main()