# 증분 색인 갱신 — 구현 결과와 측정 (과제 X, FINAL_PROPOSAL §6-4)

날짜 2026-09-02. 머신: Apple M4, 16 GiB, Python 3.13.7, SQLite 3.50.4. 표준 라이브러리만. 정본(`wiki/`)·`raw/`·
`~/llmwiki` 원본은 건드리지 않았고, 측정은 임시 root(`bench/index_incr/`, gitignore) 에서 했다. 재현 명령은 §7.

## 0. 한 줄

`build --changed <파일…>` 가 바뀐 page 의 색인 행만 갈아 끼우고 발행본을 다시 쓴다(처음 `VACUUM INTO`, §8 뒤로는 빈 파일에 DDL·PK 순 재작성). **39가지 변경 조합
× 500문항에서 cold build 와 page 순서·점수·block_ids 가 모두 같고, 발행본(`index/search.sqlite` 포함 모든 산출물) 바이트가
같다.** 10,000 page 에서 10 page 본문 수정은 build 전체 688 ms(그중 색인 갱신+발행 166 ms), cold 6505 ms.
296 page 개인 위키(벤치 편집 대상 295, log page 제외)에서는 128 ms(색인 72 ms) 대 cold 876 ms.

## 1. 무엇을 만들었나

| 부품 | 위치 | 내용 |
|---|---|---|
| 델타 산출 | `llmwiki.page_hash_map(ws, changed, old_map)` | 힌트가 있으면 그 파일만 다시 읽고 sha 를 내며 나머지는 `index/map.json` 항목 재사용. 없으면 전량. |
| 힌트 확인 | `llmwiki.scan_changes` | `wiki/**/*.json` stat 스캔(10k 파일 25~60 ms). 지난 build 가 파일마다 기록한 `[mtime_ns, size]`(`index/search.work.json` 의 `files`) 와 다른 힌트 밖 파일은 sha 로 확인, 다르면 전량(§8-3; 처음엔 `started_ns` 2초 여유의 시각 문턱이었다). 옛 map 에 있는데 없어진 파일·map 에 없는 새 파일도 전량. |
| 델타 비교 | `llmwiki_index.map_delta(old, new)` | 순수 함수. sha 또는 source 경로가 다르면 modified(파일 이동은 같은 id 의 modified), id 가 새로 생기면 added, 사라지면 deleted. |
| 증분 적용 | `llmwiki_index.apply_delta(db, docs_by_id, delta, loader)` | 한 `BEGIN IMMEDIATE` 트랜잭션. page/blk/link/lookup 행 교체, posting 제자리 교체, 해석이 바뀐 다른 page 의 link 재해석(anchor 가 바뀌면 그 page 재색인 — loader 로 정본을 읽는다). |
| 그래프 갱신 | `llmwiki_index.refresh_graph(db, touched)` | link 가 바뀐 page 와 그 옛·새 이웃(= 바뀐 간선의 끝점) 의 `adj` 행을 통째로 다시 쓰고, 그 바깥 이웃은 허브 감쇠 가중·own_block 만 제자리에서 고친다(fanout 상한에 걸린 page 만 통째로). supersedes head 는 touched 가 속한 체인 성분만 다시 걷는다. 본문만 바뀐 page 는 그래프를 건드리지 않는다. |
| compact | `llmwiki_index.compact(db)` | 작업 DB 의 free page 가 10% 를 넘을 때만 `VACUUM`(build 안 foreground). |
| 발행 | `llmwiki_index.publish(db, path)` | 빈 tmp 에 DDL 고정 순서로 표를 만들고 `INSERT … SELECT` 로 PK 순 복사 → `os.replace` (§8-2; 처음엔 `VACUUM INTO` 였다). `search_root` 는 발행본 바이트의 sha256. |
| 작업 DB | `index/search.work.sqlite` (WAL) + `index/search.work.json` | build 만 연다. 훅은 발행본을 `immutable=1` 로 읽어 갱신 중에도 막히지 않는다. |
| 파생물 | `llmwiki.project_from_index` | 증분일 때 catalog/map/graph/routes/stats 를 정본 파일이 아니라 발행본 색인의 표에서 투영. cold 와 같은 함수(`_project_records`) 로 같은 바이트. viewer shard 는 바뀐 page 만 쓴다. |
| CLI·감시자 | `build [--changed PATH …] [--full]`, `ingest`→`build(changed=[dest, moved_from])`, `viewer/scripts/wiki-data.ts` | 감시자는 debounce 250 ms 동안 모인 경로(중복 제거) 를 `--changed` 로 넘기고, 실패하면 인자 없는 build 로 재시도. `vite.config.ts` 는 `schedule(path)` 한 줄만 바뀜. 폴링 감시자(`pollWikiData`) 는 스냅샷 차이를 넘긴다. |
| 낡음 판정 | `llmwiki.stale_index` | 전량 재투영(10k 에서 2 s) 대신 mtime 스캔 + 후보 파일 sha 대조. |

