#!/usr/bin/env python3
"""교란 코퍼스 — 여러 page 에 바이트 단위로 똑같이 복사된 block 을 page 마다 다르게 쓴다.

bench/frozen/corpus 의 생성기 인공물(질문 문장을 그대로 담은 distractor 가 6개 page 에
문자 그대로 동일)이 structural2 의 복제 block 감쇠(dup) 이득의 원천인지 확인하기 위한
코퍼스다. 질문·gold 는 bench/frozen/queries.json 을 그대로 쓴다.

규칙: 어떤 block 의 본문이 2개 이상 page 에 바이트 동일하게 나타나면 page 마다
  1. 문장 순서를 돌리고(2문장 이상일 때),
  2. 문장 끝 어미를 바꾸고("이다."→"였다."/"이라고 본다." 등),
  3. page 별 짧은 부연 "(항목 <slug>)" 을 끼워 넣고,
  4. 질문에 등장하지 않는 낱말 하나를 동의어로 바꾼다.
  어느 변형을 얼마나 쓸지는 (slug, 본문) 의 sha256 으로 정하므로 결정적이다.
  질문 토큰과의 겹침은 유지한다: 문장 **끝** 어미만 바꾸고 동의어 사전은 질문 세트에
  한 번이라도 나오는 낱말을 빼고 만든다. 스크립트 끝에서 (a) 바이트 동일 block 이
  2개 이상 page 에 남아 있지 않은지, (b) 질문 토큰(v1 낱말·v2 2-gram 둘 다)과 block 의
  겹침이 원본 이상인지 검증해 출력한다.

사용:
  python3 bench/perturb.py --src bench/frozen/corpus --queries bench/frozen/queries.json \
      --out bench/corpus_perturbed --seed 1234
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bench"))
from rankers import structural, structural2  # noqa: E402

# 문장 끝 어미 치환 후보. 키는 문장 끝(마침표 앞)에서만 맞춘다.
ENDINGS = {
    "이다": ["였다", "이라고 본다", "인 것으로 본다"],
    "있다": ["있었다", "있다고 본다", "있는 것으로 본다"],
    "없다": ["없었다", "없다고 본다"],
    "않는다": ["않았다", "않는 것으로 본다"],
    "아니다": ["아니었다", "아니라고 본다"],
    "남았다": ["남아 있다", "남은 것으로 본다"],
    "된다": ["되었다", "되는 것으로 본다"],
    "한다": ["했다", "하는 것으로 본다"],
    "다룬다": ["다루었다", "다루는 것으로 본다"],
    "따른다": ["따랐다", "따르는 것으로 본다"],
    "기록되어 있다": ["기록되었다", "기록된 것으로 본다"],
    "전환되었다": ["전환된 상태다", "전환된 것으로 본다"],
}
# 동의어 후보. 질문 세트에 등장하는 낱말은 실행 시 걸러낸다.
SYNONYMS = {
    "초안": ["원고", "시안"],
    "목록": ["명단", "리스트"],
    "후보": ["안", "대안"],
    "판단": ["결론", "해석"],
    "문구": ["구절", "표현"],
    "표현": ["문구", "구절"],
    "근거": ["증거", "논거"],
    "판본": ["버전", "판"],
    "해석": ["독법", "풀이"],
    "결론": ["판정", "결말"],
    "검토했지만": ["살폈지만", "확인했지만"],
    "거절된": ["기각된", "반려된"],
    "폐기된": ["버려진", "철회된"],
    "등장하지만": ["나오지만", "보이지만"],
    "page": ["문서", "항목"],
}
_SENT = re.compile(r"(?<=[.!?])\s+|\n+")


def h(*parts: str) -> int:
    return int.from_bytes(hashlib.sha256("\x1f".join(parts).encode("utf-8")).digest()[:8], "big")


def split_sentences(text: str) -> list[str]:
    return [s for s in _SENT.split(text) if s.strip()]


def query_words(queries: list[dict]) -> set[str]:
    words: set[str] = set()
    for q in queries:
        words.update(structural.tokenize(q["text"]))
        words.update(structural.query_terms(q["text"]))
        words.update(re.findall(r"[0-9A-Za-z가-힣_.\-]+", q["text"].lower()))
    return words


def perturb_text(text: str, slug: str, seed: int, syn: dict[str, list[str]]) -> str:
    r = h(str(seed), slug, text)
    sents = split_sentences(text)
    # 1. 문장 순서 회전
    if len(sents) >= 2:
        k = 1 + (r % (len(sents) - 1)) if len(sents) > 2 else 1
        if (r >> 8) & 1:
            sents = sents[k:] + sents[:k]
    # 2. 문장 끝 어미
    out = []
    for i, s in enumerate(sents):
        body, dot = (s[:-1], s[-1]) if s and s[-1] in ".!?" else (s, "")
        rr = h(str(seed), slug, text, str(i))
        for key in sorted(ENDINGS, key=len, reverse=True):
            if body.endswith(key):
                alts = ENDINGS[key]
                body = body[: -len(key)] + alts[rr % len(alts)]
                break
        out.append(body + dot)
    # 3. page 별 부연
    pos = (r >> 16) % len(out)
    aside = ["(항목 %s)", "— %s 기준", "[%s 기록]"][(r >> 24) % 3] % slug
    if (r >> 32) & 1:
        out[pos] = out[pos].rstrip(".!?") + " " + aside + (out[pos][-1] if out[pos][-1] in ".!?" else "")
    else:
        out[pos] = aside + " " + out[pos]
    new = " ".join(out) if "\n" not in text else "\n".join(out)
    # 4. 동의어 하나 (질문에 없는 낱말만)
    cands = [w for w in sorted(syn) if w in new]
    if cands:
        w = cands[(r >> 40) % len(cands)]
        alts = syn[w]
        new = new.replace(w, alts[(r >> 48) % len(alts)], 1)
    return new


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="bench/frozen/corpus")
    ap.add_argument("--queries", default="bench/frozen/queries.json")
    ap.add_argument("--out", default="bench/corpus_perturbed")
    ap.add_argument("--seed", type=int, default=1234)
    args = ap.parse_args()
    src, out = Path(args.src), Path(args.out)
    queries = json.loads(Path(args.queries).read_text(encoding="utf-8"))["queries"]
    qwords = query_words(queries)
    syn = {k: v for k, v in SYNONYMS.items()
           if k.lower() not in qwords and not any(k.lower() in w for w in qwords)}
    dropped = sorted(set(SYNONYMS) - set(syn))

    # 1) 어떤 본문이 몇 page 에 있는지
    files = sorted(p for p in src.rglob("*.json") if not p.name.startswith("."))
    pages = {f: json.loads(f.read_text(encoding="utf-8")) for f in files}
    owners: dict[str, set[str]] = {}
    for f, p in pages.items():
        for b in (p.get("blocks") or {}).values():
            t = (b.get("data") or {}).get("text") or b.get("source_text") or ""
            if t:
                owners.setdefault(t, set()).add(str(p["id"]))
    dup_texts = {t for t, o in owners.items() if len(o) >= 2}

    # 2) 교란해서 쓴다
    if out.exists():
        shutil.rmtree(out)
    changed = 0
    for f, p in pages.items():
        slug = str(p.get("slug") or p["id"])
        for b in (p.get("blocks") or {}).values():
            t = (b.get("data") or {}).get("text") or b.get("source_text") or ""
            if t in dup_texts:
                new = perturb_text(t, slug, args.seed, syn)
                if isinstance(b.get("data"), dict) and "text" in b["data"]:
                    b["data"]["text"] = new
                if "source_text" in b:
                    b["source_text"] = new
                changed += 1
        dst = out / f.relative_to(src)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(json.dumps(p, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 3) 검증 (a): 바이트 동일 block 이 2개 이상 page 에 남았는가
    owners2: dict[str, set[str]] = {}
    for f in sorted(out.rglob("*.json")):
        p = json.loads(f.read_text(encoding="utf-8"))
        for b in (p.get("blocks") or {}).values():
            t = (b.get("data") or {}).get("text") or b.get("source_text") or ""
            if t:
                owners2.setdefault(t, set()).add(str(p["id"]))
    remaining = sum(1 for o in owners2.values() if len(o) >= 2)

    # 3) 검증 (b): 질문 토큰과 block 의 겹침이 원본 이상인가 (v1 낱말, v2 2-gram)
    v1q = [set(structural.query_terms(q["text"])) for q in queries]
    v2q = [set(structural2.tokenize(q["text"])) for q in queries]
    lost_v1 = lost_v2 = checked = 0
    for t in dup_texts:
        toks1, toks2 = set(structural.tokenize(t)), set(structural2.tokenize(t))
        for pid in owners[t]:
            slug = pid.split(":", 1)[1]
            new = perturb_text(t, slug, args.seed, syn)
            n1, n2 = set(structural.tokenize(new)), set(structural2.tokenize(new))
            for a, b2 in zip(v1q, v2q):
                if a & toks1 or b2 & toks2:
                    checked += 1
                    # v1 은 어간이 낱말의 prefix 로 맞는다
                    def hit1(ts: set[str]) -> set[str]:
                        return {x for x in a if any(w.startswith(x) for w in ts)}
                    if not hit1(toks1) <= hit1(n1):
                        lost_v1 += 1
                    if not (b2 & toks2) <= (n2 & b2):
                        lost_v2 += 1
    report = {
        "seed": args.seed, "pages": len(pages), "dup_texts": len(dup_texts),
        "blocks_rewritten": changed, "dropped_synonyms_in_queries": dropped,
        "remaining_duplicate_texts_across_pages": remaining,
        "overlap_checks": checked, "overlap_lost_v1": lost_v1, "overlap_lost_v2": lost_v2,
    }
    print(json.dumps(report, ensure_ascii=False, indent=1))
    ok = remaining == 0 and lost_v1 == 0 and lost_v2 == 0
    print("OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
