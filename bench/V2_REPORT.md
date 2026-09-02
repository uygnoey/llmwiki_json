# 랭커 v2 보고 — 더 단순하고 이식성 높은 구조 검색

대상: `bench/rankers/structural2.py` (새 파일, 466 줄).
기준: `bench/rankers/structural.py` (v1, 742 줄 / 606 줄).
코퍼스·질문: `bench/frozen/` (10000 page, 500 문항, k=10). 색인 `bench/index_v2/`, 결과 `bench/results_v2/`.
표준 라이브러리만 쓴다. FTS5·qmd·외부 패키지 의존이 없다. build 는 결정적이다(두 번 build 한 sha256 동일: `eea8f5fea5e62d6d…`).

## 1. 결론 요약

**대표값은 dup 감쇠를 끈 기본값이다** (§8 교란 검증 뒤 기본값 변경. 동결 코퍼스에서만 나오던 0.920 은 생성기 인공물 이득이었다).

| | structural (v1) | structural2 (v2, 기본) | v2 dup=true (참고) |
|---|---|---|---|
| recall@5 | 0.840 | 0.840 | 0.920 |
| recall@1 | 0.720 | 0.640 | 0.720 |
| MRR@10 | **0.771** | 0.738 | 0.807 |
| nDCG@10 | **0.807** | 0.782 | 0.836 |
| block_recall@5 | 0.840 | 0.840 | 0.920 |
| stale_above | 0.00 | 0.00 | 0.00 |
| p50 / p95 (ms) | 3.43 / 5.39 | **1.69 / 2.22** | 1.71 / 2.07 |
| index_bytes | 12,218,368 | **9,347,072** | 9,347,072 |
| build_ms | 1752 | **1102** | 1104 |
| 코드 줄수 | 742 | **466** | 〃 |
| 의존성 | sqlite FTS5 trigram 빌드, fts5vocab, MARK 표식, 조사 사전 60개 | sqlite 일반 테이블(BLOB) 만 | 〃 |
| 교란 코퍼스 recall@5 / MRR (§8) | 0.840 / 0.771 | 0.840 / 0.738 | 0.840 / 0.738 |

채택 권고: **v2 (structural2, 기본 옵션 = dup 끔)**, 단 근거는 정확도 우위가 아니라 **동등한 recall@5 를 절반 지연·23% 작은 색인·37% 짧은 코드·의존성 제거로 낸다**는 점이다. MRR 은 v1 보다 0.033 낮은데 전부 crosslingual 에서 한국어 짝 page 가 영어 gold 위에 오는 순위 차이(§4.2, §8.4)이고, paraphrase MRR 은 v2 가 높다(0.26 vs 0.12). 특수 규칙(related 1-hop, concept 1-hop, alias 되먹임, gate+forward+head 근거)은 각각 일반 규칙 하나로 대체됐다.

유형별 (recall@5 / MRR@10, 원본 코퍼스):

| type | v1 | v2 기본 | 비고 |
|---|---|---|---|
| exact | 1.00 / 1.00 | 1.00 / 1.00 | 라틴 낱말을 통째 토큰으로 → `v2.3.1` 과 `v2.1.3` 이 trigram 을 공유하지 않는다 |
| relation | 1.00 / 1.00 | 1.00 / 1.00 | anchor block 가중(간선을 든 block)만으로 충분. 별도 relation 규칙 없음 |
| temporal | 1.00 / 1.00 | 1.00 / 1.00 | "head 로 접기" 한 규칙. stale_above 0 |
| crosslingual | 1.00 / 0.73 | 1.00 / **0.43** | 유일한 후퇴. §4.2, §8.4 |
| paraphrase | 0.20 / 0.12 | 0.20 / 0.26 | dup=true 면 0.60 이지만 인공물 이득(§8) |

## 2. 설계 (세 층, 규칙 하나씩)

