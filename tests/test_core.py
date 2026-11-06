"""Offline tests for parsing, conversion, builders, matcher, import pipeline and agent guardrails.

Network tests are opt-in: KIBENCH_NET=1 pytest
"""
import asyncio
import json
import os
import zipfile
from pathlib import Path

import pytest

from kibench import sexpr
from kibench.builder import footprints as FB
from kibench.builder import symbols as SB
from kibench.design.context import parse_schematic, rail_voltage
from kibench.kicad.footprint import footprint_to_text, read_footprint
from kibench.kicad.symbol import library_text, read_library, set_properties_raw, symbol_to_text
from kibench.library import easyeda, importer
from kibench.library.project_lib import KiCadLibrary
from kibench.library.verify import check_pins_pads, estimate_pitch, verify
from kibench.matcher.parse import parse_kicad_name, parse_query as parse_pkg
from kibench.matcher.score import score_row
from kibench.models import Offer, Part
from kibench.search.query import Filters, parse_conditions, parse_query
from kibench.search.units import parse_value

DATA = Path(__file__).parent / "data"


def cad(code):
    return json.loads((DATA / f"easyeda_{code}.json").read_text())


# ------------------------------------------------------------------ search parsing
def test_query_kinds():
    assert parse_query("C12594").kind == "lcsc"
    assert parse_query("tl074").kind == "mpn"
    q = parse_query("10k 0603 1%")
    assert q.kind == "parametric" and q.category == "resistor" and q.package == "0603" and q.tolerance == 1.0
    assert parse_query("100nF 0402").value[1] == "F"


def test_units():
    assert parse_value("4k7") == (4700.0, "Ω")
    assert parse_value("3V3") == (3.3, "V")
    assert abs(parse_value("10 mΩ")[0] - 0.01) < 1e-12
    assert parse_value("100nF")[1] == "F"


def test_parametric_filter():
    p = Part("X", specs={"Drain Source Voltage (Vdss)": "30V", "RDS(on)": "18mΩ@4.5V"},
             offers=[Offer("LCSC", "C1", 10, [(1, 0.1)], jlc_class="Basic")])
    assert Filters(parametric="Vds>=30V, Rds(on)<=20mΩ").match(p)[0]
    assert not Filters(parametric="Vds>=40V").match(p)[0]
    assert Filters(jlc="Basic", in_stock=True).match(p)[0]
    assert len(parse_conditions("Vds >= 30 V; Id > 2A")) == 2


# ------------------------------------------------------------------ sexpr + symbol io
def test_sexpr_roundtrip_and_props():
    sym = SB.build_symbol("TEST", [SB.PinDef("1", "VCC"), SB.PinDef("2", "GND"), SB.PinDef("3", "OUT")])
    text = library_text([symbol_to_text(sym)])
    libs = read_library(text)
    assert libs[0].name == "TEST" and len(libs[0].pins) == 3
    raw = set_properties_raw(libs[0].raw, {"MPN": "ABC-1", "Value": "ABC-1"})
    again = read_library(library_text(["\t" + raw]))[0]
    assert again.properties["MPN"] == "ABC-1" and again.properties["Value"] == "ABC-1"
    assert sexpr.parse('(a "x\\"y" (b 1))')[1] == 'x"y'


# ------------------------------------------------------------------ EasyEDA conversion
@pytest.mark.parametrize("code", ["C12594", "C2040", "C165948", "C25804", "C20917", "C2337"])
def test_easyeda_pins_match_pads(code):
    c = cad(code)
    sym = easyeda.convert_symbol(c)
    fp, model = easyeda.convert_footprint(c)
    assert {p.number for p in sym.pins} == set(fp.numbered_pads())
    assert fp.layer_bbox(("F.CrtYd",))
    # writer output parses back
    assert len(read_footprint(footprint_to_text(fp)).pads) == len(fp.pads)
    assert read_library(library_text([symbol_to_text(sym)]))


def test_easyeda_multi_unit_and_geometry():
    sym = easyeda.convert_symbol(cad("C12594"))
    assert len(sym.units) == 4                      # TL074: four amplifiers
    fp, model = easyeda.convert_footprint(cad("C12594"))
    assert estimate_pitch(fp) == pytest.approx(1.27, abs=0.01)
    r = easyeda.convert_footprint(cad("C25804"))[0]
    x1, y1, x2, y2 = r.layer_bbox(("F.Fab",))
    assert (x2 - x1) == pytest.approx(1.6, abs=0.1)     # 0603 body length
    assert model is not None and model.uuid


