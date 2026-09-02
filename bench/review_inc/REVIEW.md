# 증분 색인 보고서(bench/INCREMENTAL_REPORT.md) 적대적 검증

대상: codex 가 쓴 `bench/INCREMENTAL_REPORT.md` 와 그 근거 `bench/incremental/*.py`, `bench/results_inc/*.json`. 같은 동결 코퍼스(`bench/frozen/corpus` 10,000 page, 500문항, seed 1234) 에서 내가 다시 잰 값과 대조했다. 표준 라이브러리만, qmd·네트워크 없음. 정본·scripts·bench 원본은 읽기만 했고 새 파일은 `bench/review_inc/`, `bench/results_review_inc/`, `bench/index_review_inc/` 에만 있다.

머신: Apple M4, 16 GiB, Python 3.13.7, SQLite 3.50.4. 보고서와 같은 머신이므로 절대값이 직접 비교된다.

## 0. 판정표

| 유지 (재현됨) | 수정 필요 (숫자는 맞으나 해석·누락) | 기각 (틀림) |
|---|---|---|
| K1 증분 == 전체 재빌드: codex 16 조합 + 내 23 조합 = **39 조합 × 500문항 모두 page·score·block 일치**, segmented 전체뿐 아니라 **structural2 전체 재빌드와도** 일치 (§1) | M1 동일성 검사의 **검정력**: 16 조합 중 body 1/10/100·supersedes 1/10 다섯 조합은 top-10 page 순서가 바뀐 문항이 **0** — "무엇을 바꿔도 같은" 상태를 비교했다. 보고서에 이 사실이 없다 (§1.2) | R1 "`history.at` top-10 변화 **0 / 500**" — 원시 `signal_history_at.json` 은 **497 / 500** 이다. 표에 잘못 옮겼다 (§4.1) |
| K2 base 에서 segmented == structural2 500/500 | M2 배속 표의 분모: "전체" = segmented 전체(2.7 s). 제품 기준 structural2(1.24 s) 로 보면 body 10 은 64× 가 아니라 **31×**. 그리고 제품 `build()` 는 **5.13 s** (project 1.93 s + 파생물·shard 쓰기) 라 색인 증분만으로 build 가 45 ms 가 되지 않는다 (§2.6) | R2 "recall@5 +0.0000 → 신호 미채택" 의 근거: weight 0.02 는 top-10 인접 간격의 74% 를 못 넘는 크기이고, fold 가 temporal 을 이미 처리한다. w=0.2 에서 MRR +0.019 / recall@10 −0.034 로 **양방향 효과가 있다**. 효과 0 은 실험 설계 결과다. 게다가 날짜는 생성기 인공물(비체인 9,500 page 가 ordinal 로 2026-mm-dd) 이라 이 코퍼스로는 **판정 불가** (§4.2) |
| K3 증분 시간: body 10 **40.4 ms**(보고 45.1), 1 page 29 ms, 1,000 page 293~812 ms — 5회 중앙값으로 재현, 보고서보다 0~16% 빠름 (§2.1) | M3 **load 비용 누락**: `SegmentedRanker.load` 가 프로세스마다 **157 ms**(structural2 0.1 ms). 40k 간선을 load 때 재료화하기 때문. hook 은 프롬프트마다 새 프로세스라 실효 첫 조회는 167 ms vs 2 ms (§2.2) | R3 "낡음 판정 3,419× 빠르다": `revision.json` 비교는 색인↔map 정합만 확인하고 **wiki 편집은 못 잡는다**. 편집을 잡는 건 mtime scan(57 ms) 이나 전체 sha(631 ms) 다. 다른 것끼리의 비교 (§2.5) |
| K4 structural2 전체 build 1,243 ms(보고 1,339), segmented 2,699 ms(보고 2,832), 색인 9.35 / 76.7 MB, 조회 p50 1.69 / 6.44 ms — 재현 | M4 "segment 가 쌓인다 / 10% 넘으면 background merge" 서사: 실제 구조는 **in-place 행 교체**라 세그먼트가 쌓이지 않는다. 100 라운드 × 10 page 뒤 조회 p50 6.30 ms(불변), 파일 +0.6%, freelist 0; add/delete churn 100 라운드도 freelist 0 (§2.3). compaction 문턱은 근거가 없고, 진짜 문제는 크기(8.2×)·load(157 ms)·조회(3.8×) 다 | R4 "SQLite 바이트가 모든 조합에서 다르다" 로 결정성 논의를 끝낸 것: **`VACUUM INTO`(183 ms) 를 거치면 100 라운드 증분본과 cold build 의 바이트가 같다** (§2.4). parity 하네스를 깨지 않고 채택할 길이 있다 |
| K5 md 사본 6.93 MB(4.57 + manifest 2.37), export 2.04 s, `search.json` text 4.05 MB = 66.2% — 재현 (§3) | M5 증분 시간에 넣은 map 쓰기는 프로토타입 page-only map(1.87 MB, 4.8 ms). 제품 map(blocks 포함 8.17 MB) 은 쓰기 25 ms + 파싱 18 ms 라 증분 경로에 **+40 ms** 가 더 붙는다 (§2.1) | R5 "실제 wiki 는 6 page 중 source 1 page(16.67%) 만 snapshot" — **6 page 전부** `source_snapshot` 을 가진다(파일 바이트의 10.4%). 추정이라고 표기한 점은 맞다 (§3) |
| K6 `history.at` 을 기본 rank 신호로 넣지 않는 결론 | M6 md 비율 24.9% 는 snapshot 이 **하나도 없는** frozen 코퍼스(block 평균 72자) 기준. 실제 6 page 에서는 md 가 파일의 12.8% (§3) | R6 프로토타입 조회가 "posting 상한" 을 가진다는 암묵 가정: SQL 에 `MAX_POST` 상한이 없어 질문당 posting 행 **p50 4,498 / p95 7,137** 을 JOIN 으로 읽는다(structural2 는 토큰당 400). 규모에 선형 (§2.2) |
| K7 JSON 신호 표에 적힌 사용/미사용 판정 자체는 코드와 맞다 | M7 신호 표 누락: `type`, `created/updated`, `summary`, block `fingerprint`, `source_snapshot`, `aliases`, link kind **`source`**(schema enum 에 있고 structural2 는 wiki 0.15 로 취급), `data.items/rows`, `history.action/actor`, link `label/anchor` (§4.3) | |
| K8 base + delta + 압축 방향 자체 | M8 **동시성 누락**: journal_mode=DELETE 라 갱신 중 hook 조회가 최대 **880 ms** 대기, timeout 0 이면 `database is locked`, 그 상태에서 writer 가 **31 s** 굶는다. WAL 이 명세에 없다 (§2.7) | |
| | M9 설계: stale 을 오류로 끝내는 fail-closed, `changed_paths` 를 믿는 hash, `project_view` 개명, background merge(두 번째 writer) — CLAUDE.md·E 와 충돌 (§5) | |

