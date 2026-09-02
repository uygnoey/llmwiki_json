# 검색 벤치마크 계약 (bench/)

목적: JSON 정본의 **구조**를 쓰는 랭커가 어휘 랭커·벡터 랭커보다 나은지를
질문 유형별 수치로 판정한다. 주장 말고 숫자를 낸다.

이 파일이 유일한 계약이다. 인터페이스를 바꾸고 싶으면 먼저 coordinator에게 ask.

## 절대 규칙

- `wiki/`, `raw/`, `index/`, `scripts/`, `viewer/` **수정 금지**. 읽기만.
- 작업은 배정된 경로 안에서만. 남의 파일 건드리지 않는다.
- `git commit` / `git push` 금지. coordinator가 한다.
- 모든 생성물은 seed 고정 → 같은 seed면 바이트 단위로 같아야 한다.
- 외부 네트워크는 qmd 설치 외 금지.

## 경로 소유권

| 경로 | 소유자 |
|---|---|
| `bench/gen/**`, `bench/queries.json`, `bench/corpus/**` | W1 |
| `bench/rankers/structural.py`, `bench/index/structural/**` | W2 |
| `bench/rankers/baseline.py`, `bench/rankers/vector.py` | W3 |
| `bench/SPEC.md`, `bench/harness.py`, `bench/rankers/base.py`, `bench/results/**` | coordinator |

## 랭커 인터페이스 (bench/rankers/base.py, coordinator 제공)

```python
@dataclass
class Hit:
    page_id: str                 # "page:<slug>"
    score: float
    block_ids: list[str] = ...   # 근거 block, 없으면 빈 리스트

@dataclass
class BuildStats:
    elapsed_ms: float
    index_bytes: int             # 색인 산출물 총 바이트
    notes: dict                  # 자유 형식

class Ranker(Protocol):
    name: str
    @classmethod
    def build(cls, corpus_dir: Path, index_dir: Path, **opts) -> BuildStats: ...
    @classmethod
    def load(cls, corpus_dir: Path, index_dir: Path, **opts) -> "Ranker": ...
    def search(self, query: str, k: int = 10) -> list[Hit]: ...   # score 내림차순
```

- `build`는 `index_dir`를 통째로 재생성한다(멱등).
- `search`는 순수 조회. 색인을 다시 만들면 안 된다(지연 측정이 오염됨).
- import 실패나 의존성 부재는 예외를 던져라. harness가 "unavailable"로 기록한다.

## 코퍼스 (W1)

`bench/corpus/{sources,entities,concepts,syntheses,projects}/*.json`.
page 스키마는 `tools/schema/page.schema.json`을 따른다(검증 통과 필수).
`wiki/concepts/review-policy.json`이 실물 예시다.

`--pages N` 으로 규모 조절: **300 / 3000 / 10000** 세 지점 모두 생성 가능해야 한다.

구성비: sources 50%, entities 25%, syntheses 19%, concepts 5%, projects 1%.

필수 성질:
1. **링크**: page당 outgoing 평균 4개, 대상은 power-law로 편중. `links[]`의
   `block_id`는 실제로 그 링크를 담은 block을 가리켜야 한다. 본문엔 `[[slug]]`.
2. **대체 관계**: 전체의 8%가 `supersedes` 체인(깊이 2~4)에 속한다.
   **낡은 page의 본문이 현재 page보다 질문과 어휘가 더 겹치게 만들어라.**
   그래야 시간 축 없는 랭커가 실제로 속는다. 이게 이 벤치의 핵심이다.
3. **상충**: 3%가 `kind:"conflict"` block을 갖고 `resolution.status != "resolved"`.
4. **언어**: sources의 40%는 영어 본문. 한국어 page와 영어 page가 같은 개념을
   다루는 쌍이 존재해야 한다.
5. **aliases**: concept/entity page에 선택 필드 `aliases: {"ko": [...], "en": [...]}`.
   현재 스키마엔 없는 제안 필드다. 넣되, 다른 필드와 독립적이어야 한다
   (이걸 쓰는 랭커와 안 쓰는 랭커를 비교할 것이므로).
6. `projects`/`tags` 비우지 않는다.

## 질문 세트 (W1) — bench/queries.json

```json
{"schema_version":"1.0","seed":1234,"corpus_pages":10000,
 "queries":[
   {"id":"q00001","type":"exact","lang":"ko","text":"...",
    "gold_pages":["page:..."],"gold_blocks":["block:..."],
    "stale_pages":["page:..."],"notes":"..."}]}
```

유형별 **각 100개**(총 500개). 정답은 생성 시점에 **구성으로** 알고 있어야 한다
(사후 추정 금지).

| type | 정의 | 함정 |
|---|---|---|
| `exact` | ID·버전·날짜·설정키 같은 정확 토큰 | `v2.3.1` vs `v2.1.3` 같은 근접 오답을 코퍼스에 심을 것 |
| `relation` | A와 B의 관계를 묻는다 | 정답은 관계를 주장하는 **block** (`gold_blocks` 필수) |
| `temporal` | 현재 상태를 묻는다 | `stale_pages`에 낡은 버전. 낡은 쪽이 어휘상 더 가깝게 |
| `crosslingual` | 한국어 질문, 영어 gold page | 표면형이 전혀 안 겹쳐야 한다 |
| `paraphrase` | 정본에 없는 어휘로 묻는다 | gold page 본문에 질문 표면형이 **하나도** 없을 것 |

`stale_pages`는 temporal에 필수, 나머지는 빈 배열 가능.

## 지표 (coordinator harness)

전체 + 유형별로:
- recall@1 / @5 / @10, precision@5, MRR@10, nDCG@10
- `block_recall@5` — `gold_blocks` 있는 질문만
- **`staleness_rate`** — `stale_pages` 중 하나라도 모든 `gold_pages`보다 위에
  랭크된 질문의 비율. 낮을수록 좋다. 벡터 대비 핵심 차별 지표.
- 지연 p50/p95/p99 (ms, 질문당)
- `build_ms`, `index_bytes`

## 실행

```
python3 bench/gen/generate.py --pages 10000 --seed 1234 --out bench/corpus --queries bench/queries.json
python3 bench/harness.py --corpus bench/corpus --queries bench/queries.json --rankers baseline,structural,vector --k 10
```

harness는 `bench/results/<pages>p-<ranker>.json`과 비교표를 낸다.
