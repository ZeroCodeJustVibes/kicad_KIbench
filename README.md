# KiBench

KiBench is one window next to KiCad 10 that takes a part from idea to placed symbol. You find the part, read its
datasheet, get its symbol, footprint and 3D model, and build whatever is missing. An optional chat agent
recommends real, orderable parts using your schematic's context. **Only the deterministic pipeline writes library
files.** The AI suggests; it never generates symbols or footprints.

Built from `KiBench — KiCad Plugin Design.md` (all five milestones).

```
┌ Results ─────────────┬ Part ───────────────────────────────┬ Chat ──────────────┐
│ search box  [Ctrl+K] │ MPN · manufacturer · offers          │ design constraints │
│ filters + parametric │ [Datasheet][Sources][Footprint][Build]│ messages           │
│ merged results table │                                      │ part cards         │
└──────────────────────┴──────────────────────────────────────┴────────────────────┘
```

## Install

### As a KiCad plugin (toolbar button in the schematic and PCB editors)

```bash
python3 scripts/install_plugin.py          # copy into ~/.local/share/kicad/10.0/plugins/
python3 scripts/install_plugin.py --dev    # symlink instead (code edits apply immediately)
```

Or build a package for **Plugin and Content Manager → Install from File…**:

```bash
python3 scripts/build_pcm.py               # → dist/kibench-pcm-<version>.zip
```

Then, once:

1. KiCad → **Preferences → Plugins**: tick **Enable KiCad API** (`api.enable_server` is off by default).
   KiCad uses the Python interpreter configured there (`/usr/bin/python3` here) to build the plugin's virtual
   environment from `kicad_plugin/requirements.txt`. The first launch takes a minute while it installs PySide6.
2. Restart KiCad and open the schematic or PCB editor: the KiBench button is at the right end of the top toolbar.
   KiCad 10's project manager has no plugin toolbar slot (the `project_manager` scope is declared for future versions).
   A toolbar only shows plugins whose environment was ready when the editor opened, so reopen the editor after the first setup.
   Clicking it again raises the existing window and switches it to the current project.

