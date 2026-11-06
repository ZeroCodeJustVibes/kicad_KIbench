# KiBench — KiCad Plugin Design

Nov 5, 2026 · @Nik

## Overview

Design Assist is one dedicated window next to KiCad that takes a part from idea to placed symbol: find it, read its datasheet, get its symbol and footprint, and build what is missing. An optional chat agent recommends parts using your schematic's context. Every import is deterministic: the AI suggests, but only the algorithmic pipeline writes library files.

**Goals**

- Fast: search results in under 1 s from cache, under 3 s live
- One flow: search → datasheet → symbol/footprint → import, without leaving the window
- Fallbacks at every step: online libraries → KiCad stock footprints → parametric builder
- Design-aware chat: "low-side MOSFET for 12 V, logic-level from 3.3 V" returns real, orderable parts as clickable cards

**Non-goals**

- No AI-generated symbols or footprints, and no AI writes to libraries
- No schematic or PCB editing beyond placing a chosen symbol
- No BOM costing or ordering (out of scope for v1)

## Window layout

One window, three columns: results on the left, the selected part in the middle, chat on the right. Clicking a result or a chat card's Find button opens that part in the middle column.

&#91;embedded content: window layout · results, part detail, chat\]

The middle column's tabs follow the workflow: **Datasheet** (viewer), **Sources** (who has symbol, footprint and 3D), **Footprint** (stock matches with diff), **Build** (parametric builder). The chat column can collapse for pure search use, and Ctrl+K focuses the search box from anywhere.

## Part search

One search box queries all enabled providers in parallel and merges the results by manufacturer part number (MPN). Typing "tl074", "C12594" or "10k 0603 1%" all work: the query parser detects LCSC codes, MPNs, and parametric phrases.

**Result row:** MPN · manufacturer · package · key specs · stock/price (best source) · CAD badges (Symbol / Footprint / 3D, and which source has them) · datasheet icon.

**Filters:** category, package, in stock, JLC Basic/Extended, has CAD data, plus parametric filters when the provider supports them (Vds ≥ 30 V, Rds(on) ≤ 10 mΩ).

| Provider | Gives | Access |
| --- | --- | --- |
| LCSC / JLCPCB (EasyEDA API) | Parts, stock, price, datasheet, symbol + footprint + 3D | No key |
| Mouser | Parts, stock, price, datasheet | Free API key |
| DigiKey | Parts, parametrics, datasheet | Free OAuth app |
| Nexar / Octopart | Cross-distributor aggregation | Key, limited free tier |
| SnapMagic, Ultra Librarian, SamacSys | CAD availability per MPN | See sourcing section |

Each provider is a plugin class with `search(query, filters)` and `get(mpn)`, so adding one is a single file. Results and datasheets are cached in a local SQLite database, keyed by MPN, with a 24 h expiry for stock and price.

**Datasheet:** a click opens the PDF in a built-in viewer tab (cached to disk), with page search. Pinout and package-drawing pages are bookmarked when they can be detected by text ("Pin Configuration", "Package Outline").

## Symbol and footprint sourcing

For a selected part, Design Assist walks a fixed cascade and stops at the first source that has data, but shows all hits so you can pick another.

1. **Already in your libraries:** match by MPN field or LCSC field in project and global libraries.
2. **LCSC/EasyEDA:** direct fetch and convert (symbol, footprint, STEP), same approach as JLCImport/easyeda2kicad.
3. **SnapMagic:** search via its plugin API if a key is granted; otherwise open the part page in the browser.
4. **Ultra Librarian / SamacSys (Component Search Engine):** no open search API, so open the part page pre-filled with the MPN.
5. **KiCad stock footprint:** the default matcher (next section) proposes a standard footprint, paired with a generic or KiCad stock symbol.
6. **Build helper:** parametric footprint plus pin-table symbol.

**Download watcher:** for steps 3–4, the window watches your Downloads folder. When a ZIP for the active MPN arrives, it is unpacked and imported automatically (the impartGUI approach), so a manual download is one click on the website.

