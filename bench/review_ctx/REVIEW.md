| 유지 (3) | 수정 필요 (8) | 기각 (2) |
|---|---|---|
| ① 동결 500문항의 production 6000 수치 재현<br>② 6000에서 커버리지 병목은 검색이라는 분해<br>③ cut=0.5는 이 동결 세트의 효율적 운용점 | ① `gold_block`을 prefix 전달로 명명<br>② 320자 clip 완전성 지표 추가<br>③ 긴 block에서 바이트 배율 재표기<br>④ 상투 summary 제거의 한계 명시<br>⑤ 질문 복사·전용 간선·고정 배치 의존 분리<br>⑥ 하드링크 root의 2000바이트 편향 정정<br>⑦ stale 두 겹 설명에 head 전제 명시<br>⑧ §4를 현행 훅 계약에 맞게 보강 | ① head 계산 없이 “형식만으로 stale_leak=0”<br>② 합성 세트의 4.3배를 실위키 채택 근거로 일반화 |

# CONTEXT_REPORT 적대적 검증

## 결론

보고서의 동결 코퍼스 수치는 재현된다. production 6000은 `gold_block=0.358`, `stale_leak=0.900`, 평균 3050.7 B, B/정답 8521.5 B였고, v2-graph[cut=0.5]는 `0.920`, `0.000`, 1818.5 B, 1976.7 B였다. 반올림 전 값까지 기존 `bench/results_ctx/500q-summary.json`과 같다.

그러나 여기서 `gold_block`은 “gold block 전체가 들어갔다”가 아니다. manifest가 해당 block을 body로 선언하고 공백 제거한 본문 앞 60자가 문자열에 있으면 성공이다. 600/900/1200자로 늘린 100문항에서 prefix 성공률은 production 0.36, v2 0.92로 유지됐지만 전체 본문과 끝 60자 성공률은 둘 다 0.00이었다. 따라서 이 지표는 `gold_block_prefix60` 또는 `gold_block_selected_and_prefix_present`로 이름을 바꾸고, `full_body`·`suffix`·답 위치별 지표를 별도로 내야 한다.

6000바이트에서는 production 검색 결과를 v2 형식으로 바꿔도 커버리지는 0.358 그대로이고, v2 검색 결과를 production 형식으로 바꾸면 0.920이 된다. 이 범위의 커버리지 차이는 검색이 만든다는 보고서 판단은 유지한다. 다만 0.358 대 0.920의 크기와 4.3배 B/정답은 질문 복사 distractor, gold 전용 간선, 짧은 block, 고정 배치에 강하게 의존하므로 실위키 채택 수치로는 기각한다.

## 1. 하네스 정직성과 production 경로

### 1.1 앞 60자와 전체 본문

유형별 처음 20문항, 총 100문항의 gold block 하나씩을 600/900/1200자로 늘렸다. 앞부분은 원문을 그대로 두고 끝에 문항별 sentinel을 두었으며, 정본 변형은 `bench/review_ctx/long_root/`에만 있다. structural2와 `ctx.sqlite`는 기존 읽기 전용 색인을 쓰고, 렌더 직전 gold 본문만 변형 정본으로 교체했다. production은 변형 정본 10,000파일 뷰를 실제로 스캔했다.

| arm, 6000 B | manifest body 선택 | 앞 60자+manifest | clip 320자+manifest | 전체 본문 | 끝 60자 | 평균 B | B/prefix hit |
|---|---:|---:|---:|---:|---:|---:|---:|
| production-long | 0.360 | 0.360 | 0.360 | **0.000** | **0.000** | 3257.3 | 9048.2 |
| v2-graph-long[cut=0.5] | 0.920 | 0.920 | 0.920 | **0.000** | **0.000** | 2788.0 | 3030.4 |

같은 100문항의 원래 짧은 block에서는 production이 3050.0 B / B-hit 8472.3, v2가 1795.0 B / B-hit 1951.1이었다. 긴 block에서는 v2의 평균 payload 절감이 41.1%에서 14.4%로, B-hit 배율이 4.34배에서 2.99배로 줄었다. 검색 선택 우위(0.36→0.92)는 남지만, `clip(320)` 뒤의 결론이나 단서가 전달됐는지는 현 지표가 전혀 말하지 못한다.

