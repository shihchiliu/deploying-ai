# EnergyPlus Internal-Loads Assistant — README

> A Gradio-hosted, MCP-powered assistant for **inspecting** and (optionally) **modifying** internal loads (People, Lights, ElectricEquipment) in EnergyPlus models — with a built-in **site map** panel and **RAG** explanations from authoritative docs.

---

## Nature of the chat client

This app is an **opinionated, end-to-end UI** that wraps three capabilities:

1. **EnergyPlus MCP**
   After you upload an **IDF**, the app connects to your **EnergyPlus-MCP server** (Docker, MCP stdio) and **hard-runs**:

   * `load_idf_model` → attach the model
   * `get_model_summary` → extract *site* (lat/lon/tz/elevation), zones, and high-level context
   * `list_zones` (or parses zones from the summary when needed)

   From that point on, the assistant operates against the *loaded* model via MCP tools. (MCP is a protocol for tool-calling AI apps; see the **architecture** and **client** references. ([Model Context Protocol][1]))

2. **Map panel (city-level by default)**
   Using site **latitude/longitude** from the summary, the UI renders an **interactive map** in-chat with **Google Maps Embed API** (free to use, but **requires an API key**; you restrict it to `localhost` or your host). ([Google for Developers][2])

   * For exports or low-key usage you can also generate **static** map images from the OpenStreetMap ecosystem (keyless; respect usage policies). ([OpenStreetMap][3])

3. **RAG explanations (on inspection)**
   After each inspection, the app can run **retrieval-augmented generation (RAG)** over your embedded references (Chroma vector store) to provide a **short verdict** (✅/⚠️/❌), a few **bullets** explaining field semantics or typical ranges, and **citations**. (Chroma is an open-source vector DB for local retrieval. ([Chroma Docs][4]))

**Strict scope:** **People / Lights / ElectricEquipment** only.
No schedule **modification** is exposed in the UI. Schedule **names** may appear read-only for context.

---

## Services that it provides

### 1) Upload → auto summary (hard-wired bootstrap)

* **Upload IDF** (required, first interaction): the app saves your file under `assignment_chat/data/idfs/<name>.idf`.
* **Immediately** calls MCP:

  * `load_idf_model`
  * `get_model_summary`
  * `list_zones` (or parses zones)
* The app posts a **chat summary** and brings up a **map** (default zoom ≈ city).

> Why MCP? It standardizes tool access for LLM apps — the client lists available tools and calls them with structured arguments over a stable protocol. ([Model Context Protocol][1])

### 2) Inspect / Modify loop (chat-driven)

* **Inspect** (primary path, always available):
  Choose `{object ∈ [people, lights, equipment], zone ∈ zones}` and the assistant calls the corresponding **inspect** tool. Results are rendered as a tidy table (densities, LPD/EPD, fractions, schedule names for context).
* **RAG explainer** (on inspect):
  The assistant fetches a few citations and returns a **verdict** with **bullets** explaining the fields and common pitfalls, referencing your authoritative docs (Chroma).
* **Modify** (optional, when enabled):
  Provide the relevant field(s) for the selected object (e.g., `watts_per_floor_area` for lights). The assistant calls the **modify** tool and **automatically re-inspects** the same `{object, zone}` to confirm the change, posting a **Before → After** diff and a fresh RAG verdict.

> The UI can expose **only** the tools you allow (e.g., *inspect* tools only). The underlying MCP tool catalog is **masked** — the LLM never sees or can call anything else. This is a core design decision for **safety** and **predictability**.

### 3) Supportive, simple external APIs (optional)

These are not “data sources” for loads but **context helpers** you can toggle on:

* **Google Maps Embed** — interactive maps in the chat (key required; **free** to use). ([Google for Developers][2])
* **Sunrise-Sunset** — free API for civil sunrise/sunset; useful context for lighting discussions (requires attribution). ([Sunrise-Sunset.org][5])
* **Nager.Date Holidays** — free API to flag weekday-like occupancy on national holidays. ([Nager.Date][6])
* **Open-Meteo** — optional solar/cloud cover context (no key). *(You can add this later.)*

