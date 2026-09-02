# 컨텍스트 페이로드 보고 — 훅이 실제로 LLM 에 넣는 문자열을 잰 결과

대상: `scripts/llmwiki_context.py` 가 매 프롬프트에 붙이는 `<llmwiki-context>` 페이로드.
코퍼스·질문: `bench/frozen/` (10000 page, 500 문항). 하네스 `bench/context/harness_ctx.py`, arm `bench/context/arms.py`, 색인 `bench/index_ctx/`, 결과 `bench/results_ctx/`. 표준 라이브러리만. qmd·md 경로는 쓰지 않았다(`use_qmd=False`).
검색 순위(bench/harness.py, V2_REPORT) 가 아니라 **페이로드**를 잰 것이다: 정답 block 본문이 예산 안에 실제로 들어갔는지, 몇 바이트로 들어갔는지, 낡은 page 의 block 이 "대체됨" 표시 없이 섞였는지, 검색+투영+렌더 전체 지연.

## 1. 예산별 비교표

gold_block = gold block **본문**이 페이로드에 있는 문항 비율(arm 의 manifest 와 본문 문자열 검사 둘 다 참; 둘의 불일치 0건). stale_body = stale page 의 block 본문이 들어간 비율, stale_leak = 그중 "대체됨" 표시가 없는 비율(temporal 100문항). B/정답 = Σbytes ÷ Σ(전달된 gold block 수). est_tokens = `llmwiki_context.est_tokens`(UTF-8 3바이트 = 1토큰, 보수적). 지연은 검색+투영+렌더 전체이고 production 은 정본 10000 파일 스캔을 포함한다.

| arm | 예산 | gold_block | gold_page | gold_addr | stale_body | stale_leak | bytes 평균 | est_tokens | B/정답 | p50 ms | p95 ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| production | 2000 | 0.090 | 0.090 | 0.090 | 0.890 | 0.890 | 1646 | 549 | 18295 | 964.6 | 1083.4 |
| production | 4000 | 0.358 | 0.424 | 0.358 | 0.900 | 0.900 | 3025 | 1009 | 8449 | 964.6 | 1083.4 |
| **production** | **6000** | **0.358** | 0.424 | 0.358 | 0.900 | **0.900** | 3051 | 1017 | **8522** | 964.5 | 1083.4 |
| v2-text | 2000 | 0.760 | 0.760 | 0.760 | 0.000 | 0.000 | 1726 | 576 | 2271 | 1.95 | 2.62 |
| v2-text | 4000 | 0.840 | 0.840 | 0.840 | 0.000 | 0.000 | 3205 | 1069 | 3815 | 1.81 | 2.48 |
| v2-text | 6000 | 0.840 | 0.840 | 0.840 | 0.000 | 0.000 | 3205 | 1069 | 3815 | 1.79 | 2.43 |
| v2-graph | 2000 | 0.840 | 0.840 | 0.840 | 0.000 | 0.000 | 1945 | 649 | 2315 | 2.02 | 2.64 |
| v2-graph | 4000 | 0.920 | 0.920 | 0.920 | 0.000 | 0.000 | 3305 | 1102 | 3592 | 1.91 | 2.49 |
| **v2-graph** | **6000** | **0.920** | 0.920 | 0.920 | 0.000 | **0.000** | 3318 | 1106 | **3606** | 1.89 | 2.51 |
| v2-graph[cut=0.5] | 2000 | 0.840 | 0.840 | 0.840 | 0.000 | 0.000 | 1420 | 474 | 1690 | 1.91 | 2.56 |
| v2-graph[cut=0.5] | 4000 | 0.920 | 0.920 | 0.920 | 0.000 | 0.000 | 1818 | 606 | 1977 | 1.78 | 2.45 |
| **v2-graph[cut=0.5]** | **6000** | **0.920** | 0.920 | 0.920 | 0.000 | **0.000** | 1818 | 606 | **1977** | 1.78 | 2.43 |
| v2-graph[cut=0.7] | 6000 | 0.858 | 0.858 | 0.858 | 0.000 | 0.000 | 1213 | 405 | 1414 | 1.73 | 2.38 |
| v2-graph[k=5] | 6000 | 0.840 | 0.840 | 0.840 | 0.000 | 0.000 | 1877 | 626 | 2234 | 1.81 | 2.39 |
| v2-graph-json | 6000 | 0.920 | 0.920 | 0.920 | 0.000 | 0.000 | 3571 | 1191 | 3882 | 2.13 | 2.68 |
| v2-address | 2000 | 0.000 | 0.920 | 0.920 | 0.000 | 0.000 | 1701 | 568 | — | 1.89 | 2.50 |
| v2-address | 6000 | 0.000 | 0.920 | 0.920 | 0.000 | 0.000 | 1701 | 568 | — | 1.76 | 2.35 |
| v2-address[cut=0.5] | 6000 | 0.000 | 0.920 | 0.920 | 0.000 | 0.000 | 1020 | 340 | — | 1.72 | 2.41 |