### Standalone

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/kibench --project path/to/project      # or run it inside a project folder
```

### Chat with your Claude Pro/Max plan (no API key)

Settings → Chat → Provider **claude-code**. The chat box then runs your installed Claude Code (`claude -p`) in the
background, signed in with your own account. Its only tools are KiBench's read-only tools (no shell, file or web
access). Part cards and follow-up questions work as with the other providers. Install Claude Code and run `claude`
once to sign in first. *Claude Code model* is optional (e.g. `sonnet`); empty uses your plan's default.

The same tools also work in Claude Code in a terminal:

```bash
.venv/bin/python -m kibench.mcp_server --register     # or Settings → Chat → Connect Claude Code
cd path/to/kicad/project && claude                    # "find a 3.3 V LDO for this board that JLC stocks"
```

Tools: `search_parts`, `get_part`, `get_design_context`, `get_datasheet_text`, `match_footprint`,
`check_cad_availability`, `set_project`, and `open_in_kibench`, which shows the part in the KiBench window so you can
check and import it there. The server is plain JSON-RPC over stdio, so it needs no extra packages.

### Moving a part through the workflow

- **Sources tab:** click any source for a menu: *Import* (symbol + footprint + 3D, checked before writing) or *Open in
  Builder* (edit or complete it first). The best source (★, the most complete one) is downloaded and previewed right
  away, and also loaded into the Build tab.
- **Build tab:** a downloaded footprint and symbol appear as *From source*. A saved build keeps the source's 3D model
  and attribution. Pick a template to rebuild the footprint from datasheet dimensions instead.
  - *Package drawing* opens the datasheet's outline page next to the form.
  - *Pins from datasheet* reads the pinout page into the pin table (you confirm the rows).
  - *Ask chat for dimensions* has the chat read the dimension table, citing pages.
- **Shortcuts:** chat cards have *Import / Build / Datasheet / Search / Compare*. Right-click a search result for
  *Import*, *Open in Builder*, *Ask chat*, *Find alternatives* or *Copy MPN/LCSC*. In the chat, Enter sends and
  Shift+Enter adds a new line.
- **3D preview:** the Sources preview, the builder (2D | 3D) and the import check show the 3D model over the pads.
  STEP files are tessellated with OpenCascade via `cascadio`. Drag to rotate.
- **Pin table:** sort by clicking a column header. Selecting a pin highlights it in the footprint, 3D and symbol views.
- **Import & place / Save & place:** after writing to the library, the symbol goes on the clipboard in KiCad's schematic
  format. Press Ctrl+V in the schematic editor and click to place it. KiCad 10's API can't start an interactive symbol
  placement, so the clipboard does it.
- **Where imports go:** Settings → Library → *Save imports to* (this project, or one global library). The Sources
  preview and the builder show the target.
- **Design context:** the chat's fold-out lists the parts, rails and interfaces read from the schematic. The agent checks
  its picks against them.
- **PySide6 is pinned below 6.12:** 6.12.0 releases a reference to `None` on every QPainter call, which crashes
  Python < 3.12 (KiCad's plugin Python is often 3.10).
- **Theme:** follows the OS light/dark setting. Set `KIBENCH_THEME=dark` or `light` to override it.

### Check & Clean: schematic and board (toolbar → *Check && Clean*, Ctrl+Shift+C)

**Check** (read only) runs KiCad's ERC through `kicad-cli`, then:

- label checks: named nets that reach only one pin, names that differ only in spelling (`+3V3` / `3V3`), several
  names for the same rail voltage, I2C lines without a pull-up;
- voltage ratings: every net gets a voltage from its name (`+3V3`, `12V`, `GND`), from the design constraints' input
  range (`VIN`, `VBUS`...), or from a value you type into the *Net voltages* table (saved in
  `kibench-check.json`). Parts get ratings from fields (`Voltage`, `Vds`, `Vin max`...), capacitor values
  (`10u 16V`), and with *Look up ratings online* from distributor specs. A part whose pins see more than its rating is
  an error; a capacitor above 80 % of its rating is a warning;
- *Ask AI to review* sends it to the chat. The agent calls the new `check_schematic` tool (also available to Claude
  Code through the connector) and compares datasheet absolute maximum ratings with the volts on each part's pins.

**Clean up** proposes changes for one sheet: straighten diagonal wires (a crooked corner goes back to the spot that
makes its wires straight, otherwise a right-angle route), merge wire pieces, drop needless junction dots, line up
parts that are nearly in a row or column (whole grid steps, attached wires follow), and place reference / value texts
neatly next to the part. Every change is listed with a checkbox and shown on a before/after preview; untick what you
don't want, then *Apply*. Every step is checked against the sheet's connectivity while planning, and the whole result
against `kicad-cli`'s netlist before writing; the old sheet goes to `kibench-backups/`, and *Undo last apply* restores
it. KiCad 10's plugin API cannot edit schematics, so the file is rewritten: save in KiCad first and use
*File → Revert* afterwards.

**Board check** (read only, on the saved `.kicad_pcb`) runs KiCad's DRC with schematic parity through `kicad-cli`
(repeated messages grouped), then:

- voltage clearances: nets get voltages the same way as in the schematic check (the *Nets* table edits the same
  `kibench-check.json`); copper of nets more than 30 V apart is checked against IPC-2221B table 6-1 (B2 outer
  uncoated, B1 inner). Pads of one part are skipped (the package sets them) and zones are not included;
- silkscreen references: more than 2 mm from their part, on pads, overlapping each other, smaller than 0.8 mm /
  0.15 mm line, or hidden;
- parts outside the board outline, and decoupling capacitors (≤ 1 µF between ground and a rail) more than 5 mm from
  the IC supply pin they serve;
- clicking an issue zooms the board preview to it; *Ask AI to review* calls the new `check_board` tool (also for
  Claude Code) with every net's voltage, track widths and length, and each part's position, for track width vs.
  current and spacing reviews.

**Board tidy-up** proposes small, safe changes in the same checkbox table with a before/after preview:

- *Tidy references*: a reference that is on pads, on another text, over silkscreen lines, over the board edge or far
  from its part moves next to it (above, below, beside; its angle is kept when possible). References that already look
  fine are not touched, and a move never trades one problem for a worse kind;
- *Align parts* / *Even spacing*: identical parts that are almost in a row or column (≤ 0.5 mm) are lined up, and rows
  of three or more get equal gaps. Only parts that are not routed yet (no track or via on their pads) and not locked
  move, and only if that adds no courtyard overlap, keeps 0.2 mm to other copper and stays on the board.

*Apply* runs DRC on the result and on the original; if a problem type gets more frequent, the changes near the new
problems are left out automatically and DRC runs again. The old board goes to `kibench-backups/`; *Undo last apply*
restores it. When the board is open in KiCad's PCB editor and the API server is on, KiBench saves it there first and
reloads it after writing (KiCad's Ctrl+Z does not cover this step; use *Undo last apply*).

Dev mode, without KiCad's toolbar:

```bash
python3 scripts/install_plugin.py --dev                       # plugin = symlink to this checkout
.venv/bin/python -m kibench --project path/to/project --check  # opens the Check & Clean window directly
.venv/bin/python -m kibench.design.check path/to/project      # report in the terminal (--json for the agent's view)
.venv/bin/python -m kibench.design.clean path/to/sheet.kicad_sch            # list proposed changes
.venv/bin/python -m kibench.design.clean path/to/sheet.kicad_sch --write    # apply (backup + netlist check)
.venv/bin/python -m kibench --project path/to/project --check board         # open on a tab: check|clean|board|board-tidy
.venv/bin/python -m kibench.design.board_check path/to/project              # board report (--json, --no-drc)
.venv/bin/python -m kibench.design.board_tidy path/to/board.kicad_pcb       # list proposed changes
.venv/bin/python -m kibench.design.board_tidy path/to/board.kicad_pcb --write  # apply (backup + DRC compare)
```

`kicad-cli` is found on PATH, in a mounted KiCad AppImage, or via `KIBENCH_KICAD_CLI`.

## What's in each milestone

| Milestone | Where | Notes |
|---|---|---|
| 1. Search + datasheet | `kibench/search`, `kibench/providers`, `kibench/datasheet.py`, `ui/results.py`, `ui/datasheet_tab.py` | LCSC/JLCPCB (no key), Mouser, DigiKey, Nexar run in parallel and are merged by MPN. Same-MPN parts from *different* manufacturers stay separate. Query parser recognises LCSC codes, MPNs and phrases like `10k 0603 1%`. Parametric filters such as `Vds>=30V, Rds(on)<=10mΩ`. SQLite cache with 24 h expiry for stock and price (≈0.5 s live, ≈5 ms cached). PDF viewer with search and auto-bookmarks (pinout, absolute maximum, package outline, land pattern). |
| 2. Import | `library/easyeda.py`, `library/importer.py`, `library/project_lib.py`, `ui/verify_dialog.py` | EasyEDA → KiCad symbol (multi-unit), footprint and STEP. Writes `KiBench.kicad_sym`, `KiBench.pretty`, `KiBench.3dshapes` and registers them in the project's `sym-lib-table`/`fp-lib-table` (or a global library, set in Settings). Fields: MPN, Manufacturer, Datasheet, LCSC/Mouser/DigiKey, Source, Fetch date. A verification view shows the footprint next to the datasheet's package page and checks pins vs pads, pitch, body, courtyard and 3D model alignment. Nothing is written until you confirm. |
| 3. Matcher + watcher | `matcher/`, `library/watcher.py`, `ui/part_view.py` | Indexes all ~14.8k stock footprints by dimensions parsed from their names, falling back to pad data (≈14 s once; matching < 1 ms). Understands KiCad names, EasyEDA `SOIC-14_L8.7-W3.9-P1.27`, distributor `LQFP-48_7x7x05P` and free text `bga256 1mm`. Diff shown as `pitch ✓, body ✓, EP 3.6×3.6 vs 3.45×3.45 mm ✗`. The Downloads watcher imports SnapMagic, Ultra Librarian and SamacSys ZIPs (KiCad 6+ formats). |
| 4. Build helper | `builder/ipc7351.py`, `builder/footprints.py`, `builder/symbols.py`, `ui/build_tab.py` | Templates: QFN/DFN, SOIC/TSSOP/MSOP, BGA (depopulation, A1 naming that skips I O Q S X Z), QFP, SOT, chip R/C/L, THT header, generic connector. IPC-7351B lands at density A/B/C. Output lands within about 0.05 mm of KiCad's stock SOIC-8/QFN-32 pads, with identical BGA pads. Symbol builder takes a pin table (paste from the datasheet, CSV, or vendor pin files such as Xilinx), auto-lays out pins (power top, ground bottom, inputs left, outputs right, grouped by prefix) and splits large parts into one unit per bank. Checks run before saving. |
| 5. Chat agent | `agent/`, `ui/chat.py`, `design/context.py`, `kicad_ipc.py` | Providers: Anthropic (official `anthropic` SDK, default `claude-opus-5-5`), OpenRouter, local Ollama/LM Studio. Read-only tools: `search_parts`, `get_part`, `get_design_context`, `get_datasheet_text`, `match_footprint`, `check_cad_availability`, `recommend_parts`. Cards accept only MPNs that tools returned, fill stock, price and specs from tool data, highlight numbers in the reason that no tool returned, and flag limit claims without a datasheet citation. Card buttons: Open datasheet, Show in search, Get symbol/footprint, Compare. |

## Decisions on the open questions

- **SnapMagic key:** optional. Without one, KiBench opens the search page and the Downloads watcher imports the ZIP. With a key (Settings), it queries availability directly. That API call has **not** been tested against a real key.
- **KiCad version:** KiCad 10 only (paths, lib-table format, IPC API).
- **Qt vs wxPython:** KiBench runs as its *own process* next to KiCad, so it never loads into KiCad's wx event loop
  and the two toolkits never conflict. Qt (PySide6) gives a modern UI with a built-in PDF viewer and search, and it
  installs into the plugin's own venv. The cost is a ~200 MB download on first launch.
- **Packaging:** a KiCad 10 IPC plugin (`kicad_plugin/plugin.json`) with a button in the schematic and PCB editor toolbars. KiCad 10's project manager does not show plugin buttons yet; the `project_manager` scope is declared so it appears once KiCad supports it. It is also installable through the PCM. The project comes from the KiCad API, then from KiCad's open-project list, then from `--project`.
- **License:** GPL-3.0-or-later (same as KiCad). The EasyEDA converter is a clean-room implementation, so no AGPL code is included. Imported EasyEDA parts carry their source attribution in the `Source` field, as EasyEDA's library terms require.

## Deviations from the design document

- **Footprint generator:** self-contained instead of KicadModTree. It follows the same KLC conventions and avoids an unmaintained dependency in KiCad's venv.
- **Library name:** `KiBench` instead of `DesignAssist`. It can be changed in Settings.
- **Symbol placement:** stays manual, as designed. After an import the lib id is copied to the clipboard for KiCad's Add Symbol chooser.
- **Selection ("replace this"):** read through the IPC API (`get_selection_as_string`) from the schematic editor, falling back to the PCB editor.

## Tests

```bash
.venv/bin/python -m pytest -q              # 37 offline tests (fixtures in tests/data; kicad-cli ones skip without it)
KIBENCH_NET=1 .venv/bin/python -m pytest -q   # + live LCSC search
```

The tests cover query parsing, unit parsing, parametric filters, the EasyEDA converter on 6 real parts, every builder
template, the BGA naming, pin parsing and layout, the matcher, a full import into a temporary project (lib tables,
fields, re-import replacing in place, vendor ZIP) and the agent guardrails. Generated libraries were also loaded with
`kicad-cli sym/fp upgrade` from KiCad 10.0.8.

## Not verified yet

- Mouser, DigiKey and Nexar: written against their documented APIs but not run without keys.
- The live Claude/OpenRouter/local chat in the KiBench window: the agent loop was tested with a scripted model, without
  paid API calls. The Claude Code provider and connector were tested live.
- Check & Clean on your own designs: tested on KiCad's 90 demo sheets (connectivity unchanged on all of them, and
  kicad-cli netlists identical on the ones applied) and on a deliberately messy copy of `ecc83`, not yet on a live
  KiCad session with *File → Revert*.
- Board check & tidy-up: pad positions match KiCad's IPC-D-356 export on all 18 demo boards (front and back side);
  the tidy-up was applied to 5 demo boards with no new DRC problems. Saving and reloading a board that is open in
  KiCad through the API (`save` / `revert`) has not been run against a live PCB editor yet.
- Clicking the toolbar button inside a running KiCad: `plugin.json` validates against KiCad's schema and the plugin is installed, but it hasn't been launched from KiCad.
