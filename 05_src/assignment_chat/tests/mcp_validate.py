# -*- coding: utf-8 -*-
"""
EnergyPlus-MCP probe via official MCP Python SDK.
Parses/inspects an IDF with multiple read-only tools before/alongside validate_idf,
and prints server logs to pinpoint issues.

Run from project root (.../05_src/assignment_chat):
  python -m assignment_chat.tests.mcp_probe --idf data/idfs/YourModel.idf
  # or:
  python -m assignment_chat.tests.mcp_probe --use-sample
"""

import argparse, asyncio, os, shlex, shutil, subprocess, sys
from pathlib import Path
from typing import Optional

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
    # mcp.ClientResult has .content (list of TextContent/JSONContent) and maybe .structuredContent
    if getattr(tool_result, "structuredContent", None) is not None:
        print(tool_result.structuredContent)
        return
    printed = False
    for c in getattr(tool_result, "content", []) or []:
        if hasattr(c, "text") and c.text:
            print(c.text)
            printed = True
        elif hasattr(c, "type") and c.type == "text" and hasattr(c, "text"):
            print(c.text); printed = True
        elif hasattr(c, "data"):
            print(c.data); printed = True
    if not printed:
        print("(no printable content)")

async def main_async(idf_arg: Optional[str], use_sample: bool):
    target_idf = prechecks(Path(idf_arg) if idf_arg else None, use_sample)
    rel = None if use_sample else Path(idf_arg).name
    confirm_container_sees_idf(IMAGE, IDFS_DIR, rel)

    # Configure stdio server launch; SDK handles handshake/framing per MCP spec.
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

            # 1) load_idf_model (parse/attach)
            info(f"Loading model: {target_idf}")
            try:
                r = await session.call_tool("load_idf_model", {"idf_path": target_idf})
                print_content("load_idf_model", r)
            except Exception as e:
                err(f"load_idf_model error: {e}")

            # 2) get_model_summary
            try:
                r = await session.call_tool("get_model_summary", {"idf_path": target_idf})
                print_content("get_model_summary", r)
            except Exception as e:
                err(f"get_model_summary error: {e}")

            # 3) inspect_* helpers (optional, but useful to prove parse worked)
            for tool_name in [
                "inspect_schedules",
                "inspect_people",
                "inspect_lights",
                "inspect_electric_equipment",
                "check_simulation_settings",
            ]:
                if tool_name in names:
                    try:
                        r = await session.call_tool(tool_name, {"idf_path": target_idf})
                        print_content(tool_name, r)
                    except Exception as e:
                        err(f"{tool_name} error: {e}")

            # 4) validate_idf
            info(f"Validating: {target_idf}")
            try:
                r = await session.call_tool("validate_idf", {"idf_path": target_idf})
                print_content("validate_idf", r)
            except Exception as e:
                err(f"validate_idf error: {e}")

            # 5) logs (always fetch for completeness)
            if "get_error_logs" in names:
                try:
                    r = await session.call_tool("get_error_logs", {})
                    print_content("get_error_logs", r)
                except Exception as e:
                    err(f"get_error_logs error: {e}")

            if "get_server_logs" in names:
                try:
                    r = await session.call_tool("get_server_logs", {})
                    print_content("get_server_logs", r)
                except Exception as e:
                    err(f"get_server_logs error: {e}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--idf", type=str, help="Host path under data/idfs (e.g., data/idfs/YourModel.idf)")
    ap.add_argument("--use-sample", action="store_true", help="Validate built-in sample instead of your file")
    args = ap.parse_args()
    asyncio.run(main_async(args.idf, args.use_sample))

if __name__ == "__main__":
    main()