낡은 page 처리를 검색기가 아니라 **형식**이 막는지 보려고 structural2 의 supersedes 접기(fold) 를 끈 교란 arm 도 쟀다(같은 색인, 조회 규칙만 끔):

| arm (fold=false) | 예산 | gold_block | stale_body | stale_leak | bytes | B/정답 |
|---|---:|---:|---:|---:|---:|---:|
| v2-text[fold=false] | 6000 | 0.730 | 1.000 | **1.000** | 3205 | 4390 |
| v2-graph[fold=false] | 6000 | 0.810 | 0.000 | **0.000** | 2750 | 3396 |
| v2-graph[cut=0.5,fold=false] | 6000 | 0.810 | 0.000 | 0.000 | 1802 | 2225 |

유형별 (예산 6000, gold_block / B/정답):

| arm | exact | relation | temporal | crosslingual | paraphrase |
|---|---:|---:|---:|---:|---:|
| production | 0.80 / 3937 | 0.34 / 9818 | 0.45 / 7063 | 0.20 / 15321 | 0.00 / — |
| v2-text | 1.00 / 3056 | 1.00 / 3139 | 1.00 / 3432 | 1.00 / 3173 | 0.20 / 16119 |
| v2-graph | 1.00 / 2636 | 1.00 / 3242 | 1.00 / 4026 | 1.00 / 3552 | 0.60 / 5219 |
| v2-graph[cut=0.5] | 1.00 / 2636 | 1.00 / 968 | 1.00 / 788 | 1.00 / 2625 | 0.60 / 3459 |
| v2-graph[cut=0.7] | 1.00 / 1374 | 1.00 / 968 | 1.00 / 788 | 1.00 / 1588 | 0.29 / 4647 |

전체 표(모든 arm × 예산, 유형별) 는 `bench/results_ctx/500q-summary.json`, 문항별 행은 `500q-<arm>-<예산>.json` 의 `per_query`.

## 2. 판정

