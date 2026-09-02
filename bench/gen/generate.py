#!/usr/bin/env python3
"""Deterministic synthetic corpus and query generator for bench/SPEC.md."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TYPE_LAYOUT = (
    ("source", "sources", 50),
    ("entity", "entities", 25),
    ("synthesis", "syntheses", 19),
    ("concept", "concepts", 5),
    ("project", "projects", 1),
)
MIN_PAGES = 300
QUERY_COUNT_PER_TYPE = 100
TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣._:-]+")


@dataclass(frozen=True)
class PageMeta:
    ordinal: int
    type: str
    directory: str
    type_index: int
    slug: str
    page_id: str
    english: bool = False


@dataclass(frozen=True)
class Edge:
    target: int
    kind: str
    role: str
    label: str


def allocate_type_counts(total: int) -> dict[str, int]:
    """Largest-remainder allocation for the exact 50/25/19/5/1 mix."""
    floors: list[int] = []
    remainders: list[tuple[int, int]] = []
    for position, (_, _, percent) in enumerate(TYPE_LAYOUT):
        product = total * percent
        floors.append(product // 100)
        remainders.append((product % 100, position))
    for _, position in sorted(remainders, key=lambda item: (-item[0], item[1]))[
        : total - sum(floors)
    ]:
        floors[position] += 1
    return {layout[0]: floors[position] for position, layout in enumerate(TYPE_LAYOUT)}


def make_metas(total: int) -> tuple[list[PageMeta], dict[str, list[PageMeta]]]:
    counts = allocate_type_counts(total)
    metas: list[PageMeta] = []
    by_type: dict[str, list[PageMeta]] = {name: [] for name, _, _ in TYPE_LAYOUT}
    ordinal = 0
    source_english = counts["source"] * 40 // 100
    for page_type, directory, _ in TYPE_LAYOUT:
        for type_index in range(counts[page_type]):
            ordinal += 1
            slug = f"{page_type}-{type_index + 1:06d}"
            meta = PageMeta(
                ordinal=ordinal,
                type=page_type,
                directory=directory,
                type_index=type_index,
                slug=slug,
                page_id=f"page:{slug}",
                english=page_type == "source" and type_index < source_english,
            )
            metas.append(meta)
            by_type[page_type].append(meta)
    return metas, by_type


def partition_chains(member_count: int) -> list[int]:
    """Partition members into deterministic chain lengths in the 2..4 range."""
    if member_count < 2:
        return []
    result: list[int] = []
    remaining = member_count
    pattern = (2, 3, 4)
    pattern_index = 0
    while remaining:
        if remaining <= 4:
            if remaining == 1:
                result[-1] += 1
            else:
                result.append(remaining)
            break
        length = pattern[pattern_index % len(pattern)]
        pattern_index += 1
        if remaining - length == 1:
            length = length - 1 if length > 2 else length + 1
        result.append(length)
        remaining -= length
    if sum(result) != member_count or any(length < 2 or length > 4 for length in result):
        raise AssertionError(f"invalid temporal partition: {result}")
    return result


def block_id(meta: PageMeta, role: str) -> str:
    return f"block:{meta.slug}:{role}"


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def make_block(
    meta: PageMeta,
    role: str,
    text: str,
    *,
    kind: str = "paragraph",
    refs: list[str] | None = None,
    resolution: dict[str, str] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": block_id(meta, role),
        "kind": kind,
        "data": {"text": text},
        "refs": list(dict.fromkeys(refs or [])),
        "source_text": text,
        "fingerprint": fingerprint(text),
    }
    if resolution is not None:
        value["resolution"] = resolution
    return value


def hangul_marker(offset: int, index: int) -> str:
    return chr(0xAC00 + offset + index)


def lexical_code(index: int) -> str:
    """Four-letter code whose first four characters are unique for benchmark queries."""
    chars = ["a"] * 4
    value = index
    for position in range(3, -1, -1):
        chars[position] = chr(ord("a") + value % 26)
        value //= 26
    return "".join(chars)


def body_text(page: dict[str, Any]) -> str:
    return "\n".join(
        str(page["blocks"][item].get("source_text", "")) for item in page["block_order"]
    )


def tokens(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_RE.findall(text)]


def safe_recreate_directory(path: Path) -> None:
    resolved = path.resolve()
    forbidden = {Path("/").resolve(), Path.home().resolve(), Path.cwd().resolve()}
    if resolved in forbidden:
        raise ValueError(f"refusing to replace broad output directory: {resolved}")
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise ValueError(f"output must be a real directory: {path}")
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


class CorpusBuilder:
    def __init__(self, page_count: int, seed: int) -> None:
        self.page_count = page_count
        self.seed = seed
        self.rng = random.Random(seed)
        self.metas, self.by_type = make_metas(page_count)
        self.meta_by_ordinal = {meta.ordinal: meta for meta in self.metas}
        self.required_edges: dict[int, list[Edge]] = {
            meta.ordinal: [] for meta in self.metas
        }
        self.special_text: dict[int, dict[str, list[str]]] = {
            meta.ordinal: {} for meta in self.metas
        }
        self.questions: list[dict[str, Any]] = []
        self.question_number = 0
        self.chain_members: list[list[PageMeta]] = []
        self.temporal_position: dict[int, tuple[int, int]] = {}
        self.conflict_ordinals: set[int] = set()
        self.cross_pairs: list[tuple[PageMeta, PageMeta]] = []
        self.relation_targets: dict[int, PageMeta] = {}
        self.title_terms: dict[int, list[str]] = {meta.ordinal: [] for meta in self.metas}
        self.hub_order = list(self.metas)
        self.rng.shuffle(self.hub_order)

    def add_text(self, meta: PageMeta, role: str, text: str) -> None:
        self.special_text[meta.ordinal].setdefault(role, []).append(text)

    def add_title_term(self, meta: PageMeta, term: str) -> None:
        if term not in self.title_terms[meta.ordinal]:
            self.title_terms[meta.ordinal].append(term)

    def distractor_meta(self, query_type: str, query_index: int, position: int) -> PageMeta:
        """Return deterministic distractor pages; 10k uses disjoint pools, 300 may reuse."""
        offsets = {"exact": 0, "relation": 600, "paraphrase": 1200, "crosslingual": 1800}
        pool = self.by_type["entity"]
        offset = offsets[query_type] + query_index * 6 + position
        return pool[offset % len(pool)]

    def add_edge(self, owner: PageMeta, edge: Edge) -> bool:
        existing = self.required_edges[owner.ordinal]
        if owner.ordinal == edge.target or any(item.target == edge.target for item in existing):
            return False
        existing.append(edge)
        return True

    def add_question(
        self,
        query_type: str,
        lang: str,
        text: str,
        gold_pages: list[str],
        gold_blocks: list[str],
        stale_pages: list[str],
        notes: str,
    ) -> None:
        self.question_number += 1
        self.questions.append(
            {
                "id": f"q{self.question_number:05d}",
                "type": query_type,
                "lang": lang,
                "text": text,
                "gold_pages": gold_pages,
                "gold_blocks": gold_blocks,
                "stale_pages": stale_pages,
                "notes": notes,
            }
        )

    def prepare_temporal(self) -> None:
        member_count = (self.page_count * 8 + 50) // 100
        candidates = self.by_type["synthesis"][:member_count]
        cursor = 0
        for chain_index, length in enumerate(partition_chains(member_count)):
            chain = candidates[cursor : cursor + length]
            cursor += length
            self.chain_members.append(chain)
            for position, meta in enumerate(chain):
                self.temporal_position[meta.ordinal] = (chain_index, position)
                if position == length - 1:
                    self.add_text(meta, "temporal", "최신 결정에 따라 체계는 활성 단계로 전환되었다.")
                else:
                    self.add_text(meta, "temporal", "과거 판본에는 폐기된 판단이 기록되어 있다.")
                if position:
                    previous = chain[position - 1]
                    if not self.add_edge(
                        meta,
                        Edge(previous.ordinal, "supersedes", "temporal", "이전 판본"),
                    ):
                        raise AssertionError("duplicate temporal edge")

        profiles = (
            "strong", "weak", "strong", "current", "strong",
            "weak", "strong", "weak", "strong", "current",
            "strong", "strong", "weak", "strong", "current",
            "strong", "weak", "strong", "weak", "strong",
        )
        for query_index in range(QUERY_COUNT_PER_TYPE):
            chain = self.chain_members[query_index % len(self.chain_members)]
            current = chain[-1]
            profile = profiles[query_index % len(profiles)]
            old_name = f"{lexical_code(query_index)}-legacy"
            new_name = f"{lexical_code(query_index + 100)}-active"
            text = f"{old_name} 운용 상태는 지금 무엇인가"
            if profile == "strong":
                for stale in chain[:-1]:
                    self.add_text(
                        stale,
                        "temporal",
                        f"{old_name} 운용 상태는 지금 중단 단계라고 기록되어 있다.",
                    )
                self.add_text(
                    current,
                    "temporal",
                    f"명칭이 {new_name}으로 바뀐 뒤 서비스가 정상 단계로 전환되었다.",
                )
            elif profile == "weak":
                for stale in chain[:-1]:
                    self.add_text(
                        stale,
                        "temporal",
                        f"{old_name} 운용 상태는 지금 중단 단계라고 기록되어 있다.",
                    )
                self.add_title_term(current, old_name)
                self.add_text(
                    current,
                    "temporal",
                    f"{old_name} 운용 현황은 검증을 마치고 정상 단계로 전환되었다.",
                )
            else:
                for stale in chain[:-1]:
                    self.add_text(stale, "temporal", f"{old_name}의 초기 기록은 폐기되었다.")
                self.add_title_term(current, old_name)
                self.add_text(
                    current,
                    "temporal",
                    f"{old_name} 운용 상태는 지금 정상 단계이며 모든 점검을 마쳤다.",
                )
            self.add_question(
                "temporal",
                "ko",
                text,
                [current.page_id],
                [block_id(current, "temporal")],
                [meta.page_id for meta in chain[:-1]],
                f"난이도={profile}; supersedes 체인의 최신 page가 정답이다.",
            )

    def prepare_crosslingual(self) -> None:
        sources = self.by_type["source"]
        english_count = sum(meta.english for meta in sources)
        english = sources[:english_count]
        korean = sources[english_count : english_count * 2]
        if len(english) < 1 or len(korean) != len(english):
            raise AssertionError("not enough crosslingual source pairs")
        self.cross_pairs = list(zip(english, korean))
        for en_meta, ko_meta in self.cross_pairs:
            self.add_text(
                en_meta,
                "crosslingual",
                "This record and its Korean counterpart describe the same concept.",
            )
            self.add_text(
                ko_meta,
                "crosslingual",
                "이 기록과 영어 대응 문서는 같은 개념을 다룬다.",
            )
            if not self.add_edge(
                en_meta, Edge(ko_meta.ordinal, "related", "crosslingual", "Korean pair")
            ):
                raise AssertionError("duplicate English crosslingual edge")
            if not self.add_edge(
                ko_meta, Edge(en_meta.ordinal, "related", "crosslingual", "영어 쌍")
            ):
                raise AssertionError("duplicate Korean crosslingual edge")

        ko_adjectives = (
            "푸른",
            "고요한",
            "단단한",
            "맑은",
            "빠른",
            "깊은",
            "밝은",
            "넓은",
            "작은",
            "정교한",
        )
        ko_nouns = (
            "등대",
            "나침반",
            "교량",
            "정원",
            "항로",
            "수문",
            "관측소",
            "저장고",
            "통로",
            "돛대",
        )
        en_adjectives = (
            "azure",
            "silent",
            "resilient",
            "lucid",
            "swift",
            "deep",
            "radiant",
            "broad",
            "compact",
            "precise",
        )
        en_nouns = (
            "beacon",
            "compass",
            "bridge",
            "garden",
            "route",
            "gate",
            "observatory",
            "vault",
            "passage",
            "mast",
        )
        for query_index in range(QUERY_COUNT_PER_TYPE):
            pair = self.cross_pairs[query_index % len(self.cross_pairs)]
            en_meta, ko_meta = pair
            adjective = query_index // 10
            noun = query_index % 10
            remainder = query_index % 10
            if remainder in {0, 3, 6, 9}:
                profile = "zero"
                query = f"{ko_adjectives[adjective]} {ko_nouns[noun]} 복구 규칙은 무엇인가"
                gold_text = (
                    f"The {en_adjectives[adjective]} {en_nouns[noun]} follows a "
                    "restoration protocol based on verified structural evidence."
                )
                extra_distractors = 1
            else:
                product = f"{lexical_code(query_index + 200)}-atlas"
                query = f"{product} 복구 규칙은 무엇인가"
                gold_text = (
                    f"The {product} restoration protocol relies on verified structural evidence."
                )
                if remainder in {1, 7}:
                    profile = "partial-easy"
                    extra_distractors = 1
                else:
                    profile = "partial-hard"
                    extra_distractors = 5
            self.add_text(en_meta, "crosslingual", gold_text)
            self.add_text(
                ko_meta,
                "crosslingual",
                f"{query}은 검증된 구조 근거를 따른다.",
            )
            for position in range(extra_distractors):
                distractor = self.distractor_meta("crosslingual", query_index, position)
                self.add_text(
                    distractor,
                    "crosslingual-distractor",
                    f"{query}라는 문구는 번역 후보 목록에만 있고 이 page는 정답 근거가 아니다.",
                )
            self.add_question(
                "crosslingual",
                "ko",
                query,
                [en_meta.page_id],
                [block_id(en_meta, "crosslingual")],
                [],
                f"난이도={profile}; 한국어 쌍 {ko_meta.page_id}와 related인 영어 source가 정답이다.",
            )

    def prepare_exact(self) -> None:
        versions = (
            ("v2.3.1", "v2.1.3"),
            ("v4.7.2", "v4.2.7"),
            ("v6.1.8", "v6.8.1"),
            ("v3.9.4", "v3.4.9"),
        )
        temporal_members = (self.page_count * 8 + 50) // 100
        gold_pool = self.by_type["synthesis"]
        for query_index in range(QUERY_COUNT_PER_TYPE):
            gold = gold_pool[(temporal_members + query_index) % len(gold_pool)]
            correct, near = versions[query_index % len(versions)]
            setting_key = f"cfg.pipeline.{query_index:03d}"
            hard = query_index % 5 == 4
            distractor_count = 6 if hard else 2
            query = f"오로라 배포군 설정 키 {setting_key}의 현재 버전은 {correct}인가"
            self.add_text(
                gold,
                "exact",
                f"오로라 배포군의 설정 키 {setting_key}에 현재 적용된 버전은 {correct}이다.",
            )
            distractor_ids: list[str] = []
            for position in range(distractor_count):
                distractor = self.distractor_meta("exact", query_index, position)
                distractor_ids.append(distractor.page_id)
                self.add_text(
                    distractor,
                    "exact-distractor",
                    (
                        f"오로라 배포군 설정 키 {setting_key}의 현재 버전은 {correct}인가를 "
                        f"검토했지만 {correct}는 거절된 후보이고 이 초안에는 {near}만 남았다."
                    ),
                )
            self.add_question(
                "exact",
                "ko",
                query,
                [gold.page_id],
                [block_id(gold, "exact")],
                [],
                f"난이도={'hard' if hard else 'easy'}; 근접 오답 {','.join(distractor_ids)}에는 {near}가 기록되어 있다.",
            )

    def prepare_relation(self) -> None:
        temporal_members = (self.page_count * 8 + 50) // 100
        owner_pool = self.by_type["synthesis"]
        target_pool = self.by_type["concept"]
        for query_index in range(QUERY_COUNT_PER_TYPE):
            owner = owner_pool[(temporal_members + 100 + query_index) % len(owner_pool)]
            target = target_pool[(self.seed + query_index * 7) % len(target_pool)]
            edge = Edge(target.ordinal, "related", "relation", "선행 근거")
            if not self.add_edge(owner, edge):
                raise AssertionError("could not assign relation target")
            self.relation_targets[owner.ordinal] = target
            service = f"{lexical_code(query_index + 300)}-astra"
            storage = f"{lexical_code(query_index + 400)}-boreal"
            query = f"{service} 서비스와 {storage} 저장소는 어떤 관계인가"
            self.add_text(
                owner,
                "relation",
                (
                    f"{service} 서비스와 {storage} 저장소의 관계는 선행 근거이며 "
                    f"[[{target.slug}]]로 연결된다."
                ),
            )
            self.add_text(
                target,
                "relation-target",
                f"{storage} 저장소는 검증 자료를 보관한다.",
            )
            hard = query_index % 3 == 2
            distractor_count = 6 if hard else 2
            distractor_ids: list[str] = []
            distractor_text = (
                f"{service} 서비스와 {storage} 저장소는 같은 색인 목록에 등장하지만 "
                "이 page는 둘의 관계를 판단하지 않는다."
            )
            for position in range(distractor_count):
                distractor = self.distractor_meta("relation", query_index, position)
                distractor_ids.append(distractor.page_id)
                self.add_text(distractor, "relation-distractor", distractor_text)
            hide_block = not hard and query_index % 3 == 1
            if hide_block:
                for position in range(6):
                    self.add_text(
                        owner,
                        f"relation-decoy-{position}",
                        distractor_text,
                    )
            self.add_question(
                "relation",
                "ko",
                query,
                [owner.page_id],
                [block_id(owner, "relation")],
                [],
                (
                    f"난이도={'hard' if hard else 'easy'}; block-hidden={str(hide_block).lower()}; "
                    f"{','.join(distractor_ids)}는 두 이름만 언급한다."
                ),
            )

    def prepare_paraphrase(self) -> None:
        temporal_members = (self.page_count * 8 + 50) // 100
        candidates = self.by_type["synthesis"]
        for query_index in range(QUERY_COUNT_PER_TYPE):
            gold = candidates[(temporal_members + 200 + query_index) % len(candidates)]
            canonical_marker = f"정본표현{hangul_marker(1200, query_index)}"
            product = f"{lexical_code(query_index + 500)}-nimbus"
            query = f"{product} 서비스 — 검수 생략 배포 승인 요건은 무엇인가"
            remainder = query_index % 10
            if remainder in {0, 3, 6, 9}:
                profile = "zero"
                gold_text = (
                    f"심사 없이 출시를 허가하는 기준은 {canonical_marker} 절차에 기록되어 있다."
                )
                distractor_count = 2
            else:
                shared_service = query_index % 2 == 1
                profile = "partial-easy" if remainder in {1, 7} else "partial-hard"
                service_text = " 서비스" if shared_service else ""
                if profile == "partial-easy":
                    self.add_title_term(gold, product)
                gold_text = (
                    f"{product}{service_text} 심사 없이 출시를 허가하는 기준은 "
                    f"{canonical_marker} 절차에 기록되어 있다."
                )
                distractor_count = 2 if profile == "partial-easy" else 6
            self.add_text(gold, "paraphrase", gold_text)
            distractor_ids: list[str] = []
            for position in range(distractor_count):
                distractor = self.distractor_meta("paraphrase", query_index, position)
                distractor_ids.append(distractor.page_id)
                self.add_text(
                    distractor,
                    "paraphrase-distractor",
                    f"{query}이라는 표현은 폐기된 초안에만 있고 이 page는 정답이 아니다.",
                )
            self.add_question(
                "paraphrase",
                "ko",
                query,
                [gold.page_id],
                [block_id(gold, "paraphrase")],
                [],
                f"난이도={profile}; distractors={','.join(distractor_ids)}.",
            )

    def prepare_conflicts(self) -> None:
        count = (self.page_count * 3 + 50) // 100
        stride = max(1, self.page_count // count)
        cursor = self.page_count - 1
        while len(self.conflict_ordinals) < count:
            self.conflict_ordinals.add(self.metas[cursor % self.page_count].ordinal)
            cursor -= stride

    def generic_text(self, meta: PageMeta) -> str:
        cluster = meta.ordinal % 97
        if meta.english:
            return (
                f"Benchmark record {meta.ordinal} preserves canonical evidence for knowledge "
                f"cluster {cluster}. Structural context determines how this record is used."
            )
        return (
            f"벤치마크 문서 {meta.ordinal}은 지식 묶음 {cluster}의 정본 근거를 기록한다. "
            "이 기록의 쓰임은 구조적 맥락으로 결정된다."
        )

    def title(self, meta: PageMeta) -> str:
        if meta.english:
            base = f"Source Record {meta.type_index + 1:06d}"
        else:
            base = f"{meta.type.capitalize()} 정본 {meta.type_index + 1:06d}"
        terms = self.title_terms[meta.ordinal]
        return f"{' '.join(terms)} — {base}" if terms else base

    def powerlaw_target(self, owner: PageMeta, excluded: set[int]) -> PageMeta:
        for _ in range(self.page_count * 2):
            rank = min(self.page_count - 1, int((self.rng.random() ** 3.2) * self.page_count))
            target = self.hub_order[rank]
            if target.ordinal != owner.ordinal and target.ordinal not in excluded:
                return target
        for target in self.hub_order:
            if target.ordinal != owner.ordinal and target.ordinal not in excluded:
                return target
        raise AssertionError("no available link target")

    def page_dates(self, meta: PageMeta) -> tuple[str, str]:
        temporal = self.temporal_position.get(meta.ordinal)
        if temporal is None:
            month = meta.ordinal % 12 + 1
            day = meta.ordinal % 28 + 1
            date = f"2026-{month:02d}-{day:02d}"
            return date, date
        _, position = temporal
        dates = ("2023-02-01", "2024-04-15", "2025-06-20", "2026-08-30")
        return dates[0], dates[position]

    def make_page(self, meta: PageMeta) -> dict[str, Any]:
        created, updated = self.page_dates(meta)
        blocks: dict[str, dict[str, Any]] = {}

        def add_block(value: dict[str, Any]) -> None:
            blocks[value["id"]] = value

        add_block(make_block(meta, "content", self.generic_text(meta)))
        available_roles = set(self.special_text[meta.ordinal])
        roles: list[str] = [
            role
            for role in ("exact", "temporal", "crosslingual", "paraphrase")
            if role in available_roles
        ]
        roles.extend(sorted(role for role in available_roles if role.startswith("relation-decoy-")))
        if "relation" in available_roles:
            roles.append("relation")
        roles.extend(sorted(available_roles.difference(roles)))
        for role in roles:
            stored_parts = self.special_text[meta.ordinal].get(role)
            if not stored_parts:
                continue
            parts = list(stored_parts)
            refs: list[str] = []
            if role == "relation":
                refs = [
                    self.meta_by_ordinal[edge.target].slug
                    for edge in self.required_edges[meta.ordinal]
                    if edge.role == "relation"
                ]
            elif role == "crosslingual":
                refs = [
                    self.meta_by_ordinal[edge.target].slug
                    for edge in self.required_edges[meta.ordinal]
                    if edge.role == "crosslingual"
                ]
                if refs:
                    if meta.english:
                        parts.append(f"See [[{refs[0]}]] for the paired entry.")
                    else:
                        parts.append(f"짝지은 근거는 [[{refs[0]}]]에 있다.")
            elif role == "temporal":
                refs = [
                    self.meta_by_ordinal[edge.target].slug
                    for edge in self.required_edges[meta.ordinal]
                    if edge.role == "temporal"
                ]
                if refs:
                    parts.append(f"이 판본은 [[{refs[0]}]]을 대체한다.")
            add_block(make_block(meta, role, "\n".join(parts), refs=refs))

        if meta.ordinal in self.conflict_ordinals:
            conflict_text = (
                "Two incompatible readings remain pending; no conclusion has been selected."
                if meta.english
                else "서로 양립할 수 없는 두 해석이 남아 있으며 아직 결론을 정하지 않았다."
            )
            add_block(
                make_block(
                    meta,
                    "conflict",
                    conflict_text,
                    kind="conflict",
                    resolution={"status": "unresolved"},
                )
            )

        edges = list(self.required_edges[meta.ordinal])
        excluded = {edge.target for edge in edges}
        while len(edges) < 4:
            target = self.powerlaw_target(meta, excluded)
            excluded.add(target.ordinal)
            edges.append(Edge(target.ordinal, "wiki", "links", target.slug))

        generic_edges = [edge for edge in edges if edge.role == "links"]
        if generic_edges:
            targets = [self.meta_by_ordinal[edge.target] for edge in generic_edges]
            if meta.english:
                link_text = "Connected records: " + ", ".join(
                    f"[[{target.slug}]]" for target in targets
                ) + "."
            else:
                link_text = "연결 항목: " + ", ".join(
                    f"[[{target.slug}]]" for target in targets
                ) + "."
            add_block(make_block(meta, "links", link_text, refs=[item.slug for item in targets]))

        links: list[dict[str, str]] = []
        for edge in edges:
            target = self.meta_by_ordinal[edge.target]
            links.append(
                {
                    "target": target.slug,
                    "label": edge.label,
                    "anchor": "",
                    "kind": edge.kind,
                    "block_id": block_id(meta, edge.role),
                }
            )

        source_pages = self.by_type["source"]
        if meta.type == "source":
            sources = ["user:2026-09-01"]
        else:
            sources = [source_pages[meta.ordinal % len(source_pages)].page_id]
        page: dict[str, Any] = {
            "schema_version": "1.0",
            "id": meta.page_id,
            "slug": meta.slug,
            "title": self.title(meta),
            "type": meta.type,
            "created": created,
            "updated": updated,
            "tags": [meta.type, "benchmark", f"cluster-{meta.ordinal % 17:02d}"],
            "projects": [("alpha", "beta", "shared")[meta.ordinal % 3]],
            "sources": sources,
            "summary": (
                "Synthetic English benchmark page."
                if meta.english
                else "구조 검색용 합성 벤치마크 page."
            ),
            "blocks": blocks,
            "block_order": list(blocks),
            "links": links,
            "history": [
                {
                    "at": updated,
                    "action": "generated",
                    "actor": "bench-gen",
                    "note": f"seed={self.seed}",
                }
            ],
        }
        if meta.type in {"concept", "entity"}:
            page["aliases"] = {
                "ko": [f"{meta.type}가니어-{meta.type_index + 1:06d}"],
                "en": [f"{meta.type}-alias-{meta.type_index + 1:06d}"],
            }
        return page

    def build(self) -> tuple[list[tuple[PageMeta, dict[str, Any]]], dict[str, Any]]:
        # Question preparation order intentionally differs from output order; q IDs are reset below.
        self.prepare_temporal()
        temporal_questions = list(self.questions)
        self.questions.clear()
        self.question_number = 0

        self.prepare_crosslingual()
        crosslingual_questions = list(self.questions)
        self.questions.clear()
        self.question_number = 0

        self.prepare_exact()
        exact_questions = list(self.questions)
        self.questions.clear()
        self.question_number = 0

        self.prepare_relation()
        relation_questions = list(self.questions)
        self.questions.clear()
        self.question_number = 0

        self.prepare_paraphrase()
        paraphrase_questions = list(self.questions)
        self.questions.clear()
        self.question_number = 0
        self.prepare_conflicts()

        # Re-number in the SPEC table order.
        ordered = (
            exact_questions
            + relation_questions
            + temporal_questions
            + crosslingual_questions
            + paraphrase_questions
        )
        for number, question in enumerate(ordered, start=1):
            question["id"] = f"q{number:05d}"
        self.questions = ordered

        pages = [(meta, self.make_page(meta)) for meta in self.metas]
        # 실제 운용 사용자가 한국어로 질문하므로 모든 query의 lang은 ko로 유지한다.
        # 영어는 crosslingual gold evidence에만 남고 제품명/차용어는 자연스럽게 공유된다.
        queries = {
            "schema_version": "1.0",
            "seed": self.seed,
            "corpus_pages": self.page_count,
            "queries": self.questions,
        }
        self.assert_invariants(pages, queries)
        return pages, queries

    def assert_invariants(
        self,
        pages: list[tuple[PageMeta, dict[str, Any]]],
        query_set: dict[str, Any],
    ) -> None:
        by_id = {page["id"]: page for _, page in pages}
        if len(pages) != self.page_count or len(by_id) != self.page_count:
            raise AssertionError("page count or page IDs are invalid")
        counts = allocate_type_counts(self.page_count)
        observed = {page_type: 0 for page_type, _, _ in TYPE_LAYOUT}
        for _, page in pages:
            observed[page["type"]] += 1
            if not page["tags"] or not page["projects"]:
                raise AssertionError(f"empty tags/projects: {page['id']}")
            if len(page["links"]) != 4:
                raise AssertionError(f"outgoing link count is not four: {page['id']}")
            for link in page["links"]:
                owner_block = page["blocks"].get(link["block_id"])
                if owner_block is None or f"[[{link['target']}]]" not in owner_block["source_text"]:
                    raise AssertionError(f"invalid link block: {page['id']} -> {link['target']}")
        if observed != counts:
            raise AssertionError(f"type mix mismatch: {observed} != {counts}")

        temporal_members = sum(len(chain) for chain in self.chain_members)
        expected_temporal = (self.page_count * 8 + 50) // 100
        if temporal_members != expected_temporal:
            raise AssertionError("temporal membership ratio mismatch")
        expected_conflicts = (self.page_count * 3 + 50) // 100
        conflicts = sum(
            block["kind"] == "conflict"
            for _, page in pages
            for block in page["blocks"].values()
        )
        if conflicts != expected_conflicts:
            raise AssertionError("conflict ratio mismatch")
        english_sources = sum(meta.english for meta in self.by_type["source"])
        if english_sources != len(self.by_type["source"]) * 40 // 100:
            raise AssertionError("English source ratio mismatch")

        query_types: dict[str, int] = {}
        surface_profiles = {
            "paraphrase": {"zero": 0, "partial": 0},
            "crosslingual": {"zero": 0, "partial": 0},
        }
        temporal_profiles = {"strong": 0, "weak": 0, "current": 0}
        for query in query_set["queries"]:
            query_types[query["type"]] = query_types.get(query["type"], 0) + 1
            if query["lang"] != "ko":
                raise AssertionError(f"benchmark user questions must be Korean: {query['id']}")
            for page_id in query["gold_pages"] + query["stale_pages"]:
                if page_id not in by_id:
                    raise AssertionError(f"query references missing page: {query['id']} {page_id}")
            for gold_page in query["gold_pages"]:
                for gold_block in query["gold_blocks"]:
                    if gold_block in by_id[gold_page]["blocks"]:
                        break
                else:
                    raise AssertionError(
                        f"query has no gold block on gold page: {query['id']} {gold_page}"
                    )
            query_tokens = set(tokens(query["text"]))
            if query["type"] in {"paraphrase", "crosslingual"}:
                for gold_page in query["gold_pages"]:
                    gold_body = body_text(by_id[gold_page]).lower()
                    overlap = [token for token in query_tokens if token in gold_body]
                    profile = "zero" if "난이도=zero" in query["notes"] else "partial"
                    surface_profiles[query["type"]][profile] += 1
                    if self.page_count >= 10_000 and profile == "zero" and overlap:
                        raise AssertionError(
                            f"zero-overlap profile leaked in {query['type']} {query['id']}: {overlap}"
                        )
                    if (
                        self.page_count >= 10_000
                        and profile == "partial"
                        and not 1 <= len(overlap) <= 2
                    ):
                        raise AssertionError(
                            f"partial-overlap profile has {len(overlap)} tokens in "
                            f"{query['type']} {query['id']}: {overlap}"
                        )
            if query["type"] == "temporal":
                profile = next(
                    name for name in temporal_profiles if f"난이도={name}" in query["notes"]
                )
                temporal_profiles[profile] += 1
                current_overlap = len(query_tokens.intersection(tokens(body_text(by_id[query["gold_pages"][0]]))))
                for stale_page in query["stale_pages"]:
                    stale_overlap = len(
                        query_tokens.intersection(tokens(body_text(by_id[stale_page])))
                    )
                    if (
                        self.page_count >= 10_000
                        and profile in {"strong", "weak"}
                        and stale_overlap <= current_overlap
                    ):
                        raise AssertionError(
                            f"temporal lexical trap is too weak: {query['id']} {stale_page}"
                        )
                    if (
                        self.page_count >= 10_000
                        and profile == "current"
                        and current_overlap <= stale_overlap
                    ):
                        raise AssertionError(
                            f"current lexical profile does not win: {query['id']} {stale_page}"
                        )
        expected_query_types = {
            name: QUERY_COUNT_PER_TYPE
            for name in ("exact", "relation", "temporal", "crosslingual", "paraphrase")
        }
        if query_types != expected_query_types:
            raise AssertionError(f"query mix mismatch: {query_types}")
        expected_surface = {"zero": 40, "partial": 60}
        for query_type, observed_profiles in surface_profiles.items():
            if observed_profiles != expected_surface:
                raise AssertionError(
                    f"{query_type} surface profile mismatch: {observed_profiles}"
                )
        if temporal_profiles != {"strong": 55, "weak": 30, "current": 15}:
            raise AssertionError(f"temporal profile mismatch: {temporal_profiles}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--out", type=Path, default=Path("bench/corpus"))
    parser.add_argument("--queries", type=Path, default=Path("bench/queries.json"))
    args = parser.parse_args(argv)
    if args.pages < MIN_PAGES:
        parser.error(f"--pages must be at least {MIN_PAGES}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    builder = CorpusBuilder(args.pages, args.seed)
    pages, queries = builder.build()
    safe_recreate_directory(args.out)
    for meta, page in pages:
        write_json(args.out / meta.directory / f"{meta.slug}.json", page)
    write_json(args.queries, queries)
    print(
        f"generated pages={args.pages} queries={len(queries['queries'])} "
        f"seed={args.seed} out={args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