### 1.1 review_inc §5.3 설계와 다른 점 — 왜

review_inc 는 base(압축 posting BLOB) + delta(행) + tomb 세 층을 제안했다. 구현은 **한 층**이다. term 행 하나에
`(block rid, tf, 길이, 구조 flag)` 를 rid 순 BLOB 으로 두고, page 가 바뀌면 그 page 의 항목만 C 수준 탐색(`array.index`
/ `bisect`) 으로 빼고 끼운다. 세 층이 필요했던 이유(정렬된 impact BLOB 을 건드리지 않으려고) 가 사라졌기 때문이다 —
impact 를 build 때 굽지 않고 **조회 때 live df·평균 길이로 계산**하므로 posting 에는 정렬이 없고 항목 교체가 곧 갱신이다.

같은 이유로 rid 가 배열 위치가 아니라 **내용 해시**다: page rid = page id 의 47비트 blake2b, block rid = page rid << 16 |
block_order 위치. 증분본과 cold build 의 표 내용이 이력과 무관하게 같아지고, 모든 표가 WITHOUT ROWID 또는 그 rid 를
INTEGER PRIMARY KEY 로 쓰므로 `VACUUM INTO` 가 PK 순으로 다시 쓴 발행본의 바이트가 같다(§3, §4). tomb/delta 층이
없으니 review_inc 의 "delta+tomb > block 10% 이면 compact" 문턱은 대상이 없고, free page 10% 문턱만 남았다.

값을 치른 곳은 조회다(§5): 토큰마다 posting 을 전부 읽어 live impact 를 계산한 뒤 상위 MAX_POST 를 고르므로
10,000 page 에서 조회 p50 이 4.6 ms 다(structural3 의 impact 정렬 BLOB 앞 400개 읽기는 1.7~2.5 ms). 296 page 에서는 2.4 ms.

## 2. 동일성 — 39 조합 × 500문항 (bench/incr/identity.py, bench/results_incr/identity.json)

bench/frozen/corpus 10,000 page 의 cold build 를 base 로 두고, 조합마다 base 사본에 변경을 가한 뒤 `build(changed=힌트)` 로
증분, 같은 정본을 `build(full=True)` 로 cold. 비교 대상은 500문항 top-10 의 `(page_id, score, block_ids)` 와 `index/` +
`viewer/public/data/` 전체 파일의 sha256. `≠cold` 는 두 색인의 결과가 다른 문항 수, `순서 변화` 는 base 대비 top-10 page
순서가 바뀐 문항 수(검정력 — 0 이면 그 조합은 "무엇을 바꿔도 같은" 문항만 본 것이다).

