#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BCL (NREL Building Component Library) API client — fixed parser.

- Uses requests (proxy/TLS friendly).
- Supports: probe (by UUID), sanity (bundle-only), scoped search.
- Correctly parses the real BCL schema: payload["result"][i]["measure"|"component"].
- Optional: --proxy, --insecure, --debug; OS compat and archetype filters.

Docs: https://bcl.nrel.gov/documentation
"""

from __future__ import annotations
import argparse, json, sys
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    print("This script requires 'requests'. Install it with:\n  pip install requests", file=sys.stderr)
    raise

BCL_BASE = "https://bcl.nrel.gov/api"

SCOPE_DEFAULT_TAG = {
    "people":   "People",
    "lights":   "Electric Lighting.Lighting Equipment",
    "equipment":"Equipment",
}

# ---------------- HTTP ----------------

def make_session(proxy: Optional[str], insecure: bool, timeout: float = 25.0) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "bcl-client/6.0 (python-requests)",
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
    })
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    s.verify = not insecure
    s.timeout = timeout
    return s

def get_json(s: requests.Session, url: str, params: Optional[List[Tuple[str, str]]] = None, debug: bool = False) -> Dict[str, Any]:
    if debug:
        q = "&".join([f"{k}={v}" for (k,v) in (params or [])])
        print(f"[GET] {url}{'?' + q if q else ''}")
    r = s.get(url, params=params)
    if debug:
        print(f"[STATUS] {r.status_code}")
        snippet = (r.text[:400] + "…") if len(r.text) > 400 else r.text
        print(f"[BODY SNIPPET]\n{snippet}\n")
    r.raise_for_status()
    return r.json()

# ------------- Parsing / Normalization -------------

def _extract_raw_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    BCL returns:
      {
        "result": [
          {"measure": {...}}, {"measure": {...}}, ...
        ],
        "total_results": N,
        ...
      }
    Older examples sometimes show "results"/"docs", but live API uses "result".
    """
    items = []
    if isinstance(payload, dict):
        arr = payload.get("result") or []
        for entry in arr:
            if "measure" in entry and isinstance(entry["measure"], dict):
                items.append(entry["measure"])
            elif "component" in entry and isinstance(entry["component"], dict):
                items.append(entry["component"])
    return items

