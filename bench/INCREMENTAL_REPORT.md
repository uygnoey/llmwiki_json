# JSON 정본 네이티브 색인의 증분 유지와 Markdown 사본 제거 비용

대상은 `bench/frozen/corpus` 10,000 page와 `bench/frozen/queries.json` 500문항이다. seed는 1234이며 표준 라이브러리만 사용했다. qmd는 실행하지 않았고, `wiki/`, `raw/`, `index/`, `scripts/`, `viewer/`, 기존 ranker와 동결 코퍼스는 수정하지 않았다.

## (a) Markdown 경로 비용

크기는 실제 파일 바이트를 MiB(2²⁰ bytes)로 바꾼 값이다. `project()`와 `export_markdown()`은 각각 3회 실행한 중앙값이며, 임시 Workspace와 출력은 `bench/index_inc/task_f/cost_workspace` 아래에 두었다.

| 산출물 | bytes | MiB | 생성 비용 / 비고 |
|---|---:|---:|---|
| JSON 정본 10,000 shard | 27,854,863 | 26.564 | 입력 |
| `catalog.json` | 3,829,542 | 3.652 | 아래 `project()` 한 번에 함께 생성 |
| `map.json` | 8,654,103 | 8.253 | symlink가 해석된 절대 source 경로 포함 |
| `search.json` | 6,110,043 | 5.827 | 파생물 19.19%, 정본+파생물 10.24% |
| `graph.json` | 12,974,464 | 12.373 | 〃 |
| `routes.json` | 266,404 | 0.254 | 〃 |
| 위 5개 파생물 합계 | 31,834,556 | 30.360 | `project()` **1,963.761 ms**, pretty write 106.001 ms |
| Markdown 본문 10,000 `.md` | 4,565,658 | 4.354 | `export_markdown()`에 포함 |
| Markdown `manifest.json` | 2,366,363 | 2.257 | ID/file/hash 대응표 |
| `index/markdown` 상당 합계 | **6,932,021** | **6.611** | `export_markdown()` **1,865.701 ms** |
| 제거 가능한 `search.json` + Markdown | **13,042,064** | **12.438** | qmd 자체 색인은 제외한 직접 중복만 |

임시 root의 `wiki` symlink 때문에 `Workspace.rel()`이 source를 절대 경로로 기록했다. 같은 map을 실제 checkout의 `wiki/...` 상대 경로 길이로 정규화한 추정치는 8,174,103 bytes(7.795 MiB)이고, 위 실측에는 480,000 bytes의 경로 길이 오버헤드가 있다. frozen의 제안 필드 `aliases`만 임시 schema 사본에 허용했으며 저장소 schema는 바꾸지 않았다.

`search.json`의 `text` 필드만 4,046,768 bytes다. 즉 JSON block 본문을 한 번 더 평탄화한 부분이 `search.json` 전체의 66.23%다. Markdown mirror는 정본 크기의 24.89%이고 매 실행마다 10,000개 파일을 전부 지우고 다시 쓴다. qmd 색인 비용은 과제 범위에서 제외했으므로 이 표에 더하지 않았다.

`source_snapshot.text`는 frozen에 없어서 **추정**했다. 실제 `wiki/`는 6 page 중 source가 1 page(16.67%)이고, 그 한 source snapshot text가 460 bytes다. 이 비율과 표본 평균을 그대로 10,000 page에 외삽하면 source 1,667 page, 766,820 bytes(0.731 MiB, 추정 정본의 2.68%)다. frozen의 source 50% 구성비를 쓰면 2,300,000 bytes(2.193 MiB), 현재 실제 wiki처럼 모든 type이 snapshot을 가진다고 보고 전체 6-page 평균을 쓰면 2,915,000 bytes(2.780 MiB)다. 표본이 source 1개뿐이라 불확실성이 크며, `source_snapshot`은 검색 사본이 아니라 정본의 원문 재현 정보이므로 Markdown mirror와 함께 자동 삭제할 대상으로 보지 않는다.

