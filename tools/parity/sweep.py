#!/usr/bin/env python3
"""대량 훑기 — corpus 가 놓치는 것을 잡는다.

corpus 는 손으로 고른 입력이라 "case 가 밟지 않은 구간" 을 볼 수 없다. 실제로
첫 후보 구현은 corpus 24개를 전부 통과하고도 |v|>=1e16 에서 float 표기가
틀렸다. 손으로 고른 시험은 자기가 생각한 것만 시험한다.

여기서는 값의 공간을 통째로 훑는다.

`floats`  지수 -320..308 전 구간 + layout 좌표 분포 + 무작위 크기
`casefold` 모든 Unicode scalar (surrogate 제외)

`sweep.ts` 가 같은 표본으로 후보의 답을 내고, `parity.py sweep` 이 대조한다.
표본은 씨앗이 고정돼 있어 언제 돌려도 같다.
"""
from __future__ import annotations

import json
import math
import random
import sys
import unicodedata
from typing import Any

SEED = 20260826
LAYOUT_RADIUS = 18.0
GOLDEN_FRACTION = 0.6180339887498949


def layout_coordinates() -> list[float]:
    """scripts/llmwiki.py 의 좌표 계산을 그대로 돌려 실제 산출물 분포를 담는다."""
    values: list[float] = []
    for count in (1, 2, 7, 40, 259, 1000):
        for index in range(count):
            radial = math.sqrt((index + 0.72) / (count + 0.72))
            angular = (index * GOLDEN_FRACTION) % 1.0
            angle = -math.pi / 2 + 0.035 + (math.tau - 0.07) * angular
            values.append(round(LAYOUT_RADIUS * radial * math.cos(angle), 6))
            values.append(round(LAYOUT_RADIUS * radial * math.sin(angle), 6))
    return values


def float_sample() -> list[float]:
    rng = random.Random(SEED)
    values = layout_coordinates()
    for exponent in range(-320, 309):
        values += [float(f"1e{exponent}"), float(f"9.87654321e{exponent}"),
                   float(f"-1.5e{exponent}")]
    values += [0.0, -0.0, 1.0, -1.0, 18.0, 1e-4, 9.999e-5, 1e-5, 1e-6, 1e-7,
               1e15, 1e16, 1e17, 0.1 + 0.2, 1 / 3, 2.0 ** 53, -(2.0 ** 53),
               5e-324, 1.7976931348623157e308]
    values += [round(rng.uniform(-1e6, 1e6), 6) for _ in range(2000)]
    values += [rng.uniform(-1e-3, 1e-3) for _ in range(1000)]
    values += [rng.uniform(-1e21, 1e21) for _ in range(2000)]
    values += [float(rng.randint(-10 ** 18, 10 ** 18)) for _ in range(1000)]
    return [value for value in values if math.isfinite(value)]


def casefold_sample() -> list[str]:
    return [chr(cp) for cp in range(0x110000) if not 0xD800 <= cp <= 0xDFFF]


def main() -> None:
    floats = float_sample()
    folded = {char: char.casefold() for char in casefold_sample()
              if char.casefold() != char}
    payload: dict[str, Any] = {
        "seed": SEED,
        "floats": {"count": len(floats), "values": floats,
                   "answers": [json.dumps(value) for value in floats]},
        "casefold": {"unicode": unicodedata.unidata_version,
                     "scalars": 0x110000 - 0x800, "changed": folded},
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