| 시나리오 | 요청 | 힌트 파일 | 델타 (add/mod/del) | 재색인 | 증분 ms | cold ms | ≠cold (500문항) | 바이트 | base 대비 순서 변화 | 유형 |
|---|---:|---:|---|---:|---:|---:|---:|---|---:|---|
| body | 1 | 1 | 0/1/0 | 0 | 696 | 7071 | 0 | 동일 | 0 | — |
| body | 10 | 10 | 0/10/0 | 0 | 715 | 7261 | 0 | 동일 | 0 | — |
| body | 100 | 100 | 0/100/0 | 0 | 946 | 7252 | 0 | 동일 | 0 | — |
| body | 1000 | 1000 | 0/1000/0 | 0 | 1551 | 7100 | 0 | 동일 | 1 | crosslingual 1 |
| add | 1 | 1 | 1/0/0 | 0 | 761 | 7275 | 0 | 동일 | 2 | relation 2 |
| add | 10 | 10 | 10/0/0 | 0 | 812 | 7572 | 0 | 동일 | 5 | relation 5 |
| add | 100 | 100 | 100/0/0 | 0 | 964 | 7137 | 0 | 동일 | 105 | crosslingual 6, relation 99 |
| add | 1000 | 1000 | 1000/0/0 | 0 | 1693 | 7744 | 0 | 동일 | 304 | crosslingual 12, exact 92, paraphrase 2, relation 100, temporal 98 |
| delete | 1 | 1 | 0/0/1 | 1 | 711 | 7217 | 0 | 동일 | 3 | relation 3 |
| delete | 10 | 10 | 0/0/10 | 2 | 837 | 7220 | 0 | 동일 | 5 | relation 5 |
| delete | 100 | 100 | 0/0/100 | 15 | 1079 | 6950 | 0 | 동일 | 177 | relation 79, temporal 98 |
| delete | 1000 | 1000 | 0/0/1000 | 100 | 1388 | 6636 | 0 | 동일 | 319 | crosslingual 11, exact 85, paraphrase 25, relation 100, temporal 98 |
| supersedes | 1 | 1 | 0/1/0 | 0 | 758 | 7264 | 0 | 동일 | 0 | — |
| supersedes | 10 | 10 | 0/10/0 | 0 | 786 | 7253 | 0 | 동일 | 0 | — |
| supersedes | 100 | 100 | 0/100/0 | 0 | 1094 | 7366 | 0 | 동일 | 98 | crosslingual 98 |
| supersedes | 1000 | 1000 | 0/1000/0 | 0 | 1885 | 7187 | 0 | 동일 | 100 | crosslingual 98, relation 2 |
| chain_extend | 1 | 1 | 1/0/0 | 0 | 767 | 7532 | 0 | 동일 | 1 | temporal 1 |
| chain_extend | 10 | 10 | 10/0/0 | 0 | 759 | 7051 | 0 | 동일 | 39 | temporal 39 |
| chain_extend | 100 | 100 | 100/0/0 | 0 | 913 | 7390 | 0 | 동일 | 201 | crosslingual 1, relation 100, temporal 100 |
| chain_head_delete | 1 | 1 | 0/0/1 | 0 | 773 | 7493 | 0 | 동일 | 1 | temporal 1 |
| chain_head_delete | 10 | 10 | 0/0/10 | 0 | 830 | 7245 | 0 | 동일 | 75 | temporal 75 |
| chain_head_delete | 100 | 100 | 0/0/100 | 0 | 1071 | 7140 | 0 | 동일 | 104 | crosslingual 1, paraphrase 1, relation 2, temporal 100 |
| chain_middle_delete | 1 | 1 | 0/0/1 | 1 | 776 | 7491 | 0 | 동일 | 1 | temporal 1 |
| chain_middle_delete | 10 | 10 | 0/0/10 | 7 | 842 | 7164 | 0 | 동일 | 8 | temporal 8 |
| chain_middle_delete | 100 | 100 | 0/0/100 | 67 | 1228 | 7456 | 0 | 동일 | 103 | crosslingual 1, paraphrase 1, relation 1, temporal 100 |
| df_shift | 1 | 1 | 0/1/0 | 0 | 743 | 7664 | 0 | 동일 | 153 | crosslingual 1, paraphrase 53, relation 99 |
| df_shift | 10 | 10 | 0/10/0 | 0 | 774 | 7759 | 0 | 동일 | 202 | crosslingual 6, paraphrase 92, relation 100, temporal 4 |
| df_shift | 100 | 100 | 0/100/0 | 0 | 1292 | 7588 | 0 | 동일 | 294 | crosslingual 50, exact 82, paraphrase 43, relation 100, temporal 19 |
| slug_rename | 1 | 1 | 1/0/1 | 1 | 876 | 7040 | 0 | 동일 | 121 | crosslingual 2, exact 2, paraphrase 20, temporal 97 |
| slug_rename | 10 | 10 | 10/0/10 | 4 | 1021 | 7130 | 0 | 동일 | 144 | crosslingual 24, exact 2, paraphrase 20, temporal 98 |
| slug_rename | 100 | 100 | 100/0/100 | 46 | 1333 | 7278 | 0 | 동일 | 184 | crosslingual 42, exact 3, paraphrase 31, relation 8, temporal 100 |
| block_remove | 10 | 10 | 0/10/0 | 0 | 735 | 7555 | 0 | 동일 | 0 | — |
| block_remove | 100 | 100 | 0/100/0 | 0 | 974 | 7172 | 0 | 동일 | 4 | temporal 4 |
| move_source | 10 | 20 | 0/10/0 | 0 | 679 | 7150 | 0 | 동일 | 0 | — |
| move_source | 100 | 200 | 0/100/0 | 0 | 943 | 7270 | 0 | 동일 | 0 | — |
| empty_blocks | 10 | 10 | 0/10/0 | 0 | 758 | 7372 | 0 | 동일 | 3 | relation 3 |
| empty_blocks | 100 | 100 | 0/100/0 | 0 | 981 | 7588 | 0 | 동일 | 15 | relation 11, temporal 4 |
| title_collide | 10 | 10 | 0/10/0 | 0 | 737 | 7180 | 0 | 동일 | 0 | — |
| title_collide | 100 | 100 | 0/100/0 | 0 | 951 | 7295 | 0 | 동일 | 0 | — |

