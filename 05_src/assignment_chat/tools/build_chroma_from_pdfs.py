# -*- coding: utf-8 -*-
"""
Build a deterministic, section-aware Chroma vector store from PDFs (no TOC).

Defaults (best-practice):
- Model: text-embedding-3-large (3072-d)
- Per-page chunks with exact page metadata
- Heading detection via font-size + heuristics
- Header-augmented chunks: "Book | Section | p.X" + text
- Adaptive overlap (~20% of chunk size; floor at 30 tokens)
- Min-chars guard (skip very short pages)
- Incremental build via manifest (skip unchanged chunks)
- Records embedding model & dimension in manifest
- Optional --reset to wipe persisted DB

Usage:
  python -m tools.build_chroma_from_pdfs \
    --collection eplus_refs \
    --pdf-dir ./data/pdfs \
    --persist-dir ./data/chroma \
    [--embed-model text-embedding-3-large] \
    [--reset]
"""

import os, re, sys, time, math, json, hashlib, argparse
from pathlib import Path
from typing import List, Dict, Iterable, Tuple

import fitz  # PyMuPDF
import tiktoken
from chromadb import PersistentClient
from openai import OpenAI
from openai import APIError, RateLimitError, APIConnectionError

# ---------- Defaults ----------
DEFAULT_COLLECTION   = "eplus_refs"
DEFAULT_EMBED_MODEL  = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large")
DEFAULT_PDF_DIR      = Path(__file__).resolve().parents[1] / "data" / "pdfs"
DEFAULT_PERSIST_DIR  = Path(__file__).resolve().parents[1] / "data" / "chroma"

EMBED_BATCH   = int(os.getenv("EMBED_BATCH", "128"))
UPSERT_BATCH  = int(os.getenv("UPSERT_BATCH", "1000"))

# Chunk sizing (good balance for manuals)
TARGET_TOKENS     = int(os.getenv("TARGET_TOKENS", "320"))
TOKEN_OVERLAP_ENV = os.getenv("TOKEN_OVERLAP", "auto")  # "auto" or int
TOKEN_CODEC       = os.getenv("TOKEN_CODEC", "cl100k_base")
MIN_CHARS_PER_PAGE = int(os.getenv("MIN_CHARS_PER_PAGE", "80"))

OPENAI_KEY_FALLBACK_FILE = r"C:\Users\scliu\Desktop\deploying-ai\05_src\.secrets"

def manifest_path(persist_dir: Path, collection: str) -> Path:
    return Path(persist_dir) / f"{collection}.__manifest__.json"

# ---------- Key handling ----------
def ensure_openai_key():
    if os.getenv("OPENAI_API_KEY"):
        return
    key_path = Path(OPENAI_KEY_FALLBACK_FILE)
    if key_path.exists():
        raw = key_path.read_text(encoding="utf-8").strip()
        for line in raw.splitlines():
            line = line.strip()
            if line:
                os.environ["OPENAI_API_KEY"] = line
                break
    if not os.getenv("OPENAI_API_KEY"):
        print("[ERR] OPENAI_API_KEY not set and fallback key file not found/usable.", file=sys.stderr)
        sys.exit(1)

# ---------- Text & chunking ----------
def clean_text(text: str) -> str:
    if not text: return ""
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()

def looks_like_heading(line: str) -> bool:
    if not line: return False
    if re.match(r"^\d+(\.\d+)*(\.|:)?\s+\S", line): return True
    if re.match(r"^\d+(\.\d+)*\.?$", line): return True
    if len(line) <= 90 and (line.isupper() or line.endswith(":")): return True
    if len(line) <= 90 and re.match(r"^[A-Z][A-Za-z0-9 ,;:/\-\(\)]+$", line): return True
    return False

def resolve_overlap(target_tokens: int) -> int:
    if TOKEN_OVERLAP_ENV.lower() == "auto":
        return max(30, int(0.2 * target_tokens))
    try:
        return max(0, int(TOKEN_OVERLAP_ENV))
    except Exception:
        return max(30, int(0.2 * target_tokens))