1. **production 훅은 6000바이트를 쓰고도 정답 block 을 세 문항 중 하나에만 넣는다(0.358).** 상위 5 page × 최대 6 block 이지만 실제로는 page 당 1.1 block, 3051 바이트를 쓴다 — 예산이 남는데 정답이 없다. 원인은 검색이다: 어휘 랭커가 정답과 distractor 를 동점(page_id 순 tie-break)으로 세워 exact 도 0.80, relation 0.34, crosslingual 0.20, paraphrase 0.00 이다. 2000 바이트에서는 0.090 — page 단위 채움이라 머리말 586 바이트 뒤에 page 두 개가 들어가는데 그 둘이 distractor 다.
2. **낡은 주장은 production 에서 temporal 100문항 중 90건이 표시 없이 들어간다(stale_leak 0.900).** 55건은 낡은 block 만 있고 정답이 없으며, 35건은 둘이 나란히 있는데 어느 쪽이 현재인지 페이로드가 말해 주지 않는다(정본 본문의 "이 판본은 X 를 대체한다" 문장에만 기댄다). v2 arm 은 모두 0 인데 이유가 다르다: fold 가 켜진 structural2 는 낡은 page 를 head × 0.3 으로 내려 상위 10 밖으로 보내므로 v2-text 도 0 이지만, fold 를 끄면 v2-text 는 **100%** 새고 v2-graph 는 여전히 0 이다 — v2-graph 는 head≠self 인 page 의 본문을 싣지 않고 `P <slug> … sup→<head>` 한 줄만 내기 때문이다. 즉 "낡은 문서 차단"은 검색기 규칙과 페이로드 형식 두 겹으로 있어야 하고, 형식 쪽만으로도 누출은 0 이다(정답 회수는 fold 가 맡는다: 0.92 → 0.81).
3. **같은 예산에서 정답 하나당 바이트는 production 8522 → v2-graph 3606 (2.4배) → v2-graph[cut=0.5] 1977 (4.3배) 로 준다.** 커버리지는 0.358 → 0.920. cut=0.5(1위 점수의 절반 미만 page 는 버림)는 커버리지를 하나도 잃지 않고 바이트를 45% 줄인다 — 잘려 나가는 것은 relation·temporal 의 동점 distractor(0.43 배)뿐이다. cut=0.7 은 paraphrase 의 7위 gold 를 자르므로(0.92→0.86) 넘지 않는 편이 낫다. **이 컷은 이 질문 세트에서 고른 값**이라 감사 보고서가 지적한 종류의 절벽인지 실제 질문으로 다시 봐야 한다.
4. **형식 선택 근거 — 축약 텍스트 > compact JSON > production 마크다운.** 같은 내용을 JSON 으로 내면(v2-graph-json) 253 바이트(7.6%) 더 든다: 따옴표·키 이름·중괄호가 본문 짧은 block 에서는 본문보다 비싸다. production 형식은 page 머리에 title/summary/tags/projects/score/file 6줄(page 당 약 300 바이트) 을 싣는데 그중 질문에 답할 때 쓰이는 것이 없다 — 이 벤치에서 summary 는 전 page 동일 상투문이고 file 은 map.json 이 안다. v2-graph 의 P 줄은 slug·type·updated·sources 만(약 60 바이트), B 줄은 `slug#tail 상태 | 본문`, E 줄은 related/supersedes 만(wiki 링크는 본문 `[[…]]` 에 이미 있다). 머리말도 586 → 410 바이트.
5. **v2-text(검색만 교체) 는 커버리지를 0.84 로 올리지만 바이트/정답은 3815 로 v2-graph 보다 나쁘고 낡은 page 표시가 없다.** 검색기 교체만으로는 절반이다.
6. **2단계 전략(v2-address).** 주소만 주면 1701 바이트(cut=0.5 면 1020) 에 gold 주소가 0.92 들어가고, 그 뒤 `llmwiki_get(selector="slug#tail")` 로 정답 block 하나를 집는 비용이 평균 677 바이트(indent=2 JSON)다. 합쳐 1698~2379 바이트로 v2-graph[cut=0.5] 의 1818 과 비슷하다 — 단 LLM 이 맞는 주소를 고른다는 가정 위에서, 그리고 도구 왕복 한 번을 더 낸다. 주소 줄의 "한 줄 요약"이 이 코퍼스에서는 정보가 없어(제목·summary 가 상투문) 실제 wiki 에서 다시 봐야 한다.
7. **지연.** production p50 965 ms / p95 1083 ms (10000 파일 스캔), v2 arm 은 p50 1.8~2.0 ms / p95 2.4~2.7 ms (검색 sqlite 9.3 MB + 투영 sqlite 12.1 MB). 500배.
8. **비용 한 줄.** 만 건 wiki 에서 하루 100 프롬프트마다 페이로드를 붙이면 production 은 약 **10.2만 토큰/일**(3.05 M/월, Opus 5 입력 $5/M 기준 **$15.3/월**, Sonnet 5 $6.1), v2-graph[cut=0.5] 는 **6.1만 토큰/일**($9.1 / $3.6) 로 정답 커버리지 2.6배를 내면서 토큰은 40% 적다. (est_tokens 는 한글 1자=1토큰인 보수 추정이라 실제 청구는 더 적다.)