읽는 법.

- **39 조합 모두 mode=incremental, ≠cold 0, 바이트 동일.** 체인 head 교체(chain_extend 100: temporal 100문항의 1위가 새
  page 로), head 삭제, 중간 삭제, df 대량 이동(df_shift 100: 294문항 순서 변화), slug 개명(id 도 함께 — 제품 validate 가
  `id == page:slug` 를 요구한다), 파일 이동(sha 같고 source 다름 → modified), 빈 block(색인 항목 0개 page), 제목 충돌까지.
- 검정력이 0 인 조합(body 1/10/100, supersedes 1/10, slug_rename, block_remove 10, move_source, title_collide) 은 review_inc
  §1.2 와 같은 이유다 — 질문 토큰과 겹치지 않거나 순위에 있던 page 가 아니다. 그 조합에서도 바이트 동일은 표 내용 전체의
  동일성을 확인하므로 검정력이 있다.
- `재색인` 열은 힌트 밖 page 의 link 해석이 바뀌어 anchor(큐레이션 간선을 든 block, posting flag ×1.35) 가 달라져 다시 색인한
  page 수다. 삭제·개명으로 supersedes 대상이 사라진 page 가 여기 잡힌다(delete 1000: 100 page, chain_middle_delete 100: 67).
  반대 방향(새 page 가 dangling 큐레이션 link 를 살림) 은 이 코퍼스에 없어 tests/test_incremental.py 의
  `title_collision`·`new_page_resolves_a_dangling_related_link` 가 고정한다.

## 3. 비용 — 합성 10,000 page (bench/incr/cost.py --name frozen, bench/results_incr/cost_frozen.json)

build 전체(`llmwiki.build`, 파생물 JSON·viewer shard 쓰기 포함) 의 벽시계 ms. 5회 중앙값. `phases` 는 단계별: scan(mtime 스캔·상태
읽기) / hash(힌트 파일 sha·델타) / index(apply_delta + refresh_graph + commit + compact 검사 + VACUUM INTO 발행 + 파일 sha) /
project(색인 표 → catalog·map·graph·routes·stats) / write(JSON 10개 + shard).

| 항목 | ms (중앙값) | 5회 원값 | 단계 |
|---|---:|---|---|
| cold build (`--full`) | 6505 | 6345, 6505, 6781 (3회) | {'scan': 27.9, 'full': 4510.6, 'write': 2216.7} |
| 증분 page 1 (본문 수정) | 657 | 657, 665, 609, 630, 738 | {'scan': 61.3, 'hash': 15.6, 'index': 194.9, 'project': 268.7, 'write': 186.3} |
| 증분 page 10 (본문 수정) | 688 | 697, 688, 667, 663, 702 | {'scan': 55.3, 'hash': 18.8, 'index': 204.8, 'project': 238.3, 'write': 173.6} |
| 증분 page 100 (본문 수정) | 913 | 913, 889, 922, 886, 959 | {'scan': 61.5, 'hash': 32.6, 'index': 409.3, 'project': 247.0, 'write': 196.6} |
| 증분 page 1000 (본문 수정) | 1512 | 1512, 1770, 1572, 1500, 1511 | {'scan': 80.1, 'hash': 146.3, 'index': 658.5, 'project': 249.4, 'write': 364.3} |
| 조회 (cold 색인, warm) | p50 4.641 / p95 6.488 | 새 프로세스 open+첫 조회 5.73 ms | |

100 라운드 × 10 page 본문 수정(라운드마다 다른 page, 누적 1,000 page = 10%):

| 라운드 | build ms | 색인 ms | 발행본 bytes | 작업 DB bytes / freelist | 조회 p50 / p95 ms |
|---:|---:|---:|---:|---|---|
| 1 | 715 | 196 | 36,478,976 | 38,506,496 / 0 | 4.442 / 6.429 |
| 10 | 719 | 188 | 36,487,168 | 38,551,552 / 1 | 5.792 / 8.799 |
| 25 | 724 | 202 | 36,495,360 | 38,629,376 / 1 | 4.392 / 6.202 |
| 50 | 791 | 233 | 36,524,032 | 38,719,488 / 2 | 4.34 / 6.105 |
| 100 | 704 | 180 | 36,597,760 | 38,965,248 / 3 | 4.304 / 6.302 |

100 라운드 뒤: 발행본 `search.sqlite` 바이트 == 같은 정본의 cold build **동일**, 500문항 결과 동일 **동일**,
갱신 시간 중앙값 723 ms · 최대 899 ms — 누적 열화 없음. 작업 DB freelist 는 posting 제자리 교체라 한 자리 수다.