합계: 유지 8, 수정 필요 9, 기각 6.

## 1. 동일성 주장 재현

### 1.1 방법

`bench/review_inc/identity.py`. codex 의 `build_case` 를 import 해 16 조합을 그대로 만들고, 같은 overlay 위에 내 mutation 9 종을 더했다. 각 조합마다 (a) `segment_base` 복사본에 `incremental_update`, (b) segmented 전체 재빌드, (c) **structural2 전체 재빌드** 를 만들어 500문항 top-10 의 `(page_id, score, block_ids)` 를 셋 다 비교했다. 추가로 base 결과와 비교해 **top-10 page 순서가 바뀐 문항 수**(검정력) 를 적었다.

추가 mutation:

- `chain_extend`: temporal gold(체인 head) 를 대체하는 새 page 추가 → head 가 바뀌어야 한다.
- `chain_head_delete` / `chain_middle_delete`: 체인 head / 중간 page 삭제 → 삭제된 page 를 가리키는 supersedes 간선 처리.
- `df_shift`: page 첫 block 에 질문 200개 본문을 통째로 삽입 → 질문 토큰 df·평균 길이가 크게 이동.
- `slug_rename`: in-degree 최상위 page 의 slug·제목 개명 → 들어오던 간선이 dangling.
- `block_remove`: 첫 block 제거 + 순서 반전 → block seq 어긋남.
- `move_source`: 내용 동일·파일 경로만 이동 → sha 같고 source 다름.
- `empty_blocks`: 모든 block 본문 비움 → block 0개 page.
- `title_collide`: 사전순 앞 page 의 제목을 relation gold 의 slug 로 → lookup 우선순위 충돌.

### 1.2 결과 (39 조합)

열: 요청 page 수, 실제 변경 수, 증분 ms(내 값 / 보고서), segmented 전체 ms, structural2 전체 ms, 증분≠seg전체, 증분≠structural2, **base 대비 top-10 순서가 바뀐 문항 수**, 유형별.

| 시나리오 | 요청 | 변경 | 증분 ms | 보고서 | seg 전체 | s2 전체 | ≠seg | ≠s2 | 순서 변화 | 유형 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| body | 1 | 1 | 33.2 | 30.4 | 3059 | 1362 | 0 | 0 | 0 | — |
| body | 10 | 10 | 39.2 | 45.1 | 2921 | 1280 | 0 | 0 | 0 | — |
| body | 100 | 100 | 113.4 | 119.0 | 2820 | 1216 | 0 | 0 | 0 | — |
| body | 1000 | 1000 | 807.7 | 815.5 | 3758 | 1858 | 0 | 0 | 1 | relation 1 |
| add | 1 | 1 | 30.0 | 29.4 | 3770 | 1330 | 0 | 0 | 1 | relation 1 |
| add | 10 | 10 | 38.1 | 38.5 | 3036 | 1264 | 0 | 0 | 2 | relation 2 |
| add | 100 | 100 | 97.3 | 71.0 | 3217 | 1380 | 0 | 0 | 20 | relation 16, temporal 4 |
| add | 1000 | 1000 | 317.1 | 306.8 | 3305 | 1427 | 0 | 0 | 178 | exact 9, relation 100, temporal 66, paraphrase 3 |
| delete | 1 | 1 | 31.9 | 30.6 | 3073 | 1397 | 0 | 0 | 2 | relation 2 |
| delete | 10 | 10 | 41.9 | 37.6 | 3169 | 1391 | 0 | 0 | 6 | relation 5, temporal 1 |
| delete | 100 | 100 | 88.6 | 88.9 | 2990 | 1387 | 0 | 0 | 101 | relation 91, temporal 9, crosslingual 1 |
| delete | 1000 | 1000 | 543.5 | 549.4 | 2565 | 1220 | 0 | 0 | 314 | exact 88, relation 100, temporal 20, crosslingual 7, paraphrase 99 |
| supersedes | 1 | 1 | 31.2 | 31.2 | 2979 | 1445 | 0 | 0 | 0 | — |
| supersedes | 10 | 10 | 47.6 | 41.9 | 3109 | 1412 | 0 | 0 | 0 | — |
| supersedes | 100 | 100 | 117.9 | 130.1 | 2938 | 1348 | 0 | 0 | 101 | relation 1, crosslingual 100 |
| supersedes | 1000 | 1000 | 828.2 | 910.4 | 2979 | 1283 | 0 | 0 | 106 | relation 6, crosslingual 100 |
| chain_extend | 1 | 1 | 31.7 | — | 2994 | 1313 | 0 | 0 | 1 | temporal 1 |
| chain_extend | 10 | 10 | 38.2 | — | 3057 | 1378 | 0 | 0 | 100 | temporal 100 |
| chain_extend | 100 | 100 | 55.4 | — | 3012 | 1295 | 0 | 0 | 103 | relation 3, temporal 100 |
| chain_head_delete | 1 | 1 | 33.5 | — | 2986 | 1305 | 0 | 0 | 1 | temporal 1 |
| chain_head_delete | 10 | 10 | 39.6 | — | 2858 | 1378 | 0 | 0 | 100 | temporal 100 |
| chain_head_delete | 100 | 100 | 104.1 | — | 2931 | 1402 | 0 | 0 | 105 | relation 4, temporal 100, crosslingual 1 |
| chain_middle_delete | 1 | 1 | 35.3 | — | 2888 | 1412 | 0 | 0 | 100 | temporal 100 |
| chain_middle_delete | 10 | 10 | 38.7 | — | 2926 | 1391 | 0 | 0 | 100 | temporal 100 |
| chain_middle_delete | 100 | 100 | 102.3 | — | 2914 | 1401 | 0 | 0 | 104 | exact 1, relation 2, temporal 100, crosslingual 1 |
| df_shift | 1 | 1 | 37.1 | — | 2990 | 1331 | 0 | 0 | 100 | relation 100 |
| df_shift | 10 | 10 | 63.7 | — | 3074 | 1341 | 0 | 0 | 203 | exact 2, relation 100, temporal 1, crosslingual 1, paraphrase 99 |
| df_shift | 100 | 100 | 334.7 | — | 3044 | 1423 | 0 | 0 | 326 | exact 74, relation 100, temporal 5, crosslingual 48, paraphrase 99 |
| slug_rename | 1 | 1 | 32.0 | — | 2960 | 1395 | 0 | 0 | 0 | — |
| slug_rename | 10 | 10 | 52.5 | — | 3027 | 1335 | 0 | 0 | 0 | — |
| slug_rename | 100 | 100 | 187.7 | — | 3032 | 1317 | 0 | 0 | 0 | — |
| block_remove | 10 | 10 | 40.7 | — | 3006 | 1311 | 0 | 0 | 0 | — |
| block_remove | 100 | 100 | 100.7 | — | 3100 | 1314 | 0 | 0 | 1 | temporal 1 |
| move_source | 10 | 10 | 43.6 | — | 3083 | 1326 | 0 | 0 | 0 | — |
| move_source | 100 | 100 | 138.5 | — | 3002 | 1312 | 0 | 0 | 0 | — |
| empty_blocks | 10 | 10 | 45.0 | — | 3055 | 1282 | 0 | 0 | 1 | relation 1 |
| empty_blocks | 100 | 100 | 99.6 | — | 2984 | 1346 | 0 | 0 | 12 | relation 11, temporal 1 |
| title_collide | 10 | 10 | 42.8 | — | 3010 | 1350 | 0 | 0 | 5 | relation 5 |
| title_collide | 100 | 100 | 112.9 | — | 2987 | 1294 | 0 | 0 | 99 | relation 99 |

