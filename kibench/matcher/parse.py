"""Parse package descriptions into dimensions.

Handles KiCad stock names (`BGA-256_17.0x17.0mm_Layout16x16_P1.0mm`), EasyEDA/LCSC
package strings (`SOIC-14_L8.7-W3.9-P1.27-LS6.0-BL`), distributor strings
(`LQFP-48_7x7x05P`) and hand-typed queries (`bga256 1mm`, `QFN-32 5x5 0.5`).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Optional, Set

FAMILY_GROUPS = {
    "QFN": ["QFN", "VQFN", "WQFN", "UQFN", "LQFN", "TQFN", "HVQFN", "MLF", "MLP", "QFN_THERMAL", "VFQFPN", "UFQFPN", "XQFN"],
    "DFN": ["DFN", "WDFN", "UDFN", "TDFN", "VDFN", "SON", "WSON", "VSON", "USON", "XSON", "X2SON", "TSON", "LLP"],
    "QFP": ["LQFP", "TQFP", "QFP", "EQFP", "HTQFP", "PQFP", "MQFP", "VQFP", "HLQFP"],
    "SOIC": ["SOIC", "SO", "SOP", "HSOP", "SOIC_W", "PSOP"],
    "SSOP": ["SSOP", "TSSOP", "HTSSOP", "QSOP", "MSOP", "VSSOP", "HVSSOP", "TVSOP", "TSOP", "HSSOP", "US8", "MSOP_EP"],
    "BGA": ["BGA", "TFBGA", "LFBGA", "VFBGA", "UFBGA", "FBGA", "CABGA", "CSBGA", "WFBGA", "FCBGA", "PBGA", "XFBGA", "UBGA"],
    "CSP": ["WLCSP", "CSP", "DSBGA", "WLP", "UCSP"],
    "LGA": ["LGA", "VLGA", "TFLGA"],
    "SOT": ["SOT", "TSOT", "SC", "SOT-23", "SOT-323", "SOT-363", "SOT-563", "SOT-89", "SOT-223", "SOT-143", "SOT-353", "SOT-523", "SOT-723", "SOT-883", "SOT-1123"],
    "TO": ["TO", "DPAK", "D2PAK", "D3PAK", "IPAK"],
    "DIP": ["DIP", "PDIP", "CDIP", "CERDIP", "SMDIP"],
    "SOD": ["SOD", "SMA", "SMB", "SMC", "MELF", "MINIMELF", "MICROMELF", "D_SMA", "D_SMB", "D_SMC"],
    "CHIP": ["R", "C", "L", "LED", "D", "FUSE", "CHIP", "CAP", "RES", "IND"],
    "HEADER": ["PINHEADER", "PINSOCKET", "HDR", "HEADER", "IDC"],
}
_FAMILY_TO_GROUP = {f: g for g, fs in FAMILY_GROUPS.items() for f in fs}
CHIP_SIZES = {"01005": "0402", "0201": "0603", "0402": "1005", "0603": "1608", "0805": "2012", "1206": "3216",
              "1210": "3225", "1806": "4516", "1812": "4532", "2010": "5025", "2512": "6332", "2220": "5750",
              "0306": "0816", "0612": "1632", "1020": "2550", "2816": "7142"}
METRIC_TO_IMPERIAL = {v: k for k, v in CHIP_SIZES.items()}
VARIANT_WORDS = ["handsolder", "handsoldering", "thermalvias", "socket", "longpads", "horizontal", "vertical",
                 "smd", "pad", "castellated", "topep", "bottomep", "clearance", "tabpin", "slugdown", "slugup",
                 "pin1left", "pin1right", "lockingtabs", "wide", "narrow"]
VENDOR_PREFIX = re.compile(
    r"^(?:Texas|TI|Analog|ADI|Diodes|ST|STM|Lattice|Xilinx|Altera|Infineon|NXP|Microchip|Maxim|ON|Onsemi|"
    r"Nexperia|Renesas|Rohm|Toshiba|Vishay|Linear|LT|AMS|Alliance|Intel|Broadcom|Cypress|Fairchild|IR|Micrel|"
    r"Silabs|SiliconLabs|TDK|Murata|Panasonic|Bosch|Nordic|Espressif|Qorvo|Skyworks|Semtech|Sensirion|"
    r"Monolithic|MPS|Richtek|Torex|Kemet|AVX|Wurth|Amphenol|Molex|JST|Hirose|TE|Samtec)_", re.I)


@dataclass
class PackageSpec:
    family: str = ""
    group: str = ""
    pins: Optional[int] = None
    body_x: Optional[float] = None
    body_y: Optional[float] = None
    pitch: Optional[float] = None
    pitch2: Optional[float] = None
    ep_x: Optional[float] = None
    ep_y: Optional[float] = None
    has_ep: bool = False
    rows: Optional[int] = None
    cols: Optional[int] = None
    lead_span: Optional[float] = None
    chip: str = ""                          # imperial size code for 2-terminal chips
    variants: Set[str] = field(default_factory=set)

    def to_row(self) -> dict:
        d = asdict(self)
        d["variants"] = ",".join(sorted(self.variants))
        return d


def family_group(fam: str) -> str:
    f = fam.upper().replace(" ", "")
    if f in _FAMILY_TO_GROUP:
        return _FAMILY_TO_GROUP[f]
    for k, g in _FAMILY_TO_GROUP.items():
        if len(k) >= 3 and f.startswith(k):
            return g
    for k, g in _FAMILY_TO_GROUP.items():
        if len(k) >= 3 and k in f:
            return g
    return f


def _f(s) -> Optional[float]:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _dims_pair(a: float, b: float):
    return a, b


def parse_kicad_name(name: str) -> PackageSpec:
    """Parse a KiCad stock footprint name."""
    s = PackageSpec()
    n = VENDOR_PREFIX.sub("", name)
    low = n.lower()
    flat = low.replace("_", "").replace("-", "")
    s.variants = {v for v in VARIANT_WORDS if v in flat}
    if "handsoldering" in s.variants:
        s.variants.discard("handsoldering")
        s.variants.add("handsolder")
    # 2-terminal chips: R_0603_1608Metric, C_0402_1005Metric, LED_0805_2012Metric
    m = re.match(r"^(R|C|L|LED|D|Fuse|CP_Elec|C_Elec)_(\d{4,5})_(\d{4})Metric", n)
    if m:
        s.family, s.group, s.pins, s.chip = m.group(1).upper(), "CHIP", 2, m.group(2)
        mm = m.group(3)
        s.body_x, s.body_y = float(mm[:2]) / 10, float(mm[2:]) / 10
        return s
    m = re.match(r"^(PinHeader|PinSocket)_(\d+)x(\d+)_P([\d.]+)mm", n)
    if m:
        s.family, s.group = m.group(1).upper(), "HEADER"
        s.rows, s.cols = int(m.group(2)), int(m.group(3))
        s.pins, s.pitch = s.rows * s.cols, float(m.group(4))
        return s
    m = re.match(r"^([A-Za-z][A-Za-z0-9]*?)(?:-(\d+))?(?:-(\d+))?(?:-(\d+)EP)?(?=[_\-]|$)", n)
    if m:
        fam = m.group(1)
        s.family = fam.upper()
        if s.family in ("SOT", "TO", "SOD", "SC") and m.group(2):
            s.family = f"{s.family}-{m.group(2)}"
            if m.group(3):
                s.pins = int(m.group(3))
        elif m.group(2):
            s.pins = int(m.group(2))
            if m.group(3) and s.family not in ("SOT",):
                # e.g. MSOP-12-16 (12 of 16 pins populated) -> keep first
                pass
        if m.group(4) or re.search(r"-\d+EP", n):
            s.has_ep = True
    s.group = family_group(s.family.split("-")[0]) if s.family else ""
    if s.family.startswith(("SOT-", "SOD-", "SC-")):
        s.group = family_group(s.family.split("-")[0])
    m = re.search(r"_([\d.]+)x([\d.]+)(?:x[\d.]+)?mm", n)
    if m:
        s.body_x, s.body_y = float(m.group(1)), float(m.group(2))
    m = re.search(r"_P([\d.]+)mm(?:P([\d.]+)mm)?", n)
    if m:
        s.pitch = float(m.group(1))
        s.pitch2 = _f(m.group(2))
    m = re.search(r"_EP([\d.]+)x([\d.]+)mm", n)
    if m:
        s.ep_x, s.ep_y, s.has_ep = float(m.group(1)), float(m.group(2)), True
    m = re.search(r"_Layout(\d+)x(\d+)", n)
    if m:
        s.rows, s.cols = int(m.group(1)), int(m.group(2))
    m = re.search(r"_W([\d.]+)mm", n)
    if m:
        s.lead_span = float(m.group(1))
    if s.family in ("SOT-23",) and s.pins is None:
        s.pins = 3
    return s


def parse_query(text: str) -> PackageSpec:
    """Parse a free-form or distributor package description."""
    t = text.strip()
    s = PackageSpec()
    up = t.upper().replace("×", "X").replace("*", "X")
    # EasyEDA style: SOIC-14_L8.7-W3.9-P1.27-LS6.0-BL / R0603 / C0402 / HDR-TH_40P-P2.54
    m = re.match(r"^(R|C|L|LED|D|F)(\d{4})(?:_|$)", up)
    if m and m.group(2) in CHIP_SIZES:
        s.family, s.group, s.pins, s.chip = {"F": "FUSE"}.get(m.group(1), m.group(1)), "CHIP", 2, m.group(2)
        return s
    m = re.fullmatch(r"\s*(\d{4,5})\s*(?:\((\d{4})\s*METRIC\))?\s*", up)
    if m and m.group(1) in CHIP_SIZES:
        s.family, s.group, s.pins, s.chip = "CHIP", "CHIP", 2, m.group(1)
        return s
    if re.search(r"\bHDR\b|HEADER|PIN\s*HEADER", up):
        s.family, s.group = "PINHEADER", "HEADER"
        m = re.search(r"(\d+)P\b", up) or re.search(r"1X(\d+)", up)
        if m:
            s.pins = int(m.group(1))
        m = re.search(r"(\d+)X(\d+)", up)
        if m:
            s.rows, s.cols = int(m.group(1)), int(m.group(2))
            s.pins = s.rows * s.cols
        elif s.pins:
            s.rows, s.cols = 1, s.pins
        m = re.search(r"P(\d+\.\d+)", up)
        s.pitch = float(m.group(1)) if m else 2.54
        if "-H-" in up or "HORIZ" in up or "RIGHT" in up:
            s.variants.add("horizontal")
        else:
            s.variants.add("vertical")
        return s
    # family + pins: "BGA-256", "bga256", "SOT-23-5", "SOT23", "QFN32", "LQFN-56"
    # Note: "_" is a word character, so use explicit look-arounds instead of \b.
    m = re.search(r"(?<![A-Z0-9])(SOT|SOD|TO|SC)-?(\d+[A-Z]?)(?:-(\d+))?(?![0-9])", up)
    if m:
        s.family = f"{m.group(1)}-{m.group(2)}"
        s.group = family_group(m.group(1))
        if m.group(3):
            s.pins = int(m.group(3))
        elif s.family == "SOT-23":
            s.pins = 3
    else:
        m = re.search(r"(?<![A-Z0-9])([A-Z]{2,8}?)-?(\d{1,4})(?:-(\d+)EP)?(?![0-9.])", up)
        if m:
            s.family = m.group(1)
            s.group = family_group(s.family)
            s.pins = int(m.group(2))
            if m.group(3):
                s.has_ep = True
    if re.search(r"(?:-|_|\b)EP\b|EXPOSED|THERMAL\s*PAD|-\d+EP", up):
        s.has_ep = True
    # EasyEDA dimensions
    lm = re.search(r"_L([\d.]+)-W([\d.]+)", up)
    if lm:
        s.body_x, s.body_y = sorted([float(lm.group(1)), float(lm.group(2))])
    pm = re.search(r"-P([\d.]+)(?:-|$)", up)
    if pm:
        s.pitch = float(pm.group(1))
    lsm = re.search(r"-LS([\d.]+)", up)
    if lsm:
        s.lead_span = float(lsm.group(1))
    # Distributor style "LQFP-48_7x7x05P" (05P = 0.5 mm pitch)
    # pitch code: 05P = 0.5, 065P = 0.65, 127P = 1.27, 10P = 1.0
    dm = re.search(r"_(\d+(?:\.\d+)?)X(\d+(?:\.\d+)?)(?:X(\d+(?:\.\d+)?))??X(\d{2,3})P(?![A-Z0-9])", up)
    if dm:
        s.body_x, s.body_y = float(dm.group(1)), float(dm.group(2))
        code = dm.group(4)
        s.pitch = float(code[0] + "." + code[1:])
    # Free form: "17x17", "5x5mm", "17×17", "1.0 mm", "0.5mm pitch", "P0.65"
    if s.body_x is None:
        bm = re.search(r"(\d+(?:\.\d+)?)\s*X\s*(\d+(?:\.\d+)?)\s*(?:MM)?(?!\s*LAYOUT)", up)
        if bm:
            a, b = float(bm.group(1)), float(bm.group(2))
            # BGA "Layout 16x16" vs body: integers > 4 with BGA family and pins == a*b are a grid
            if s.group == "BGA" and s.pins and a * b == s.pins and a == int(a):
                s.rows, s.cols = int(a), int(b)
            else:
                s.body_x, s.body_y = a, b
    em = re.search(r"EP\s*([\d.]+)\s*X\s*([\d.]+)", up)
    if em:
        s.ep_x, s.ep_y, s.has_ep = float(em.group(1)), float(em.group(2)), True
    if s.pitch is None:
        rest = re.sub(r"EP\s*[\d.]+\s*X\s*[\d.]+|\d+(?:\.\d+)?\s*X\s*\d+(?:\.\d+)?", " ", up)
        pm2 = (re.search(r"(?:(?<![A-Z])P|PITCH\s*)(\d+(?:\.\d+)?)", rest)
               or re.search(r"(\d+(?:\.\d+)?)\s*MM", rest)
               or re.search(r"(?<![\w.])(\d?\.\d+|[12]\.\d+)(?![\w.])", rest))
        if pm2:
            v = float(pm2.group(1))
            if 0.2 <= v <= 2.54:
                s.pitch = v
    if "HANDSOLDER" in up.replace(" ", "").replace("-", ""):
        s.variants.add("handsolder")
    if "THERMALVIA" in up.replace(" ", ""):
        s.variants.add("thermalvias")
    return s