def test_easyeda_bad_model_origin_reset():
    _, model = easyeda.convert_footprint(cad("C20917"))
    assert abs(model.offset[0]) < 5 and "origin reset" in model.title


# ------------------------------------------------------------------ builders
def test_builders_match_kicad_conventions():
    soic = FB.build("dual", {}, "B")
    p1 = next(p for p in soic.pads if p.number == "1")
    assert p1.x == pytest.approx(-2.475, abs=0.05) and p1.w == pytest.approx(1.95, abs=0.1)
    qfn = FB.build("qfn", {}, "B")
    assert len(qfn.numbered_pads()) == 33
    bga = FB.build("bga", {"rows": 16, "cols": 16, "depop": "center 4x4"}, "B")
    names = {p.number for p in bga.pads}
    assert len(names) == 240 and "A1" in names and "I1" not in names and "J1" in names
    for key in FB.TEMPLATES:
        fp = FB.build(key, {}, "B")
        assert read_footprint(footprint_to_text(fp)).pads


def test_bga_row_letters_skip():
    rows = FB.bga_row_names(22)
    # 20 letters remain after skipping I, O, Q, S, X, Z; then AA, AB, ...
    assert "I" not in rows and "O" not in rows and rows[19] == "Y" and rows[20] == "AA" and rows[21] == "AB"


def test_symbol_parsing_and_layout():
    pins = SB.parse_pin_text("1 VIN Power supply input\n2 GND Ground\n3 EN Enable\n4 FB Feedback\n5 SW Switch\n6 PGOOD Power good")
    assert [p.resolved_type() for p in pins] == ["power_in", "power_in", "input", "input", "output", "output"]
    sym = SB.build_symbol("X", pins)
    sides = {p.name: p.rot for p in sym.pins}
    assert sides["VIN"] == 270 and sides["GND"] == 90 and sides["EN"] == 0 and sides["SW"] == 180
    csv_pins = SB.parse_csv("Pin,Name,Type,Bank\nA1,GND,P,\nA2,IO_1,IO,14\nB1,IO_2,IO,15\n")
    assert [p.number for p in csv_pins] == ["A1", "A2", "B1"] and csv_pins[1].group == "14"
    multi = SB.build_symbol("F", csv_pins, split="group")
    assert len(multi.units) == 3


def test_checks_before_save():
    fp = FB.build("sot", {}, "B")
    good = [(str(i), f"P{i}", "passive") for i in range(1, 6)]
    assert all(c.ok for c in check_pins_pads(good, fp))
    bad = good[:-1] + [("9", "X", "passive")]
    assert not all(c.ok for c in check_pins_pads(bad, fp))


# ------------------------------------------------------------------ matcher
def test_matcher_parsing():
    s = parse_kicad_name("BGA-256_17.0x17.0mm_Layout16x16_P1.0mm")
    assert (s.group, s.pins, s.body_x, s.pitch, s.rows) == ("BGA", 256, 17.0, 1.0, 16)
    q = parse_pkg("LQFP-48_7x7x05P")
    assert (q.pins, q.body_x, q.pitch) == (48, 7.0, 0.5)
    q = parse_pkg("SOIC-14_L8.7-W3.9-P1.27-LS6.0-BL")
    assert (q.group, q.pins, q.pitch, q.body_x, q.body_y) == ("SOIC", 14, 1.27, 3.9, 8.7)
    q = parse_pkg("BGA-256, 1.0 mm, 17×17")
    assert (q.pins, q.pitch, q.body_x) == (256, 1.0, 17.0)


def test_matcher_scoring_prefers_exact():
    q = parse_pkg("QFN-32 5x5 0.5 EP3.45x3.45")
    row = lambda name: {**parse_kicad_name(name).to_row(), "lib": "L", "name": name, "path": "",
                        "grp": parse_kicad_name(name).group}
    a = score_row(q, row("QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm"))
    b = score_row(q, row("QFN-32-1EP_5x5mm_P0.5mm_EP3.6x3.6mm"))
    c = score_row(q, row("QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm_ThermalVias"))
    assert a.score > b.score and a.score > c.score
    assert "pitch" in a.explain()