전체 본문 일치는 320자 clip과 양립할 수 없으므로, 제품 지표는 다음처럼 나누는 편이 정직하다.

- retrieval: gold page/block ID가 선택됐는가
- delivery: 선택된 block의 앞 320자가 실제 payload에 들어갔는가
- completeness: 원문 전체 또는 답 span이 들어갔는가
- truncation risk: 답 span이 320자 뒤에 있어 손실됐는가

이번 변형은 답 span 라벨을 새로 만들지 않았으므로 마지막 항목의 실제 정답률은 판정하지 않았다. 전체 본문 0.00을 곧바로 답변 정확도 0.00으로 해석해서도 안 된다.

### 1.2 production 호출 사슬

`ProductionArm.prepare`는 `C.build_context(root, query, use_qmd=False, ...)`를 직접 부른다. 동적 wrapper로 q00001 한 건을 추적한 결과는 `retrieve` 1회 → `project_hit` 5회 → `render` 1회였다. 이는 현행 `build_context → retrieve → project_hit → render` 경로와 일치한다. 별도 랭커나 cached document 경로는 타지 않았다.

예산별 재렌더는 `build_context`가 만든 `Result`와 projection을 재사용한다. 이 코퍼스에는 `tools/config/context.json`이 없어 always preamble이 없으므로 2000/4000/6000 결과는 예산마다 `build_context`를 다시 부른 것과 동등하다. qmd는 명시적으로 껐다.

### 1.3 하드링크 root 경로 편향

하드링크 자체는 파일 내용, 상대 `file:` 경로, 검색 순위에 영향을 주지 않았다. 문제는 벤치 root 문자열이 실제 root보다 21바이트 길다는 점이다(53자 대 32자). 원본 arm은 긴 root로 먼저 예산 결정을 한 뒤 결과 문자열에서만 실제 root로 치환한다.

| 예산 | 원본 arm gold / stale_leak | 예산 계산 전 root 보정 gold / stale_leak | 판정 변화 |
|---:|---:|---:|---:|
| 2000 | 0.090 / **0.890** | 0.090 / **0.900** | 1/500 |
| 4000 | 0.358 / 0.900 | 0.358 / 0.900 | 0/500 |
| 6000 | 0.358 / 0.900 | 0.358 / 0.900 | 0/500 |

q00297 한 건에서 긴 벤치 root는 1267 B, 1 page/1 block만 넣어 stale body를 우연히 제외했다. 실제 root로 예산을 계산하면 1987 B, 2 page/3 block이 들어가 stale leak가 생긴다. 따라서 보고서의 2000 B stale 0.890은 프로덕션 상당값 0.900으로 고쳐야 한다. 중심 결론인 4000/6000 수치에는 영향이 없다.

## 2. 검색과 렌더 형식의 2×2 분해

모든 값은 500문항, 6000 B다. 교차 arm은 `bench/review_ctx/review_arms.py`에만 구현했다.

| 검색 | 렌더 | cut | prefix gold | stale_leak | 평균 B | B/prefix hit |
|---|---|---:|---:|---:|---:|---:|
| production | production | — | 0.358 | 0.900 | 3050.7 | 8521.5 |
| production | v2-graph | — | 0.358 | 0.000 | 1524.7 | 4258.9 |
| structural2 | production | 0.5 | 0.920 | 0.000 | 3245.4 | 3527.6 |
| structural2 | v2-graph | 0.5 | 0.920 | 0.000 | 1818.5 | 1976.7 |

해석은 세 부분으로 나뉜다.

