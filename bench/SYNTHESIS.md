# 종합 보고 — md 없이 JSON 정본만으로 만 건 이상에서 검색·컨텍스트를 극한까지

날짜 2026-09-02. 조사만 했고 아무것도 고치지 않았다. `wiki/ raw/ index/ scripts/ viewer/` 는 그대로이며 모든 산출물은 `bench/` 아래에 있다.

근거 문서 넷:

| 문서 | 작성 | 내용 |
|---|---|---|
| `bench/CONTEXT_REPORT.md` | claude fable 5.1 (과제 E) | 훅이 LLM 에 넣는 페이로드의 토큰 효율·낡은 본문 누출 측정, JSON 부분 그래프 형식, 설계 제안 §4 |
| `bench/review_ctx/REVIEW.md` | codex (과제 G) | E 의 적대적 검증: 지표 정직성, 긴 block, 검색×형식 2×2 분해, cut sweep, head 의존, 훅 계약 충돌 |
| `bench/INCREMENTAL_REPORT.md` | codex (과제 F) | md 사본 비용, map.json sha 델타 증분 색인 프로토타입, 낡음 판정, JSON 신호 표, 설계 제안 |
| `bench/review_inc/REVIEW.md` | claude fable 5.1 (과제 H) | F 의 적대적 검증: 39조합 동일성, 5회 중앙값, 누적·동시성·결정성, history.at 재실험, E·F 를 합친 단일 설계 §5.3 |

앞선 라운드(`bench/audit/REPORT.md`, `bench/V2_REPORT.md`)의 결론은 그대로 전제한다: 합성 코퍼스라 정확도의 절대값은 일반화할 수 없고, 구조 랭커 v2(`structural2`)는 v1 과 같은 recall@5 0.840 을 절반 지연·표준 라이브러리로 낸다.

## 1. 사용자 질문에 대한 답

**"왜 qmd 는 md 를 쓰나"** — qmd 가 md 도구라서 `index/markdown` 사본을 만들어 먹였을 뿐이다. 이번 조사로 그 경로는 세 가지 이유로 폐기해도 잃는 것이 없다: qmd BM25 는 한국어 문장에서 조사 하나로 빈 결과가 나고(고쳐도 이 벤치에서 0.030), 벡터는 상주시켜도 1.3초에 낡은 판을 위로 올리며, md 사본은 만 건에서 약 12 MiB 의 순수 중복이고 build 마다 2초를 더 쓴다.

**"만 건에서 속도·토큰·정확도·낡은 문서"** — 네 축 모두 JSON 구조만으로 되고, 무엇이 되고 무엇이 아직 안 되는지가 아래 표다.

| 축 | 현재 프로덕션 | JSON 네이티브 (측정값) | 확실한 것 | 아직 모르는 것 |
|---|---|---|---|---|
| 속도 (훅 1회) | 965 ms (정본 만 파일 스캔) | **1.8 ms** (build 때 구운 색인 + 투영) | 색인이 코퍼스 크기에서 풀린다. 300→10,000 page 에서 p50 0.9→1.7 ms | 프로토타입 증분 색인 구조는 프로세스당 load 157 ms 라 그대로는 안 된다 (§3) |
| 토큰 (프롬프트당) | 1017 토큰, 정답 하나당 8,522 B | **606 토큰, 1,977 B** (cut=0.5) | 머리말·상투 메타 제거와 block 단위 채움이 바이트를 줄인다. cut=0.5 는 절벽이 아니다 (0.30~0.55 평탄) | 320자 clip 뒤 본문은 양쪽 다 0. 긴 block 에서 절감이 41%→14% 로 준다. 무주입 문턱은 재지 못했다 |
| 정확도 (정답 block 이 페이로드에) | 0.358 | **0.920** | 6000 B 에서 차이는 검색이 만든다 (형식만 바꾸면 0.358 그대로) | 절대값은 합성 코퍼스 인공물에 의존. 자연 문서 세트 전까지 배율을 주장하지 않는다 |
| 낡은 문서 | 100문항 중 90건이 표시 없이 섞임 | **0건** | 세 겹이 필요하다: 검색 fold + 색인의 head 주석 + 본문 생략 형식. head 주석이 없으면 형식만으로는 1.00 샌다 | supersedes 의 fork·cycle 은 conflict 로 남겨야 한다 (지금 원형은 임의 head 로 접는다) |
| 색인 유지 | build 5.1 s 전량 | page 10개 변경 시 **40~80 ms** 증분 | 39가지 변경에서 증분 = 전체 재빌드 = structural2 (500문항 page·점수·block 일치). VACUUM INTO 로 바이트까지 같아져 parity 유지 | `project()` 가 2초라 build 전체는 2초 아래로 못 간다. WAL 없이는 갱신 중 훅이 880 ms 기다리거나 writer 가 굶는다 |