# ------------------------------------------------------------------ import pipeline
def test_commit_lcsc_and_zip(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "demo.kicad_pro").write_text("{}")
    lib = KiCadLibrary(proj, "KiBench", proj)
    part = Part("TL074CDR", "Texas Instruments", offers=[Offer("LCSC", "C12594", 100, [(1, 0.25)])])
    prep = importer.prepare_lcsc(cad("C12594"), part, b"ISO-10303-21;\n")
    assert verify(prep).ok
    rec = importer.commit(prep, lib)
    assert rec.lib_id == "KiBench:TL074CDR"
    assert "KiBench" in (proj / "sym-lib-table").read_text() and "KiBench" in (proj / "fp-lib-table").read_text()
    syms = read_library((proj / "KiBench.kicad_sym").read_text())
    props = syms[0].properties
    assert props["MPN"] == "TL074CDR" and props["LCSC"] == "C12594" and props["Footprint"].startswith("KiBench:")
    assert "Fetch date" in props and "EasyEDA" in props["Source"]
    fp_text = next((proj / "KiBench.pretty").glob("*.kicad_mod")).read_text()
    assert "${KIPRJMOD}/KiBench.3dshapes/" in fp_text
    # re-import replaces instead of duplicating; table entries are not duplicated
    importer.commit(importer.prepare_lcsc(cad("C12594"), part), lib)
    assert len(read_library((proj / "KiBench.kicad_sym").read_text())) == 1
    assert (proj / "sym-lib-table").read_text().count('(name "KiBench")') == 1
    # vendor zip
    fp = FB.build("sot", {}, "B")
    sym = SB.build_symbol("LDO1", [SB.PinDef(str(i), f"P{i}") for i in range(1, 6)])
    z = tmp_path / "LIB_LDO1.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("LDO1/KiCad/LDO1.kicad_sym", library_text([symbol_to_text(sym)]))
        zf.writestr(f"LDO1/KiCad/{fp.name}.kicad_mod", footprint_to_text(fp))
    prep = importer.prepare_zip(z, "LDO1")
    assert prep.source == "SamacSys" and verify(prep).ok
    importer.commit(prep, lib)
    assert {s.name for s in read_library((proj / "KiBench.kicad_sym").read_text())} == {"TL074CDR", "LDO1"}


# ------------------------------------------------------------------ design context
def test_design_context(tmp_path):
    sch = tmp_path / "a.kicad_sch"
    sch.write_text('(kicad_sch (version 20250901)'
                   ' (symbol (lib_id "power:+3V3") (property "Reference" "#PWR01") (property "Value" "+3V3"))'
                   ' (symbol (lib_id "Device:R") (property "Reference" "R1") (property "Value" "10k")'
                   '  (property "Footprint" "Resistor_SMD:R_0603_1608Metric") (property "MPN" "RC0603FR-0710KL"))'
                   ' (label "SDA") (label "SCL") (global_label "12V"))')
    ctx = parse_schematic(sch)
    assert ctx.rails["+3V3"] == 3.3 and ctx.rails["12V"] == 12.0
    assert "I2C" in ctx.interfaces and ctx.parts[0].mpn == "RC0603FR-0710KL"
    assert rail_voltage("1V8") == 1.8


# ------------------------------------------------------------------ agent guardrails
def test_agent_cards_only_from_tool_results():
    from kibench.agent.tools import ToolBox
    tb = ToolBox(engine=None, context_fn=lambda: {})
    p = Part("AO3400A", specs={"Drain Source Voltage (Vdss)": "30V", "Gate Threshold Voltage": "1.45V"})
    tb._register([p])
    out = asyncio.run(tb.t_recommend_parts([
        {"mpn": "AO3400A", "reason": "Vds 30V, Vgs(th) 1.45V; 99A max"}, {"mpn": "NOPE123", "reason": "x"}]))
    assert out["shown"] == ["AO3400A"] and out["rejected"] == ["NOPE123"]
    card = tb.cards[0]
    assert card.unverified == ["99A"] and card.uncited


@pytest.mark.skipif(not os.environ.get("KIBENCH_NET"), reason="network test (set KIBENCH_NET=1)")
def test_live_lcsc_search():
    from kibench.config import Settings
    from kibench.search.engine import SearchEngine
    res = asyncio.run(SearchEngine(Settings()).search("C12594"))
    assert res.parts and res.parts[0].lcsc == "C12594"