1. 6000 B에서 커버리지 0.358→0.920은 검색이 만든다. production 검색에 compact 형식을 씌워도 gold는 늘지 않는다.
2. 같은 검색에서 v2 형식은 바이트를 줄인다. production 검색은 3050.7→1524.7 B, v2 검색은 3245.4→1818.5 B다. 다만 이 절감의 무손실성은 상투 summary와 짧은 block에 기대므로 실문서에서는 다시 재야 한다.
3. 2000 B에서는 렌더도 커버리지에 관여한다. production 검색+production 렌더는 0.090, 같은 검색+v2 block 단위 렌더는 0.358이었다. “원인은 검색뿐”이라는 문장은 6000 B에만 맞다.

cut을 끈 structural2 검색+production 렌더도 gold 0.920이었지만 평균 5637.4 B / B-hit 6127.6이었다. 같은 무컷 v2-graph는 기존 결과에서 3317.5 B / 3606.0 B-hit이다. compact 형식의 비용 이점은 재현되지만 실제 정보 손실 여부는 이 합성 문서로 판정할 수 없다.

## 3. cut=0.5 민감도

| cut | prefix gold | 평균 B | B/prefix hit | exact | relation | temporal | cross | paraphrase |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.30 | 0.920 | 2833.7 | 3080.1 | 1.00 | 1.00 | 1.00 | 1.00 | 0.60 |
| 0.40 | 0.920 | 2443.0 | 2655.4 | 1.00 | 1.00 | 1.00 | 1.00 | 0.60 |
| 0.50 | 0.920 | 1818.5 | 1976.7 | 1.00 | 1.00 | 1.00 | 1.00 | 0.60 |
| 0.55 | 0.920 | 1595.6 | 1734.3 | 1.00 | 1.00 | 1.00 | 1.00 | 0.60 |
| 0.60 | 0.914 | 1433.8 | 1568.7 | 1.00 | 1.00 | 1.00 | 1.00 | 0.57 |
| 0.70 | 0.858 | 1213.1 | 1413.8 | 1.00 | 1.00 | 1.00 | 1.00 | 0.29 |

cut=0.5 자체는 감사 보고서의 `W_RELATED_HOP`형 절벽이 아니다. 0.30~0.55의 넓은 구간에서 gold가 완전히 같고, 0.5를 20% 높인 0.6에서도 전체 하락은 0.006뿐이다. 0.7에서 0.056이 더 떨어지지만 손실은 모두 생성기가 3위/7위로 고정 배치한 paraphrase다. 따라서 “0.5만 가능한 날카로운 튜닝값”은 아니며, 오히려 합성 배치가 만든 단계형 경계다.

다만 이 sweep도 같은 500문항에서 값을 고르고 검증한 것이다. 0.5는 동결 세트의 운용점으로는 유지하되 기본값 확정에는 독립 자연문서 validation과 무관련 질문 세트가 필요하다. 수치만 보면 0.55가 같은 커버리지에 더 작지만, 같은 데이터에서 더 공격적으로 고르는 것은 추가 과적합이므로 추천하지 않는다.

## 4. 합성 코퍼스 인공물 의존

| 인공물 | 관측된 영향 | 실제에도 남는 것 | 현재 결론 |
|---|---|---|---|
| 짧은 block(평균 79자, 최대 205자) | 원본에서 clip이 0회다. 600~1200자 변형에서는 prefix hit가 0.36/0.92인데 full/suffix는 양쪽 0.00, v2의 평균 byte 절감은 같은 100문항에서 41.1%→14.4%로 축소됐다. | block ID 선택과 앞부분 전달은 유지된다. | 완전성·4.3배 일반화는 불가 |
| 전 page 동일 상투 summary | production은 title/summary/tags/projects/score/file을 싣고 v2는 대부분 버린다. production 검색의 형식만 바꿔 3050.7→1524.7 B가 됐지만 gold는 그대로였다. | 고정 머리말·중복 metadata 절감 원리는 유효하다. | 실제 summary가 disambiguation하는 비용은 미측정 |
| 질문을 복사한 부정 distractor | production 6000 유형별 gold는 exact .80, relation .34, temporal .45, cross .20, paraphrase .00이다. compact 렌더로는 하나도 회복되지 않았다. | adversarial 부정 문서를 다뤄야 한다는 문제는 실제에도 있다. | production .358의 절대 크기는 합성 난이도 의존 |
| gold 전용 related/supersedes | structural2+production 형식만으로 전체 .920을 회수한다. relation/cross/temporal 1.00의 크기는 생성기가 정답 필드에 맞춘 결과다. | block-owned relation 근거와 supersedes 의미는 제품 가설로 유효하다. | 검색 일반 우월성의 증거는 아님 |
| 고정 distractor 수·page_id tie-break | paraphrase가 cut 0.55까지 .60, 0.6에서 .57, 0.7에서 .29로 단계적으로 꺾인다. production `rank`도 동점이면 `page_id` 오름차순이다. | 결정적 tie-break 자체는 필요하다. | 이 ID 배치의 순위를 자연 품질로 해석 불가 |