읽는 법.

- 10k 에서 증분 build 의 바닥은 **색인이 아니라 파생물**이다: project 238 ms + write 174 ms ≈ 450 ms 는 catalog 3.8 MB·map 8 MB·graph
  13 MB 를 매번 다시 직렬화하는 값이고(review_inc §2.6 이 말한 build 하한), 색인 갱신은 10 page 에 166 ms 다. 그 안에서도
  `VACUUM INTO` + `os.replace` + 파일 sha 가 ~130 ms(37 MB 복사) 로 반이 넘는다. page 1 개와 10 개의 차이가 거의 없는 이유다.
- review_inc §2.1 의 프로토타입 "body 10 = 40 ms" 는 posting 만 갱신하고 map 은 page-only 로 쓴 값이다. 제품 경로에서 같은 부분
  (apply_delta + refresh_graph) 은 10 page 에 약 25 ms 이고 나머지는 발행·파생물이다.
- 1,000 page(10%) 도 증분이 cold 의 1/4 이다. 25% 를 넘으면 `large-delta` 로 cold 로 간다.

## 4. 비용 — 개인 위키 복사본 296 page (편집 대상 295) (bench/incr/cost.py --name personal, bench/results_incr/cost_personal.json)

`~/llmwiki/wiki` 를 임시 root 로 복사(원본 미수정). 질문 집합이 없어 제목·본문 첫 8어절에서 200문항을 결정적으로 뽑았다.
page 의 25% 가 74 이라 증분 규모는 1·10 만 잰다.

| 항목 | ms (중앙값) | 5회 원값 | 단계 |
|---|---:|---|---|
| cold build (`--full`) | 876 | 888, 840, 876 (3회) | {'scan': 4.3, 'full': 750.4, 'write': 118.0} |
| 증분 page 1 (본문 수정) | 90 | 130, 84, 86, 90, 97 | {'scan': 5.6, 'hash': 0.8, 'index': 41.1, 'project': 18.2, 'write': 30.7} |
| 증분 page 10 (본문 수정) | 128 | 136, 125, 129, 125, 128 | {'scan': 5.6, 'hash': 3.9, 'index': 65.8, 'project': 17.6, 'write': 34.5} |
| 조회 (cold 색인, warm) | p50 2.392 / p95 3.707 | 새 프로세스 open+첫 조회 1.99 ms | |

| 라운드 | build ms | 색인 ms | 발행본 bytes | 작업 DB bytes / freelist | 조회 p50 / p95 ms |
|---:|---:|---:|---:|---|---|
| 1 | 148 | 87 | 9,142,272 | 9,560,064 / 0 | 2.317 / 3.74 |
| 10 | 153 | 77 | 9,154,560 | 9,666,560 / 3 | 2.341 / 3.656 |
| 25 | 131 | 55 | 9,179,136 | 9,719,808 / 3 | 2.347 / 3.663 |
| 50 | 217 | 122 | 9,203,712 | 9,789,440 / 2 | 2.335 / 3.673 |
| 100 | 159 | 84 | 9,252,864 | 9,854,976 / 2 | 2.289 / 3.78 |

100 라운드 뒤 발행본 바이트 == cold **동일**, 200문항 결과 동일 **동일**. 개인 위키 규모에서는 10 page 증분이
cold 의 6.8배 빠르고 build 전체가 0.1 초대다 — 감시자의 debounce(250 ms) 보다 짧다.

첫 실행에서 이 위키의 100 라운드가 cold 와 갈라져(supersedes 체인 안의 page 를 본문만 고쳤을 때 체인 자리 head·sup_state 가
기본값으로 초기화되는 버그) 고쳤고, 그 시나리오를 tests/test_incremental.py `test_body_edit_of_a_chain_member_keeps_its_head`
로 고정했다. 합성 코퍼스에는 체인 page 본문 수정 조합이 없어 잡히지 않았던 것이다 — 실제 위키로 다시 잰 값이 있는 이유다.

## 5. 값을 치른 곳 · 남은 것