def token_chunks(text: str, target_tokens: int, overlap_tokens: int, encoding) -> Iterable[str]:
    if not text: return
    toks = encoding.encode(text)
    step = max(1, target_tokens - overlap_tokens)
    for start in range(0, len(toks), step):
        end = min(len(toks), start + target_tokens)
        chunk = encoding.decode(toks[start:end]).strip()
        if chunk: yield chunk
        if end >= len(toks): break

# ---------- Heading detection ----------
def page_heading_candidate(doc: fitz.Document, page_1based: int) -> Tuple[str, float]:
    page = doc[page_1based - 1]
    d = page.get_text("dict") or {}
    lines = []
    for block in d.get("blocks", []):
        if block.get("type") != 0: continue
        for line in block.get("lines", []):
            y0 = min((s.get("bbox", [0,0,0,0])[1] for s in line.get("spans", [])), default=1e9)
            text = "".join(s.get("text", "") for s in line.get("spans", [])) or ""
            text = text.strip()
            if not text: continue
            sizes = [float(s.get("size", 0)) for s in line.get("spans", []) if float(s.get("size", 0)) > 0]
            avg_size = sum(sizes)/len(sizes) if sizes else 0.0
            lines.append((y0, avg_size, text))
    if not lines: return ("", 0.0)
    lines.sort(key=lambda x: (x[0], -x[1]))
    top_lines = lines[:15]
    sizes_all = [sz for (_, sz, _) in lines[:50] if sz > 0]
    body_size = 0.0
    if sizes_all:
        sizes_all.sort()
        mid = len(sizes_all)//2
        body_size = sizes_all[mid] if len(sizes_all)%2==1 else 0.5*(sizes_all[mid-1]+sizes_all[mid])
    best = ("", 0.0)
    for y0, sz, text in top_lines:
        if len(text) > 140: continue
        heuristic = looks_like_heading(text)
        size_boost = 1.0 if (body_size>0 and sz>=body_size+1.5) else 0.0
        score = (sz-body_size) + (3.0 if heuristic else 0.0) + (1.0 if y0<200 else 0.0)
        if heuristic or size_boost>0:
            if score>best[1]: best=(text, score)
    if best[0]=="": best=(top_lines[0][2], 0.0)
    return best

def detect_sections_by_pages(doc: fitz.Document) -> List[Dict]:
    sections, current = [], None
    for p in range(1, doc.page_count+1):
        title, score = page_heading_candidate(doc, p)
        title = title.strip()
        is_heading = looks_like_heading(title) or score>=2.5
        if is_heading:
            if current:
                current["end_page"] = p-1
                sections.append(current)
            current = {"title": title[:200] or "(untitled section)", "level":2, "start_page":p, "end_page":p}
        else:
            if current: current["end_page"]=p
            else: current={"title":"Front Matter","level":2,"start_page":1,"end_page":p}
    if current: sections.append(current)
    clean=[]
    for s in sections:
        sp=max(1,int(s["start_page"])); ep=max(sp,int(s["end_page"]))
        clean.append({**s,"start_page":sp,"end_page":min(ep,doc.page_count)})
    return clean

# ---------- Per-page chunk iterator ----------
def iter_section_page_chunks(pdf_path: Path, encoding, target_tokens: int, overlap_tokens: int) -> Iterable[Dict]:
    doc = fitz.open(pdf_path); book = pdf_path.stem
    try:
        sections = detect_sections_by_pages(doc)
        print(f"  [sections] page-heading → {len(sections)}")
        page_to_sec={}
        for si,s in enumerate(sections):
            for p in range(s["start_page"], s["end_page"]+1):
                page_to_sec[p]=(si, s["title"], s.get("level",2))
        chunk_counter: Dict[int,int]={}
        for p in range(1, doc.page_count+1):
            raw = doc[p-1].get_text("text") or ""
            text = clean_text(raw)
            if not text or len(text)<MIN_CHARS_PER_PAGE: continue
            si, sec_title, sec_level = page_to_sec.get(p, (-1,"Unknown Section",2))
            header = f"{book} | {sec_title} | p.{p}\n\n"  # improves grounding
            if si not in chunk_counter: chunk_counter[si]=0
            for chunk in token_chunks(text, target_tokens, overlap_tokens, encoding):
                local_idx = chunk_counter[si]
                yield {
                    "id": f"{book}-s{si:04d}-p{p:04d}-c{local_idx:03d}",
                    "text": header + chunk,
                    "document_id": book, "book": book, "source_pdf": pdf_path.name,
                    "section_title": sec_title, "section_level": int(sec_level),
                    "section_index": si, "chunk_index": local_idx,
                    "page": p, "page_span": f"{p}",
                }
                chunk_counter[si]+=1
    finally:
        doc.close()