def test_mcp_server_protocol(tmp_path):
    """Handshake, tool list and an offline tool call over the stdio transport."""
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "demo.kicad_pro").write_text("{}")
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "set_project", "arguments": {"path": str(tmp_path)}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "bogus", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 5, "method": "nope"},
    ]
    r = subprocess.run([sys.executable, str(root / "kibench" / "mcp_server.py")], cwd=tmp_path,
                       input="".join(json.dumps(m) + "\n" for m in reqs), capture_output=True, text=True, timeout=60)
    out = {m["id"]: m for m in map(json.loads, r.stdout.splitlines())}
    assert out[1]["result"]["protocolVersion"] == "2025-06-18"
    names = {t["name"] for t in out[2]["result"]["tools"]}
    assert {"search_parts", "get_datasheet_text", "open_in_kibench"} <= names and "recommend_parts" not in names
    assert json.loads(out[3]["result"]["content"][0]["text"])["project"] == str(tmp_path)
    assert out[4]["error"]["code"] == -32602 and out[5]["error"]["code"] == -32601


def test_claude_code_cards():
    from kibench.agent.claude_code import ClaudeCodeAgent
    from kibench.models import Offer, Part
    p = Part(mpn="AO3400A", manufacturer="AOS", offers=[Offer(source="LCSC", sku="C20917")])
    payload = json.dumps({"shown": ["AO3400A"], "cards": [{"part": p.to_dict(), "reason": "fits", "citations": [],
                                                           "unverified": ["9 V"], "uncited": False}]})
    cards = ClaudeCodeAgent._cards([{"type": "text", "text": payload}])
    assert cards[0].part.lcsc == "C20917" and cards[0].unverified == ["9 V"]
    assert ClaudeCodeAgent._cards("not json") == []


def test_package_bookmark_skips_tape_and_reel():
    from kibench.datasheet import detect_bookmarks
    texts = ["Intro", "PACKAGE MATERIALS INFORMATION\nTAPE AND REEL INFORMATION\nDevice Package Drawing Pins",
             "MECHANICAL DATA\nDIM MIN MAX\ne 1.27 BSC"]
    bm = {b.title: b.page for b in detect_bookmarks(texts)}
    assert bm["Package outline"] == 2