따라서 production 0.358은 6000 B에서는 “렌더가 나빠서”가 아니라 검색 결과가 gold page/block을 선택하지 못해서다. 그러나 “production 검색이 본질적으로 나쁘다”는 뜻은 아니다. 이 코퍼스가 어휘 검색에는 질문 복사 distractor를 주고 structural2에는 gold 전용 간선과 고정 배치를 준 만큼, 0.358→0.920의 효과 크기는 공정한 자연문서 추정치가 아니다.

## 5. 낡은 문서: 형식과 head 계산을 분리

보고서의 `V2GraphArm.status`는 `page["head"] != page["page_id"]`이면 `superseded`라고 한다. 이 `head`는 문자열 형식이 알아낸 것이 아니라 `CtxIndex.build`가 모든 supersedes 간선을 역방향으로 접어 미리 저장한 값이다. fold=false 실험도 검색 랭커의 fold만 끄고 `ctx.sqlite.page.head`는 그대로 둔다. 그러므로 기존 “형식 쪽만으로도 누출은 0”은 정확히는 “검색 점수 fold를 꺼도, 별도 색인 head 주석을 소비하는 렌더 방어로 0”이다.

head를 page_id로 강제해 접기 정보를 제거하되 edge와 block을 그대로 둔 temporal 100문항 결과는 다음과 같다.

| arm | temporal gold | stale_body | stale_leak | 평균 B |
|---|---:|---:|---:|---:|
| fold=false, head 있음, v2-graph (기존 보고서) | 0.45 | 0.00 | 0.00 | — |
| fold=false, **head 없음**, v2-graph | 0.45 | **1.00** | **1.00** | 3926.7 |
| fold=false, **head 없음**, v2-graph cut=0.5 | 0.45 | **0.95** | **0.95** | 1127.3 |

supersedes 간선만 있고 head 계산이 없을 때 형식이 할 수 있는 일은 제한된다.

- 현재 page와 `current supersedes→old` 간선이 둘 다 payload에 있으면 LLM에 관계를 표시할 수 있다.
- old page만 검색되면 outgoing 간선은 old에 없으므로, 형식만으로 old임을 알 수 없다.
- 모든 reverse supersedes 간선을 조회해 old에 `sup→head`를 붙이는 순간 그것은 이미 head/역방향 접기 계산이다.
- 따라서 stale body를 보장적으로 막으려면 검색 fold와 독립된 reverse lookup/head annotation이 필요하다. 둘 다 없으면 본문을 전부 금지하는 것 외에는 0 누출 보장이 없다.

제품 설계에서는 `head`를 형식 기능이라 부르지 말고 `temporal status projection`이라는 별도 단계로 명세해야 한다. 다중 successor, fork, cycle, 32-hop 초과는 임의 head로 접지 말고 unresolved temporal conflict로 표시해야 한다.

## 6. §4 설계 제안과 현행 계약 충돌·누락