읽는 법.

- **39 조합 모두 mismatch 0**, structural2 와도 0. supersedes 로 head 가 바뀌는 경우(`chain_extend` 100개가 temporal 100문항의 1위를 새 page 로 바꿈), head 삭제로 체인이 짧아지는 경우, 중간 삭제로 체인이 끊기는 경우, df 가 크게 움직여 326문항의 순위가 바뀌는 경우까지 증분과 전체가 같았다. 이유는 코드에 있다: `termstat` 을 삭제 page 의 term 별 block 수만큼 정확히 빼고, 평균 길이를 `block` 표에서 다시 세며, graph·hub 감쇠·head 는 load 때 전부 다시 만든다. 즉 프로토타입은 "증분 색인" 이 아니라 **posting 만 증분이고 나머지는 매 load 마다 전체 재계산** 이다. 동일성이 나오는 대신 §2.2 의 load 비용을 낸다.
- **검정력.** codex 의 body 1/10/100, supersedes 1/10 은 순서 변화 0 이다. body 는 첫 block 끝에 `증분본문수정-0000` 을 붙일 뿐이라 질문 토큰과 겹치지 않고, 점수는 avg_len 이동으로 소수점 6자리에서만 흔들린다(그래서 signature 는 448~482문항이 바뀌지만 순위는 그대로). supersedes 1/10 은 무작위 page 쌍이라 어떤 질문의 top-10 에도 없다. 보고서의 "16개 조합 모두 일치" 는 참이지만 그중 5개는 아무것도 검사하지 않은 것과 같다. 순위를 실제로 흔든 것은 내 chain·df 시나리오와 codex 의 add/delete 100·1000 이다.
- `slug_rename`·`move_source` 는 순서 변화가 0 인데, 전자는 허브 page(가중 0.15/(1+ln in-degree)) 가 원래 top-10 에 없고 후자는 내용이 같기 때문이다. 버그가 아니라 검정력 0 이다.

## 2. 비용 주장 재현

### 2.1 5회 중앙값 (`bench/review_inc/cost.py`, `bench/results_review_inc/cost.json`)

| 항목 | 내 값 | 보고서 | 비고 |
|---|---:|---:|---|
| structural2 전체 build | **1,242.9 ms** | 1,338.9 | 9,347,072 B |
| segmented 전체 build | **2,699.4 ms** | 2,832.0 | 76,703,467 B (sqlite 74.83 MB + map 1.87 MB) |
| segmented load (프로세스당) | **156.7 ms** | 없음 | structural2 0.103 ms |
| load + 첫 조회 | **167.0 ms** | 없음 | structural2 2.13 ms |
| 조회 p50 / p95 (warm, 1,000회) | 6.44 / 10.97 ms | 6.6 / 11.3 | structural2 1.69 / 2.31 |
| 질문당 읽는 posting 행 | p50 4,498 / p95 7,137 | 없음 | structural2 는 토큰당 400 상한 |
| map.json 쓰기 | page-only 4.8 ms / 제품형 25.0 ms | 증분에 page-only 포함 | 제품형 8,174,103 B, 파싱 18.2 ms |

증분 (5회 중앙값 / 보고서 / 5회 원값):

| 종류 | page | 중앙값 ms | 보고서 | 원값 |
|---|---:|---:|---:|---|
| body | 1 | 28.9 | 30.4 | 29, 31, 30, 28, 29 |
| body | 10 | 40.4 | 45.1 | 41, 40, 38, 39, 41 |
| body | 100 | 115.9 | 119.0 | 116, 116, 116, 116, 114 |
| body | 1000 | 811.7 | 815.5 | 808, 803, 812, 818, 812 |
| add | 1 | 28.7 | 29.4 | 29, 29, 29, 28, 29 |
| add | 10 | 38.5 | 38.5 | 38, 42, 39, 37, 37 |
| add | 100 | 66.3 | 71.0 | 100, 66, 68, 64, 63 |
| add | 1000 | 293.1 | 306.8 | 300, 292, 291, 293, 297 |
| delete | 1 | 28.9 | 30.6 | 29, 29, 29, 29, 28 |
| delete | 10 | 36.1 | 37.6 | 37, 36, 36, 36, 36 |
| delete | 100 | 82.8 | 88.9 | 84, 83, 84, 81, 82 |
| delete | 1000 | 529.5 | 549.4 | 532, 529, 554, 523, 529 |
| supersedes | 1 | 29.0 | 31.2 | 29, 29, 29, 30, 28 |
| supersedes | 10 | 37.2 | 41.9 | 39, 38, 37, 37, 37 |
| supersedes | 100 | 103.2 | 130.1 | 105, 103, 103, 103, 103 |
| supersedes | 1000 | 766.8 | 910.4 | 772, 767, 764, 763, 774 |