**Import target:** a project library `DesignAssist.kicad_sym`, `DesignAssist.pretty`, and `DesignAssist.3dshapes`, registered in the project's `sym-lib-table` and `fp-lib-table`. A global library is a setting. Every imported symbol gets fields: MPN, Manufacturer, Datasheet, LCSC/Mouser/DigiKey numbers, Source, Fetch date.

**Verification view** before import: footprint preview next to the datasheet's package-drawing page, pad count vs pin count check, pitch and body size shown, plus a 3D model alignment check. You confirm, then it writes.

## Default footprint matcher

The matcher turns a package description like "BGA-256, 1.0 mm, 17×17" into a ranked list of KiCad stock footprints, using parsed dimensions rather than text similarity.

**Index (built once, refreshed on KiCad update):** parse every footprint name in the stock `.pretty` libraries into fields. KiCad's naming convention encodes them, e.g. `Package_BGA:BGA-256_17.0x17.0mm_Layout16x16_P1.0mm` → family BGA, pins 256, body 17.0×17.0 mm, grid 16×16, pitch 1.0 mm. Same for QFN (exposed pad size), SOIC, TSSOP, SOT, DFN, QFP, headers and connectors. Fall back to reading pad data from the `.kicad_mod` file when the name lacks a field.

**Query sources:** the package field from the distributor ("LQFP-48\_7x7x05P"), the datasheet's package section, or typed by hand ("bga256 1mm").

**Scoring:** pin count and family must match; then pitch (exact), body size (±0.1 mm), exposed pad, and variant (hand-solder, thermal vias) rank the rest. The top hit shows with what matched and what differs, e.g. "pitch ✓, body ✓, EP 5.6 vs 5.3 mm ✗".

**Speed:** the index is a few thousand rows in SQLite, so matching is instant. One click assigns the footprint to the symbol being imported.

## Build helper

When no source has the part, the build helper makes the footprint from a few datasheet numbers and the symbol from a pin table. Both are generated by fixed rules, not by AI.

**Footprint builder**

- Pick a family template: BGA, QFN/DFN, QFP, SOIC/TSSOP, SOT, two-terminal chip, through-hole header, generic connector
- Enter the datasheet dimensions in a form laid out like the package drawing: pitch, pin count, body D×E, lead span, lead width/length (min/nom/max), exposed pad
- Pads are computed with IPC-7351 rules at a chosen density level (A/B/C); a live preview updates as you type
- BGA extras: depopulated balls by pattern or list, ball naming A1… with skipped letters (I, O, Q, S, X, Z)
- Generated with KicadModTree (the library behind kicad-footprint-generator), so silkscreen, courtyard and fab layer follow KiCad library conventions

**Symbol builder**