## (b) 증분 vs 전체 재빌드

기존 `structural2` 전체 build의 3회 값은 1,338.9 / 1,295.6 / 1,341.1 ms, 중앙값 **1,338.9 ms**, 색인은 9,347,072 bytes다. 구현한 `SegmentedRanker`의 최초 전체 build는 2,832.046 ms, 76,703,467 bytes다. 후자는 compact impact BLOB 대신 page별 block TF row와 전역 `termstat`을 저장하고 조회 시 현재 DF·평균 길이로 impact를 계산한다.

아래 `전체`는 같은 segmented 포맷의 전체 재빌드다. 따라서 speedup은 같은 의미·같은 포맷끼리 비교한 값이다. 증분 시간에는 page-only map 읽기/쓰기, changed JSON parse, SQLite transaction과 `termstat` 갱신이 들어가며, 실험용 overlay 생성과 기준 색인 복사는 제외한다.

| 변경 종류 | page | 증분 ms | 전체 ms | 배속 | 500문항 결과 |
|---|---:|---:|---:|---:|---|
| 본문 block 수정 | 1 | 30.361 | 3,032.687 | 99.89× | 일치 |
| 본문 block 수정 | 10 | **45.137** | 2,912.741 | 64.53× | 일치 |
| 본문 block 수정 | 100 | 119.049 | 2,866.758 | 24.08× | 일치 |
| 본문 block 수정 | 1,000 | 815.539 | 2,882.102 | 3.53× | 일치 |
| 새 page 추가 | 1 | 29.449 | 2,935.473 | 99.68× | 일치 |
| 새 page 추가 | 10 | 38.461 | 2,932.974 | 76.26× | 일치 |
| 새 page 추가 | 100 | 70.967 | 2,972.255 | 41.88× | 일치 |
| 새 page 추가 | 1,000 | 306.825 | 3,235.504 | 10.55× | 일치 |
| page 삭제 | 1 | 30.573 | 2,933.034 | 95.94× | 일치 |
| page 삭제 | 10 | 37.570 | 2,911.476 | 77.50× | 일치 |
| page 삭제 | 100 | 88.902 | 3,047.186 | 34.28× | 일치 |
| page 삭제 | 1,000 | 549.419 | 2,648.803 | 4.82× | 일치 |
| `supersedes` 간선 추가 | 1 | 31.239 | 3,025.281 | 96.84× | 일치 |
| `supersedes` 간선 추가 | 10 | 41.907 | 3,001.612 | 71.63× | 일치 |
| `supersedes` 간선 추가 | 100 | 130.130 | 3,243.649 | 24.93× | 일치 |
| `supersedes` 간선 추가 | 1,000 | 910.350 | 3,123.590 | 3.43× | 일치 |

각 page 수에서 네 변경 종류의 증분 중앙값은 1 page 30.467 ms, 10 page 40.184 ms, 100 page 103.976 ms, 1,000 page 682.479 ms다. 본문 10개라는 대표값은 45.137 ms다.

동일성은 순위 page ID뿐 아니라 top-10의 **score와 근거 block ID까지** 500/500문항에서 비교했다. 16개 조합 모두 mismatch 0이다. 변경 없는 base에서도 segmented와 원본 structural2가 500/500문항에서 page·score·block까지 같았다. 반면 SQLite 파일 바이트는 모든 조합에서 다르다. row 삽입 순서와 free page가 물리 파일을 달리 만들기 때문이며, map root는 모두 같았다.

가장 단순한 page-segment 구조는 update를 싸게 만들었지만 비용도 명확하다.