| 항목 | 값 | 이유 · 대안 |
|---|---|---|
| 조회 p50 (10k) | 4.6 ms (p95 6.5) — structural3 는 2.5 | posting 전량 읽기 + live impact. 정확한 top-K 를 prefix 로 끊으려면 (mult, tf) 그룹 · 길이 오름차순 정렬과 그룹별 상한 검사가 필요하다 — 결정적이라 바이트 동일을 깨지 않으며, 다음 단계 후보. 296 page 에서는 2.4 ms 라 실사용 영향은 없다. |
| 색인 크기 (10k) | 36.5 MB — structural3 9.35 MB | link 표(+색인 4개)·lookup·page.meta(파생물 투영용 원문 필드)·blk 전 block 행 이 더해졌다. 296 page 에서 9.1 MB. 발행 복사(`VACUUM INTO`) 시간이 크기에 비례하므로 link.block_id 를 rid 로 바꾸면 ~3 MB 를 줄일 수 있다. |
| cold build (10k) | 6505 ms — 이전 제품 build 5.1 s(review_inc §2.6) | page_record(해석·토큰화)·SQL 행 삽입이 structural3 의 메모리 일괄 생성보다 느리고, 파생물이 색인 표를 거친다. 증분이 목적이라 cold 는 더 다듬지 않았다. |
| `search_root` | 발행본 파일 sha256 | 표 내용 지문(`logical_digest`) 은 10k 에서 0.3 초라 publish 마다 계산하지 않는다. parity 는 바이트가 다를 때만 논리 지문으로 원인을 가른다. |
| 파생물 | 전량 직렬화 | catalog/map/graph 는 전 page 를 담는 파일이라 증분으로 줄일 수 없다. 색인 표에서 투영해 정본 재독·재검증(10k 에서 4 s) 은 없앴다. |
| 허브 간선 | 간선 하나에 이웃 수천 행 patch | 허브 감쇠(1/(1+ln in-degree)) 의 정의상 허브의 in-degree 가 바뀌면 허브로 가는 모든 행의 가중이 바뀐다. 통째로 다시 계산하지 않고 UPDATE 로 고친다(10k 의 최대 허브 1,841 in-degree 에서 refresh 46 ms). |

## 6. 테스트

`tests/test_incremental.py` 34개 (전체 500개 통과, `python3 -m unittest discover -s tests -t .`):
(a) 시나리오 17개 — 본문 수정·체인 member 본문 수정·추가·삭제·supersedes 추가·head 교체·head 삭제·중간 삭제·df 이동·
허브 slug 개명·block 제거+재배열·파일 이동·빈 block·제목 충돌(dangling 해석)·새 page 가 dangling related 를 살림(재색인)·
한 배치에 셋·heading_paths 옵션·6라운드 누적 — 각각 fixture 14문항의 page 순서·점수·block_ids·head 가 cold 와 같고
(b) `index/` + `viewer/public/data/` 바이트가 cold 와 같다. (c) 힌트 밖 편집·새 파일·삭제·mtime 을 과거로 돌린 편집 →
전량(`unhinted-change:*`), 힌트 경로는 힌트 밖 파일을 열지 않음, 25% 초과 → `large-delta`. (d) compact: freelist 10% 문턱
전후, build 안 보고. (e) 갱신 8회 동안 다른 스레드의 훅 조회(open_index + search) 가 오류 없이 도는가, 증분 뒤 훅이 새
revision 을 쓰는가. (f) CLI `--changed`(절대·상대)·`--full`, ingest 가 힌트로 증분(이동 포함), 감시자 인자 형식(bun 으로
`buildArgs`/`changedPaths` 실행). 순수 함수: rid 해시, `map_delta`, 다른 순서로 채운 작업 DB 의 publish 바이트 동일.

`tools/parity/parity.py build` 는 cold 두 번 대조에 더해 cold → 편집 → `--changed` → cold 의 산출물 바이트를 대조한다(실제 wiki 6 page: 동일).

## 7. 재현

```bash
python3 bench/incr/cost.py --name frozen                         # 10,000 page: cold·증분 1/10/100/1000·100라운드 (~6 분)
python3 bench/incr/cost.py --name personal --wiki ~/llmwiki/wiki  # 개인 위키 복사본 (~2 분)
python3 bench/incr/identity.py                                    # 39 조합 동일성 (~12 분)
python3 -m unittest tests.test_incremental                        # 34 tests
python3 tools/parity/parity.py build
```

결과 JSON: `bench/results_incr/{cost_frozen,cost_personal,identity}.json`, 로그 `*.log`. 임시 root 는 `bench/index_incr/`.

## 8. 검증 결함 수정 (과제 FIX-2, 2026-09-02)

grok 블랙박스(bench/grok_incr/REPORT.md)와 codex 코드 검증(bench/review_incr/REVIEW.md)이 낸 결함을 고쳤다. 수정 범위는
`scripts/llmwiki_index.py`(search·project_graph·publish), `scripts/llmwiki.py`(scan_changes·build 상태 기록), tests, docs 다.
정본·`raw/`·`viewer/`·`~/llmwiki` 는 건드리지 않았다.

### 8-1. 접힌 head 가 렌더에서 버려져 주입 0바이트 (grok s5)