증분 절대값은 재현된다(보고서보다 0~16% 빠름; 보고서는 1회 측정). 다만 이 시간은 page-only map 을 쓰는 프로토타입 기준이고, 제품 map(blocks 26,930 항목 포함) 을 쓰면 읽기 18 + 쓰기 25 ms 가 더 붙어 body 10 은 약 **80 ms** 가 된다.

### 2.2 load 비용과 조회 비용 — 보고서가 빠뜨린 두 항목

`SegmentedRanker._load_structure` 가 `page` 10,000행과 `edge` 40,000행을 읽어 lookup·anchor·head·adj 를 매번 만든다. 157 ms 다. structural2 는 이것을 build 때 굽고 load 는 0.1 ms 다. hook(`llmwiki_context.py`) 은 프롬프트마다 새 프로세스이므로 이 비용은 매 질문에 붙는다. 보고서의 "p50 6.6 ms" 는 warm 프로세스 안의 조회만 잰 것이다.

조회 자체도 상한이 없다. `_lex` 의 SQL 이 `WHERE p.term=?` 로 term 의 모든 posting 을 JOIN 해 Python 에서 정렬한 뒤 400개를 자른다. 질문당 p50 4,498행, 최대 7,137행이다. 10만 page 면 10배다. structural2 는 impact 정렬 BLOB 의 앞 400개만 읽는다. 조회 시간 3.8× 차이의 원인이 이것이다.

### 2.3 증분 누적 곡선 — compact/merge 없이 100 라운드

10 page 본문 수정을 라운드마다 다른 page 에 100 라운드(총 1,000 page, 전체의 10%) 연속 적용했다.

| 라운드 | 갱신 ms | sqlite bytes | freelist | 조회 p50 / p95 ms |
|---:|---:|---:|---:|---:|
| 0 | — | 74,833,920 | 0 | 6.39 / 10.93 |
| 1 | 41.5 | 74,846,208 | 0 | 6.34 / 11.31 |
| 10 | 44.2 | 74,883,072 | 0 | 6.36 / 10.92 |
| 25 | 37.6 | 74,944,512 | 0 | 6.32 / 10.86 |
| 50 | 39.0 | 75,075,584 | 0 | 6.32 / 10.86 |
| 100 | 42.7 | 75,284,480 | 0 | 6.30 / 11.05 |

100 라운드 갱신 중앙값 40.7 ms, 최대 49.9 ms. 끝난 색인은 전체 재빌드(75,374,592 B) 와 500문항 일치, structural2 와도 일치. add 10 → 다음 라운드에 delete 10 을 100 라운드 돌린 churn 도 갱신 중앙값 42.9 ms, 최대 71.8 ms, 마지막 freelist 0, 조회 p50 6.53 ms 다.

결론: 이 구조에는 **누적 열화가 없다.** `post` 가 `(term, block_key)` PK 의 단일 표라 갱신은 행 교체이고 SQLite 가 free page 를 같은 파일 안에서 재사용한다. 보고서의 "1,000 page 삭제 뒤 76.5 MB vs 재빌드 69.5 MB" 는 삭제 직후의 free page 일 뿐 다음 삽입이 채운다. 따라서 "delta 비율이 10% 를 넘을 때 병합" 같은 문턱은 이 프로토타입에서 나온 수치가 아니다. 병합이 필요한 건 §5.3 처럼 base 를 압축 BLOB 으로 바꿨을 때이고, 그때 문턱은 다시 재야 한다.

### 2.4 결정성 — `VACUUM INTO` 로 정규화된다

100 라운드 증분본과 같은 정본의 cold build 는 raw 바이트가 다르다(보고서 대로). 그러나 둘 다 `VACUUM INTO` 로 복사하면 **바이트가 같다**(67,428,352 B, 183 ms). `tools/parity/parity.py build` 가 `index/` 전 파일의 sha 를 두 번 대조하므로, publish 단계에서 `VACUUM INTO tmp && os.replace` 를 하면 증분 경로도 parity 를 지킨다. 보고서는 "다르다" 에서 멈췄다.

### 2.5 낡음 판정 — 무엇을 재는지

| 방식 | ms | 무엇을 잡나 |
|---|---:|---|
| `revision.json` read+compare | 0.016 | 색인의 `meta.map_root` 가 `index/map.json` 과 맞는가. **wiki 편집은 못 잡는다** |
| `wiki/**/*.json` mtime scan | 56.9 | map 이후 바뀐 파일이 있는가 |
| 전체 sha(`make_map`: 읽기+파싱+sha) | 631.2 | 무엇이 바뀌었는가 (delta 산출) |
| 변경 10 파일만 sha | 0.57 | 변경 목록을 알 때의 delta 산출 |
| 제품 `stale_index(ws)` (전량 재투영) | 2,035.9 | 지금 CLI 가 하는 것 |

보고서의 3,419× 는 첫 줄과 둘째 줄의 비교인데 둘은 다른 질문에 답한다. dev 서버 감시자가 켜져 있으면 편집 감지는 감시자가 맡고 hook 은 첫 줄만 봐도 되지만, CLI 만 쓰는 경로(사용자가 JSON 을 손으로 고침) 에서는 둘째 줄 이상이 필요하다. §5.3 에서 둘을 나눠 적었다.

### 2.6 "전체 build" 의 진짜 크기