1. **어휘층 — 순수 Python 역색인.** 토큰 = 한글 run 의 음절 2-gram + 라틴/숫자 run 낱말. 조사 처리를 색인·조회 어디서도 하지 않는다: `정책은` → `정책`,`책은` 이므로 질문의 `정책` 이 그대로 맞고, `cfg.pipeline.000의` 는 문자 종류 경계에서 잘려 `cfg.pipeline.000` 이 된다. BM25 impact(idf 포함, block 구조 계수 곱함)를 build 때 계산해 **impact 내림차순** posting 으로 sqlite 일반 테이블에 BLOB 저장. 조회 = 토큰당 posting 앞 400개만 읽어 합산(비용 = 토큰 수 × 400 으로 유계). 질문 토큰 가중 = idf³ (§4.3). block rid→page rid 는 4바이트/block 배열 하나를 load 때 메모리에 올린다(10000 page 에 148 KB).
2. **그래프층 — 간선 가중 bounded 확산.** build 때 인접 리스트에 kind 가중(related 1.0, wiki 0.15, wiki 류는 허브 감쇠 ÷(1+ln indeg), page 당 상위 64개)을 구워 둔다. 조회는 어휘 상위 30 page 를 seed 로 2 step 확산. non-backtracking(방금 온 간선으로 되돌아가지 않음), node 집계는 **max**(가장 센 경로 하나). 간선을 받은 page 의 근거 block 은 그 page 위의 역방향 간선 block(색인에 저장). 종류별 특수 규칙 없음.
3. **시간축 — supersedes 체인을 head 로 접기.** build 때 chain head 와 head 의 "X 를 대체한다" block 을 page 행에 적는다. 조회 규칙 하나: `score[head] = max(score[head], score[old])`, `score[old] = score[head] × 0.3`. v1 의 gate·forward·head 근거 block 세 장치가 이 한 줄에서 나온다.

## 3. Ablation (10000 page, 500 문항, `bench/results_v2/10000p-*.json`)

기본 arm 은 dup 끔·idf³·확산 2 step·max 집계·fold 켬·tie 없음이다.

