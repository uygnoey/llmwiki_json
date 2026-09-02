# 검색 벤치마크 타당성 감사

| 항목 | 인공물 | 실제에도 성립 | 판정불가 |
|---|---|---|---|
| exact 1.00 | 짧은 긍정 gold와 질문 전체를 복사한 긴 부정 distractor의 BM25 길이 차이로 1.00이다. 구조 이득이 아니다. | 희소 식별자 검색 자체는 실제 위키에도 유용하다. | 실제 문서에서 같은 식별자를 긍정·부정하는 근접 오답을 가르는 정도는 측정하지 않았다. |
| relation 1.00 | 생성기가 gold block에만 정답 `related` 간선을 달아 완벽한 라벨을 준다. | `links[].block_id`로 관계를 주장한 block을 식별하는 원리는 실제에도 성립한다. | 6-page 실제 wiki에서 정답 질문 세트가 없어 효과 크기는 모른다. |
| temporal 1.00 / stale 0.00 | 55 strong 문항은 `W_SUPERSEDE_FORWARD`가 없으면 정답이 아예 top 10 밖으로 사라지도록 구성됐다. | 명시적 `supersedes`로 최신판을 전진시키고 구판을 강등하는 의미는 타당하다. | 실제 wiki에는 `supersedes`가 0개라 1.00과 stale 0.00의 전이는 검증할 수 없다. |
| crosslingual 1.00 | 100/100 질문을 한국어 짝 page에 글자 그대로 넣고 gold로 전용 `related` 간선을 연결했다. | 사람이 검증한 번역 짝 간선이 실제로 있다면 1-hop은 유효하다. | 실제 wiki에는 alias도 `related`도 없으므로 현재 데이터에서의 crosslingual 품질은 모른다. |
| paraphrase 0.20 | easy 20개는 distractor 2개라 항상 3위, hard 40개는 6개라 항상 7위다. zero 40개는 전부 top 10 밖이다. | 없음. 이번 결과에서 구조/개념 일반화는 관측되지 않았다. | 자연 문서 paraphrase 성능은 이 합성 세트로 판정할 수 없다. |
| qmd vsearch 0.196 | 모든 page의 같은 템플릿, 정답에서 빠진 불투명 4글자 식별자, 질문을 그대로 복사한 부정 distractor가 vector arm을 불리하게 한다. | qmd가 이 특정 코퍼스에서 실패했다는 사실은 실제 측정값이다. | 자연 문서에서 vector가 구조 랭커보다 약하다는 일반 결론은 낼 수 없다. |
| “queries.json으로 튜닝하지 않았다” | `W_RELATED_HOP` 25% 감소만으로 zero crosslingual 40개가 3위에서 9위로 내려가는 절벽은 결과 과적합의 증거다. | 다른 네 핵심 상수는 ±50% 범위에서 recall@5가 안정적이었다. | 작성 의도·튜닝 이력은 코드와 결과만으로 검증할 수 없다. |

## 판정

`structural recall@5 0.840 > baseline 0.464 > qmd vsearch 0.196`은 이 동결 합성 코퍼스의 수치로는 재현되지만, **구조 색인이 실제 위키 검색에서 일반적으로 우월하다는 증거로는 타당하지 않다.** 420개 구조 랭커 hit 중 exact 100개와 paraphrase 20개는 구조가 아니라 어휘/배치 효과이고, crosslingual 80개 개선과 temporal 55개 개선은 생성기가 랭커의 `related`·`supersedes` 경로에 맞춰 만든 문제다. relation의 block-owned edge와 temporal의 최신판 의미는 제품 아이디어로는 타당하지만, 완벽한 점수의 크기는 합성 설계에서만 성립한다.

동결 재현성은 확인했다. 10,000개 파일의 상대 경로+바이트 SHA-256은 `75ae99...66ca`, 질문 SHA-256은 `fa0180...6225`로 `bench/frozen/MANIFEST.json`과 일치한다. 원본 랭커·생성기·동결 파일은 수정하지 않았고, 구조 실험 색인은 `bench/index_audit`, 결과는 `bench/results_audit`만 사용했다.