이 수치의 한계는 감사 보고서(`bench/audit/REPORT.md`) 와 같다: 합성 코퍼스라 block 이 짧고(평균 79자, 최대 205자) 320자 clip 이 한 번도 걸리지 않았으며, 질문 500개가 전부 정답이 있는 질문이라 **무주입(below-threshold) 판정은 재지 못했다**(production 은 20건 무주입, 전부 paraphrase). 실제 위키 block 은 길고 질문의 절반은 위키와 무관하므로 clip 과 무주입 문턱은 실문서 질문 세트로 따로 재야 한다.

## 3. 방법

- **하네스** `bench/context/harness_ctx.py`: 예산 2000/4000/6000 마다 arm 이 만든 문자열을 받아 위 지표를 낸다. gold/stale 판정은 arm 이 낸 manifest(무엇을 어떤 상태로 실었는지) 와 본문 문자열 검사(block 본문 앞 60자, 공백 제거) 를 **둘 다** 요구한다 — 불일치는 `manifest_mismatch` 로 집계하며 전 arm 0 이었다. 낡은 page 의 "대체됨 표시"는 manifest 에서 그 page 의 status 가 `superseded` 인 것으로 정의했다(production 형식에는 그런 표시가 없다).
- **production arm**: `scripts.llmwiki_context.build_context(root, query, use_qmd=False)` 를 그대로 부른다. root 는 `bench/index_ctx/root/wiki/**` 에 동결 코퍼스를 **하드링크**한 뷰다(baseline.py 의 심링크 방식은 `path.resolve()` 가 절대경로를 풀어 `file:` 줄이 실제보다 50바이트 길어지므로 바꿨다). 머리말의 root 경로는 실제 저장소 경로로 치환해 바이트를 셌다. build_context 는 예산과 무관하게 한 번 스캔하므로 질문당 한 번 부르고 `render`/`render_hint` 만 예산별로 다시 돌렸다(build_context 내부와 같다). `tools/config/context.json` 이 없어 always 몫은 0.
- **v2 arm**: `bench/rankers/structural2.py` 를 `bench/index_ctx/structural2/` 에 build(기본 옵션, dup 끔) 하고, 투영용 `bench/index_ctx/ctx.sqlite`(page/blk/edge 표, `CtxIndex.build`) 를 따로 굽는다. 조회는 두 sqlite 만 읽는다(정본 파일 접근 없음). 근거 block 은 structural2 가 돌려주는 `block_ids`(head 의 supersedes anchor → 간선 소유 block → 어휘 상위, 최대 4).
- **v2-graph 형식**(`bench/context/arms.py::V2GraphArm`):
  ```
  <llmwiki-context v=2>
  정본(wiki/**/*.json) 부분 그래프. P=page(slug type updated src=근거 sources) B=block(<slug>#<id> 상태 | 본문) E=간선(block kind→대상). 상태 cur=현재 주장, conflict=미판정 상충(양쪽 병기), sup→X=X 로 대체된 낡은 page(본문 생략, 인용 금지). 근거 밖 내용은 모른다고 답하라. 더 필요하면 llmwiki_get(selector="<slug>#<id>").
  P synthesis-000002 synthesis 2024-04-15 src=page:source-002503
  B synthesis-000002#temporal cur | 최신 결정에 따라 체계는 활성 단계로 전환되었다. … 이 판본은 [[synthesis-000001]]을 대체한다.
  E synthesis-000002#temporal supersedes→synthesis-000001
  P synthesis-000001 synthesis 2023-02-01 sup→synthesis-000002
  </llmwiki-context>
  ```
  block 단위로 예산을 채운다(page 단위인 production 과 다름). B 가 하나도 못 들어가는 page 의 P 줄은 버린다. `slug#tail` 주소는 `llmwiki_get` 의 `resolve_blocks` 가 이미 푸는 축약형이다.