> If you prefer keyless maps, use **Leaflet + OSM tiles** (respect usage/policies) or OSM static map images for snapshots. ([OpenStreetMap][3])

---

## Decisions made related to the implementation

### A) **Strict startup gate:** IDF upload → auto MCP bootstrap

* Immediately run: `load_idf_model`, `get_model_summary`, `list_zones` (or parse zones).
* Persist a single **MCP session** per UI session — don’t spin containers per click.
* Save IDFs beneath `assignment_chat/data/idfs/` and pass `/idfs/<name>` to MCP.

**Rationale:** Lower latency; deterministic setup; fewer moving parts on each action.

---

### B) **Tool exposure is masked (policy-driven)**

* At dialogue time, the LLM only sees the **two inspect tools** (and later, only the modifies you explicitly add).
* All other MCP tools remain invisible to the LLM.
* The UI injects the **idf_path** automatically; the LLM only supplies `zone` (and, for modify, the relevant fields).
* Arguments are validated against the tool’s expected schema before calling MCP.

**Rationale:** Flexibility of LLM tool routing **without** exposing risky tools (e.g., schedule modification). This matches MCP’s design (client curates capabilities and routes calls). ([Model Context Protocol][1])

---

### C) **People / Lights / Equipment only; schedules read-only**

* The app surfaces schedule **names** read-only for context (e.g., “Lights schedule: …”) but never offers schedule changes in the UI.
* Any attempt by the user to modify schedules is refused with a short explanation and a redirect to supported actions.

**Rationale:** Keeps scope tight; avoids the “resolved schedule values” gap; focuses the assistant on internal load magnitudes and fractions.

---

### D) **RAG on every inspection (and after modify)**

* After any **inspect**, the app calls RAG to produce a short, cited explanation:

  * ✅/**⚠️**/**❌** verdict,
  * 2–4 bullets,
  * links/refs to your **embedded** EnergyPlus/standard docs.
* After any **modify**, the app **re-inspects** and follows with a **post-change** RAG verdict.

**Rationale:** Users get a short, trustworthy “why” every time — grounded by your own curated material (Chroma). (Chroma is a lightweight, local vector database designed for retrieval. ([Chroma Docs][4]))

---

### E) **Maps: Google Embed by default, with key hygiene**

* **Maps Embed API** is free, but an **API key is required** on every request; we recommend **HTTP referrer** restrictions (e.g., `http://localhost/*`) and **API restriction** to **Maps Embed API**. ([Google for Developers][2])
* The key is read from your local secrets file (e.g., `.secrets`) and **never** logged.
* If no key is available or no lat/lon in the summary, the map panel degrades gracefully.

**Alternative:** OSM static images (keyless) for light usage or reports — be mindful of community server usage policies. ([OpenStreetMap][3])

---

### F) **Gradio as the UI framework**

* We use **Gradio Blocks** to compose a small app: upload → summary → map → controls → chat. (Blocks = low-level API for custom layouts and event wiring.) ([Gradio][7])
* Core components: `File/UploadButton` (upload), `HTML` (map iframe), `Dropdown/Radio/Number` (actions), and `Chatbot` for the transcript. ([Gradio][8])
* The MCP session handle and model paths live in a `gr.State` object.

**Rationale:** Single-file deployability; easy events; no frontend JS required.

---

### G) **No PDFs**

Per your decision, **PDF generation is out of scope**. The design keeps the structure (change log, map URLs) so a report generator can be added later without touching the core flow.

---

## End-to-end user flow (what users experience)

1. **Open the app** → a short intro and a single **Upload IDF** control.
2. **Upload** → the app auto-runs MCP **summary**, then shows:

   * a chat message with site/zone summary,
   * a **city-level interactive map** centered at site lat/lon.
3. **Choose** `{zone, object, action}` (or type your instruction in chat).
4. **Inspect** → the app calls the relevant inspect tool, shows a table, then a short, cited **RAG** explainer.
5. **Modify** (when enabled) → app applies the change, **re-inspects** automatically, shows a **Before → After** diff, and a post-change RAG verdict.
6. **Repeat** for more zones/objects. The MCP session persists while the app runs.