## 기준 성능과 유형별 원인

| 유형 | recall@5 | MRR@10 | stale_above | 무엇이 맞히게 했나 | 판정 |
|---|---:|---:|---:|---|---|
| 전체 | 0.840 | 0.7714 | 0.000 | 아래 다섯 유형의 합 | 일반화 불가 |
| exact | 1.000 | 1.0000 | — | FTS5 trigram/BM25. 희소 토큰 보너스나 그래프 없이도 gold의 짧은 긍정 block이 긴 부정 distractor보다 높다. | 인공물 중심 |
| relation | 1.000 | 1.0000 | — | lexical이 gold를 top 5에 넣고 `W_RELATION=0.30` 또는 curated anchor가 1위로 올린다. | 원리는 실제에도 성립, 완벽 점수는 인공물 |
| temporal | 1.000 | 1.0000 | 0.000 | `W_SUPERSEDE_FORWARD=1.00`이 구판 lexical 점수를 최신판에 넘기고 `GATE_SUPERSEDED=0.30`이 구판을 내린다. | 의미는 타당, 실제 효과 판정불가 |
| crosslingual | 1.000 | 0.7333 | — | 질문을 그대로 담은 한국어 짝에서 `W_RELATED_HOP=0.85`로 영어 gold에 이동한다. `W_CURATED_ANCHOR=0.35`가 zero 40개를 top 5 안에 유지한다. | 인공물 |
| paraphrase | 0.200 | 0.1238 | — | gold와 공유한 제품 코드의 어휘 점수뿐이다. 구조 hop은 기여하지 않는다. | 인공물, 실패한 일반화 |

### exact

- 100/100 문항의 distractor가 질문 문장을 글자 그대로 포함한다.
- `cfg.pipeline.NNN`의 문서 빈도는 easy 80문항에서 3, hard 20문항에서 7이다. 희소 토큰이기는 하지만 `W_RARE_MATCH=0`에서도 recall@5/MRR은 1.00이고 `LEX_TAIL_W=1`도 변화가 없다.
- 따라서 perfect score의 직접 원인은 구조가 아니라 gold block이 짧고 긍정형인 반면 distractor block은 질문 복사, 거절 설명, 근접 버전을 함께 넣어 더 긴 생성기 문장이라는 점이다.

### relation

- 100/100 gold block만 `related` 간선을 소유하고, 100/100 distractor는 두 식별자를 모두 담되 관계 간선은 소유하지 않는다.
- `W_RELATION=0`이면 recall@5는 1.00 그대로지만 모든 gold가 1위에서 2위로 내려가 MRR이 1.00→0.50이다.
- `W_RELATION=0`과 `W_CURATED_ANCHOR=0`을 함께 끄면 easy 67개는 1위, hard 33개는 2위(MRR 0.835)다. 즉 구조는 top-5 회수가 아니라 순위/근거 선택에 기여한다.
- block이 어느 링크를 주장하는지 쓰는 것은 실제 JSON 정본에도 있는 성질이다. 다만 생성기는 바로 그 필드를 정답 라벨로 만들었으므로 1.00은 낙관적이다.

### temporal

- 프로필은 strong 55, weak 30, current 15다. strong+weak 85개 모두 stale 쪽 lexical overlap이 gold보다 크며, gold 100개 모두 `supersedes` 간선을 가진다.
- `W_SUPERSEDE_FORWARD=0`이면 strong 55개가 top 10 밖으로 사라져 temporal recall@5가 1.00→0.45, stale_above가 0.00→0.37이다.
- `GATE_SUPERSEDED=1`로 강등만 끄면 recall@5는 1.00이지만 MRR은 1.00→0.785, stale_above는 0.00→0.37이다. 전진은 정답 회수, gate는 구판/신판 순서에 각각 기여한다.
- 이 시간축 의미는 설계상 정당하지만 실제 6-page wiki에는 `supersedes`가 없어 실증되지 않았다.

### crosslingual