- **재현**
  ```
  python3 - <<'EOF'   # production 뷰 (하드링크)
  import os; from pathlib import Path
  src=Path('bench/frozen/corpus').resolve(); dst=Path('bench/index_ctx/root/wiki')
  for p in sorted(src.rglob('*.json')):
      t=dst/p.relative_to(src); t.parent.mkdir(parents=True,exist_ok=True); os.link(p,t)
  EOF
  python3 bench/context/harness_ctx.py --arms production --out bench/results_ctx            # ~8분 (색인 build 포함)
  python3 bench/context/harness_ctx.py --no-rebuild --out bench/results_ctx --arms \
    "v2-text,v2-graph,v2-graph:cut=0.5,v2-graph:cut=0.7,v2-graph:k=5,v2-graph-json,v2-address,v2-address:cut=0.5,v2-text:fold=false,v2-graph:fold=false,v2-graph:fold=false;cut=0.5"
  python3 bench/context/harness_ctx.py --merge --out bench/results_ctx && python3 bench/context/report_tables.py
  ```
  결과 JSON 은 지연값을 빼면 결정적이다(같은 색인, 같은 질문).

## 4. 설계 제안 — llmwiki_context.py 가 정본 스캔 대신 index/search.sqlite 를 읽고 v2-graph 를 내보내려면

구현하지 않았다. 정본·scripts 는 손대지 않았고, 아래는 함수 단위의 변경 명세다. 원칙 셋:
(1) `search.sqlite` 는 `index/*.json` 과 같은 **파생물**이라 `python3 scripts/llmwiki.py build` 만 만든다,
(2) 훅은 색인이 없거나 낡았으면 지금의 정본 스캔으로 **fail-open** 한다,
(3) 주입 본문은 여전히 정본 block 본문이다 — 색인의 `blk.text` 는 `map.json` 의 page sha256 으로 정본과 묶여 있고, 어긋나면 그 page 만 정본 파일에서 다시 읽는다.

### 4.1 색인 (scripts/llmwiki.py, build 단계)

| 함수 | 변경 |
|---|---|
| `project(ws)` | 그대로. `map.json` 의 page 별 `sha256` 이 신선도 키다. sqlite 는 JSON 이 아니라 `project()` 의 반환 dict 에 넣지 않는다. |
| `build(ws)` | `project()` 뒤에 `build_search_index(pages, ws.index / "search.sqlite", revision)` 를 부른다(신규). 내용 = `bench/rankers/structural2.py::Structural2Ranker.build` 의 표(`page`,`blk`,`post`,`adj`,`meta`) + `bench/context/arms.py::CtxIndex.build` 의 표(`page` 확장 열 slug/title/type/updated/sources/projects/tags/summary/head/file/unresolved, `blk` 확장 열 text/kind/pos/unresolved/refs, `edge`). 두 `page`·`blk` 표는 하나로 합친다(rid 정수 키 + page_id/block_id 텍스트 키). `meta` 에 `revision`(revision.json 값) 과 page 별 `sha256`(map.json 값) 을 적는다. build 는 결정적이어야 한다(structural2 는 sha 동일 확인됨). `viewer/public/data/` 에는 복사하지 않는다(뷰어가 안 쓴다). |
| dev 서버 감시 | `wiki/**/*.json` 변경이 이미 build 를 다시 부르므로 search.sqlite 도 같이 갱신된다. 변경 없음. |

### 4.2 검색 (scripts/llmwiki_context.py)