def normalize_results(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = _extract_raw_items(payload)
    out: List[Dict[str, Any]] = []
    for r in raw:
        uuid = r.get("uuid") or r.get("id")
        name = r.get("display_name") or r.get("name") or ""
        # tags may appear as 'tags' or 'measure_tags' depending on object
        tags = r.get("measure_tags") or r.get("component_tags") or r.get("tags") or []
        repo = "/".join([x for x in [r.get("org"), r.get("repo")] if x])
        desc = (r.get("description") or r.get("long_description") or "").strip().replace("\n", " ")
        out.append({
            "uuid": uuid,
            "name": name,
            "bundle": r.get("bundle") or "measure",
            "tags": tags,
            "repo": repo,
            "release_tag": r.get("release_tag") or r.get("latest_version") or "",
            "summary": (desc[:297] + "...") if len(desc) > 300 else desc,
            "source_url": f"https://bcl.nrel.gov/content/{uuid}" if uuid else None,
        })
    return out

def print_table(rows: List[List[str]]) -> None:
    if not rows:
        print("(no results)")
        return
    widths = [max(len(str(x)) for x in col) for col in zip(*rows)]
    for i, row in enumerate(rows):
        print(" | ".join(str(x).ljust(widths[j]) for j, x in enumerate(row)))
        if i == 0:
            print("-+-".join("-" * w for w in widths))

# ------------- Search helpers -------------

def search_measures(
    s: requests.Session,
    keyword: Optional[str],
    measure_tag: Optional[str],
    os_version: Optional[str],
    archetype: Optional[str],
    page: int,
    rows: int,
    debug: bool,
) -> Dict[str, Any]:
    """
    Use keywordless form per docs:
      https://bcl.nrel.gov/api/search/
    Add {keyword}.json path only if you provide a real keyword (not '*').
    """
    params: List[Tuple[str, str]] = [("fq", "bundle:measure")]
    if measure_tag:
        tag_val = f"\"{measure_tag}\"" if any(c in measure_tag for c in (" ", ".")) else measure_tag
        params.append(("fq", f"measure_tags:{tag_val}"))
    if os_version:
        params.append(("fq", f"openstudio_version:{os_version}"))
    if archetype:
        params.append(("fq", 'attr_name:"Building Type"'))
        params.append(("fq", f"attr_value:{archetype}"))
    params.extend([("show_rows", str(rows)), ("page", str(page))])

    if keyword and keyword != "*":
        url = f"{BCL_BASE}/search/{keyword}.json"
        try:
            return get_json(s, url, params=params, debug=debug)
        except requests.HTTPError:
            pass  # fall back to keywordless

    url = f"{BCL_BASE}/search/"
    return get_json(s, url, params=params, debug=debug)

# ---------------- Commands ----------------

def cmd_probe(args: argparse.Namespace) -> None:
    s = make_session(args.proxy, args.insecure)
    url = f"{BCL_BASE}/search/"
    params = [("fq", f"uuid:{args.uuid}")]
    data = get_json(s, url, params=params, debug=args.debug)
    results = normalize_results(data)
    print(json.dumps(results, indent=2))
    print(f"\n(count={len(results)})")

def cmd_sanity(args: argparse.Namespace) -> None:
    s = make_session(args.proxy, args.insecure)
    data = search_measures(
        s=s, keyword="*", measure_tag=None, os_version=None, archetype=None,
        page=0, rows=10, debug=args.debug
    )
    results = normalize_results(data)
    header = ["Name", "UUID", "Release", "URL"]
    rows = [header] + [[r["name"] or "-", (r.get("uuid") or "")[:8] + "…", r.get("release_tag") or "-", r.get("source_url") or "-"] for r in results]
    print_table(rows)
    print(f"\n(count={len(results)})")

def cmd_search(args: argparse.Namespace) -> None:
    s = make_session(args.proxy, args.insecure)
    tag = args.tag or SCOPE_DEFAULT_TAG[args.scope.lower()]
    data = search_measures(
        s=s, keyword=args.keyword, measure_tag=tag, os_version=args.os,
        archetype=args.archetype, page=args.page, rows=args.rows, debug=args.debug
    )
    results = normalize_results(data)
    if not results and args.try_untagged:
        data = search_measures(
            s=s, keyword=args.keyword, measure_tag=None, os_version=args.os,
            archetype=args.archetype, page=args.page, rows=args.rows, debug=args.debug
        )
        results = normalize_results(data)

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return

    header = ["(tag)", "Name", "UUID", "Release", "Repo", "URL"]
    rows = [header]
    for r in results:
        rows.append([
            tag if args.tag or results else "(none)",
            r.get("name") or "-",
            (r.get("uuid") or "")[:8] + "…",
            r.get("release_tag") or "-",
            r.get("repo") or "-",
            r.get("source_url") or "-",
        ])
    print_table(rows)
    if not results:
        print("\nNo results. Try:\n"
              "  • --try-untagged\n"
              "  • --tag \"Electric Lighting\"  |  --tag \"People\"  |  --tag \"Equipment\"\n"
              "  • remove --os or set --keyword office / school / retail")

# ---------------- CLI ----------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="BCL Search client (fixed parser).")
    sub = p.add_subparsers(dest="cmd", required=True)

    u = sub.add_parser("probe", help="Probe by UUID (should return exactly one item)")
    u.add_argument("--uuid", default="339a2e3a-273c-4494-bb50-bfe586a0647c")
    u.add_argument("--debug", action="store_true")
    u.add_argument("--proxy")
    u.add_argument("--insecure", action="store_true")
    u.set_defaults(func=cmd_probe)

    s = sub.add_parser("sanity", help="Bundle-only fetch to confirm API returns items")
    s.add_argument("--debug", action="store_true")
    s.add_argument("--proxy")
    s.add_argument("--insecure", action="store_true")
    s.set_defaults(func=cmd_sanity)

    q = sub.add_parser("search", help="Scoped search")
    q.add_argument("--scope", choices=list(SCOPE_DEFAULT_TAG), required=True)
    q.add_argument("--keyword", default="*")
    q.add_argument("--os")
    q.add_argument("--archetype")
    q.add_argument("--tag")
    q.add_argument("--rows", type=int, default=25)
    q.add_argument("--page", type=int, default=0)
    q.add_argument("--try-untagged", action="store_true")
    q.add_argument("--debug", action="store_true")
    q.add_argument("--proxy")
    q.add_argument("--insecure", action="store_true")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_search)
    return p

def main() -> None:
    args = build_parser().parse_args()
    args.func(args)

if __name__ == "__main__":
    main()