- 의심 1은 그대로 확인됐다. 100/100 한국어 짝 page가 질문 전체를 글자 그대로 포함하고, 100/100이 전용 `related` 간선으로 영어 gold에 연결된다. gold 자체가 zero-overlap인 것은 40개뿐이며 나머지 60개는 불투명 제품 코드를 공유한다.
- `W_RELATED_HOP=0`이면 recall@5가 1.00→0.20이다. zero 40개는 top 10 밖, partial-easy 20개는 3위, partial-hard 40개는 7위가 된다.
- `W_CURATED_ANCHOR=0`이면 zero 40개가 6위로 밀려 recall@5가 1.00→0.60이다.
- 기존 `use_aliases=False` 결과가 baseline과 완전히 같고 이번 `W_CONCEPT_HOP=0`도 완전히 같다. 따라서 structural.py docstring의 “crosslingual/paraphrase는 alias 되먹임이 유일한 경로” 설명과 달리, 이 동결 세트에서 개념층의 측정 기여는 0이다.

### paraphrase

- 의심 2도 수치대로 확인됐다. zero 40개에서 distractor→gold 간선은 0개다. 질문 토큰을 가진 page→gold 간선은 q00424와 q00444의 2개뿐이며 둘 다 생성기의 무작위 `wiki`/`links` 간선이다. `related`·`supersedes` 간선은 0개다.
- 구조 랭커의 실제 gold 순위는 zero 40개 전부 `>10`, partial-easy 20개 전부 3위, partial-hard 40개 전부 7위다. hard의 7위는 질문 전체를 복사한 distractor가 정확히 6개이기 때문이다.
- `W_RARE_MATCH=0`은 recall@5 0.20에는 영향이 없지만 hard 40개를 top 10 밖으로 보내 recall@10을 0.60→0.20으로 낮춘다. `related`, concept hop, relation, tail 합산은 recall@5에 영향이 없다.
- 따라서 과거의 “그래프 경로 29개” 같은 연결 가능성은 관련성 신호가 아니다. 이 문제에서 gold로 가는 의도된 구조 경로는 없다.

## 절제 행렬

각 arm은 같은 500문항, k=10이며 structural.py 파일을 수정하지 않고 검색 시점 모듈 상수만 monkeypatch했다.

| arm | 전체 R@5 | 전체 MRR | stale | exact | relation | temporal | cross | para |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| baseline | 0.840 | 0.7714 | 0.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0.20 |
| `W_RARE_MATCH=0` | 0.840 | 0.7600 | 0.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0.20 |
| `W_CURATED_ANCHOR=0` | 0.760 | 0.7581 | 0.00 | 1.00 | 1.00 | 1.00 | 0.60 | 0.20 |
| `W_SUPERSEDE_FORWARD=0` | 0.730 | 0.6614 | 0.37 | 1.00 | 1.00 | 0.45 | 1.00 | 0.20 |
| `GATE_SUPERSEDED=1` | 0.840 | 0.7284 | 0.37 | 1.00 | 1.00 | 1.00 | 1.00 | 0.20 |
| `W_RELATED_HOP=0` | 0.680 | 0.6495 | 0.00 | 1.00 | 1.00 | 1.00 | 0.20 | 0.20 |
| `W_CONCEPT_HOP=0` | 0.840 | 0.7714 | 0.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0.20 |
| `W_RELATION=0` | 0.840 | 0.6714 | 0.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0.20 |
| `LEX_TAIL_W=1` | 0.840 | 0.7714 | 0.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0.20 |
| `W_RELATION=0,W_CURATED_ANCHOR=0` | 0.680 | 0.6584 | 0.00 | 1.00 | 1.00 | 1.00 | 0.20 | 0.20 |

마지막 조합 arm의 전체 하락은 relation이 아니라 anchor가 cross zero를 top 5 밖으로 보내기 때문이다. relation 자체는 R@5 1.00, MRR 0.835다.

## 민감도 sweep과 튜닝 주장

표의 값은 `전체 recall@5 / 직접 영향을 받은 유형 recall@5`다. factor 순서는 0.50x, 0.75x, 1.25x, 1.50x다.