| 계약 | 현행 `llmwiki_context.py` | §4의 문제 | 필요한 수정 |
|---|---|---|---|
| 실행 fail-open | hook 전체 예외를 잡고 stdout 없이 exit 0; 셸 wrapper도 실패를 삼킨다. | §4.2의 scan fallback과 실행 fail-open을 같은 말로 쓴다. SQLite 오류·lock·schema mismatch 경로가 없다. | 두 계약을 구분하고 DB 예외 즉시 scan 또는 무주입, 최종 exit 0을 테스트한다. |
| 6초 watchdog | `LLMWIKI_CONTEXT_TIMEOUT`, 기본 6초 `SIGALRM`; 설치 hook 외부 timeout은 10초다. | 색인 열기, hash 확인, stale 상위 page 읽기, scan fallback을 6초 안에 끝내는 예산이 명세에 없다. | 단계별 deadline, SQLite `mode=ro`, 짧은 busy timeout, close, timeout 회귀 테스트를 넣는다. |
| 파생물 build 원자성 | JSON `dump`는 tmp→`os.replace`지만 여러 산출물은 순차 교체된다. | 새 SQLite를 직접 build하면 hook이 반쯤 만들어진 DB나 revision 전환 중 상태를 볼 수 있다. | 별도 tmp DB에서 transaction/VACUUM/검증 후 `os.replace`; revision/schema/version 불일치를 안전하게 처리한다. |
| canonical freshness | 현재 검색/본문 모두 매번 `wiki/**/*.json`을 읽어 새 page도 즉시 보인다. | §4.5는 stale index가 고른 top 10만 hash 검증한다. 새 page와 점수가 바뀌어 top 10에 와야 할 수정 page는 후보가 아니어서 영구 누락된다. 이는 canonical fail-open이 아니다. | 신선도를 확증할 수 없으면 scan fallback. stale index 사용은 명시적 degraded mode로만 허용하고 누락 가능성을 stats에 낸다. |
| 무주입 문턱 | 절대 `MIN_SCORE=6`, coverage, 숫자-only 제거, `MIN_MATCHED=5` 면제, hint의 별도 score/coverage/matched를 쓴다. | structural2 1위=1 상대점수는 무관련 질문도 후보가 있으면 항상 1이다. 제안의 raw impact/coverage는 음성 세트 없이 미보정이며 기존 CLI `min_score`, hint 계약도 빠졌다. | 무관련·부분 관련·긴 지시문 validation을 먼저 만들고 절대 신호로 silence 우선 보정. 기존 hint/wide/numeric 규칙과 옵션 호환을 명시한다. |
| MCP get은 정본 읽기 | `find_doc`가 정본을 찾고 `get_page`가 정본 block object를 반환한다. | stale index lookup miss, 새 page, 잘못된 `file`, root 탈출, page ID 불일치 때 canonical fallback이 없다. | index는 주소 힌트만. miss/sha/id 불일치면 `find_doc`; `file.resolve()`가 `root/wiki` 안인지 확인한 뒤 정본 한 파일을 읽는다. `blk.text`를 MCP get 결과로 쓰지 않는다. |
| 보안 마스킹 | summary, block, outline, page metadata, mode=page 전체 JSON까지 `redact`한다. | §4.3은 B와 sources만 언급한다. address의 title/summary, P/E 값, always, search row, 오류 문자열, 제어문자 escaping이 빠졌다. SQLite에 우발 credential 원문을 복제하는 문제도 없다. | 모든 최종 문자열/JSON에 중앙 redaction+escaping. 가능하면 색인에는 redacted text 또는 검색용 토큰만 저장하고 파일 권한을 제한한다. |
| byte+token 상한 | 모든 production render가 두 상한을 검사하고 잘린 markdown을 내지 않는다. | 벤치 `V2GraphArm.run`은 byte만 검사한다. §4는 둘을 보존한다고 쓰지만 원형 코드가 그 계약을 증명하지 않는다. | group 추가마다 byte와 `est_tokens`를 함께 검사하고 6000 B/2000 token 회귀 테스트를 둔다. |
| always/config/환경 호환 | always 고정 몫, pinned 중복 제거, max bytes/tokens/pages/blocks, qmd 환경변수와 CLI가 있다. | 새 `Hit.doc=None`, group 반환으로 pinned 제거 코드가 그대로 동작하지 않는다. qmd 제거는 기본 동작 변경이고 cut 외 기존 옵션 매핑이 빠졌다. | always index miss는 canonical fallback, pinned를 page_id로 제거, 기존 환경/CLI 계약의 유지·폐기를 항목별 결정한다. |
| temporal 정확성 | 현재 production은 stale 상태를 계산하지 않는다. | 원형 `CtxIndex`는 `succ.setdefault`, 32-hop guard로 fork/cycle을 조용히 하나의 head로 만든다. | fork/cycle/다중 최신판을 conflict로 보존하고 본문 억제 이유를 payload와 stats에 낸다. |