- base index가 9.35 MB에서 76.70 MB로 8.21배 커졌다. 문자열 page/block key와 정규화 posting row가 원인이다.
- segmented 조회는 보통 p50 약 6.6 ms, p95 약 11.3 ms로 원본 structural2의 p50 1.74 ms, p95 2.32 ms보다 느리다.
- 1,000 page 삭제 뒤 증분 파일은 76,516,467 bytes지만 전체 재빌드는 69,549,171 bytes다. foreground `VACUUM`을 하지 않아 삭제 공간을 free page로 남긴 결과다.

따라서 프로토타입 구조 자체를 그대로 제품 채택하는 것보다, page 단위 delta를 foreground에 쓰고 integer ID/varint BLOB으로 압축한 base segment를 background에서 병합하는 구성이 낫다. 삭제는 tombstone/SQLite free page로 즉시 반영하고 `freelist_count/page_count` 또는 delta 비율이 10%를 넘을 때만 병합한다.

### 낡음 판정

| 방식 | 10,000 page 중앙값 | 상대 비용 |
|---|---:|---:|
| `revision.json` root 문자열 read+compare | **0.017 ms** | 1× |
| `wiki/**/*.json` rglob + 10,000 `stat()` mtime scan | **58.133 ms** | 3,419.59× |

root 비교는 2,000회씩 7 round, mtime scan은 7 round 측정했다. hook은 매 질문마다 파일 tree를 훑지 말고 revision root만 확인해야 한다. 어떤 page가 바뀌었는지는 build/ingest 단계에서 map hash delta로 계산한다.

## (c) JSON 신호 활용

`render_markdown()`은 frontmatter와 `source_text`를 이어 붙인다. 따라서 일부 값은 완전히 없어지고, `sources`·`projects`·`tags`는 문자열 frontmatter로 남지만 typed field로서의 경계와 rank/route 의미가 사라진다.

| JSON 신호 | Markdown mirror에서 | 현재 색인·순위 사용 | 현재 페이로드 사용 | 판정 |
|---|---|---|---|---|
| page ID | 본문에 없음; filename/manifest로 우회 | structural2 `page` row와 최종 tie-break | `id`, `slug` | 사용 |
| block ID | 사라짐 | `blk` row, top block 근거 | block `id`, 선택 projection | 사용 |
| `links[].block_id` | target/anchor 소유권 사라짐 | anchor 1.35×, 역방향 간선 근거 block | rank 결과 근거로 간접 사용; `get fields=links`로 원형 조회 | 사용 |
| link `kind` (`related`, `supersedes`, `wiki`) | wikilink text만 남고 kind 사라짐 | edge weight, hub 감쇠, supersedes head fold | on-demand link metadata | 사용 |
| block `refs` | wikilink 문자열은 남아도 명시 배열·block 귀속은 사라짐 | structural2는 직접 안 씀; `project.implied_links()`는 graph에 사용 | block별 `refs` | 일부 사용 |
| block `kind` | Markdown syntax로 일부 추정 가능하나 `current/conflict` 정규값은 보장 안 됨 | `current` 1.04×, unresolved conflict 0.97× | kind, flagged | 사용 |
| `resolution.status` | 사라짐 | unresolved conflict multiplier | conflict status | 사용 |
| `sources` | frontmatter 문자열로만 남음 | structural2는 직접 안 씀; project graph는 source edge로 사용 | page sources | 일부 사용 |
| `projects` | frontmatter 문자열로만 남음 | structural2는 안 씀; 기존 scan ranker와 graph route/group은 사용 | page projects | 일부 사용 |
| `tags` | frontmatter 문자열로만 남음 | structural2는 안 씀; 기존 scan ranker는 사용 | page tags | 일부 사용 |
| `history.at` | 사라짐 | 기본 structural2는 안 씀; 이번 wrapper만 실험 | `get fields=history`일 때만 | 실험, 기본 미채택 |

### 추가 신호 실험: `history.at`

`bench/incremental/history_signal.py`는 structural2 top-100 후보에 각 page 최신 `history.at`의 날짜 분위수 × 0.02를 더하는 wrapper다. 500문항 결과는 다음과 같다.