| 상수(기준값) | 0.50x | 0.75x | 1.25x | 1.50x | 부수 효과 |
|---|---:|---:|---:|---:|---|
| `W_CURATED_ANCHOR` (0.35) | .840 / 1.00 cross | .840 / 1.00 | .840 / 1.00 | .840 / 1.00 | 0까지 끌 때만 cross가 0.60으로 하락 |
| `W_RELATED_HOP` (0.85) | .760 / .60 cross | .760 / .60 | .840 / 1.00 | .840 / 1.00 | 1.25x부터 relation MRR 하락, 1.50x에서 0.50 |
| `GATE_SUPERSEDED` (0.30) | .840 / 1.00 temporal | .840 / 1.00 | .840 / 1.00 | .840 / 1.00 | 이 범위 stale 0.00; 1.0에서 stale 0.37 |
| `W_RARE_MATCH` (0.60) | .840 / 변화 없음 | .840 / 변화 없음 | .840 / 변화 없음 | .840 / 변화 없음 | 0에서 paraphrase R@10만 0.60→0.20 |
| `MIN_RELATED_SRC_LEX` (0.50) | .840 / 1.00 cross | .840 / 1.00 | .840 / 1.00 | .840 / 1.00 | 낮추면 cross MRR 0.7333→0.4333, R@5는 유지 |

명확한 절벽은 `W_RELATED_HOP`이다. 기준 0.85에서 zero crosslingual 40개는 모두 3위인데, 25% 낮춘 0.6375에서는 모두 9위가 되어 cross R@5가 1.00→0.60, 전체가 0.84→0.76으로 떨어진다. 0.425에서는 40개가 전부 top 10 밖이다. 반대로 1.0625 이상이면 cross 100개가 전부 1위지만, 무작위 related hop이 relation 정답을 밀어 relation MRR이 0.665(1.25x), 0.50(1.50x)로 나빠진다. 이 값은 서로 다른 유형 사이의 매우 좁은 순위 경계에 놓여 있다.

그러므로 “queries.json으로 튜닝하지 않았다”는 작성 과정 주장은 **판정불가**다. 이를 입증할 독립 validation set, 사전 등록값, commit provenance가 없다. 다만 frozen queries에 대한 결과는 `W_RELATED_HOP` 한 값에 절벽형으로 맞아 있어 **결과 과적합은 명확**하다. 나머지 네 상수의 local recall@5는 안정적이라는 반대 증거도 함께 남긴다.

## qmd vector arm 공정성

전체 qmd vsearch arm의 유형별 recall@5는 exact 0.02, relation 0.47, temporal 0.11, crosslingual 0.10, paraphrase 0.28이다. paraphrase 첫 10개를 기존 project-local `llmwiki_bench` 색인에서 직접 조회했다. 재색인·collection 변경은 하지 않았다. 설치된 qmd는 각 `vsearch`에서 원문, 두 vec 변형, HyDE의 4 vector query를 사용했으며 아래 score는 qmd가 반환한 융합 점수다.

| query | profile | gold rank (score) | distractor rank (score) |
|---|---|---|---|
| q00401 | zero | >655 (미반환) | 101 (.4525), 64 (.4583) |
| q00402 | partial-easy | 3 (.5030) | 9 (.4887), 36 (.4742) |
| q00403 | partial-hard | 23 (.4922) | 91 (.4755), 95 (.4747), 43 (.4865), 123 (.4703), 118 (.4713), 98 (.4742) |
| q00404 | zero | >425 (미반환) | 30 (.4784), 24 (.4793) |
| q00405 | partial-hard | 130 (.4570) | 36 (.4764), 59 (.4715), 28 (.4810), 34 (.4770), 47 (.4741), 9 (.4930) |
| q00406 | partial-hard | 7 (.4766) | 12 (.4699), 33 (.4606), 23 (.4645), 32 (.4609), 21 (.4668), 11 (.4705) |
| q00407 | zero | >424 (미반환) | 5 (.4844), 2 (.5036) |
| q00408 | partial-easy | 6 (.4886) | 31 (.4675), 61 (.4604) |
| q00409 | partial-hard | 3 (.4836) | 255 (.4245), 42 (.4577), 110 (.4449), 11 (.4717), 6 (.4752), 13 (.4705) |
| q00410 | zero | >420 (미반환) | 1 (.4982), 10 (.4641) |