유지할 제안도 있다. search.sqlite를 `build`만 생성하고 hook은 읽기 전용으로 여는 것, `should_skip`을 `"<llmwiki-context"`로 넓히는 것, MCP get에서 최종 object는 정본 파일 하나에서 읽는 것, scan fallback을 남기는 방향은 현행 계약과 맞는다. 단 §4.5의 stale-index top-10 검증은 scan fallback을 대신할 수 없다.

상대 점수 cut과 무주입 문턱은 반드시 분리해야 한다. cut은 이미 관련하다고 판정된 후보 집합을 줄이는 장치이고, 무주입은 질문 전체가 위키와 무관할 때 아무것도 내보내지 않는 안전 장치다. 1위 대비 비율을 무주입에 쓰면 단 하나의 우연한 posting도 1.0이 되어 항상 주입될 수 있다.

## 7. 보고서와 다른 값

| 항목 | 기존 보고 | 재측정 | 설명 |
|---|---:|---:|---|
| production 6000 gold / stale / B-hit | .358 / .900 / 8522 | .358 / .900 / 8521.5 | 일치(반올림) |
| v2 cut=.5 6000 gold / stale / B-hit | .920 / .000 / 1977 | .920 / .000 / 1976.7 | 일치(반올림) |
| production 2000 stale_leak | .890 | **.900** | 실제 root 길이로 예산 계산 시 q00297 한 건 추가 |
| cut=.7 gold | .858 | .858 | 일치 |
| 긴 block full body | 미측정 | **0.000 / 0.000** | production / v2, clip 320 때문에 전체·suffix 미전달 |
| fold=false, head 없는 형식 stale_leak | “형식만으로 0” | **1.000** (cut=.5는 .950) | 기존 형식이 ctx head를 소비했음 |

## 8. 재현과 무결성

```bash
python3 bench/review_ctx/make_long_variant.py
python3 bench/review_ctx/trace_production.py
python3 bench/review_ctx/run_fast.py
python3 bench/review_ctx/run_long.py
python3 bench/review_ctx/run_production_cross.py
python3 bench/context/harness_ctx.py --no-rebuild \
  --out bench/results_review_ctx/cut_sweep --budgets 6000 \
  --arms 'v2-graph:cut=0.3,v2-graph:cut=0.4,v2-graph:cut=0.5,v2-graph:cut=0.55,v2-graph:cut=0.6,v2-graph:cut=0.7'
```

원본 동결 hash는 corpus `75ae99b8…66ca`, queries `fa0180ae…6225`로 `bench/frozen/MANIFEST.json`과 일치한다. 기존 색인은 build하지 않았고 읽기 전용으로 열었다. 검증 시점 SHA-256은 `bench/index_ctx/structural2/structural2.db = eea8f5fe…4bc7a16`, `bench/index_ctx/ctx.sqlite = 3b97b86f…e9fe05`다. `wiki/`, `raw/`, `index/`, `scripts/`, `viewer/`, `bench/context/`, `bench/frozen/`, `bench/CONTEXT_REPORT.md`는 수정하지 않았다.

원시 결과는 다음에 있다.

- `bench/results_review_ctx/production_cross.json`: production native/root 보정/production-search+v2-format 500문항
- `bench/results_review_ctx/fast_arms.json`: v2-search+production-format와 no-head stale arm
- `bench/results_review_ctx/long_blocks.json`: 100문항 긴 block strict 판정
- `bench/results_review_ctx/cut_sweep/500q-summary.json`: cut sweep
- `bench/results_review_ctx/production_trace.json`: production 동적 호출 사슬