원인 둘. (1) 옛 page 의 본문 질문에 색인이 head 를 1위로 올리지만, head 의 supersedes 링크에 `block_id` 가 없고 질문 토큰이
head 본문에 없으면 근거 block 이 비어 "B 가 없는 P 는 버린다" 규칙에 걸렸다. (2) 옛 page 는 head × STALE_SHOW(0.30) 점수라
cut(0.5) 아래·상위 k 밖으로 밀려 `sup→` 줄이 아예 없었다. 전량 build 도 같았다 — 렌더 결함이다.

수정: `Index.search` 가 접힌 head 에 근거를 반드시 붙인다 — (1) supersedes anchor block (2) 질문 토큰과 맞는 head block (3) head
의 첫 본문 block(ev=1, heading·제목 제외). 옛 page 는 head 가 상위 k 에 있으면 k 밖이어도 hit 에 따라오고, `project_graph` 는
head 가 cut 을 넘으면 옛 page 를 cut 에서 면제해 **head 묶음 바로 뒤**에 `P 옛-slug … sup→head` 한 줄로 둔다(예산이 뒤에서
끊겨도 남는다). 옛 본문은 여전히 한 글자도 나가지 않는다.

| 재현 | 전 | 후 |
|---|---|---|
| 6 page `/tmp` 복사본, 옛 본문 질문 `Thursday releases. Each note carries the change` (`context --json`) | grade=strong, `text` 0바이트, head `blocks: []` | `P zxq-super-head … / B zxq-super-head#3267… cur \| ZXQSUPERHEADAA11 … / P release-process concept 2026-08-21 sup→zxq-super-head`, 옛 문장 없음 |
| 같은 질문 hook stdout | 0바이트 | 위 P/B/P 를 담은 JSON |
| `build --changed` 증분 vs `--full` | 같음(둘 다 빈 출력) | 같음(context 바이트·search.sqlite 바이트 동일) |
| 개인 위키 복사본의 실제 supersedes 체인(인수인계 page 09-01 ← 09-02), 옛 본문 질문 | head 는 나오지만 `sup→` 줄 없음 | head 본문 3 block 뒤에 `P <옛-slug> source 2026-09-01 sup→<head-slug>` |

tests/test_index.py 에 고정: 옛 본문 질문 → head 본문 + `sup→` 줄(anchor 없음, 첫 본문 block), anchor block 이 있으면 그 block 이
근거(search JSON 의 block_ids 포함), 옛 본문 문장 미출력, `limit=1` 이라도 옛 page 가 따라옴, 기본 cut 에서 `sup→` 줄이 head 바로 뒤.

### 8-2. 30회 증분 뒤 sqlite 파일 바이트가 cold 와 다름 (grok s7 small)

원인: `VACUUM INTO` 는 원본의 schema cookie(+1) 를 헤더에 복사한다. 작업 DB 의 compact(`VACUUM`) 가 cookie 를 올리므로 삭제로
compact 가 돌았던 6 page 코퍼스에서 헤더 1바이트(offset 43) 가 달랐다(19 vs 18). 296 page 는 compact 가 없어 같았다.

수정(과제의 (a)): `publish` 가 빈 tmp 파일에 `SCHEMA` 와 같은 순서로 DDL 을 만들고 표마다 `INSERT INTO pub.t SELECT * FROM main.t`
로 복사한다 — ORDER BY 없는 `SELECT *` 라야 sqlite 의 xfer 최적화가 b-tree 를 키 순으로 직접 옮기고 인덱스도 원본 인덱스 순으로
채워 `VACUUM INTO` 와 같은 크기가 나온다(ORDER BY 를 붙이면 10k 에서 355 ms·38.6 MB 로 느리고 컸다). 순회가 PK 순이므로 결정적
이고, cookie 는 DDL 문장 수(17) 로 고정이다.

| 항목 | 전 (`VACUUM INTO`) | 후 |
|---|---|---|
| 10k publish 7회 중앙값 (CPU 유휴) | 139 ms | 187 ms (+47) |
| 10k 발행본 크기 | 36,597,760 B | 36,597,760 B (논리 지문 동일) |
| 10k 증분 build 1 page 전체 (5회 중앙값) | 618 ms (codex §7.1) / 657 ms (§3) | 730 ms — index 264 ms(publish +47), scan 57 ms(stat 에 size 포함·기록 읽기 2 ms), write 175 ms(상태 파일 623 KB) |
| grok s7 6 page: 수정 10 + 추가 10 + 삭제 10 (compact 1회) 뒤 sqlite 바이트 == cold | 다름 (cookie 19 vs 18) | 같음 (cookie 17 == 17), search_root·catalog/map/graph/routes/stats 동일 |
| grok s7 296 page 복사본 (compact 0회) | 같음 | 같음 |
| codex identity.py 39조합 × 500문항 | 통과 | 통과 (mismatch 0, 39/39 bytes 동일, all_incremental) |

