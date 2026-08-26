---
name: ingest
description: raw/의 새 소스를 JSON 위키에 통합한다. "ingest 해줘", "소스 처리해줘" 요청 시 사용.
---

# Ingest

규칙의 정본은 저장소 루트 `INIT.md`다.

1. `raw/`에서 대상 소스를 찾고 정독한다. 원본은 절대 수정하지 않는다.
2. 핵심 내용과 강조점을 사용자와 짧게 논의한다. 배치 요청이면 생략할 수 있다.
3. 소스 요약은 `wiki/sources/*.json`에 작성하고 관련 entity/concept/synthesis/project JSON을 생성하거나 갱신한다.
4. 모든 page와 block에는 영속 ID를 부여하고, 근거는 `sources`와 block `refs`로 연결한다.
   - `projects`·`tags`를 반드시 채운다 — 그래프의 프로젝트 그룹과 태그 색상이 이 두 필드에서만 나온다.
   - JSON을 직접 쓸 때는 본문에 넣은 `[[링크]]`를 page `links` 배열에도 넣는다. 빠지면 `lint`가 `missing from links` 경고를 낸다.
   - 뒤집힌 주장은 `supersedes`, 곁가지 관계는 `related`로 남긴다. 둘 다 그래프 선이 된다.
5. 상충은 `kind: "conflict"` block과 `resolution.status`로 보존한다. 새 주장으로 옛 주장을 지우지 않고 `history`와 `supersedes`를 남긴다.
6. 자격증명·접속 정보는 저장하지 않는다. 값이 있으면 `(접속 정보 생략)`으로 대체하거나 ingest를 중단한다.
7. `python3 scripts/llmwiki.py build`, `validate`, `lint`를 실행한다. build가 index와 JSON map, viewer data를 함께 갱신한다.
8. `wiki/log.jsonl`에 ingest 기록이 추가됐는지 확인하고 변경된 페이지·상충을 보고한다.