| arm | recall@5 | MRR@10 | stale_above | p50 / p95 ms | top-10 변화 |
|---|---:|---:|---:|---:|---:|
| structural2 | 0.8400 | 0.7381 | 0.0000 | 1.740 / 2.324 | — |
| + `history.at` | 0.8400 | 0.7381 | 0.0000 | 1.829 / 2.399 | 0 / 500 |

recall@5 변화는 **+0.0000**이다. frozen의 `history.at`은 생성 날짜일 뿐 질문 적합도 라벨이 아니며, structural2의 explicit `supersedes` fold가 temporal 100문항을 이미 처리한다. 그러므로 이 신호는 구현 가능성은 확인했지만 기본 rank 신호로 채택하지 않는다. 실제 문서에서 “최근 변경” 의도가 있는 별도 질의 세트가 생기기 전에는 `supersedes`보다 약한 tie-break로도 넣지 않는 편이 안전하다.

## 프로토타입 구조와 mutation 방법

`bench/incremental/segment_index.py`의 SQLite는 다음 논리 row를 가진다.

- `page`: page ID, source, sha256, slug/title, projects/tags, `history_at`
- `block`: page-scoped 안정 key, block ID/order, token length, kind/resolution
- `post`: `(term, block_key, page_id, tf)` page segment
- `termstat`: 현재 live block DF
- `edge`: source page별 원래 link order, target/kind/block ID
- `meta`: page/block 수, 전체 token 길이, map root

update는 이전 `page.sha256`과 새 map의 `pages[*].sha256`를 비교해 deleted/modified row를 지우고 added/modified JSON만 읽는다. BM25의 DF와 평균 block 길이는 live `termstat`/meta에서 계산하므로 기존 page의 posting을 다시 쓰지 않아도 전체 재빌드와 같은 score가 나온다. graph, hub degree, supersedes head는 40,000 edge 규모에서 load 시 결정적으로 재료화한다.

mutation은 정렬된 page ID와 고정 규칙을 썼다. 본문은 첫 block의 `data.text`·`source_text`·fingerprint를 함께 바꿨고, 추가 page는 ID/slug/block ID를 새로 매긴 clone, 삭제는 shard 제거, supersedes는 기존 비-supersedes page의 첫 block을 anchor로 명시 edge를 추가했다. 새 map hash와 변경 JSON은 실험 case마다 만들었으며 전체 재빌드와 증분은 같은 overlay를 읽었다.

## 함수 단위 제품 설계 제안

구현 범위 밖의 제안이며 `scripts/`는 수정하지 않았다.

### `scripts/llmwiki.py`