def test_builder_loads_downloaded_source():
    """A downloaded LCSC part goes into the builder as-is: source footprint, source symbol, 3 pins."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from kibench.library import importer
    from kibench.ui.build_tab import BuildTab
    from kibench.ui.state import AppState
    prep = importer.prepare_lcsc(cad("C20917"))
    tab = BuildTab(AppState(None))
    tab.load_source(prep)
    assert tab.uses_source_fp and tab.fp is prep.footprint
    assert tab.sym_mode.currentData() == "source" and len(tab.sym.pins) == 3
    tab.table.item(0, 1).setText("DRAIN")          # editing the pin table switches to a generated symbol
    tab.rebuild()
    assert tab.sym_mode.currentData() == "generated" and "DRAIN" in [p.name for p in tab.sym.pins]
    tab.clear_source()
    assert tab.tpl.findData("__source__") == -1
    app.processEvents()


def test_place_snippet_and_mesh(tmp_path):
    """Import & place clipboard text embeds the library symbol; the OBJ loader reads EasyEDA meshes."""
    from kibench.kicad.place import snippet
    from kibench.kicad.symbol import library_text
    from kibench.library import easyeda
    from kibench import model3d
    sym = easyeda.convert_symbol(cad("C20917"))
    from kibench.kicad.symbol import symbol_to_text
    text = library_text([symbol_to_text(sym)])
    sn = snippet(text, sym.name, "KiBench")
    assert sn.startswith("(lib_symbols") and f'(lib_id "KiBench:{sym.name}")' in sn
    assert sn.count('(pin "') == 3 and '(symbol "KiBench:' in sn
    obj = "newmtl a\nKd 1 0 0\nendmtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nv 1 1 0\nusemtl a\nf 1 2 3 4\n"
    m = model3d.load_obj(obj).transformed((1, 0, 0), (0, 0, 90))
    assert len(m.tris) == 2 and m.colors[0] == (1.0, 0.0, 0.0)
    (a, b, _), = m.tris[:1]
    assert abs(b[0] - 1) < 1e-9 and abs(b[1] + 1) < 1e-9      # (1,0,0) rotated -90° about z, then +1 in x


# ---------------------------------------------------------------------- schematic check & clean
_R_LIB = '''(symbol "Device:R" (pin_numbers (hide yes)) (pin_names (offset 0)) (in_bom yes) (on_board yes)
      (property "Reference" "R" (at 2.032 0 90) (effects (font (size 1.27 1.27))))
      (property "Value" "R" (at 0 0 90) (effects (font (size 1.27 1.27))))
      (symbol "R_0_1" (rectangle (start -1.016 -2.54) (end 1.016 2.54) (stroke (width 0.254) (type default)) (fill (type none))))
      (symbol "R_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -3.81 90) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))))'''


def _r(ref, x, y, rot=0, value="10k", fx=None, fy=None):
    fx = x + 2.54 if fx is None else fx
    fy = y if fy is None else fy
    return f'''  (symbol (lib_id "Device:R") (at {x} {y} {rot}) (unit 1) (in_bom yes) (on_board yes)
    (uuid "00000000-0000-0000-0000-{abs(hash(ref)) % 10**12:012d}")
    (property "Reference" "{ref}" (at {fx} {fy - 1.27} 0) (effects (font (size 1.27 1.27)) (justify left)))
    (property "Value" "{value}" (at {fx} {fy + 1.27} 0) (effects (font (size 1.27 1.27)) (justify left)))
    (pin "1" (uuid "11111111-0000-0000-0000-{abs(hash(ref + '1')) % 10**12:012d}"))
    (pin "2" (uuid "22222222-0000-0000-0000-{abs(hash(ref + '2')) % 10**12:012d}")))'''


def _wire(a, b, n):
    return f'  (wire (pts (xy {a[0]} {a[1]}) (xy {b[0]} {b[1]})) (stroke (width 0) (type default)) (uuid "w{n}"))'


def _sheet(*items):
    return ('(kicad_sch (version 20250114) (generator "eeschema")\n  (uuid "abcd")\n  (paper "A4")\n'
            f'  (lib_symbols {_R_LIB})\n' + "\n".join(items) + '\n  (sheet_instances (path "/" (page "1")))\n)\n')


def test_sheet_geometry_and_connectivity():
    from kibench.kicad.schematic import SheetModel
    # R1 vertical at (50,50): pins at (50,46.19) and (50,53.81); R2 rotated 90: pins left/right
    t = _sheet(_r("R1", 50, 50), _r("R2", 70, 46.19, 90), _wire((50, 46.19), (66.19, 46.19), 1))
    m = SheetModel(t)
    r1, r2 = m.symbols
    assert sorted(p.pos for p in r1.pins) == [(50.0, 46.19), (50.0, 53.81)]
    assert sorted(p.pos for p in r2.pins) == [(66.19, 46.19), (73.81, 46.19)]
    conn = m.connectivity()
    assert any(len(g) == 2 for g in conn)                    # R1.1 and R2.1 are joined by the wire
    # mirror is applied after rotation
    from kibench.kicad.schematic import transform
    assert transform(0, 3.81, (0, 0), 90, "x") == (-3.81, 0.0) or transform(0, 3.81, (0, 0), 90, "x")[1] == 0


def test_clean_straightens_tidies_and_keeps_connections():
    from kibench.design import clean
    from kibench.kicad.schematic import SheetModel
    # crooked corner at (56.9, 47.0) between R1.1 and R3.1; R2's texts far away
    t = _sheet(_r("R1", 50, 50), _r("R3", 60, 40), _r("R2", 80, 50, fx=95, fy=60),
               _wire((50, 46.19), (56.9, 47.0), 1), _wire((56.9, 47.0), (60, 43.81), 2))
    p = clean.plan(t)
    kinds = {c.kind for c in p.changes}
    assert "wire" in kinds and "fields" in kinds
    m2 = SheetModel(p.text)
    assert all(w.horizontal or w.vertical for w in m2.live_wires())
    assert m2.connectivity() == SheetModel(t).connectivity()
    r2 = next(s for s in m2.symbols if s.ref == "R2")
    assert r2.fields["Reference"].box().distance(r2.body) < 2.5
    # deselecting a change rebuilds a consistent plan without it
    wire_keys = {c.key for c in p.changes if c.kind == "wire"}
    p2 = clean.plan(t, exclude=wire_keys)
    assert not any(c.kind == "wire" for c in p2.changes)
    assert any(not (w.horizontal or w.vertical) for w in SheetModel(p2.text).live_wires())


def test_clean_align_moves_wires_along():
    from kibench.design import clean
    from kibench.kicad.schematic import SheetModel
    # R1 and R2 side by side, R2 1.27 mm lower; a horizontal wire joins their top pins
    t = _sheet(_r("R1", 50, 50), _r("R2", 60, 51.27), _wire((50, 46.19), (60, 47.46), 9))
    p = clean.plan(t, clean.CleanOptions(wires=False, merge=False, junctions=False, fields=False))
    assert [c.kind for c in p.changes] == []            # the joining wire is diagonal: aligning is refused
    t = _sheet(_r("R1", 50, 50), _r("R2", 60, 51.27), _r("R4", 70, 50),
               _wire((60, 47.46), (60, 40), 9))
    p = clean.plan(t, clean.CleanOptions(wires=False, merge=False, junctions=False, fields=False))
    assert [c.refs for c in p.changes] == [["R2"]]
    m2 = SheetModel(p.text)
    r2 = next(s for s in m2.symbols if s.ref == "R2")
    assert r2.pos == (60.0, 50.0)
    w = m2.live_wires()[0]
    assert {w.a, w.b} == {(60.0, 46.19), (60.0, 40.0)}  # the vertical wire just got longer


def test_check_voltages_and_labels():
    from kibench.design import check as chk
    from kibench.kicad.cli import Component, NetNode, Netlist
    assert chk.parse_volts("10uF 16V") == (None, 16.0)
    assert chk.parse_volts("1.8V ~ 5.5V") == (1.8, 5.5)
    assert chk.net_voltage("/power/+3V3", {}, None) == (3.3, "name")
    assert chk.net_voltage("GND", {}, None)[0] == 0.0
    assert chk.net_voltage("VIN", {}, 24.0) == (24.0, "constraints")
    assert chk.net_voltage("Net-(R1-Pad1)", {"Net-(R1-Pad1)": 48}, None) == (48, "override")
    r = chk.part_ratings("C3", "100n 16V", {})
    assert r and r[0].vmax == 16 and r[0].kind == "across"
    r = chk.part_ratings("U1", "LDO", {"Vin max": "6V"})
    assert r and r[0].kind == "supply" and r[0].vmax == 6
    rep = chk.Report()
    rep.parts = [chk.PartInfo("C3", "100n 14V", ratings=[chk.Rating("across", 14, None, "value")],
                              pins=[{"pin": "1", "net": "+12V", "volts": 12.0, "type": "passive", "function": ""},
                                    {"pin": "2", "net": "GND", "volts": 0.0, "type": "passive", "function": ""}]),
                 chk.PartInfo("C4", "100n 10V", ratings=[chk.Rating("across", 10, None, "value")],
                              pins=[{"pin": "1", "net": "+12V", "volts": 12.0}, {"pin": "2", "net": "GND", "volts": 0.0}]),
                 chk.PartInfo("U1", "LDO", ratings=[chk.Rating("supply", 6, None, "field")],
                              pins=[{"pin": "1", "net": "+12V", "volts": 12.0, "type": "power_in", "function": "VIN"}])]
    chk._check_voltages(rep)
    msgs = [(i.severity, i.refs[0]) for i in rep.issues]
    assert ("warning", "C3") in msgs and ("error", "C4") in msgs and ("error", "U1") in msgs
    nl = Netlist({"U2": Component("U2"), "R5": Component("R5")},
                 {"+3V3": [NetNode("U2", "1")], "SDA": [NetNode("U2", "5"), NetNode("R5", "1")],
                  "/sub/3V3": [NetNode("R5", "2")]})
    rep = chk.Report()
    rep.nets = [chk.NetInfo(n, *chk.net_voltage(n, {}, None)) for n in nl.nets]
    chk._check_labels(rep, nl)
    chk._check_interfaces(rep, nl, {n.name: n.volts for n in rep.nets})
    text = " | ".join(i.message for i in rep.issues)
    assert "Similar net names" in text and "only one pin" in text
    assert "pull-up" not in text                         # R5 pulls SDA to a 3V3 net


def test_check_tool_registered():
    from kibench.agent.tools import TOOL_SPECS, ToolBox
    assert "check_schematic" in [t["name"] for t in TOOL_SPECS]
    assert hasattr(ToolBox, "t_check_schematic")


def test_check_and_clean_on_demo_with_kicad_cli(tmp_path):
    from kibench.kicad import cli
    if not cli.find_kicad_cli():
        pytest.skip("kicad-cli not installed")
    import glob
    import shutil
    from kibench.design import check as chk, clean
    demo = sorted(glob.glob("/tmp/.mount_kicad*/share/kicad/demos/ecc83")) + \
        sorted(glob.glob("/usr/share/kicad/demos/ecc83"))
    if not demo:
        pytest.skip("KiCad demos not found")
    proj = tmp_path / "ecc83"
    shutil.copytree(demo[0], proj)
    sch = proj / "ecc83-pp.kicad_sch"
    sch.write_text(sch.read_text().replace('(property "Value" "10uF"', '(property "Value" "10uF 25V"', 1))
    chk.save_overrides(proj, {"Net-(P3-P1)": 250})
    rep = chk.run_check(proj)
    assert rep.erc_ran and any(i.category == "Voltage" and i.severity == "error" and "C1" in i.refs for i in rep.issues)
    p = clean.plan(sch.read_text())
    ok, msg = clean.verify(sch.read_text(), p.text, sch.name, proj)
    assert ok, msg
    bak = clean.write(sch, p.text, proj)
    assert bak.exists() and sch.read_text() == p.text


# ---------------------------------------------------------------------- board check & tidy
def _bfp(ref, x, y, rot=0, pads=(), ref_at=(0, -2), value="10k", lib="Lib:R_0805"):
    pads_s = "".join(
        f'(pad "{n}" smd rect (at {lx} {ly} {rot}) (size {w} {h}) (layers "F.Cu" "F.Mask") (net {net}))'
        for n, lx, ly, w, h, net in pads)
    return (f'(footprint "{lib}" (layer "F.Cu") (uuid "u-{ref}") (at {x} {y} {rot})\n'
            f'  (property "Reference" "{ref}" (at {ref_at[0]} {ref_at[1]} {rot}) (layer "F.SilkS") '
            f'(effects (font (size 1 1) (thickness 0.15))))\n'
            f'  (property "Value" "{value}" (at 0 2 {rot}) (layer "F.Fab") (effects (font (size 1 1) (thickness 0.15))))\n'
            f'  {pads_s}\n'
            f'  (fp_rect (start -1.7 -1) (end 1.7 1) (layer "F.CrtYd")))\n')


BNETS = {"GND": 1, "+48V": 2, "+3V3": 3, "N1": 4, "N2": 5}


def _bboard(*items, tracks=""):
    nets = "".join(f'(net {i} "{n}")' for n, i in BNETS.items())
    return ('(kicad_pcb (version 20241229) (generator "pcbnew")\n'
            '(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (5 "F.SilkS" user) (25 "Edge.Cuts" user))\n'
            f'(net 0 ""){nets}\n(gr_rect (start 0 0) (end 60 40) (layer "Edge.Cuts"))\n'
            + "".join(items) + tracks + ")\n")


def _br(ref, x, y, n1="N1", n2="N2", **kw):
    return _bfp(ref, x, y, pads=[("1", -1, 0, 0.9, 1.0, BNETS[n1]), ("2", 1, 0, 0.9, 1.0, BNETS[n2])], **kw)


def test_board_model_transform_and_roundtrip():
    from kibench.kicad.board import BoardModel
    text = _bboard(_br("R1", 10, 10, rot=90, ref_at=(0, -2)))
    m = BoardModel(text)
    assert m.render() == text
    fp = m.footprints[0]
    p1 = fp.pads[0]
    assert abs(p1.pos[0] - 10) < 1e-9 and abs(p1.pos[1] - 11) < 1e-9      # local (-1, 0) rotated 90° ccw
    t = fp.reference
    assert abs(t.pos[0] - 8) < 1e-9 and abs(t.pos[1] - 10) < 1e-9
    t.pos = (12.0, 10.0)
    m2 = BoardModel(m.render())
    assert m2.footprints[0].reference.pos == (12.0, 10.0)
    assert m2.footprints[0].pads[0].pos == p1.pos


def test_board_tidy_refs_align_and_routed():
    from kibench.design import board_tidy as bt
    text = _bboard(_br("R1", 10, 10, ref_at=(0, 0)),                  # reference right on its pads
                  _br("R2", 18, 10.3), _br("R3", 26, 10),             # R2 slightly out of the row
                  _br("R4", 10, 25), _br("R5", 16, 25.3),             # R4 is routed: R5 lines up with it
                  tracks='(segment (start 9 25) (end 9 30) (width 0.25) (layer "F.Cu") (net 4))')
    p = bt.plan(text)
    keys = {c.key for c in p.changes}
    assert "refs:u-R1" in keys
    assert "align:u-R2:y" in keys and "align:u-R5:y" in keys
    assert not any(k.startswith("align:u-R4") or k.startswith("align:u-R1") or k.startswith("align:u-R3")
                   for k in keys)
    m = p.model.by_ref()
    assert abs(m["R2"].pos[1] - 10) < 1e-9 and abs(m["R5"].pos[1] - 25) < 1e-9
    r1 = m["R1"]
    assert not any(pd.box().overlaps(r1.reference.box()) for pd in r1.pads)
    # an unticked change stays out, the rest stays the same
    p2 = bt.plan(text, exclude={"align:u-R2:y"})
    assert {c.key for c in p2.changes} == keys - {"align:u-R2:y"}
    assert abs(p2.model.by_ref()["R2"].pos[1] - 10.3) < 1e-9


def test_board_check_clearance_and_decoupling(tmp_path):
    from kibench.design import board_check as bc
    assert abs(bc.cap_farads("100n") - 1e-7) < 1e-15 and abs(bc.cap_farads("4u7 16V") - 4.7e-6) < 1e-12
    assert bc.cap_farads("0.1uF") == pytest.approx(1e-7)
    assert bc.required_spacing(48, True) == 0.6 and bc.required_spacing(48, False) == 0.1
    assert bc.required_spacing(1000, True) == pytest.approx(5.0)
    u1 = _bfp("U1", 40, 20, lib="Lib:SOIC8", value="LM358",
             pads=[(str(i + 1), -2.5 + i, 0, 0.6, 1.5, BNETS["+3V3"] if i == 0 else BNETS["N1"]) for i in range(8)])
    c1 = _br("C1", 15, 20, "GND", "+3V3", value="100n")                 # 25 mm away from U1
    j1 = _bfp("J1", 30, 32, pads=[("1", 0, 0, 1, 1, BNETS["+48V"])], lib="Lib:Pad")
    gnd = '(segment (start 25 32.8) (end 35 32.8) (width 0.25) (layer "F.Cu") (net 1))'
    pcb = tmp_path / "t.kicad_pcb"
    pcb.write_text(_bboard(u1, c1, j1, tracks=gnd))
    rep = bc.run_board_check(tmp_path, pcb, overrides={}, drc=False)
    msgs = [(i.category, i.severity, i.message) for i in rep.issues]
    assert any(c == "Clearance" and s == "error" and "+48V" in m and "GND" in m for c, s, m in msgs), msgs
    assert any(c == "Decoupling" and "C1" in m and "U1" in m for c, s, m in msgs), msgs
    agent = rep.for_agent()
    assert any(n["name"] == "+48V" and n["volts"] == 48 for n in agent["nets"])
    assert {p["ref"] for p in agent["parts"]} >= {"U1", "C1", "J1"}


def test_board_tool_registered():
    from kibench.agent.tools import TOOL_SPECS, ToolBox
    assert "check_board" in [t["name"] for t in TOOL_SPECS]
    assert hasattr(ToolBox, "t_check_board")


def test_board_check_and_tidy_on_demo_with_kicad_cli(tmp_path):
    from kibench.kicad import cli
    if not cli.find_kicad_cli():
        pytest.skip("kicad-cli not installed")
    import glob
    import shutil
    from kibench.design import board_check as bc, board_tidy as bt
    demo = sorted(glob.glob("/tmp/.mount_kicad*/share/kicad/demos/pic_programmer")) + \
        sorted(glob.glob("/usr/share/kicad/demos/pic_programmer"))
    if not demo:
        pytest.skip("KiCad demos not found")
    d = tmp_path / "pic"
    shutil.copytree(demo[0], d)
    rep = bc.run_board_check(d, overrides={})
    assert rep.drc_ran and rep.footprints > 50
    board = d / "pic_programmer.kicad_pcb"
    text = board.read_text()
    p, ok, msg, _dropped = bt.plan_verified(text, board)
    assert ok, msg
    assert p.changes and all(c.kind == "refs" for c in p.changes)     # fully routed: only texts move
    bak = bt.write(board, p.text, d)
    assert bak.exists() and board.read_text() == p.text