## 2. 페이로드 (E + G)

### 2.1 재현된 숫자 (500문항, 6000 B)

| arm | 정답 block 전달 | 낡은 본문 누출 | 평균 B | 토큰 | B/정답 | p50 |
|---|---:|---:|---:|---:|---:|---:|
| production (지금 훅) | 0.358 | 0.900 | 3,051 | 1,017 | 8,522 | 965 ms |
| v2-text (검색만 교체) | 0.840 | 0.000 | 3,205 | 1,069 | 3,815 | 1.8 ms |
| v2-graph (JSON 부분 그래프) | 0.920 | 0.000 | 3,318 | 1,106 | 3,606 | 1.9 ms |
| **v2-graph cut=0.5** | **0.920** | **0.000** | **1,818** | **606** | **1,977** | 1.8 ms |
| v2-address (주소만, 2단계용) | 0.000 (주소 0.920) | 0.000 | 1,701 | 568 | — | 1.8 ms |

G 가 소수점까지 재현했다. 2000 B 의 production 낡은 누출은 하드링크 경로 길이 때문에 0.890 으로 나왔고 실제는 0.900 이다.

### 2.2 검색 × 형식 분해 (G)

| 검색 | 형식 | 정답 전달 | 평균 B |
|---|---|---:|---:|
| production | production | 0.358 | 3,051 |
| production | v2-graph | 0.358 | 1,525 |
| structural2 | production | 0.920 | 3,245 |
| structural2 | v2-graph | 0.920 | 1,818 |

6000 B 에서 정답 전달은 검색이 정하고, 바이트는 형식이 정한다. 2000 B 에서는 형식도 전달에 관여한다 (page 단위 채움 0.090 → block 단위 0.358).

### 2.3 G 가 깬 것

- **지표 이름.** `gold_block` 은 "gold block 의 앞 60자가 페이로드에 있다" 다. 600~1200자로 늘린 100문항에서 앞 60자 전달은 production 0.36 / v2 0.92 로 유지되지만 **전체 본문·끝 60자 전달은 둘 다 0.00** 이다. 320자 clip 때문이다. 제품 지표는 선택(retrieval) · 전달(앞 320자) · 완전성(답 span) 으로 나눠야 하고, 답 span 라벨이 없어 실제 정답률은 판정하지 않았다.
- **절감 배율.** 긴 block 에서 v2 의 바이트 절감은 41%→14%, 정답당 바이트 배율은 4.3배→3.0배로 준다. "4.3배" 는 짧은 block 과 상투 summary 에 기댄 값이다.
- **"형식만으로 낡은 누출 0" 은 기각.** v2-graph 의 `sup→head` 표시는 색인이 build 때 supersedes 를 역방향으로 접어 둔 `head` 열에서 온다. 그 열을 없애면 형식만으로는 temporal 100문항에서 **1.00 샌다** (cut=0.5 는 0.95). 낡은 page 만 검색됐을 때 outgoing 간선은 그 page 에 없으므로 형식은 알 길이 없다. 따라서 "temporal status projection" 을 검색 fold 와 별개의 단계로 명세해야 한다.
- **cut=0.5 는 절벽이 아니다.** 0.30~0.55 에서 정답 전달 0.920 동일, 0.6 에서 0.914, 0.7 에서 0.858 (전부 paraphrase 의 생성기 고정 배치). 다만 같은 500문항에서 고른 값이라 기본값 확정은 자연 문서 세트 뒤로.