- Pin table: number, name, type (power in, I/O, passive…), side, group
- Fill it by pasting from the datasheet PDF (the viewer's text selection), from a CSV, or from the vendor's pin file
- Auto-layout: power pins up, ground down, inputs left, outputs right, grouped by name prefix (PA0…, GPIO…)
- Multi-unit split for large packages (e.g. one unit per bank for a BGA-256 FPGA)

**Checks before save:** pin count equals pad count, every pin number exists on the footprint, no duplicate numbers.

## Chat agent

The chat panel answers "which part does X" with real parts from the search providers, shown as clickable cards. The model only ever sees and recommends parts that the search tools actually returned, so it cannot invent MPNs.

**Model providers** (one interface, chosen in settings):

| Provider | API | Notes |
| --- | --- | --- |
| Anthropic (Claude) | Messages API, native tool use | Best tool-use quality |
| OpenRouter | OpenAI-compatible chat + tools | Any model, one key |
| Local (Ollama, LM Studio) | OpenAI-compatible endpoint | Offline; weaker tool use |

**Design context** is built from the open project and sent as a compact summary:

- Power rails from net and label names (+3V3, +5V, VBAT, 12V) with parsed voltages
- Existing parts: reference, value, MPN, footprint (so it can reuse a package or a part family)
- Interfaces inferred from labels (SDA/SCL, SPI, USB\_D+/D−, CAN)
- A **Design constraints** box you edit: input range, max current, temperature, assembly house (JLC Basic preferred), size limits
- The current selection in KiCad, when you ask "replace this"

**Tools the agent can call** (read-only, no file writes):

- `search_parts(query, filters)` and `get_part(mpn)`: the same providers as the search panel
- `get_design_context()`: rails, parts, constraints
- `get_datasheet_text(mpn, pages?)`: extracted text, so it can check absolute max ratings and pinouts
- `match_footprint(package)`: the default matcher
- `check_cad_availability(mpn)`: which sources have symbol and footprint

**Part cards** in the chat show MPN, why it fits ("Vgs(th) 1.2 V, OK for 3V3 gate drive"), stock and price, and CAD badges. Buttons: **Open datasheet**, **Show in search**, **Get symbol/footprint** (starts the deterministic cascade), **Compare** (adds it to a side-by-side table).

**Guardrails:** every number in a card comes from tool results, not model text; the agent must cite the datasheet page for claims about limits; API keys are stored in the OS keyring.

## Architecture and KiCad integration

Design Assist runs as a separate Python process next to KiCad, so network calls never freeze the editor. Only the importer writes to KiCad; the chat agent reaches everything through read-only tools.

&#91;embedded content: architecture · window, core services, external services, KiCad\]

**KiCad integration.** KiCad's IPC API (kicad-python) started in the PCB editor, and schematic support is still limited in KiCad 10 ([KiCad dev docs](https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/for-addon-developers/), [adom capability notes](https://wiki.adom.inc/adom/kicad-bridge/files/capabilities.py)). So design context comes from parsing `.kicad_sch` files read-only, refreshed on save. The IPC API supplies PCB selection and footprint data. Placing a symbol stays manual: it appears under DesignAssist in KiCad's Add Symbol chooser.

**Packaging.** A PCM plugin adds a toolbar button that launches the standalone window and passes the open project path.

**Stack.** Python 3.11+, PySide6, httpx (async provider calls), SQLite, PyMuPDF or QtPdf for datasheets, KicadModTree for footprints, kiutils or sexpdata for KiCad files, keyring for API keys.

## Milestones

Build in five phases, each usable on its own; the chat comes last because it depends on everything before it.

1. **Search + datasheet:** LCSC and Mouser providers, merged results, cache, PDF viewer.
2. **Import:** LCSC/EasyEDA conversion, project library writing, field mapping, verification view.
3. **Matcher + watcher:** stock footprint index and scoring; Downloads watcher for SnapMagic, Ultra Librarian and SamacSys ZIPs.
4. **Build helper:** footprint templates (QFN, SOIC, BGA first), pin-table symbol builder.
5. **Chat agent:** provider abstraction, design context, tools, part cards, compare table.

## Open questions

- [ ] Does SnapMagic grant an API key for a free KiCad tool, or does v1 rely on the browser + watcher?
  - [ ] need to check. api keys for this should be purely optional.
- [ ] Target KiCad 10 only, or keep KiCad 9 support (no schematic IPC on either; file parsing works on both)?
  - [ ] only kicad 10.
- [ ] Qt (PySide6) for a modern UI vs wxPython to match KiCad and avoid a second toolkit?
  - [ ] -> explain what this means. as long as its compatble. modern is better.
- [ ] Ship as a PCM plugin with a launcher, or as a standalone app that finds the open project? -> should load as a plugin in kicad and get ther own icon in the kicad main view when opening the app. the project exploere thing. and listed under tools
- [ ] Which DigiKey and Nexar tiers are needed for parametric search volume?
- [ ] License for code derived from easyeda2kicad or JLCImport (AGPL vs MIT implications). -> my license should be opensource
