# -*- coding: utf-8 -*-
"""
RAG test over a per-page, section-aware Chroma collection.

- Auto-uses the embedding model recorded by the builder manifest (so query dim matches index dim).
- Overfetch → keyword boost → per-section/per-page selection.
- Clean, page-accurate citations.

Usage:
  python -m tests.test_rag --query "How do schedule rules work in EnergyPlus?" \
    --top-k 10 --per-section 2 --per-page 1 --overfetch 8 --show 8
"""

import os, re, sys, time, json, argparse
from pathlib import Path
from typing import List, Dict, Any, Tuple
from collections import defaultdict

from chromadb import PersistentClient
from openai import OpenAI

DEFAULT_COLLECTION  = os.getenv("CHROMA_COLLECTION", "eplus_refs")
DEFAULT_PERSIST_DIR = Path(__file__).resolve().parents[1] / "data" / "chroma"
DEFAULT_EMBED_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large")
DEFAULT_ANSWER_MODEL= os.getenv("OPENAI_ANSWER_MODEL", "gpt-4o-mini")
OPENAI_KEY_FALLBACK_FILE = r"C:\Users\scliu\Desktop\deploying-ai\05_src\.secrets"

def ensure_openai_key():
    if os.getenv("OPENAI_API_KEY"): return
    key_path = Path(OPENAI_KEY_FALLBACK_FILE)
    if key_path.exists():
        raw = key_path.read_text(encoding="utf-8").strip()
        for line in raw.splitlines():
            line=line.strip()
            if line: os.environ["OPENAI_API_KEY"]=line; break
    if not os.getenv("OPENAI_API_KEY"):
        print("[ERR] OPENAI_API_KEY not set and fallback key file not found/usable.", file=sys.stderr); sys.exit(1)

def read_manifest(persist_dir: Path, collection: str) -> Dict[str, Any]:
    path = Path(persist_dir) / f"{collection}.__manifest__.json"
    if not path.exists(): return {}
    try: return json.loads(path.read_text(encoding="utf-8"))
    except Exception: return {}

def embed_query(client: OpenAI, model: str, text: str) -> List[float]:
    t0=time.time(); resp = client.embeddings.create(model=model, input=[text])
    print(f"[embed] model={model} took {time.time()-t0:.2f}s")
    return resp.data[0].embedding

def normalize_results(results: Dict[str, Any]) -> List[Dict[str, Any]]:
    out=[]; ids=results.get("ids",[[]])[0]; docs=results.get("documents",[[]])[0]
    metas=results.get("metadatas",[[]])[0]; dists=results.get("distances",[[]])[0]
    for i in range(len(ids)):
        dist = dists[i] if i<len(dists) and dists[i] is not None else None
        score = (1.0-float(dist)) if (dist is not None) else None
        out.append({"id":ids[i], "document":docs[i], "metadata":metas[i], "distance":dist, "score":score})
    out.sort(key=lambda r: (-(r["score"] if r["score"] is not None else -1))); return out

def section_key(meta: Dict[str, Any]) -> Tuple[str,str]:
    book=(meta.get("book") or meta.get("document_id") or "?").lower().strip()
    title=(meta.get("section_title") or "?").lower().strip()
    return (book,title)

def page_key(meta: Dict[str, Any]) -> Tuple[str,str,int]:
    book=(meta.get("book") or meta.get("document_id") or "?").lower().strip()
    title=(meta.get("section_title") or "?").lower().strip()
    page=int(meta.get("page") or -1); return (book,title,page)

def select_results(rows: List[Dict[str, Any]], limit: int, per_section: int=2, per_page: int=1) -> List[Dict[str, Any]]:
    kept=[]; by_section=defaultdict(int); by_page=defaultdict(int)
    for r in rows:
        meta=r.get("metadata") or {}
        s_key=section_key(meta); p_key=page_key(meta)
        if per_section is not None and by_section[s_key]>=per_section: continue
        if per_page is not None and by_page[p_key]>=per_page: continue
        kept.append(r); by_section[s_key]+=1; by_page[p_key]+=1
        if len(kept)>=limit: break
    return kept

def format_citation(meta: Dict[str, Any]) -> str:
    book=meta.get("book") or meta.get("document_id") or "Unknown Book"
    sec =meta.get("section_title") or "(untitled section)"
    p   =meta.get("page"); return f'{book} — "{sec}" (p. {p})' if p else f'{book} — "{sec}"'

def build_context_blocks(rows: List[Dict[str, Any]], show: int) -> Tuple[str,List[str]]:
    blocks=[]; cites=[]
    for i,r in enumerate(rows[:show]):
        m=r["metadata"]; citation=format_citation(m)
        cites.append(f"[{i+1}] {citation}")
        blocks.append(f"### Chunk {i+1}\nSource: {citation}\n\n{r['document']}\n")
    return "\n\n".join(blocks), cites