### 2.4 E §4 설계 제안에서 고쳐야 할 것 (G §6)

| 계약 | 문제 | 필요한 것 |
|---|---|---|
| fail-open | "스캔 폴백" 과 "예외 시 exit 0" 을 같은 말로 씀. sqlite 오류·lock·schema 불일치 경로 없음 | 두 계약 분리, DB 예외 즉시 스캔 또는 무주입, 최종 exit 0 테스트 |
| 6초 워치독 | 색인 열기·hash 확인·정본 재독·폴백의 시간 예산 없음 | 단계별 deadline, `mode=ro`, busy_timeout, 회귀 테스트 |
| 원자성 | 반쯤 만든 DB 나 revision 전환 중 상태를 훅이 볼 수 있음 | tmp DB 에 build → 검증 → `os.replace` |
| 신선도 | 낡은 색인의 상위 10 만 sha 대조하면 새 page·순위가 바뀐 page 는 영구 누락 | 신선도를 확증 못 하면 스캔 폴백. 낡은 색인 사용은 명시적 degraded 모드 |
| 무주입 문턱 | structural2 점수는 1위=1.0 상대값이라 무관한 질문도 후보가 있으면 항상 주입 | cut(후보 축소)과 무주입(절대 신호)을 분리. 무관·부분 관련 질문 세트로 먼저 보정 |
| MCP get | 색인 miss·새 page·id 불일치 시 정본 폴백 없음 | 색인은 주소 힌트만, 최종 object 는 `root/wiki` 안의 정본 파일 하나에서 |
| 마스킹 | B·sources 만 언급 | 모든 최종 문자열에 중앙 `redact` + escaping |
| 바이트+토큰 상한 | 원형 코드는 바이트만 검사 | 둘 다 검사, 6000 B / 2000 token 회귀 테스트 |
| supersedes fork/cycle | 원형은 임의 head 로 접음 | conflict 로 보존하고 이유를 페이로드·stats 에 |

## 3. 증분 색인과 md 사본 (F + H)

### 3.1 재현된 숫자

| 항목 | F 보고 | H 재측정 (5회 중앙값) |
|---|---:|---:|
| structural2 전체 build | 1,339 ms | 1,243 ms |
| 프로토타입(segmented) 전체 build | 2,832 ms | 2,699 ms |
| 색인 크기 | 9.35 MB vs 76.7 MB | 같음 (8.2배) |
| 조회 p50 | 1.74 vs 6.6 ms | 1.69 vs 6.44 ms |
| **프로세스당 load** | 보고 없음 | **0.1 ms vs 157 ms** |
| 증분: 본문 page 10개 | 45 ms | 40 ms (제품 map 이면 약 80 ms) |
| 증분: page 1 / 100 / 1,000 | 30 / 119 / 816 ms | 29 / 116 / 812 ms |
| md 사본 (본문 + manifest) | 6.93 MB, export 1.9 s | 같음, 2.0 s |
| search.json / 그중 text | 6.11 / 4.05 MB | 같음 |
| 제품 `build` 전체 | 보고 없음 | **5.1 s** (project 1.93 s + 파생물·shard 쓰기) |
| 제품 `stale_index` | 보고 없음 | 2.0 s (전량 재투영) |

### 3.2 유지된 것