`-n 10000` 요청에도 qmd가 양의 후보로 반환한 page는 질문별 420~822개였다. 따라서 미반환 gold의 정확한 전역 점수는 없고 표에는 안전한 순위 하한만 썼다.

공정성 판정은 **“코퍼스가 vector에 불리하게 만들어졌다”**다.

- zero 4개 표본은 질문의 유일한 entity 식별자(`aatg-nimbus` 등)가 gold 본문에 아예 없다. 반면 모든 distractor가 식별자와 질문 전체를 그대로 담는다. 본문만 보는 vector가 어떤 gold가 그 entity의 답인지 식별할 정보가 없다. 네 gold 모두 수백 개 반환 집합에도 들지 못했다.
- partial 6개는 gold에도 식별자가 있어 구분 정보가 생긴다. gold 순위는 3, 23, 130, 7, 6, 3이고, 이 중 top 10이 4/6이다. vector가 의미를 전혀 못 읽는다는 결과가 아니다.
- 10,000/10,000 page가 동일한 기본 템플릿 문장을 갖고, 질문 식별자는 의미 없는 4글자 코드다. 모든 paraphrase distractor는 질문 전체를 복사한 뒤 “정답이 아니다”라고 부정한다. 이는 의미 검색보다 표면 유사도를 의도적으로 함정에 빠뜨리는 설계다.

따라서 qmd가 이 adversarial synthetic corpus에서 실제로 약한 것은 맞지만, 0.196을 자연 문서 vector 검색의 품질로 해석하면 안 된다. 특히 zero 질문은 vector 성능 문제가 아니라 본문에서 entity↔gold 대응 정보가 삭제된 식별 불가능 문제다.

## 실제 wiki 대비

현재 `wiki/`는 6 page, 8 link이며 link kind는 전부 `wiki`다. alias가 있는 page 0, `related` 0, `supersedes` 0, `sources`가 채워진 page 0이다. 합성 코퍼스는 10,000 page 모두 outgoing link가 정확히 4개이고 모두 같은 기본 템플릿을 가진다.

실제 문서에도 옮겨갈 수 있는 결론은 제한적이다.

- FTS trigram의 한국어 prefix 처리와 희소 식별자 취급은 실제 exact 검색 후보로 합리적이다. 다만 이번 exact 1.00은 그 품질을 구조 baseline과 분리해 증명하지 못했다.
- `links[].block_id`로 관계 주장 근거를 고르는 기능은 실제 정본 스키마와 맞는다. relation의 MRR 개선은 제품 가설로 유지할 가치가 있다.
- `supersedes`가 실제로 큐레이션되면 최신판 전진/구판 강등은 의미적으로 타당하다. 현재 실제 데이터에는 사례가 없어 가중치와 오류율은 별도 실문서 질문 세트가 필요하다.

합성 코퍼스에서만 성립하는 결론은 다음과 같다.

- crosslingual 1.00, paraphrase의 고정 3위/7위, temporal 1.00의 크기, qmd 대비 4.29배 우위(0.840/0.196)는 모두 생성기의 질문 복사·전용 간선·고정 distractor 수·식별자 삭제에 의존한다.
- concept/alias 층 우위는 관측되지 않았다. 실제 wiki에도 alias가 없으므로 지금은 주장할 근거가 없다.
- 10,000-page scale의 수치가 6-page 실제 wiki에 직접 적용된다고 볼 수 없다.

다음 유효성 검증에는 합성 generator와 독립적으로 작성한 자연 문서 질문, 질문을 보지 않고 고정한 가중치, 자연스러운 부정/대체 문서, 그리고 별도 tuning/validation split이 필요하다.

## 재실행

```bash
python3 bench/audit/corpus_probe.py
python3 bench/audit/ablate.py --index-root bench/index_audit --out bench/results_audit/ablation.json --rebuild-index
python3 bench/audit/sweep.py --index-root bench/index_audit --out bench/results_audit/sensitivity.json
python3 bench/audit/vector_probe.py --sample 10 --limit 10000
```