1. `Workspace.search_db -> root/index/search.sqlite`를 추가한다. `Workspace.markdown`은 제거하고, `render_markdown()`은 사람이 명시적으로 한 page를 렌더할 때만 남긴다. `export_markdown()`/CLI `export-markdown`은 제거한다.
2. `page_hash_map(ws, changed_paths=None)`를 분리한다. cold build는 정본을 한 번 읽어 production `map.json`을 만들고, ingest/file-watcher 경로는 알려진 changed path만 다시 hash한다. 결과는 page ID → source/pointer/sha256와 canonical root hash다.
3. `map_delta(old_map, new_map)`를 순수 함수로 두어 added/modified/deleted page ID를 반환한다. SQLite의 `meta.map_root`가 새 root와 같으면 build를 즉시 끝낸다.
4. `update_search_sqlite(ws, docs_by_id, delta, map_root)`를 추가한다. 한 `BEGIN IMMEDIATE` transaction에서 changed page의 block TF, kind/resolution, links, DF와 tombstone을 갱신한다. 성공 commit 뒤에만 `meta.map_root`를 바꾼다.
5. `project(ws)`에서 `search.json` 생성을 제거하고 `project_view(ws)`로 이름/책임을 좁힌다. catalog/map/graph/routes/stats는 viewer 파생물로 유지한다. `build(ws, changed_paths=None)`는 `page_hash_map → map_delta → update_search_sqlite → project_view → revision publish` 순서로 실행한다.
6. `revision.json`은 `map_root`, `search_root`, viewer projection root를 각각 갖게 한다. hook은 `map_root == search.meta.map_root`만 O(1) 비교하고, 불일치하면 stale index 오류를 내며 Markdown/qmd나 full scan으로 조용히 우회하지 않는다.
7. `query(ws, text, limit)`는 `project(ws)["search.json"]` 전량 생성/scan 대신 `search.sqlite` reader를 호출한다. 결과는 page ID, score, block IDs만 받고 map의 source/pointer로 필요한 JSON block만 hydrate한다.
8. `ingest()`는 작성한 destination path/page ID를 반환해 caller가 `build(changed_paths=[dest])`에 넘기게 한다. rename/update 때 `moved_from`도 deleted delta로 함께 넘긴다. 정본 write와 색인 transaction 사이 crash가 나면 revision 불일치가 명시적으로 남는다.
9. foreground delete는 free page/tombstone만 만들고 `VACUUM`하지 않는다. `compact_search_index()`를 별도 유지보수 함수로 두어 delta/live ratio 또는 `freelist_count/page_count > 0.10`일 때 integer ID + varint page-segment BLOB으로 병합한다.

### `scripts/llmwiki_context.py`

1. `SearchIndex.open(root)`가 `index/search.sqlite`, `index/map.json`, `index/revision.json`만 연다. 세 root가 맞지 않으면 typed stale 오류를 반환한다.
2. `search_index(query, limit)`가 structural2의 tokenizer/BM25/graph/fold를 실행하고 `(page_id, score, block_ids)`를 반환한다. block ID가 있으므로 Markdown chunk를 다시 파싱하지 않는다.
3. `hydrate_hits(root, map, hits)`가 선택된 page shard와 block만 읽어 현재 `project_hit()` 형식으로 만든다. `refs`, `kind`, `resolution.status`, sources/projects/tags를 그대로 보존한다.
4. `retrieve()`의 `load_corpus → rank → qmd_slugs` 경로를 `SearchIndex.search → hydrate_hits`로 교체한다. `qmd_slugs()`와 qmd collection/env/timeout 설정은 삭제한다.
5. `find_doc()`/`get_page()`는 전량 `load_corpus()` scan 대신 map의 page/block 주소를 쓴다. `load_corpus()`는 doctor/migration/cold rebuild 전용으로 제한한다.
6. payload budget은 현재처럼 page 전체가 아니라 selected block projection에 적용한다. 여기서 JSON의 안정 block ID와 resolution/source metadata가 Markdown 대비 직접적인 토큰 절약을 만든다. 이번 과제는 tokenizer별 token 수를 측정하지 않았으므로 byte 절감과 구조 projection 효과를 token 수치로 둔갑시키지 않는다.

### compact base + delta 권고

현재 normalized prototype은 정확성 검증용으로 단순하지만 76.70 MB다. 제품 `search.sqlite`는 `base_post(term, df, tf/length/mult varint BLOB)`, `delta_post(page_id, term, block_id, tf)`, `tombstone(page_id, generation)`의 두 층을 권한다. query는 live DF/평균 길이로 base와 delta impact를 계산하고 tombstone을 건너뛴다. background merge만 BLOB을 다시 정렬하면 foreground 1~100 page 갱신 비용과 structural2의 compact 저장을 함께 얻을 수 있다.

## 재현

```bash
python3 bench/incremental/run_experiment.py
python3 -m py_compile bench/incremental/*.py
```

원시 결과는 `bench/results_inc/costs.json`, `incremental.json`, `signal_history_at.json`, `staleness.json`에 있다. 기준 색인은 `bench/index_inc/task_f/{structural2,segment_base}`에 남겼다.
