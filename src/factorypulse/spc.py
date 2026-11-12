"""p 管制圖（不良率），樣本數每天不同，所以管制界限逐點計算。

中心線用 Phase I 迭代估計：先用全部資料算 p̄，把超出上限的點排除後重算，直到穩定。
這樣異常期間的資料不會把中心線拉高、讓異常變得不明顯。

判定規則（只看不良率上升，也就是良率下滑）：
  OUT_OF_CONTROL：p > UCL
  RUN_ABOVE_CENTER：連續 7 點高於中心線（小幅但持續的惡化）
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

RUN_LENGTH = 7
SIGMA = 3.0


@dataclass
class ChartPoint:
    key: date
    n: int
    ng: int
    center: float = 0.0
    ucl: float = 0.0
    lcl: float = 0.0
    rules: list[str] = field(default_factory=list)

    @property
    def p(self) -> float:
        return self.ng / self.n if self.n else 0.0


def limits(pbar: float, n: int) -> tuple[float, float]:
    s = math.sqrt(pbar * (1 - pbar) / n) if n else 0.0
    return min(1.0, pbar + SIGMA * s), max(0.0, pbar - SIGMA * s)


def estimate_center(points: list[ChartPoint], max_iter: int = 20) -> float:
    included = [p for p in points if p.n > 0]
    pbar = 0.0
    for _ in range(max_iter):
        n, ng = sum(p.n for p in included), sum(p.ng for p in included)
        pbar = ng / n if n else 0.0
        keep = [p for p in included if p.p <= limits(pbar, p.n)[0]]
        if len(keep) == len(included) or not keep:
            break
        included = keep
    return pbar


def p_chart(points: list[ChartPoint], center: float | None = None) -> list[ChartPoint]:
    points = sorted(points, key=lambda p: p.key)
    pbar = estimate_center(points) if center is None else center
    run = 0
    for p in points:
        p.center = pbar
        p.ucl, p.lcl = limits(pbar, p.n)
        p.rules = []
        if p.p > p.ucl:
            p.rules.append("OUT_OF_CONTROL")
        run = run + 1 if p.p > pbar else 0
        if run >= RUN_LENGTH:
            p.rules.append("RUN_ABOVE_CENTER")
    return points


RULE_ZH = {"OUT_OF_CONTROL": "超出管制上限", "RUN_ABOVE_CENTER": f"連續 {RUN_LENGTH} 點高於中心線"}