| 현재 함수 | 변경 |
|---|---|
| `load_corpus(root)` | 훅 경로에서 **호출하지 않는다**. `open_index(root) -> sqlite3.Connection \| None` 신설: `index/search.sqlite` 를 `mode=ro` 로 열고 `meta.revision` 을 `index/revision.json` 과 비교, 파일이 없으면 `None`. `None` 이면 지금의 `load_corpus`+`rank` 경로로 떨어진다(fail-open, stats 에 `fallback:"scan"`). revision 이 다를 때는 §4.5. |
| `query_tokens` / `tokenize` / `match_strength` / `STOPWORDS` | 스캔 폴백 전용으로 남긴다. 색인 경로의 토큰화는 `structural2.tokenize`(한글 음절 2-gram + 라틴 낱말) 를 옮긴다 — 조사 사전이 필요 없다. |
| `rank(docs, tokens)` | 색인 경로: `search_index(db, query, k) -> list[Hit]` 신설 = `Structural2Ranker._lex` + `_ppr` + fold 규칙(조회부 약 140줄을 옮긴다; `bench/rankers/` 를 import 하지 않는다). 상수 `MAX_POST, IDF_POW, SEEDS, STEPS, W_GRAPH, STALE_SHOW` 등을 모듈 상수로 가져온다. |
| `Hit` / `Doc` | `Hit` 을 `(page_id, score, block_ids, head, raw_top, via, doc: Doc \| None)` 으로 바꾼다. `doc` 은 스캔 폴백일 때만 채운다. `Result` 는 그대로. |
| `rank_blocks(doc, tokens, idf, limit)` | 색인 경로에서는 부르지 않는다. 근거 block 은 `search_index` 가 고른 `block_ids` 이고 본문은 `fetch_blocks(db, block_ids)` 로 `blk.text` 에서 읽는다. 폴백 전용으로 남긴다. |
| `retrieve(root, query, ...)` | 시그니처 유지. `db = open_index(root)` 가 있으면 `search_index`, 없으면 기존 경로. **무주입/힌트 판정**은 지금의 절대 점수(`MIN_SCORE=6.0`, idf 합) 를 못 쓴다 — structural2 점수는 1위=1.0 인 상대값이다. 대신 (a) 1위 page 의 raw 어휘 impact(`_lex` 가 정규화하기 전 값) 와 (b) posting 이 있던 질문 토큰 비율(coverage) 로 판정: `raw_top < MIN_IMPACT` → below-threshold, `coverage < MIN_COVERAGE and matched < MIN_MATCHED` → hint. 문턱값은 실제 wiki 질문으로 다시 잡아야 한다(이 벤치는 전부 정답이 있는 질문이라 무주입 문턱을 재지 못했다). `use_qmd` 분기와 `qmd_slugs` 는 제거하거나 기본 False(사용자 결정: qmd 제외). |

### 4.3 투영·렌더

| 현재 함수 | 변경 |
|---|---|
| `project_hit(hit, tokens, idf, max_blocks, max_block_chars)` | `project_graph(db, hits, *, cut=0.5, block_chars=320) -> list[Group]` 로 대체(`bench/context/arms.py::V2GraphArm.lines` 가 원형). `Group` = P 줄 자료(slug/type/updated/sources/head) + B 항목(주소 `slug#tail`, 상태 `cur\|conflict\|superseded`, `clip(redact(text))`) + E 항목(related/supersedes 간선, `edge` 표에서 `src_block IN (...)` 한 번). `cut`: 1위 점수 대비 비율 미만 page 는 버린다(환경변수 `LLMWIKI_CONTEXT_CUT`, 기본 0.5). 낡은 page(head≠self) 는 B 없이 P 한 줄 `sup→head`. `unresolved_conflicts` 건수는 block 상태 `conflict` 로 대체한다. 폴백 경로는 `Doc` 에서 같은 `Group` 을 만든다(`project_graph_from_doc`). |
| `render(result, pages, ...)` | `render_graph(groups, *, max_bytes, max_tokens, preamble)` 로 대체(`V2GraphArm.run` 이 원형). **block 단위** 채움: page 마다 P 줄 + 들어가는 B/E 줄만, B 가 하나도 못 들어가는 page 의 P 는 버린다(낡은 page 의 P 는 정보라 남긴다). 머리말은 `GRAPH_HEAD`(410 바이트) 로 — 지금 머리말 586 바이트 중 저장소 경로·build 명령은 매 프롬프트에 필요 없다. 바이트·토큰 상한 계약과 "잘라낸 마크다운을 내보내지 않는다" 규칙은 그대로. |
| `render_page(page)` | 삭제(B 줄로 흡수). |
| `render_hint(result, pages, ...)` | `render_addr(groups, ...)` 로 교체: `V2AddressArm` 형식(`- slug (type updated) 제목: 요약 → slug#tail, …`, 낡은 page 는 `- slug sup→head`). 본문 없음. |
| `render_always` / `always_slugs` / `always_budget` / `render_always_only` | 유지. `find_doc` 대신 §4.4 의 `find_page` 로 page 를 찾고 block 본문은 `blk` 표에서 읽는다. |
| `build_context(root, query, **options)` | 흐름 유지: preamble → `retrieve` → (hint 면 `render_addr`, 아니면 `project_graph`+`render_graph`) → `render_always_only` 폴백. 옵션에 `cut` 추가. 반환 `pages` 는 `groups` 로 바뀌므로 `run_hook` 의 `stats["pages"]` 도 group 의 page_id 로. |
| `redact` / `est_tokens` / `clip` | 변경 없음. `redact` 는 B 줄 본문과 P 줄 sources 에 그대로. |