---

## Operations & deployment notes

* **MCP server**: we keep one container/session per user session (volume-mounting `…/data/idfs → /idfs`). The session is closed on app teardown.
* **Secrets**:

  * `GOOGLE_MAPS_EMBED_KEY` lives in a local, untracked file (e.g., `.secrets`), restricted by **HTTP referrer** and **API** to **Maps Embed** only. ([Google for Developers][9])
* **Network**: Map embeds load from Google; optional helpers (Sunrise-Sunset, Nager.Date) reach public APIs. ([Sunrise-Sunset.org][5])
* **Caching**: You may cache OSM static images if you enable them (respect policy). ([OpenStreetMap][3])
* **Performance**: Keep the MCP session warm. Avoid container restarts between actions.

---

## Limitations / non-goals

* No **schedule value** resolution (8760) inside this app; schedule names appear read-only.
* No HVAC loop validation or autosizing checks.
* No batch simulations; we focus on “inspect/modify internal loads” plus explanations.

---

## Roadmap (optional)

* Add `modify_*` tools to the LLM tool manifest (behind a toggle), with automatic **post-modify inspect** & diff.
* Add small, **keyless** context APIs (Sunrise-Sunset, Nager.Date) to annotate anomalies. ([Sunrise-Sunset.org][5])
* Optional: show **Open-Meteo** cloud/irradiance context near the map. *(Great for lighting justifications.)*
* Optional: switch the map provider or add a **Leaflet + OSM** panel for keyless interactive maps. ([OpenStreetMap][10])

---

## References

* **Model Context Protocol (MCP):** Architecture & client docs. ([Model Context Protocol][1])
* **EnergyPlus-MCP** (project context; public materials): ([ScienceDirect][11])
* **Gradio Blocks / File / UploadButton:** ([Gradio][7])
* **Chroma (vector store):** introduction & getting started. ([Chroma Docs][4])
* **Google Maps Embed API:** usage & key setup. ([Google for Developers][2])
* **OpenStreetMap static map images & context:** ([OpenStreetMap][3])
* **Sunrise-Sunset API:** free, attribution required. ([Sunrise-Sunset.org][5])
* **Nager.Date public holidays API:** ([Nager.Date][6])

---

### TL;DR

Upload an IDF → the app **loads & summarizes** via MCP → shows an **interactive map** → you **inspect** internal loads (and optionally **modify**, with automatic re-inspect) — every inspection comes with a **short, cited RAG explanation** to help modelers understand what they’re seeing.

[1]: https://modelcontextprotocol.io/docs/learn/architecture?utm_source=chatgpt.com "Architecture overview"
[2]: https://developers.google.com/maps/documentation/embed/usage-and-billing?utm_source=chatgpt.com "Maps Embed API Usage and Billing"
[3]: https://wiki.openstreetmap.org/wiki/Static_map_images?utm_source=chatgpt.com "Static map images"
[4]: https://docs.trychroma.com/?utm_source=chatgpt.com "Chroma Docs: Introduction"
[5]: https://sunrise-sunset.org/api?utm_source=chatgpt.com "Sunset and sunrise times API"
[6]: https://date.nager.at/API?utm_source=chatgpt.com "Public Holiday API - Nager.Date"
[7]: https://www.gradio.app/docs/gradio/blocks?utm_source=chatgpt.com "Gradio Blocks Docs"
[8]: https://www.gradio.app/docs/gradio/file?utm_source=chatgpt.com "File"
[9]: https://developers.google.com/maps/documentation/embed/get-api-key?utm_source=chatgpt.com "Set up the Maps Embed API"
[10]: https://www.openstreetmap.org/?utm_source=chatgpt.com "OpenStreetMap"
[11]: https://www.sciencedirect.com/science/article/pii/S2352711025003334?utm_source=chatgpt.com "EnergyPlus-MCP: A model-context-protocol server for ai- ..."