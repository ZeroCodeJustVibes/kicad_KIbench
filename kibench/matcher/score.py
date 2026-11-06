"""Rank stock footprints against a parsed package spec, with a readable diff."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .index import FootprintIndex
from .parse import PackageSpec, parse_query


@dataclass
class Match:
    lib: str
    name: str
    score: float
    matched: List[str] = field(default_factory=list)
    differs: List[str] = field(default_factory=list)
    path: str = ""

    @property
    def lib_id(self) -> str:
        return f"{self.lib}:{self.name}"

    def explain(self) -> str:
        return ", ".join([f"{m} ✓" for m in self.matched] + [f"{d} ✗" for d in self.differs])


def _fmt(v: Optional[float]) -> str:
    return "?" if v is None else f"{v:g}"


def score_row(q: PackageSpec, r: dict) -> Match:
    m = Match(r["lib"], r["name"], 100.0, path=r["path"])
    variants = set(filter(None, (r["variants"] or "").split(",")))
    if q.family and r["family"] and q.family.upper() == r["family"].upper():
        m.score += 6
        m.matched.append("family")
    if q.pins:
        m.matched.append(f"{q.pins} pins")
    # pitch (exact)
    if q.pitch is not None:
        if r["pitch"] is not None and abs(r["pitch"] - q.pitch) < 0.011:
            m.score += 30
            m.matched.append("pitch")
        else:
            m.score -= 45
            m.differs.append(f"pitch {_fmt(r['pitch'])} vs {_fmt(q.pitch)} mm")
    # body size (±0.1 mm, either orientation)
    if q.body_x is not None and q.body_y is not None:
        if r["body_x"] is not None and r["body_y"] is not None:
            a = sorted([q.body_x, q.body_y])
            b = sorted([r["body_x"], r["body_y"]])
            err = max(abs(a[0] - b[0]), abs(a[1] - b[1]))
            if err <= 0.1 + 1e-9:
                m.score += 25
                m.matched.append("body")
            else:
                m.score -= min(40, 10 + err * 15)
                m.differs.append(f"body {b[0]:g}×{b[1]:g} vs {a[0]:g}×{a[1]:g} mm")
        else:
            m.score -= 5
    # chip size code
    if q.chip:
        if r["chip"] == q.chip:
            m.score += 40
            m.matched.append(q.chip)
        else:
            m.score -= 60
            m.differs.append(f"size {r['chip'] or '?'} vs {q.chip}")
        if q.family not in ("CHIP", "") and r["family"] != q.family:
            m.score -= 20
    # exposed pad
    if q.has_ep or q.ep_x is not None:
        if not r["has_ep"]:
            m.score -= 25
            m.differs.append("no exposed pad")
        elif q.ep_x is not None and r["ep_x"] is not None:
            a = sorted([q.ep_x, q.ep_y or q.ep_x])
            b = sorted([r["ep_x"], r["ep_y"] or r["ep_x"]])
            err = max(abs(a[0] - b[0]), abs(a[1] - b[1]))
            if err <= 0.15:
                m.score += 12 - err * 40
                m.matched.append("EP")
            else:
                m.score -= 8
                m.differs.append(f"EP {b[0]:g}×{b[1]:g} vs {a[0]:g}×{a[1]:g} mm")
        else:
            m.score += 4
            m.matched.append("EP")
    elif r["has_ep"]:
        m.score -= 15
        m.differs.append("has exposed pad")
    # BGA grid
    if q.rows and q.cols and r["rows"] and r["cols"]:
        if sorted([q.rows, q.cols]) == sorted([r["rows"], r["cols"]]):
            m.score += 10
            m.matched.append(f"grid {q.rows}×{q.cols}")
        else:
            m.score -= 10
            m.differs.append(f"grid {r['rows']}×{r['cols']}")
    if q.lead_span and r["lead_span"]:
        if abs(q.lead_span - r["lead_span"]) <= 0.3:
            m.score += 5
    # variants: prefer the plain footprint unless the query asks for one
    for v in variants:
        if v in q.variants:
            m.score += 8
            m.matched.append(v)
        elif v in ("vertical",) and not q.variants:
            pass
        else:
            m.score -= 4
    for v in q.variants:
        if v not in variants and v not in ("vertical",):
            m.score -= 6
            m.differs.append(f"not {v}")
    return m


def match(index: FootprintIndex, query, limit: int = 12) -> List[Match]:
    q = query if isinstance(query, PackageSpec) else parse_query(str(query))
    if not q.group:
        return []
    rows = index.candidates(q.group, q.pins)
    if not rows and q.pins is None:
        return []
    out = [score_row(q, r) for r in rows]
    out.sort(key=lambda m: (-m.score, len(m.name)))
    return out[:limit]