- **증분 = 전체 재빌드.** codex 16조합 + claude 23조합(체인 head 교체·삭제, 중간 삭제, df 이동, slug 개명, block 제거, 파일 이동, 빈 block, 제목 충돌) = 39조합 × 500문항에서 page·점수·block 이 전체 재빌드와도, structural2 와도 일치. 다만 codex 조합 중 5개는 순위를 하나도 안 흔든 검정력 0 의 실험이었다.
- **누적 열화 없음.** 10 page 씩 100라운드(전체의 10%) 뒤에도 조회 p50 6.30 ms, 파일 +0.6%, freelist 0. 행 교체 구조라 세그먼트가 쌓이지 않는다. F 의 "10% 넘으면 background merge" 문턱은 이 프로토타입에서 나온 수치가 아니다.
- **결정성은 회복 가능.** 증분본과 cold build 는 raw 바이트가 다르지만 `VACUUM INTO`(183 ms) 를 거치면 바이트까지 같다. parity 하네스를 깨지 않는다.
- **낡음 판정.** revision 루트 비교 0.017 ms 는 "색인이 map 과 맞나" 만 답한다. "wiki 가 편집됐나" 는 mtime 스캔 57 ms 또는 전체 sha 631 ms 가 답한다. 둘을 나눠 써야 한다.
- **md 사본 제거는 무손실.** 만 건에서 `index/markdown` 6.9 MB + `search.json` 6.1 MB 의 직접 중복이 사라지고 build 마다 2초를 아낀다. 실제 6 page 는 전부 `source_snapshot` 을 가져 md 비율은 12.8% (F 의 24.9% 는 snapshot 없는 합성 코퍼스 값). `source_snapshot` 은 검색 사본이 아니라 원문 재현 정보라 md 와 함께 지울 대상이 아니다.

### 3.3 H 가 깬 것

- **load 비용.** 프로토타입은 40k 간선의 그래프·head 를 load 마다 재료화한다. 훅은 프롬프트마다 새 프로세스라 실효 첫 조회가 167 ms 다 (structural2 2 ms). "증분 색인"이라기보다 "posting 만 증분, 나머지는 매번 전체 재계산" 이다. 조회도 posting 상한이 없어 질문당 4,500~7,100행을 읽는다 (규모에 선형).
- **`history.at` 표 오기.** "top-10 변화 0/500" 은 원시 JSON 의 497/500 과 모순. weight 0.02 는 정답 주변 간격의 99.8% 보다 작아 효과가 안 보였을 뿐, w=0.2 에서 MRR +0.019 / recall@10 −0.034 로 양방향 효과가 있다. 그러나 생성기가 날짜를 ordinal 로 찍어 비체인 page 의 95% 가 gold 보다 새롭다. **이 코퍼스로는 판정 불가**이고, supersedes fold 를 대체하지는 못한다 (fold 끄면 어떤 w 에서도 temporal 0.45).
- **동시성.** `journal_mode=DELETE` 라 1,000 page 갱신 중 훅 조회가 880 ms 대기하거나, timeout 0 이면 `database is locked` 에 writer 가 31초 굶는다. WAL + busy_timeout 이 명세에 있어야 한다.
- **JSON 신호 표 누락.** link kind `source`(schema enum 에 있고 큐레이션된 인용인데 structural2 는 wiki 0.15 취급), block `fingerprint`(block 단위 증분·dup 의 자연스러운 키인데 page sha 만 씀), 손으로 쓴 JSON 에서 `links[]` 가 비면 그래프층이 0 간선이 되는 점.
- **build 하한.** 검색 색인을 45 ms 로 만들어도 `project()` 가 2초라 build 는 2초 아래로 못 간다. 사용자가 원하는 "속도" 가 훅 응답이면 load 가, build 시간이면 `project()` 가 병목이다.

## 4. 합친 설계 (H §5.3 + G §6, 구현 안 함)

원칙 다섯: (1) `index/search.sqlite` 는 `build` 만 쓴다 (2) 훅은 색인이 없으면 스캔, 낡았으면 hit 단위 검증으로 fail-open (3) 색인은 base(압축 BLOB) + delta(행) 두 층, 조회는 항상 live df 로 impact 계산 (4) 결정성은 publish 때 `VACUUM INTO` 로 (5) writer 는 하나, reader 는 WAL.