제품 `scripts/llmwiki.py build` 를 같은 임시 workspace 에서 3회 돌리면 **5,132 ms** (project 1,930 + index/*.json 쓰기 + viewer/public/data 복제 + 10,000 shard 쓰기) 다. 보고서의 1,339 ms 는 bench 랭커의 build 이지 제품 build 가 아니다. 검색 색인을 45 ms 로 만들어도 `project()` 가 그대로면 build 는 2 s 아래로 못 내려간다. 사용자가 원하는 "속도" 가 hook 응답이면 §2.2 의 load 가, `build` 시간이면 `project()` 가 병목이다. 색인 증분은 둘 중 어느 쪽도 직접 풀지 않는다.

### 2.7 동시성 (`bench/review_inc/concurrency.py`)

프로토타입은 `journal_mode=DELETE` 다. 1,000 page 갱신(1.15 s) 중 다른 connection 으로 조회를 돌리면:

| reader timeout | 조회 수 | 오류 | 조회 최대 지연 | writer 시간 |
|---|---:|---:|---:|---:|
| Python 기본 5 s | 5 | 0 | **880 ms** (갱신이 끝날 때까지 대기) | 1,149 ms |
| 0 | 4,567,239 | 4,567,144 `database is locked` | 28 ms | **31,216 ms** (reader 가 SHARED lock 을 붙잡아 writer 가 굶는다) |

dev 서버 감시자 build 와 hook 이 겹치는 상황이 바로 이것이다. WAL + busy_timeout 이 명세에 있어야 한다.

## 3. Markdown 비용 주장 (`bench/review_inc/mdcost.py`)

| 항목 | 내 값 (5회 중앙값) | 보고서 |
|---|---:|---:|
| `project()` | 1,930 ms | 1,964 |
| `export_markdown()` | 2,045 ms | 1,866 |
| md 본문 / manifest | 4,565,658 / 2,366,363 B | 같음 |
| `search.json` / 그중 text | 6,110,043 / 4,046,768 B (66.2%) | 같음 |
| map.json (symlink 절대경로) | 8,654,103 B | 같음 |

숫자는 전부 재현된다. 가정 검증:

- **source_snapshot.** 보고서는 "실제 wiki 6 page 중 source 1 page 만 snapshot, 460 B" 로 외삽했다. 실제로는 **6 page 전부** snapshot 을 가진다(207~460 B, 파일 바이트의 10.4%). 세 번째 시나리오("모든 type 이 snapshot") 가 실제에 맞는 것이고 첫 번째("source 만") 는 틀린 전제다. 추정이라고 표기한 점, 표본이 작다고 적은 점은 맞다.
- **md 비율 24.9%.** frozen 코퍼스에는 `source_snapshot` 이 0 page 이고 block 이 평균 72자(최대 94자) 다. md 는 정본에서 본문만 뽑으므로 정본이 짧을수록 비율이 커 보인다. 실제 6 page 에서 `render_markdown` 결과는 파일의 **12.8%** 다. "정본의 1/4" 는 합성 코퍼스 값이다.
- **manifest 2.37 MB** 가 md 비용의 34% 인데 이것은 pretty JSON 이라서다. md 사본을 없애면 같이 없어지므로 결론에는 영향 없다.
- md·qmd 를 없앤다는 사용자 결정이 이미 있으므로 이 절의 실용적 의미는 "삭제해도 잃는 것이 없다" 는 확인이다. 그건 맞다.

## 4. JSON 신호 표와 `history.at`

### 4.1 보고서 표의 오기

"top-10 변화 0 / 500" 은 원시 `bench/results_inc/signal_history_at.json` 의 `changed_top10_rankings: 497` 과 모순된다. 내 재실행(`bench/review_inc/signal.py`) 도 w=0.02 에서 **497** 이다. recall/MRR 이 그대로인 이유는 순서가 바뀐 곳이 정답 아래쪽(5~10위) 뿐이기 때문이다.

### 4.2 "효과 0" 은 신호가 아니라 실험의 성질

| 사실 | 값 |
|---|---:|
| top-10 인접 점수 간격 중 0.02 미만 | 73.6% |
| 1위-2위 간격 중 0.02 미만 | 14.6% |
| 첫 정답과 바로 아래 page 의 간격 중 0.02 미만 | 0.2% |

weight 0.02 × 분위수(≤1) 는 최대 0.02 다. 정답 주변 간격의 99.8% 보다 작으니 정답 위치를 못 건드리고, 아래쪽 동률 근처만 뒤집는다. 그래서 497 은 바뀌고 지표는 안 바뀐다.

weight sweep (fold on / off, `bench/results_review_inc/signal.json`):

| arm | R@1 | R@5 | R@10 | MRR | temporal R@5 | stale_above | cross MRR | relation MRR |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| fold on, w=0 | 0.640 | 0.840 | 0.920 | 0.7381 | 1.00 | 0.00 | 0.433 | 1.000 |
| fold on, w=0.02 | 0.640 | 0.840 | 0.920 | 0.7381 | 1.00 | 0.00 | 0.433 | 1.000 |
| fold on, w=0.1 | 0.638 | 0.840 | 0.910 | 0.7364 | 1.00 | 0.00 | 0.439 | 0.995 |
| fold on, w=0.2 | **0.686** | 0.840 | 0.886 | **0.7573** | 1.00 | 0.00 | **0.631** | 0.925 |
| fold on, w=0.5 | 0.598 | 0.820 | 0.854 | 0.7007 | 1.00 | 0.00 | 0.616 | 0.795 |
| fold on, w=1.0 | 0.380 | 0.584 | 0.610 | 0.4717 | 0.33 | 0.00 | — | — |
| fold off, w=0 | 0.530 | 0.730 | 0.810 | 0.6281 | 0.45 | 0.55 | 0.433 | 1.000 |
| fold off, w=0.2 | 0.576 | 0.730 | 0.776 | 0.6473 | **0.45** | **0.55** | 0.631 | 0.925 |
| fold off, w=1.0 | 0.344 | 0.548 | 0.634 | 0.4442 | 0.15 | 0.55 | — | — |

읽는 법.

- w=0.2 에서 MRR 이 +0.019 오르고 recall@10 이 −0.034 내린다. crosslingual MRR 이 0.433→0.631 로 뛰고 relation MRR 이 1.0→0.925 로 떨어진다. 즉 신호는 **효과가 있다**, 다만 방향이 유형마다 다르다. 보고서의 "0" 은 w=0.02 라는 선택의 결과다.
- fold 를 끄면 temporal 은 어떤 w 에서도 0.45 를 못 넘고 stale_above 0.55 그대로다. `history.at` 은 supersedes fold 를 **대체하지 못한다**. 이 부분의 보고서 결론(supersedes 가 처리한다) 은 맞다.
- 왜 못 넘나: 생성기 `page_dates()` 는 체인 page 에만 위치별 날짜(2023-02-01 / 2024-04-15 / 2025-06-20 / 2026-08-30) 를 주고, 나머지 9,500 page 에는 `2026-{ordinal%12+1}-{ordinal%28+1}` 을 찍는다. temporal gold 의 날짜 분위수 평균은 0.235, stale 은 0.008, **비체인 page 는 0.493** 이고 비체인 page 의 95.4% 가 gold 중앙값보다 새롭다. 최신성을 세게 밀면 gold 가 아니라 무관한 2026 page 가 올라온다(w=1.0 에서 temporal 0.15). crosslingual 이 오르는 것도 ko/en 쌍의 ordinal 차이일 뿐이다.
- 따라서 판정: **"이 합성 코퍼스에 시간 정보가 무의미" 쪽이다.** `history.at` 이 실제 wiki 에서 가치가 있는지는 "최근 변경" 의도의 질문 세트가 있어야 알 수 있고, 지금 코퍼스로는 채택도 기각도 못 한다. 기본 rank 신호로 넣지 않는다는 결론은 유지하되 근거를 바꿔 적어야 한다.

### 4.3 신호 표에서 틀리거나 빠진 것

적힌 11 행의 "현재 색인·순위 사용" 판정은 코드와 맞다. 빠진 것:

| 신호 | 어디에 있나 | 지금 | 판정 |
|---|---|---|---|
| link kind **`source`** | schema enum `["wiki","source","supersedes","related"]`, `implied_links` 가 `sources` 필드를 이 kind 로 만든다 | structural2 는 `EDGE_W` 에 없어 wiki 0.15 취급. 큐레이션된 인용 간선인데 자동 `[[링크]]` 와 같은 무게 | 표에 없음. 가중치 결정 필요(related 1.0 과 wiki 0.15 사이) |
| `type` | page | rank 미사용, E 의 P 줄·viewer 색상 | 표에 없음 |
| `created` / `updated` | page | rank 미사용, E 의 P 줄 | 표에 없음. `history.at` 과 중복 정보 |
| `summary` | page | `search.json` text·catalog 에만 | 표에 없음. 이 벤치에선 전 page 상투문 |
| block `fingerprint` | block | 미사용 | 표에 없음. **block 단위 증분·dup 판정의 자연스러운 키**인데 프로토타입은 page sha 만 쓴다 |
| `source_snapshot` | page | 미색인(옳다) | 표에 없음. "색인하지 않는다" 를 명시해야 실제 wiki 색인 크기 추정이 된다 |
| `aliases` | frozen 제안 필드 | 미사용 (V2: alias 층 효과 0) | 표에 없음 |
| `data.items` / `data.rows` / `data.level` | block | `block_text` 가 items/rows 를 평탄화해 색인 | 표에 없음 |
| `history[].action/actor/note` | page | 미사용 | 표에 없음 |
| link `label` / `anchor` | link | 미사용 | 표에 없음 |
| block `refs` 가 없는 `[[링크]]` | block 본문 | `implied_links` 는 본문 정규식으로 보충, structural2 는 `links[]` 만 읽음 | 표의 "일부 사용" 은 맞지만 **손으로 쓴 JSON 에서 `links[]` 가 비어 있으면 ranker 그래프층이 0 간선**이 된다는 점이 빠짐 |

## 5. 설계 제안 비판 — F(INCREMENTAL_REPORT) 명세 vs CLAUDE.md vs E(CONTEXT_REPORT §4)

### 5.1 CLAUDE.md 규칙과의 충돌

| 규칙 | F 명세 | 판정 |
|---|---|---|
| 파생물은 `build` 로만 갱신 | `build(ws, changed_paths=None)`, `ingest()` 가 `build(changed_paths=[dest])` 호출 | 맞다. 단 `page_hash_map(changed_paths)` 는 호출자가 준 목록을 **믿는다**. 손으로 고친 JSON 이 목록에 없으면 map·색인이 조용히 낡는다. `changed_paths` 는 힌트일 뿐이고, 진실은 항상 전체 sha(2 s) 아니면 mtime scan(58 ms) 으로 확인해야 한다. |
| dev 서버가 `wiki/**/*.json` 변경을 감시해 자동 재생성 | 언급 없음 | 감시자(`viewer/scripts/wiki-data.ts`) 는 `scripts/llmwiki.py build` 를 **인자 없이** spawn 한다. F 대로면 감시자는 매번 전체 hash 경로를 탄다. 감시자가 chokidar 의 변경 경로를 `--changed` 로 넘겨야 증분이 실제로 쓰인다. 또 감시자 build 와 CLI build 가 겹치면 sqlite writer 둘 → `BEGIN IMMEDIATE` 는 있지만 busy timeout·WAL 이 명세에 없다(§2.7 의 실측: DELETE 저널에서 갱신 중 hook 조회는 최대 880 ms 대기, timeout 0 이면 `database is locked`). |
| build 결정성 (`tools/parity/parity.py build` 가 `index/` 전 파일 sha 를 두 번 대조) | "SQLite 바이트는 모든 조합에서 다르다… map root 는 같다" | **충돌.** parity 는 `index/` 아래 모든 파일을 지문 낸다. 증분 갱신된 `search.sqlite` 는 cold build 와 바이트가 다르므로 "정본 같으면 산출물 같다" 가 깨진다. 해결은 둘 중 하나: (a) publish 때 `VACUUM INTO` 로 정규화(§2.4 실측: 증분본과 cold build 바이트 동일), (b) parity 가 sqlite 는 바이트 대신 논리 덤프(PK 순 행) 의 sha 를 대조하고 그 값을 `revision.json.search_root` 로 낸다. |
| `index/*.json` 은 파생물, viewer 는 `viewer/public/data` 를 읽음 | `search.json` 제거, `project()`→`project_view()` | viewer 는 `graph.json`·`stats.json`·`revision.json`·`pages/*` 만 fetch 하므로 `search.json` 제거는 viewer 에 무해. 단 `scripts/llmwiki.py query` 가 `project(ws)["search.json"]` 을 쓰므로 F 7번(query→sqlite) 과 **동시에** 바꿔야 한다. 이름 변경(`project_view`) 은 테스트 13개 파일·parity 오라클이 `project`/`build` 를 부르므로 이득 없이 diff 만 키운다 — 이름은 두고 `search.json` 만 빼라. |
| 정본은 JSON, md 는 파생물 | `Workspace.markdown`/`export_markdown`/CLI `export-markdown` 제거 | 사용자 결정(md·qmd 없음) 과 맞다. 같이 바꿔야 할 것: CLAUDE.md Query 3 "qmd collection `llmwiki_json`" 문구, `tests/test_markdown.py` 의 export 테스트, `llmwiki_context.py` 의 `qmd_slugs`/`DEFAULT_QMD_COLLECTION`. `render_markdown(exact=True)`(snapshot 재현) 은 남긴다. |
| 보안정보 미저장 | 언급 없음 | 색인에 block 본문을 넣는다면(E 안) `redact()` 를 색인 시점이 아니라 **출력 시점**에 걸어야 한다(정본에 이미 치환돼 있어야 하므로 색인은 정본 그대로). 명세에 한 줄 필요. |

### 5.2 E 와 F 의 충돌 지점

| 항목 | E (CONTEXT_REPORT §4) | F (INCREMENTAL_REPORT) | 판정 |
|---|---|---|---|
| 색인 파일 | `index/search.sqlite` | `index/search.sqlite` (프로토타입 파일명은 `segments.sqlite`) | 이름은 같다. 하나로. |
| 표 구조 | structural2 impact BLOB(`page/blk/post/adj/meta`) + 투영 표(`page` 확장열, `blk.text`, `edge`) | 정규화 `post(term, block_key, page_id, tf)` + `termstat` + `edge`; graph/head 는 load 때 재료화 | E 는 증분 불가(impact 에 DF·평균길이가 구워짐), F 는 8.2 배 크고 조회 3.8 배 느리며 load 157 ms(§2.1~2.2). 둘 다 그대로는 안 된다 → §5.3 base+delta. |
| 신선도 키 | `meta.revision == revision.json`; 다르면 낡은 색인으로 순위 내고 상위 10 page 만 sha 대조·정본 재독(fail-open) | `meta.map_root == revision.map_root`; 다르면 stale 오류, 우회 없음 | E 의 두 단계가 맞다. F 의 root 비교는 "색인이 map 과 맞나" 만 보고 "map 이 wiki 와 맞나" 는 못 본다(§2.5). 오류로 끝내면 dev 서버 debounce 250 ms + build 시간 동안 hook 이 죽는다. |
| 색인 없을 때 | `load_corpus` 스캔으로 fail-open | 오류 | E. 6 page 실제 wiki 에서는 스캔이 1 ms 다. |
| 근거 본문 | `blk.text` 를 색인에 저장 | map 의 source/pointer 로 정본 shard 를 읽어 hydrate | E. 실제 wiki page 는 `source_snapshot` 을 품어 파일 하나가 block 하나보다 훨씬 크다. 대신 E 의 4.5 대로 hit page 의 sha 를 map 과 대조한다. |
| `project()` | 그대로(search.json 포함) | search.json 제거·개명 | search.json 은 뺀다(사용처가 `query()` 뿐). 개명은 안 한다. |
| 검색 코드 위치 | 조회부 140줄을 `llmwiki_context.py` 로 복사, `bench/` import 금지 | `SearchIndex` 클래스(위치 미정) | 하나의 모듈 `scripts/llmwiki_index.py` 에 build/update/search 를 두고 `llmwiki.py`(build) 와 `llmwiki_context.py`(hook) 가 둘 다 import. bench ranker 와 코드가 갈라지지 않게 bench 쪽 `structural2.py` 가 이 모듈을 import 하도록 뒤집는다. |
| build 입력 | 전체 page | `changed_paths` delta | 둘 다: `changed` 힌트가 있으면 그 파일만 hash, 없으면 전체 hash. 색인 갱신은 항상 map delta. |
| 무주입/힌트 문턱 | raw impact + coverage | 없음 | E. |
| 결정성 | "build 는 결정적이어야 한다(structural2 sha 동일 확인)" | 바이트 상이 인정 | §5.1 세 번째 줄. |
| 삭제·compaction | 없음 | tombstone/free page, 10% 넘으면 병합 | F. 단 "background merge" 는 두 번째 writer 이므로 두지 않는다. 병합은 `build` 안에서 문턱을 넘었을 때 foreground 로 한다(2.8 s, 드물다). |
| `history.at` | 안 씀 | 실험 후 미채택 | 둘 다 안 쓴다(§4). |

### 5.3 합친 단일 설계 초안 (함수 단위, 구현 없음)

원칙: (1) `index/search.sqlite` 는 `build` 만 쓴다. (2) hook 은 색인이 없으면 스캔으로, 낡았으면 hit 단위 검증으로 fail-open 한다. (3) 색인은 base(압축) + delta(행) 두 층이고, 조회는 항상 live DF·평균 길이로 impact 를 계산하므로 두 층의 점수 의미가 같다. (4) 결정성은 publish 시 정규화로 지킨다. (5) writer 는 한 번에 하나, reader 는 WAL 로 막히지 않는다.

**`scripts/llmwiki_index.py` (신규, 표준 라이브러리만)**

| 함수 | 역할 |
|---|---|
| `SCHEMA` | `meta(k,v)`; `page(rid PK, page_id, slug, title, type, updated, source, sha256, projects, tags, sources, summary, history_at, head_rid, sup_block, alive)`; `blk(rid PK, prid, block_id, seq, length, kind, unresolved, mult, text)`; `post_base(term PK, df, rids BLOB varint, tfs BLOB varint)`; `post_delta(term, brid, tf, PRIMARY KEY(term, brid))`; `tomb(brid PK)`; `edge(src_rid, ord, target TEXT, kind, block_id, PRIMARY KEY(src_rid, ord))`; `adj(src_rid, dst_rid, w, own_block)`. `PRAGMA journal_mode=WAL`. |
| `open_ro(path) -> Connection` | `mode=ro`, `busy_timeout=2000`. hook 전용. |
| `map_delta(old_pages, new_pages) -> Delta(added, modified, deleted)` | 순수 함수. sha 또는 source 가 다르면 modified. F 3번 그대로. |
| `apply_delta(db, docs_by_id, delta) -> Touched` | 한 `BEGIN IMMEDIATE`. deleted/modified page 의 blk rid 를 `tomb` 에 넣고 `post_delta` 의 해당 행을 지운다(base BLOB 은 건드리지 않는다). added/modified page 를 새 rid 로 `page/blk` 에 넣고 posting 은 `post_delta` 에만 쓴다. `post_base.df` 는 건드리지 않고 live df 는 `df_base − tomb∩base + delta` 로 조회 때 센다(§5.4). `edge` 는 src 행 교체. 돌려주는 `Touched` = 바뀐 page rid + 그 page 가 가리키거나 가리켜지던 rid. |
| `refresh_graph(db, touched)` | `adj` 를 touched 의 src/dst 행만 다시 계산한다. hub 감쇠는 dst 의 wiki in-degree 만 필요하므로 touched dst 에 들어오는 행만 다시 쓴다. supersedes head 는 touched 가 속한 체인만 다시 걷는다(guard 32). 40k 간선 전체 재료화(F 프로토타입, load 마다 §2.2 의 157 ms) 를 없앤다. |
| `compact(db)` | `tomb` 과 `post_delta` 를 `post_base` 로 접는다: term 마다 base BLOB 을 풀어 tomb 제거·delta 추가·rid 정렬·varint 재직렬화. `tomb` 비우고 `VACUUM`. 문턱: `len(tomb)+len(post_delta rows) > 0.10 × n_blocks` 또는 `freelist_count/page_count > 0.10`. `build` 안에서 foreground 로만 돈다. |
| `publish(db, path)` | commit 뒤 `VACUUM INTO tmp` → `os.replace(tmp, path)`. §2.4 에서 증분본과 cold build 의 VACUUM INTO 바이트가 같았으므로(183 ms) parity 가 그대로 성립한다. 표 구조를 바꾼 뒤에도 같은지 다시 확인하고, 다르면 `logical_digest(db)` (표를 PK 순으로 canonical JSON 직렬화한 sha) 를 `revision.json.search_root` 에 적고 parity 는 sqlite 파일 대신 그 값을 대조한다. |
| `search(db, query, k, *, fold=True) -> list[Hit(page_id, score, block_ids, head, raw_top, coverage)]` | structural2 의 `_lex/_ppr/fold` 그대로. 어휘층은 term 마다 base BLOB(앞 `MAX_POST` 만 읽는 건 impact 정렬이 없으니 불가 → 전량 읽고 tomb 제외) + delta 행을 합쳐 live idf/평균길이로 impact 를 계산한다. 비용 상한은 §2.1 의 segmented 와 같은 급(p50 6~7 ms) 이 아니라 BLOB 한 행 읽기이므로 그 사이다 — 구현 뒤 재야 한다. `raw_top`/`coverage` 는 E 4.2 의 무주입 판정용. |
| `hydrate(db, hits, *, cut, block_chars) -> list[Group]` | E 4.3 의 `project_graph`. `blk.text` 에서 읽고 `redact()` 는 여기서 건다. |
| `verify_hits(db, ws_map, hits) -> stale_pages` | E 4.5: hit page 의 `page.sha256` 과 `index/map.json` 의 sha 를 대조, 다르면 그 page 의 정본 파일을 읽어 본문·head 를 채운다. |

**`scripts/llmwiki.py`**

| 함수 | 변경 |
|---|---|
| `page_hash_map(ws, changed=None)` | `changed` 가 None 이면 전량(지금의 `load_documents`+validate), 있으면 그 파일만 다시 읽고 나머지는 `index/map.json` 의 sha 를 재사용. **단 이때도 `mtime scan`(58 ms) 으로 `changed` 밖의 변경을 잡아 있으면 전량으로 떨어진다.** |
| `project(ws)` | `search.json` 항목만 뺀다. 이름·반환 형식 유지. viewer 파생물(catalog/map/graph/routes/stats) 은 지금처럼 전량 생성 — 10k 에서 약 2 s 이며 이것이 `build` 의 하한이다(§2.6). 증분이 줄이는 것은 검색 색인 부분뿐이다. |
| `build(ws, changed=None, *, search_only=False)` | `page_hash_map → map_delta(index/map.json, new) → apply_delta → refresh_graph → (문턱) compact → publish → project(생략 가능: search_only) → revision.json{revision, map_root, search_root}`. `meta.map_root` 는 publish 성공 뒤에만 바뀐다. `ingest()` 는 `build(changed=[dest, moved_from])` 를 부른다. |
| `stale_index(ws)` | 지금의 전량 재투영(§2.5 실측) 대신 `mtime scan` 으로 `index/map.json` 보다 새 파일이 있는지 본 뒤, 있을 때만 그 파일들의 sha 를 대조한다. |
| `query(ws, text, limit)` | `llmwiki_index.search` + `hydrate`. |
| `export_markdown`, `Workspace.markdown`, CLI `export-markdown` | 제거. CLAUDE.md 의 qmd 문구도 제거. |
| `tools/parity/parity.py` | sqlite 는 §publish 의 결과에 따라 바이트 또는 `search_root` 대조. |

**`scripts/llmwiki_context.py`**

| 함수 | 변경 |
|---|---|
| `open_index(root)` | `index/search.sqlite` 있으면 `open_ro`; `meta.map_root != revision.json.map_root` 면 `stale=True` 로 열고 stats 에 남긴다. 없으면 None → 기존 `load_corpus`+`rank`. |
| `retrieve()` | 색인 있으면 `search` → `verify_hits`(stale 이거나 hit 의 파일 mtime 이 map 보다 새면) → `hydrate`. 문턱은 E 4.2. `use_qmd`/`qmd_slugs` 삭제. |
| `render`/`render_hint`/`find_doc`/`get_page`/`mcp_call` | E 4.3~4.4 그대로. |
| 감시자 `viewer/scripts/wiki-data.ts` | `build` 에 변경 경로를 `--changed` 로 넘긴다. debounce 250 ms 유지. |

### 5.4 이 설계가 남기는 불확실성 (구현 전 재야 할 것)

1. base BLOB 전량 읽기 + tomb 필터의 조회 지연: structural2 의 1.7 ms 와 segmented 의 6.6 ms 사이 어디인지.
2. `VACUUM INTO` 정규화가 새 표 구조(base BLOB + delta) 에서도 parity 를 지키는지(§2.4 는 프로토타입 구조에서 확인한 값).
3. compaction 문턱: §2.3 에서 프로토타입은 1,000 page(10%) 수정 뒤에도 열화가 없었다. base BLOB + delta 구조에서는 delta 행이 조회 비용에 직접 더해지므로 문턱을 새로 재야 한다.
4. 실제 wiki 의 block 길이(§3) 에서 `blk.text` 저장 비용.

## 6. 재현

```bash
python3 bench/review_inc/identity.py      # 39 조합 동일성 + 검정력 (~13 분)
python3 bench/review_inc/cost.py          # 5회 중앙값, 누적 100 라운드, churn, VACUUM INTO (~2 분)
python3 bench/review_inc/mdcost.py        # project/export/build/stale_index, 실제 wiki snapshot (~1 분)
python3 bench/review_inc/signal.py        # history.at sweep, 간격 분포, 날짜 구조 (~1 분)
python3 bench/review_inc/concurrency.py   # 갱신 중 조회 (~40 초)
```

결과: `bench/results_review_inc/{identity,cost,mdcost,signal,concurrency}.json`, 로그 `*.log`. 색인·임시 root 는 `bench/index_review_inc/` 에 남아 있고 지워도 된다.
