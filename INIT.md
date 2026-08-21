# llmwiki_json — JSON 정본 LLM Wiki

이 저장소는 LLM이 작성·유지보수하고 사용자가 소스 큐레이션과 질문을 담당한다. 대화는 한국어로 한다. 소스 원문의 언어는 번역하지 않는다.

Karpathy의 LLM Wiki 패턴을 따른다. raw source를 매 질문마다 다시 조립하는 RAG 앱이 아니라, LLM이 지식을 한 번 통합하고 계속 갱신하는 persistent·compounding wiki다. 기존 `llmwiki`의 ingest/query/lint/index/log 기능을 유지하되 정본 표현만 JSON으로 바꾼다.

## 정본과 파생물

- 정본: `wiki/{sources,entities,concepts,syntheses,projects}/**/*.json`
- 불변 원본: `raw/` (절대 수정하지 않는다)
- 파생물: `index/*.json`, `viewer/public/data/**`
- 파생물은 `python3 scripts/llmwiki.py build`로만 갱신한다.

## 개발 규칙

- 웹 코드는 전부 `viewer/`에만 둔다. 루트나 `app/`, `frontend/`에 웹 파일을 만들지 않는다.
- JavaScript package와 lockfile은 `viewer/package.json`, `viewer/bun.lock`만 사용한다.
- package manager는 Codex와 Claude 모두 **Bun 1.3.14**다. npm/yarn/pnpm 및 `package-lock.json`을 사용하지 않는다.
- UI는 Tailwind CSS + shadcn/ui, 그래프는 2D/3D 전환을 지원한다.
- 그래프 색상은 프로젝트(Alpha/Beta/공통/다중), page type, tag 기준을 지원한다. tag 모드는 색상 범례를 제공하고 개별 tag checkbox로 필터하지 않는다.
- 재생 버튼은 현재 필터 결과의 node와 edge를 처음부터 순차적으로 다시 표시한다.
- 개발 서버는 `wiki/**/*.json` 변경을 감시해 index/map/graph를 자동 재생성하고 열린 그래프를 즉시 갱신한다.

## 페이지 원칙

- 모든 page와 block은 영속 ID를 가진다. 배열 위치를 ID로 사용하지 않는다.
- 주장은 `sources`로 근거를 연결한다. 사용자 전달 사실은 `user:YYYY-MM-DD` source ref를 쓴다.
- 상충은 `conflict` block과 `resolution.status`로 명시한다. 미판정이면 양쪽을 병기한다.
- 새 소스가 주장을 뒤집어도 삭제하지 않고 history와 supersedes 관계를 남긴다.
- 보안정보는 저장하지 않고 `(접속 정보 생략)`으로 치환한다.

## 워크플로

### Ingest
1. raw source를 읽는다.
2. `scripts/llmwiki.py ingest` 또는 JSON 직접 작성으로 page를 만든다.
3. 관련 page를 갱신하고 source/entity/concept/synthesis/project 관계를 연결한다.
4. append-only `wiki/log.jsonl`에 기록한다.
5. `build`, `validate`, `lint`를 실행한다.

### Query
1. `index/routes.json` → `index/catalog.json` → `index/map.json` 순서로 주소를 찾는다.
2. 필요한 page/block/field만 projection한다.
3. 모르면 `query` 또는 qmd collection `llmwiki_json`을 사용한다.

### Lint
미판정 상충, 미존재 링크, 고아 페이지, 잘못된 source ref, 중복 ID, stale map을 점검한다. 수정은 사용자 확인 후 한다.