| 파일 | 핵심 |
|---|---|
| `scripts/llmwiki_index.py` (신규) | `SCHEMA`(page/blk/post_base/post_delta/tomb/edge/adj/meta, WAL) · `open_ro` · `map_delta` · `apply_delta`(한 트랜잭션, base 는 안 건드림) · `refresh_graph`(touched 만) · `compact`(build 안에서 foreground) · `publish`(VACUUM INTO → os.replace) · `search`(structural2 조회부 + live df, raw_top/coverage 반환) · `hydrate`(부분 그래프 투영, redact 는 여기서) · `verify_hits`(hit page sha 대조·정본 재독) |
| `scripts/llmwiki.py` | `page_hash_map(changed=None)` — changed 는 힌트, mtime 스캔으로 밖의 변경을 잡으면 전량 · `project()` 에서 `search.json` 만 제거(이름 유지) · `build(changed, search_only)` 파이프라인 · `stale_index` 를 mtime 기반으로 · `query` 를 색인으로 · `export_markdown`·`Workspace.markdown`·CLI `export-markdown` 제거 · CLAUDE.md 의 qmd 문구 제거 |
| `scripts/llmwiki_context.py` | `open_index`(없으면 None → 스캔) · `retrieve` 를 `search → verify_hits → hydrate` 로, 무주입은 절대 신호(raw impact + coverage) · `use_qmd`/`qmd_slugs` 제거 · `render_graph`/`render_addr` (바이트+토큰 둘 다 검사) · MCP get 은 정본 파일 하나에서 · 모든 출력에 `redact` |
| `viewer/scripts/wiki-data.ts` | 감시자가 변경 경로를 `--changed` 로 넘긴다 |
| `tools/parity/parity.py` | sqlite 는 VACUUM INTO 결과 바이트 또는 논리 덤프 sha(`revision.json.search_root`) 로 대조 |
| bench | `bench/rankers/structural2.py` 가 `scripts/llmwiki_index.py` 를 import 하도록 뒤집어 코드가 갈라지지 않게 |

구현 전에 재야 할 것 (H §5.4): base BLOB 전량 읽기 + tomb 필터의 조회 지연(1.7~6.6 ms 사이 어디인가), 새 표 구조에서 VACUUM INTO 결정성, delta 행이 조회 비용에 더해지는 구조의 compaction 문턱, 실제 wiki block 길이에서 `blk.text` 저장 비용.

## 5. 판정 요약

| 항목 | 판정 |
|---|---|
| md·qmd 경로 제거 | **채택.** 잃는 것 없음. 12 MiB·2초/build·모델 2.1 GiB 절감 |
| 훅을 build 때 구운 색인으로 | **채택.** 965 ms → 2 ms. 단 프로토타입 증분 구조(load 157 ms)는 그대로 안 되고 base+delta 로 |
| JSON 부분 그래프 페이로드 | **채택, 지표 이름 수정.** 정답 전달 0.358→0.920, 토큰 40% 절감. "완전성" 은 미측정 |
| supersedes 세 겹 (fold + head 투영 + 본문 생략) | **채택.** 어느 하나만으로는 안 된다 |
| cut=0.5 | **운용점으로 유지, 기본값 확정 보류.** 절벽 아님 |
| 증분 색인 | **방향 채택, 구조 교체.** map sha 델타·VACUUM INTO·WAL 명세 |
| `history.at` rank 신호 | **보류.** 코퍼스로 판정 불가 |
| link kind `source` 가중, block `fingerprint` 키 | **설계에 추가.** 측정은 다음 라운드 |
| 정확도 배율 (2.6배, 4.3배) | **주장하지 않음.** 자연 문서 세트 뒤로 |

## 6. 다음 (사용자 결정 사항)

1. 위 §4 설계로 구현에 들어갈지. 들어간다면 `scripts/` 를 처음 건드리는 것이므로 별도 지시가 필요하다.
2. 자연 문서 검증 세트: 실제 마크다운을 `ingest` 한 벤치용 위키 + 정답 있는 질문 + **무관한 질문**(무주입 문턱용) + 긴 block(완전성용). 정확도 절대값과 cut·무주입 문턱은 이것 없이 확정할 수 없다.
3. 그 전에 지금 바로 안전하게 할 수 있는 것: `index/markdown`·`search.json` 생성과 qmd 폴백 제거 (실측상 무손실).