# ---------- Manifest ----------
def sha1_text(s: str) -> str: return hashlib.sha1(s.encode("utf-8")).hexdigest()

def load_manifest(path: Path) -> Dict:
    if not path.exists(): return {}
    try: return json.loads(path.read_text(encoding="utf-8"))
    except Exception: return {}

def save_manifest(path: Path, data: Dict) -> None:
    tmp = str(path)+".tmp"
    Path(tmp).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(tmp).replace(path)

# ---------- Embedding & upsert ----------
def embed_with_backoff(client: OpenAI, model: str, inputs: List[str], attempt: int = 1) -> List[List[float]]:
    t0=time.time()
    try:
        resp = client.embeddings.create(model=model, input=inputs)
        print(f"    [embed/ok] {len(inputs)} in {time.time()-t0:.2f}s")
        return [d.embedding for d in resp.data]
    except (RateLimitError, APIConnectionError, APIError) as e:
        if attempt>=5: raise
        sleep_s=min(2**attempt,16)
        print(f"    [embed/retry] {e.__class__.__name__} → {sleep_s}s (attempt {attempt})")
        time.sleep(sleep_s)
        return embed_with_backoff(client, model, inputs, attempt+1)

def embed_texts(client: OpenAI, model: str, texts: List[str]) -> List[List[float]]:
    out=[]; total=len(texts)
    n_batches=math.ceil(total/EMBED_BATCH) if total else 0
    for i in range(0,total,EMBED_BATCH):
        batch=texts[i:i+EMBED_BATCH]
        print(f"  [embed] batch {i//EMBED_BATCH+1}/{n_batches} (size={len(batch)})")
        out.extend(embed_with_backoff(client, model, batch)); time.sleep(0.05)
    return out

def upsert_sliced(collection, items: List[Dict], client_openai: OpenAI, embed_model: str):
    total=len(items)
    for start in range(0,total,UPSERT_BATCH):
        end=min(total,start+UPSERT_BATCH)
        slice_items=items[start:end]
        docs=[it["text"] for it in slice_items]
        ids=[it["id"] for it in slice_items]
        metas=[{
            "document_id": it["document_id"], "book": it["book"], "source_pdf": it["source_pdf"],
            "section_title": it["section_title"], "section_level": it["section_level"],
            "section_index": it["section_index"], "chunk_index": it["chunk_index"],
            "page": it["page"], "page_span": it["page_span"],
        } for it in slice_items]
        vectors=embed_texts(client_openai, embed_model, docs)
        print(f"  [upsert] {len(vectors)} vectors")
        collection.upsert(ids=ids, documents=docs, metadatas=metas, embeddings=vectors)

# ---------- Utils ----------
def get_embedding_dim(client: OpenAI, model: str) -> int:
    resp = client.embeddings.create(model=model, input=["probe"])
    return len(resp.data[0].embedding)