결과 원본은 `bench/results_audit/ablation.json`, `sensitivity.json`, `corpus_probe.json`, `vector_probe.json`에 있다.

## qmd search 조사 제거

기존 project-local `llmwiki_bench`의 BM25 색인을 변경하지 않고 500문항을 다시 조회했다. raw는 질문 원문, stripped는 `bench/rankers/structural.py::query_terms()`의 조사 제거 토큰, backoff는 stripped가 빈 결과일 때 기존 렌더링 Markdown의 추정 DF가 큰 토큰부터 하나씩 제거하며 최대 3회 재시도한 arm이다. 실행 전후 `.qmd/index.sqlite` SHA-256은 같았다.

| arm | R@1 | R@5 | R@10 | MRR@10 | nDCG@10 | stale_above | empty | qmd calls | p50 / p95 ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| raw | 0.000 | 0.000 | 0.000 | 0.0000 | 0.0000 | 0.00 | 300/500 | 500 | 86.515 / 91.959 |
| stripped | 0.000 | 0.000 | 0.000 | 0.0000 | 0.0000 | 0.00 | 200/500 | 500 | 87.058 / 92.389 |
| backoff | 0.028 | 0.030 | 0.030 | 0.0285 | 0.0289 | 0.85 | 100/500 | 900 | 90.543 / 341.680 |

이 결과는 “qmd search가 한국어 문장을 지원하지 않아 0.000”이라는 종전 설명을 반박한다. raw도 crosslingual 100개와 paraphrase 100개에서 결과를 반환했고, stripped는 exact 100개까지 더해 300/500에서 결과를 반환했다. 즉 0.000은 엔진의 한국어 미지원이 아니라 원문의 붙은 조사와 qmd의 엄격한 AND 조건, 그리고 반환 후보의 정답 순위가 결합한 결과다. stripped exact도 100/100 비어 있지 않았지만 gold는 top 10에 하나도 없었다.

backoff는 300문항에서 재시도 없이 끝났고, temporal 100개는 가장 흔한 `무엇`을 한 번 뺀 뒤 모두 비어 있지 않게 됐다. 그중 생성기의 current 프로필 15개만 gold를 top 5에서 회수해 temporal R@5 0.15가 됐고, strong/weak 85개에서는 낡은 page가 gold보다 위라 stale_above가 0.85였다. relation 100개는 `서비스`, `저장소`, `관계`를 차례로 제거해도 식별자와 gold에 없는 `어떤`이 AND로 남아 3회 재시도 후에도 전부 비었다. 유형별 R@5는 backoff temporal 0.15 외 exact/relation/crosslingual/paraphrase가 모두 0.00이다.

따라서 조사 제거는 **qmd search의 후보 생성 기능을 복구하지만 검색 품질은 복구하지 않는다.** 전체 R@5는 raw/stripped 0.000, backoff 0.030으로 baseline 0.464와 structural 0.840보다 훨씬 낮고, backoff는 p95를 약 92ms에서 342ms로 늘린다. 이 합성 벤치에서 qmd BM25는 독립 최종 랭커로 부적합하며, 정본 랭커에 공급하는 recall-oriented 후보기로만 평가해야 한다.

### 프로덕션 폴백 결함

`scripts/llmwiki_context.py::qmd_slugs()`는 현재 원문 `query`를 그대로 `qmd search`에 넘긴다. 따라서 한국어 조사 하나가 AND 조건을 만족하지 못하면 qmd 폴백 전체가 빈 후보가 되는 동일 결함이 프로덕션에도 있다. **수정 방향: qmd 호출 전에 조사 제거 토큰 질의를 만들고, 빈 결과에는 저정보량 common token을 결정적으로 줄이는 bounded backoff를 적용한 뒤 후보를 기존 canonical ranker로 재평가한다.**

재실행 명령은 `python3 bench/audit/qmd_search_arm.py --index-dir bench/index/vector-mode_search --out bench/results_audit/qmd_search_arm.json`이다.