tests/test_incremental.py 에 고정: 27 page fixture 에서 수정 10·추가 10(큰 page)·삭제 10 을 `--changed` 로 돌려 compact 가 최소
한 번 돈 뒤 발행본 바이트·`search_root` 가 cold 와 같고, 작업 DB 의 schema cookie 는 발행본과 다르다(이력이 헤더에 없다).

### 8-3. 힌트 밖 파일의 mtime 이 과거면 변경이 조용히 누락 (codex REVIEW #4)

원인: `scan_changes` 가 `mtime >= 지난 build started_ns − 2 s` 인 파일만 sha 로 확인했다. timestamp 보존 복사·`utime` 으로 과거로
돌린 편집은 후보에서 빠져 mode=incremental 로 끝나고 sha 가 갱신되지 않았다.

수정: build 가 시작할 때 `wiki/**/*.json` 의 `[mtime_ns, size]` 를 스냅샷으로 찍어(정본을 읽기 전) `index/search.work.json` 의 `files`
에 기록하고, 다음 build 는 기록과 **다르기만 하면** sha 로 확인한다(더 오래된 mtime 포함). 같은 내용을 touch 만 한 파일은 sha 가
같아 증분이 유지되고 기록이 새 mtime 으로 갱신된다. 기록이 없는 새 파일·기록에 있는데 없어진 파일은 그대로 전량. 기록이 전혀
없는 옛 상태 파일에서는 알려진 파일을 전부 sha 로 확인한다(10k 에서 4.2 s, 한 번). 델타가 없는 `no-change` build 도 기록을 다시
쓴다 — 처음 구현은 쓰지 않아 touch 된 파일을 build 마다 다시 읽었다(10k 에서 3.4 s, 측정 중 발견). `MTIME_SLACK_NS` 는 없앴다.
`stale_index`(doctor) 도 같은 기록으로 판정한다.

| 재현 (bench/review_incr/audit.py `unhinted.backdated_outside_slack`, 개인 위키 복사본, b 의 mtime 을 started_ns − 10 s) | 전 | 후 |
|---|---|---|
| mode / reason | incremental / "" | full / `unhinted-change:wiki/concepts/21-cfr-part-11.json` |
| b 의 map sha 갱신 | False | True |

tests: 10 s 과거로 돌린 편집 → 전량·sha 갱신·기록 갱신, touch 만 → 증분 유지·다음 build 는 파일을 열지 않음(`file_shas_match` 호출 0),
기록 없는 상태 파일 → sha 확인으로 증분/전량, no-change build 의 기록 갱신. 10k 상태 파일은 623 KB, 읽기 2.3 ms.

### 8-4. compact 문턱은 free page 10% 다 (codex REVIEW #5, 문서화)

코드는 바꾸지 않았다. 이 구현은 posting 제자리 교체라 delta/tomb 층이 없고, compact(`VACUUM`) 는 작업 DB 의 free page 비율이
10% 를 넘을 때만 돈다. **본문 수정은 free page 를 만들지 않아 compact 대상이 아니고, free page 는 삭제가 만든다.** audit.py 의
수치(본문 30 page 10.17% → compact=false, 삭제 44 page 14.92% → true) 가 그 뜻이다. docs/context-injection.md 와 §1 에 명시했고,
tests/test_incremental.py 에 본문 5 page 수정 → freelist 0·compacted=False, 큰 page 삭제 → compacted=True·freelist 0 을 고정했다.

### 8-5. 유지 확인

| 항목 | 결과 |
|---|---|
| `python3 -m unittest discover -s tests -t .` | 509 tests OK (skipped 7) — 새 테스트 9 (검증 시점 500) |
| bench/review_incr/identity.py | 39/39 incremental, mismatch 0, 발행 바이트 동일 |
| bench/review_incr/audit.py | determinism_10 동일, unhinted 정상·backdated 모두 full, compact 표 동일, 동시성 hook 20회 오류 0(p50 658 ms, memory fallback), watcher 통과, hook 신선도 통과 |
| `tools/parity/parity.py build` | 두 번 동일 · search.sqlite 동일 · 증분 == cold |
| 실제 wiki `build` / `validate` / `lint` | 6 page, 0 errors 0 warnings |
| grok s5 / s7 재현 (`/tmp` 새 복사본, small·large) | §8-1, §8-2 표 |