def apply_keyword_boost(rows: List[Dict[str, Any]], query: str, weight: float=0.06) -> None:
    terms=[w.lower() for w in re.findall(r"[a-zA-Z0-9]+", query) if len(w)>=3]
    def hits(t: str)->int:
        t=(t or "").lower(); return sum(t.count(w) for w in terms)
    for r in rows:
        m=r.get("metadata") or {}
        r["_kw"]=hits(r.get("document",""))+hits(m.get("section_title",""))
        r["_combo"]=(r["score"] or 0.0)+weight*r["_kw"]
    rows.sort(key=lambda x: -x["_combo"])

SYSTEM_PROMPT=("You are a precise technical assistant. Answer using ONLY the provided context. "
               "If the answer is not in the context, say you don't find it. "
               "Cite sources inline like [1], [2].")

def answer_with_llm(client: OpenAI, model: str, question: str, context_blocks: str) -> str:
    prompt=f"{SYSTEM_PROMPT}\n\n# Question\n{question}\n\n# Context\n{context_blocks}\n"
    t0=time.time()
    resp=client.chat.completions.create(model=model,
        messages=[{"role":"system","content":SYSTEM_PROMPT},{"role":"user","content":prompt}],
        temperature=0.2)
    print(f"[llm] model={model} took {time.time()-t0:.2f}s")
    return resp.choices[0].message.content.strip()

def run_rag(query: str, collection_name: str, persist_dir: Path, embed_model_arg: str,
            answer_model: str, top_k: int, per_section: int, per_page: int,
            overfetch: int, show: int, dry_run: bool, kw_boost: bool):
    ensure_openai_key()
    oa=OpenAI()

    # Auto-match model/dim from manifest (CLI override still allowed)
    mani_path = Path(persist_dir) / f"{collection_name}.__manifest__.json"
    mani = {}
    if mani_path.exists():
        try: mani = json.loads(mani_path.read_text(encoding="utf-8"))
        except Exception: mani = {}
    mani_model = (mani.get("_meta") or {}).get("embedding_model")
    embed_model = embed_model_arg or mani_model or DEFAULT_EMBED_MODEL
    if mani_model and embed_model != mani_model:
        print(f"[warn] Overriding index model '{mani_model}' with CLI/env '{embed_model}'. Ensure dimensions match.")

    chroma = PersistentClient(path=str(persist_dir))
    collection = chroma.get_or_create_collection(collection_name)

    count = collection.count()
    print(f"[info] collection='{collection_name}', vectors={count}, persist_dir='{persist_dir}'")
    if count==0:
        print("[ERR] Collection is empty. Build the index first.", file=sys.stderr); sys.exit(1)

    qvec = embed_query(oa, embed_model, query)

    n_results = max(top_k*overfetch, top_k)
    t0=time.time()
    results = collection.query(query_embeddings=[qvec], n_results=n_results,
                               include=["metadatas","documents","distances"])
    print(f"[retrieval] n_requested={n_results} took {time.time()-t0:.2f}s")

    rows = normalize_results(results)
    if kw_boost: apply_keyword_boost(rows, query, weight=0.06)
    rows_sel = select_results(rows, limit=top_k, per_section=per_section, per_page=per_page)

    print(f"\n=== Top {len(rows_sel)} results (per_section={per_section}, per_page={per_page}) ===")
    for i,r in enumerate(rows_sel,1):
        m=r["metadata"]; score=r.get("_combo", r["score"])
        print(f"[{i}] score={score:.4f}  {format_citation(m)}  "
              f"(sec='{m.get('section_title','')[:40]}', page={m.get('page')}, chunk={m.get('chunk_index')})")

    context_blocks, cites = build_context_blocks(rows_sel, show=show)
    print("\n=== Citations ==="); [print(c) for c in cites]

    if dry_run:
        print("\n[dry-run] Skipping LLM call."); return

    print("\n=== Answer ===")
    print(answer_with_llm(oa, answer_model, query, context_blocks))

def parse_args():
    p = argparse.ArgumentParser(description="Test RAG over a per-page, section-aware Chroma collection.")
    p.add_argument("--query", required=True)
    p.add_argument("--collection", default=DEFAULT_COLLECTION)
    p.add_argument("--persist-dir", default=str(DEFAULT_PERSIST_DIR))
    p.add_argument("--embed-model", default="", help="Override embedding model for querying (auto from manifest by default).")
    p.add_argument("--answer-model", default=DEFAULT_ANSWER_MODEL)
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--per-section", type=int, default=2)
    p.add_argument("--per-page", type=int, default=1)
    p.add_argument("--overfetch", type=int, default=8)
    p.add_argument("--show", type=int, default=6)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-kw-boost", action="store_true")  # <-- argparse will expose this as a.no_kw_boost
    return p.parse_args()

if __name__ == "__main__":
    a=parse_args()
    run_rag(
        query=a.query,
        collection_name=a.collection,
        persist_dir=Path(a.persist_dir),
        embed_model_arg=a.embed_model,
        answer_model=a.answer_model,
        top_k=a.top_k,
        per_section=a.per_section,
        per_page=a.per_page,
        overfetch=a.overfetch,
        show=a.show,
        dry_run=a.dry_run,
        kw_boost=(not a.no_kw_boost),  # <-- correct attribute name
    )