| arm | recall@5 | recall@1 | MRR | nDCG | blk@5 | stale | p50 | p95 | bytes | build_ms | exact | relation | temporal | crossl. | paraph. |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| structural (v1) | 0.840 | 0.720 | 0.7714 | 0.8067 | 0.840 | 0.00 | 3.43 | 5.39 | 12,218,368 | 1752 | 1.00/1.00 | 1.00/1.00 | 1.00/1.00 | 1.00/0.73 | 0.20/0.12 |
| **structural2** (기본) | 0.840 | 0.640 | 0.7381 | 0.7824 | 0.840 | 0.00 | 1.69 | 2.22 | 9,347,072 | 1102 | 1.00/1.00 | 1.00/1.00 | 1.00/1.00 | 1.00/0.43 | 0.20/0.26 |
| dup=true | **0.920** | 0.720 | 0.8067 | 0.8357 | 0.920 | 0.00 | 1.71 | 2.07 | 〃 | 〃 | 1.00 | 1.00 | 1.00 | 1.00/0.43 | **0.60**/0.60 |
| graph=none | 0.680 | 0.640 | 0.6762 | 0.7133 | 0.680 | 0.00 | 0.70 | 1.14 | 〃 | 〃 | 1.00 | 1.00 | 1.00 | **0.20**/0.12 | 0.20 |
| fold=false | 0.730 | 0.530 | 0.6281 | 0.6724 | 0.730 | **0.55** | 1.58 | 2.16 | 〃 | 〃 | 1.00 | 1.00 | **0.45** | 1.00 | 0.20 |
| idf_pow=1 (순수 BM25) | 0.720 | 0.400 | 0.5513 | 0.6127 | 0.720 | 0.00 | 1.57 | 2.07 | 〃 | 〃 | 1.00 | 1.00/**0.50** | 1.00 | **0.60**/0.26 | **0.00** |
| idf_pow=2 | 0.760 | 0.674 | 0.7141 | 0.7437 | 0.760 | 0.00 | 1.64 | 2.10 | 〃 | 〃 | 1.00 | 1.00 | 1.00 | **0.60**/0.39 | 0.20/0.18 |
| idf_pow=2.5 | 0.840 | 0.640 | 0.7267 | 0.7557 | 0.840 | 0.00 | 1.63 | 2.15 | 〃 | 〃 | 1.00 | 1.00 | 1.00 | 1.00/0.43 | 0.20/0.20 |
| steps=1 | 0.840 | 0.640 | 0.7381 | 0.7824 | 0.840 | 0.00 | **1.13** | 1.65 | 〃 | 〃 | 1.00 | 1.00 | 1.00 | 1.00 | 0.20 |
| hub=false | 0.840 | 0.640 | 0.7360 | 0.7803 | 0.840 | 0.00 | 2.43 | 3.28 | 〃 | 〃 | 1.00 | 1.00 | 1.00 | 1.00 | 0.20 |
| wiki_w=0 (related 만) | 0.840 | 0.640 | 0.7381 | 0.7824 | 0.840 | 0.00 | 0.86 | 1.33 | 8,806,400 | 1190 | 1.00 | 1.00 | 1.00 | 1.00 | 0.20 |
| agg=sum (PageRank 식) | 0.840 | 0.640 | 0.7381 | 0.7824 | 0.840 | 0.00 | 1.88 | 2.54 | 〃 | 〃 | 1.00 | 1.00 | 1.00 | 1.00 | 0.20 |
| tie=receiver (§8.4) | 0.840 | 0.760 | 0.7981 | 0.8267 | 0.840 | 0.00 | 1.70 | 2.27 | 〃 | 〃 | 1.00 | 1.00 | 1.00 | 1.00/**0.73** | 0.20 |
| min_seed=0.5 (§8.4) | 0.840 | 0.440 | 0.6381 | 0.7086 | 0.840 | 0.00 | 1.55 | 2.25 | 〃 | 〃 | 1.00 | 1.00/**0.50** | 1.00 | 1.00/0.43 | 0.20 |

읽는 법: 그래프층은 crosslingual 80문항을, fold 는 temporal 55문항과 stale_above 를, idf³ 는 crosslingual 40 과 paraphrase·relation 순위를 책임진다. dup 은 원본에서만 paraphrase 40문항을 올리고 교란 코퍼스에서는 0 이다(§8). 나머지 knob(steps, hub, wiki_w, agg) 은 정확도 차이가 없고 지연·안전성만 바꾼다.

규모별 (`bench/corpus300`, `bench/corpus3000` 은 동결본이 아니라 참고치):

| pages | ranker | recall@5 | MRR | p50 / p95 ms | build_ms | bytes | 유형별 r@5 |
|---|---|---|---|---|---|---|---|
| 300 | v1 | 0.572 | 0.523 | 1.60 / 2.55 | 65 | 811,008 | exact 0.11, rel 0.75, temp 1.00, cross 1.00, para 0.00 |
| 300 | v2 | **0.702** | **0.576** | 0.93 / 1.10 | 55 | 671,744 | exact 0.77, rel 0.83, temp **0.77**, cross 1.00, para 0.14 |
| 3000 | v1 | 0.840 | 0.711 | 3.35 / 5.58 | 381 | 4,165,632 | 1.00 / 1.00 / 1.00 / 1.00 / 0.20 |
| 3000 | v2 | 0.840 | **0.738** | 1.79 / 2.44 | 342 | 3,301,376 | 1.00 / 1.00 / 1.00 / 1.00 / 0.20 |
| 10000 | v1 | 0.840 | **0.771** | 3.43 / 5.39 | 1752 | 12,218,368 | 1.00 / 1.00 / 1.00 / 1.00 / 0.20 |
| 10000 | v2 | 0.840 | 0.738 | 1.69 / 2.22 | 1102 | 9,347,072 | 1.00 / 1.00 / 1.00 / 1.00 / 0.20 |

지연은 규모에 거의 무관하다(posting 상한 400 × 토큰 24, 확산 frontier 50 × fanout 64 로 유계). 300 page 의 temporal 후퇴는 §4.5.

## 4. 후보 방향별 판정

### 4.1 어휘층 교체 — 채택
- 순수 Python 역색인이 FTS5 trigram+MARK 보다 **정확도 같거나 높고(exact/relation/temporal 1.00 유지), 빠르고(p50 0.70 ms, 어휘층만), 작고(색인 23% 감소), 빌드 41% 빠르다.**
- 조사 처리: 색인 시점 처리 없이 2-gram 이 흡수하는 쪽이 더 단순하다. v1 의 조사 사전 60개 + 라틴 꼬리 정규식 + `_strip_particle` 이 모두 사라졌고 exact/relation/temporal 이 그대로 1.00 이다. 남는 비용은 `책은` 같은 경계 2-gram 인데 df 가 커서 idf 가 눌러 준다.
- 저장 형식 비교(같은 posting 25,129 term, `bench/index_v2/structural2/structural2.db` 기준):

| 형식 | 바이트 | 적재 | 조회 |
|---|---|---|---|
| sqlite 일반 테이블 BLOB (채택) | 9,347,072 (post+blk+adj+page 전체) | 0 (lazy) | term 당 0.01–0.08 ms seek |
| pickle (posting 만) | 4,109,495 | 4 ms 전체 적재 | dict 조회 |
| JSON(base64) (posting 만) | 5,504,386 | 10 ms 전체 적재 | dict 조회 |

  pickle 이 가장 작고 적재도 4 ms 라 매력적이지만 색인 전체를 메모리에 올려야 하고 page 가 10배 늘면 적재도 10배다. sqlite 는 조회한 term 만 읽어 규모에 무관하며 어느 python3 에나 있다. 정확도는 형식과 무관(같은 posting). sqlite 를 채택.

### 4.2 그래프 확산 일반화 — 채택, 단 두 가지 수정이 필수였다
- 성공: `related` 1-hop, concept 1-hop, alias 되먹임 규칙을 **kind 가중치 표 하나 + 확산 하나**로 대체해도 crosslingual 1.00, relation 1.00, temporal 1.00 이 유지된다. alias 층은 v1 에서도 효과가 없었고 v2 는 아예 없다.
- 실패 1 → 수정: 대칭인 `related` 를 2 step 되돌아가게 두면 seed 가 제 짝을 거쳐 스스로를 부풀려 건너간 gold 가 묻힌다(crosslingual 0.60). **non-backtracking**(방금 온 간선으로 되돌아가지 않음)으로 1.00 회복.
- 실패 2 → 수정: PageRank 식 **합(sum) 집계는 작고 촘촘한 그래프에서 무너진다.** 300 page 에서 seed 30개의 wiki 간선(0.15, 허브 감쇠 후에도)이 몇 page 에 쌓여 직접 맞은 gold(1.0) 위로 올라가 recall@5 0.29(temporal 0.00, exact 0.02). **max 집계(가장 센 경로 하나)**로 바꾸면 0.70. 10000 page 에서는 둘이 같다(표 3). 즉 이 벤치의 "확산"은 사실상 "가장 좋은 curated 간선 하나를 건넌다"이며, 진짜 PageRank 가 필요하다는 근거는 없다.
- paraphrase partial-hard 에는 그래프가 아무 도움이 안 됐다(graph=none 과 동일 0.60). 그 40문항은 §4.4 의 어휘 규칙이 올렸다. 예상대로 zero 40문항은 질문→gold 경로가 없어 불가.
- crosslingual MRR 후퇴(0.73→0.43)는 확산의 대칭성 때문이다: partial 문항에서 한국어 짝(lex 1.0)과 영어 gold(lex 0.73)가 서로에게 related 질량을 주면 짝 1.62 vs gold 1.58 로 짝이 1위다. v1 은 약하게 맞은 page 에서는 건너지 않는 문턱(0.5)이 있어 gold 만 얻었다. 한국어 질문에 한국어 짝이 1위인 것을 오답으로 보기 어려워 문턱을 되살리지 않았다. recall@5 는 1.00.

### 4.3 idf 기울임(idf³) — 채택, 원본·교란 양쪽에서 재확인
2-gram 은 한 낱말을 토큰 여럿으로 쪼개므로 흔한 문구가 과대 대표된다. 질문 토큰 가중을 idf¹→idf³ 로 바꾸면 crosslingual(partial) 과 relation 순위가 산다. 지수 1/2/2.5/3 을 원본과 교란 코퍼스(§8) 양쪽에서 쟀다: 1 은 둘 다 무너지고(교란에서 relation 0.33), 2 는 원본에서 crosslingual 0.60 으로 불안정, 2.5 와 3 은 양쪽 모두 0.840 이며 3 이 MRR 과 300 page 규모에서 조금 낫다. 이 값은 벤치를 보고 골랐지만 두 코퍼스에서 같은 답이 나온다.

### 4.4 복제 block 감쇠(dup) — 기본값에서 끔 (옵션 `dup=true` 로 남김)
같은 본문이 N 개 page 에 그대로 복사돼 있으면 impact ÷ N. 동결 코퍼스에서는 paraphrase distractor 6 page 가 바이트 단위로 동일해서 이것 하나로 0.20→0.60 이 났다. 그러나 §8 의 교란 코퍼스(복제 block 을 page 마다 다르게 쓴 것)에서는 이득이 정확히 0 이다. 즉 +0.08 은 생성기 인공물이었고, 실제 위키에서 같은 문장이 6 page 에 그대로 반복되는 일은 드물다. 기본값을 끄고 대표값을 dup 없는 숫자(0.840 / 0.738)로 바꿨다. "여러 page 에 복사된 단락은 어느 page 의 근거도 아니다"는 원칙 자체는 남겨 두되 켜는 것은 사용자 판단이다.

### 4.5 supersedes head 접기 — 채택
한 규칙으로 temporal 1.00, stale_above 0.00, block_recall 1.00(head 의 근거 block = 색인에 적어 둔 supersedes anchor). fold=false 면 temporal 0.45, stale 0.55 로 무너진다. 낡은 page 는 head × 0.3 으로 head 아래에만 나타난다(결과에서 "대체됨" 표시 역할).
300 page 에서 temporal 0.77 인 이유: 작은 코퍼스에서는 희소 토큰(`aaaa-legacy`, df 1) 의 idf 대비가 약해(ln 1089 ≈ 7 vs 흔한 2-gram ≈ 4.3) 같은 템플릿의 다른 체인 page 가 어휘로 앞선다. 접기 자체는 작동한다(stale_above 0). 규모가 커지면 사라진다(3000 부터 1.00).

### 4.6 정직한 벡터 지연 — 상주 서버로 재도 초 단위, 융합 API 없음
qmd 2.8.3 을 `qmd mcp`(stdio JSON-RPC) 로 **한 번만 띄워 상주**시키고 `bench/index/vector-mode_query` 의 collection `llmwiki_bench` 를 조회만 했다(재색인 없음). 30 문항(유형별 6) 표본, `bench/results_v2/qmd-resident-sample30.json`.

| 모드 (MCP `query` tool) | 첫 호출(모델 적재) | p50 | p95 | recall@5 | stale_above |
|---|---|---|---|---|---|
| vec 만, rerank=false | 2.6 s | **1.32 s** | 1.36 s | 0.27 | 0.67 |
| lex(qmd BM25) 만 | 0.02 s | 5 ms | 7 ms | 0.00 | — |
| lex+vec RRF, rerank=false | 1.2 s | 1.18 s | 1.23 s | 0.20 | 0.67 |
| vec + LLM rerank | 5.3 s | 4.0 s | 5.3 s | 0.17 | 0.67 |
| query (확장+RRF+rerank), 10문항 | 23.6 s | 8.6 s | 25.2 s | 0.50 | — |

- 이전 세션의 "프로세스마다 8–12 s" 는 대부분 확장·rerank LLM 비용이었다. 상주시켜도 **순수 벡터 조회가 1.3 s** 다(질문 임베딩 + 검색; 구조 랭커의 700배). CPU 상주로 재도 ms 급이 되지 않는다.
- qmd 의 lex(BM25) 모드는 한국어 질문에 결과가 없다(`No results found`; 구조화 결과 빈 배열). 한국어 토크나이저 부재.
- 벡터 모드는 stale_above 0.67 — 시간축 없이 낡은 판을 위에 올린다. 구조 랭커의 핵심 차별점이 그대로 확인된다.
- **"구조 상위 50 후보만 벡터로 재정렬" 융합은 qmd API 로 불가능하다.** MCP tool 은 `query`(전체 collection 검색), `get`, `multi_get`, `status` 뿐이고 후보 목록을 받아 점수만 매기는 입력이 없다. 질문 임베딩만 돌려주는 API 도 없어 `index.sqlite` 의 vectors_vec 를 직접 읽어 내적하는 길도 막힌다(질문 벡터를 만들 수단이 표준 라이브러리에 없다). 가능한 유일한 융합은 두 결과 목록의 사후 RRF 인데, 1.3 s 를 매 질문에 얹는 값어치가 없다(정확도 0.27).

## 5. 실패한 시도와 이유
1. **wsum 정규화(진짜 PPR)** — related 질량이 wiki 간선 수만큼 나뉘어(1/1.45) 건너간 gold 가 형제 page 아래로. 정규화 제거.
2. **모든 kind 허브 감쇠** — related 대상 page 의 in-degree 까지 감쇠해 crosslingual zero 40문항 전멸(0.60). wiki 류만 감쇠.
3. **visited 집합 non-backtracking** — seed 인 gold(partial) 가 질량을 못 받아 crosslingual 짝이 항상 1위. 간선 단위(직전 송신자만 제외)로 교체.
4. **sum 집계** — 10000 에서는 무해하나 300 page 에서 붕괴(0.29). max 로.
5. **순수 BM25(idf¹)** — crosslingual 짝이 형제 page 와 0.88 로 붙어 확산 0.85 로도 못 넘고, paraphrase 0.18. idf³.
6. **frontier 200 / steps 3** — 정확도 동일, 지연 4.6–7 ms. frontier 50, steps 2 로 1.7 ms.

## 6. 남은 한계
- paraphrase 80문항(zero 40 + partial-hard 40): 정본에 질문 표면형이 없거나 질문을 그대로 담은 distractor 가 gold 를 누른다. 어휘·그래프 어느 방법으로도 불가. 벡터도 0.33(6문항 표본).
- crosslingual MRR 0.43: 한국어 짝 page 가 영어 gold 위에 온다. 고칠 수 있는 두 규칙은 절벽이거나 relation 을 깬다(§8.4).
- 작은 코퍼스(≤300 page)에서 템플릿 반복이 심하면 temporal 0.77. 실 wiki 는 6 page 라 확인 불가.
- idf³ 은 벤치를 보고 정한 값이다(원본·교란 양쪽에서 안정). 실 데이터에서 `IDF_POW` 를 재확인해야 한다.

## 7. 재현
```
python3 bench/harness.py --corpus bench/frozen/corpus --queries bench/frozen/queries.json --k 10 \
  --index-root bench/index_v2 --out bench/results_v2 \
  --rankers "structural,structural2,structural2:dup=true,structural2:graph=none,structural2:fold=false,structural2:idf_pow=1,structural2:idf_pow=2,structural2:idf_pow=2.5,structural2:steps=1,structural2:hub=false,structural2:wiki_w=0,structural2:agg=sum,structural2:tie=receiver,structural2:min_seed=0.5"
```
qmd 상주 측정은 `qmd mcp` 를 `bench/index/vector-mode_query` 에서 띄워 JSON-RPC 로 `tools/call name=query` 를 보냈다(스크립트는 세션 scratch 에만 있음; 결과 JSON 은 `bench/results_v2/qmd-resident-sample30.json`).

## 8. 교란 코퍼스 검증 (과제 D)

### 8.1 왜
§4.3·§4.4 가 인정했듯 idf³ 와 dup 감쇠는 이 벤치를 보고 고른 값이고, dup 은 distractor 문장이 여러 page 에 **바이트 단위로 동일**하다는 생성기 인공물에 기댄다(`bench/audit/REPORT.md` 도 같은 지적). 0.920 이 인공물 이득인지 확인하지 않고는 채택할 수 없다.

### 8.2 교란 코퍼스 (`bench/perturb.py` → `bench/corpus_perturbed/`)
`bench/frozen/corpus` 에서 2개 이상 page 에 바이트 동일하게 나타나는 block 본문 343종(1,837 block: exact/relation/paraphrase/crosslingual distractor 전부, stale page 의 "과거 판본에는…" 167개, conflict 상투문 300개)을 page 마다 다르게 다시 썼다. (slug, 본문) 의 sha256 으로 변형을 고르므로 결정적이다(두 번 만든 파일 전체 sha 동일 `9e60881c…`). 변형은 문장 순서 회전, 문장 **끝** 어미 치환(`이다`→`였다`/`이라고 본다` 등 12종), page 별 부연(`(항목 <slug>)` / `— <slug> 기준` / `[<slug> 기록]`) 삽입, 질문 세트에 나오지 않는 낱말 하나의 동의어 치환이다. 질문·gold 는 `bench/frozen/queries.json` 그대로.

스크립트가 검증해 출력한 값: 남은 "바이트 동일 block 이 2개 이상 page" **0건**, 질문 토큰(v1 어간 prefix·v2 2-gram 둘 다) 과 block 의 겹침이 원본보다 줄어든 경우 **0건**(493,000 쌍 검사). 즉 교란은 어휘 겹침을 유지한 채 복제만 없앴다.

예 (paraphrase distractor, 원본은 6 page 동일):
- entity-001213: `(항목 entity-001213) aati-nimbus 서비스 — 검수 생략 배포 승인 요건은 무엇인가이라는 표현은 폐기된 원고에만 있고 이 page는 정답이 아니었다.`
- entity-001214: `— entity-001214 기준 aati-nimbus 서비스 — … 표현은 철회된 초안에만 있고 이 page는 정답이 아니라고 본다.`

### 8.3 재측정 (`bench/results_v2p/`, 색인 `bench/index_v2p/`)

| arm | 원본 r@5 | 원본 MRR | 교란 r@5 | 교란 MRR | 원본 유형별 r@5/MRR (ex/rel/temp/cross/para) | 교란 유형별 |
|---|---|---|---|---|---|---|
| structural (v1) | 0.840 | 0.7714 | 0.840 | 0.7714 | 1.00/1.00 · 1.00/1.00 · 1.00/1.00 · 1.00/0.73 · 0.20/0.12 | 동일 |
| structural2 기본 (dup 끔, idf³) | 0.840 | 0.7381 | 0.840 | 0.7381 | 1.00/1.00 · 1.00/1.00 · 1.00/1.00 · 1.00/0.43 · 0.20/0.26 | 동일 |
| structural2 dup=true | **0.920** | 0.8067 | 0.840 | 0.7381 | … · 0.60/0.60 | … · 0.20/0.26 |
| structural2 idf_pow=1 | 0.720 | 0.5513 | **0.666** | 0.5092 | rel 1.00/0.50, cross 0.60, para 0.00 | rel **0.33**/0.17, para 0.00 |
| structural2 idf_pow=2 | **0.760** | 0.7141 | 0.840 | 0.7394 | cross **0.60**/0.39 | cross 1.00/0.52 |
| structural2 idf_pow=2.5 | 0.840 | 0.7267 | 0.840 | 0.7267 | cross 1.00/0.43, para 0.20/0.20 | 동일 |
| structural2 tie=receiver | 0.840 | 0.7981 | 0.840 | 0.7981 | cross 1.00/**0.73** | 동일 |
| structural2 min_seed=0.5 | 0.840 | 0.6381 | 0.840 | 0.6381 | rel 1.00/**0.50** | 동일 |

v1 은 교란에 완전히 무감하다(수치 동일). v2 도 dup 을 빼면 무감하다. 교란에서 v2 dup=true 와 dup 끔이 같은 것은 복제 block 이 0 이라 감쇠가 발동하지 않기 때문이다.

### 8.4 판정
1. **dup 이득은 인공물이다.** 교란에서 0.920→0.840, v1 과 동률. 기본값을 끔으로 바꾸고(옵션으로 남김) §1·§3 의 대표값을 0.840 / 0.738 로 갱신했다. 채택 근거는 정확도 우위가 아니라 동등한 recall@5 를 절반 지연·작은 색인·짧은 코드·의존성 제거로 낸다는 점으로 바뀐다. MRR 은 v1 보다 0.033 낮다(전부 crosslingual 순위).
2. **idf 지수는 3 유지.** 1 은 양쪽에서 무너지고, 2 는 원본에서만 crosslingual 0.60(dup 을 끄면 5개 동일 distractor 가 gold 를 누른다) 으로 불안정, 2.5 와 3 은 양쪽 모두 0.840 에 유형별 값도 같다. 3 이 MRR 과 300 page 에서 조금 낫다.
3. **crosslingual MRR 후퇴(0.73→0.43) 는 고치지 않는다.** 두 후보를 원본·교란 양쪽에서 쟀다.
   - `min_seed=0.5` (v1 문턱: 약한 seed 는 건너지 않음): crosslingual 은 그대로 0.43 이고 relation MRR 이 1.00→0.50 으로 깨진다. 이유는 v2 의 relation 1위가 gold 가 `related` 로 가리키는 concept page(어휘 0.48) 가 gold 에게 질량을 되돌려 주는 데 기대고 있었기 때문이다. 문턱을 두면 concept 은 받기만 하고 보내지 못해 concept(0.48+0.85) 이 gold(1.0) 위에 온다. 즉 v2 의 "보낸 쪽 vs 받은 쪽" 순위는 `0.15 × (어휘 차이)` 의 얇은 여유로 결정된다.
   - `tie=receiver` (1.0 간선으로 질량을 받은 page 가 자체 어휘 근거도 있고 보낸 page 와 `tie_eps` 안이면 받은 쪽을 위에): 양쪽 코퍼스에서 crosslingual MRR 0.73, 다른 유형 변화 없음, 전체 MRR 0.798 로 v1 을 넘는다. 그러나 `tie_eps` sweep(원본·교란 동일)이 절벽이다 — 0.02: 0.43, 0.03: 0.53, **0.04~0.055: 0.73**, 0.06 부터 relation 이 뒤집혀 recall@1 이 0.76→0.69→0.63(0.08)→0.56(0.12). crosslingual partial 의 짝-gold 간격이 2.5%, relation 의 gold-concept 간격이 5.9% 라 그 사이에만 서는 값이다. 감사 보고서가 v1 의 `W_RELATED_HOP` 에 지적한 것과 같은 종류의 과적합 절벽이라 기본값으로 켜지 않는다. 두 규칙 중 더 일반적인 것은 tie=receiver(다른 유형을 깨지 않음)이지만, 채택하지 않고 옵션으로만 남긴다.
4. 기본값 변경: `DUP_DEFAULT = False`. `IDF_POW = 3.0`, `TIE = "none"`, `MIN_SEED = 0.0` 은 그대로. 변경 뒤 원본 전 arm·규모별·교란 결과를 다시 돌려 `bench/results_v2/`, `bench/results_v2p/` 를 재생성했고 build 결정성을 다시 확인했다(sha256 `eea8f5fea5e62d6d…`).

### 8.5 재현
```
python3 bench/perturb.py --src bench/frozen/corpus --queries bench/frozen/queries.json --out bench/corpus_perturbed --seed 1234
python3 bench/harness.py --corpus bench/corpus_perturbed --queries bench/frozen/queries.json --k 10 \
  --index-root bench/index_v2p --out bench/results_v2p \
  --rankers "structural,structural2,structural2:dup=true,structural2:idf_pow=1,structural2:idf_pow=2,structural2:idf_pow=2.5,structural2:tie=receiver,structural2:min_seed=0.5"
```