### 4.4 조회(get) 와 MCP

| 현재 함수 | 변경 |
|---|---|
| `find_doc(root, selector)` | `find_page(db, selector) -> (page_id, file) \| None` 신설: `page` 표에서 slug/page_id/title 로 한 번 조회(정본 스캔 없음). `block:` / `slug#tail` 셀렉터는 `blk` 표로 page 를 찾는다. 정본 block object 가 필요한 `get_page` 는 `file` 열(= map.json 의 `source`) 의 **정본 파일 하나만** 읽어 지금의 `Doc` 을 만든다. 색인이 없으면 기존 `find_doc`. |
| `selector_block` / `resolve_blocks` / `block_view` / `page_meta` / `page_outline` / `get_page` | 변경 없음(입력이 정본 page dict 인 것은 같다). `slug#tail` 이 이미 통하므로 v2-graph 의 B 주소를 그대로 넣으면 된다. |
| `mcp_call` `llmwiki_search` | `retrieve(min_score=0, min_coverage=0)` 대신 `search_index` 결과를 돌려주고 행에 `head`·`status`·`block_ids` 를 더한다. |
| `mcp_call` `llmwiki_context` | `build_context` 그대로(형식만 바뀜). |
| `should_skip` | `"<llmwiki-context>" in text` 를 `"<llmwiki-context" in text` 로(`v=2` 속성). |
| `run_hook` | stats 에 `index_revision`, `fallback`, `cut`, P/B/E 수를 더한다. |

### 4.5 신선도

`open_index` 가 revision 불일치를 곧바로 스캔 폴백으로 처리하면, dev 서버 밖에서 정본을 고친 직후에는 훅이 매번 1초 스캔으로 떨어진다. 그래서 두 단계: (1) `meta.revision == revision.json` 이면 색인을 그대로 쓴다. (2) 다르면 검색 순위는 낡은 색인으로 내되, 상위 10 page 에 대해서만 색인의 `sha256` 과 `map.json` 을 대조하고 어긋난 page 는 `file` 열의 정본 파일에서 다시 읽어 본문·head 를 채운다(stats 에 `stale_pages:n`). `map.json` 도 파생물이라 같이 낡을 수 있으니 최종 진실은 정본 파일 자체의 sha 로 본다 — 어긋난 page 가 상위에 있을 때만 파일 몇 개를 읽으므로 비용은 무시할 만하다. 새로 추가된 page 는 build 전까지 검색되지 않는다는 점은 남는다(지금도 `index/` 는 그렇다).

### 4.6 예상 효과 (이 벤치 수치)

- 지연: 정본 스캔 p50 965 ms → 색인 조회+투영+렌더 p50 1.8 ms. 색인 크기 9.3 MB(검색) + 12.1 MB(투영, 본문 포함), 합치면 본문 중복이 없어 약 15 MB.
- 페이로드: §1 의 production → v2-graph[cut=0.5]: gold_block 0.358 → 0.920, stale_leak 0.900 → 0.000, 바이트/정답 8522 → 1977, 프롬프트당 1017 → 606 토큰.