# ---------- Build ----------
def build(collection_name: str, pdf_dir: Path, persist_dir: Path, embed_model: str, reset: bool):
    ensure_openai_key()
    persist_dir.mkdir(parents=True, exist_ok=True)

    if reset:
        print(f"[RESET] Removing existing persist dir: {persist_dir}")
        for child in Path(persist_dir).glob("*"):
            try:
                if child.is_dir():
                    for root, dirs, files in os.walk(child, topdown=False):
                        for name in files: Path(root, name).unlink(missing_ok=True)
                        for name in dirs: Path(root, name).rmdir()
                    child.rmdir()
                else:
                    child.unlink()
            except Exception:
                pass

    client_chroma = PersistentClient(path=str(persist_dir))
    collection = client_chroma.get_or_create_collection(collection_name)

    oa = OpenAI()
    encoding = tiktoken.get_encoding(TOKEN_CODEC)
    overlap_tokens = resolve_overlap(TARGET_TOKENS)
    embed_dim = get_embedding_dim(oa, embed_model)

    print(f"[INFO] pdf_dir      : {pdf_dir.resolve()}")
    print(f"[INFO] persist_dir  : {persist_dir.resolve()}")
    print(f"[INFO] collection   : {collection_name}")
    print(f"[INFO] model        : {embed_model} (dim={embed_dim})")
    print(f"[INFO] chunk tokens : ~{TARGET_TOKENS} (overlap {overlap_tokens})")
    print(f"[INFO] min chars    : {MIN_CHARS_PER_PAGE}")
    print("-"*60)

    pdfs = sorted(pdf_dir.glob("*.pdf"))
    if not pdfs:
        print("[WARN] No PDFs found."); return

    print("[INFO] Found PDFs:")
    for p in pdfs:
        try: size_mb = p.stat().st_size/(1024*1024)
        except Exception: size_mb=0.0
        print(f"  - {p.name} ({size_mb:.2f} MB)")
    print("-"*60)

    # Manifest (new format: {"_meta": {...}, "items": {...}})
    mani_path = manifest_path(persist_dir, collection_name)
    raw_mani = load_manifest(mani_path)
    if "_meta" in raw_mani and "items" in raw_mani:
        manifest_items = raw_mani.get("items", {})
        meta = raw_mani.get("_meta", {})
    else:
        manifest_items = raw_mani
        meta = {}

    meta.update({
        "embedding_model": embed_model,
        "embedding_dimension": embed_dim,
        "target_tokens": TARGET_TOKENS,
        "overlap_tokens": overlap_tokens,
        "min_chars_per_page": MIN_CHARS_PER_PAGE,
    })

    to_embed: List[Dict] = []; total_seen=0
    for pdf in pdfs:
        print(f"[+] {pdf.name}")
        for item in iter_section_page_chunks(pdf, encoding, TARGET_TOKENS, overlap_tokens):
            total_seen += 1
            h = hashlib.sha1(item["text"].encode("utf-8")).hexdigest()
            old = manifest_items.get(item["id"])
            if old == h:  # unchanged
                continue
            manifest_items[item["id"]] = h
            to_embed.append(item)

    print(f"[scan] total candidates: {total_seen} | to (re)embed: {len(to_embed)}")
    if not to_embed:
        save_manifest(mani_path, {"_meta": meta, "items": manifest_items})
        print("[ok] nothing to update."); return

    for start in range(0, len(to_embed), UPSERT_BATCH):
        end = min(len(to_embed), start + UPSERT_BATCH)
        slice_items = to_embed[start:end]
        print(f"    [do] upserting slice of {len(slice_items)} page-chunks …")
        upsert_sliced(collection, slice_items, oa, embed_model)

    save_manifest(mani_path, {"_meta": meta, "items": manifest_items})

    total = collection.count()
    print(f"[verify] collection.count() = {total}")
    print(f"[done] Chroma DB at: {persist_dir} | collection: '{collection_name}'")

def parse_args():
    p = argparse.ArgumentParser(description="Build a deterministic, section-aware Chroma store from PDFs (no TOC).")
    p.add_argument("--collection", default=DEFAULT_COLLECTION)
    p.add_argument("--pdf-dir", default=str(DEFAULT_PDF_DIR))
    p.add_argument("--persist-dir", default=str(DEFAULT_PERSIST_DIR))
    p.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL)
    p.add_argument("--reset", action="store_true", help="Wipe existing persisted DB before building.")
    return p.parse_args()

if __name__ == "__main__":
    args = parse_args()
    build(
        collection_name=args.collection,
        pdf_dir=Path(args.pdf_dir),
        persist_dir=Path(args.persist_dir),
        embed_model=args.embed_model,
        reset=args.reset,
    )