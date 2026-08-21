---
name: lint
description: JSON 위키의 모순, 낡은 주장, 고아 페이지, 누락 링크, stale map을 점검한다. "린트 해줘", "위키 점검해줘" 요청 시 사용.
---

# Lint

규칙의 정본은 저장소 루트 `INIT.md`다.

1. `python3 scripts/llmwiki.py validate`와 `python3 scripts/llmwiki.py lint --json`을 실행한다.
2. 중복 page/block ID, 끊어진 link/source ref, 미판정 conflict, 고아 page, 빈 summary, stale index/map을 확인한다.
3. index/map만 믿지 않고 관련 canonical object를 `get --pointer`로 확인한다.
4. 의미상 모순, 최신 소스가 뒤집은 주장, 빠진 개념 페이지, 누락된 cross-reference와 데이터 공백을 추가로 점검한다.
5. 발견 사항을 심각도 순으로 보고한다. canonical JSON 수정은 사용자 확인 후 진행한다.
6. 수정 후 build/validate/lint를 재실행하고 `wiki/log.jsonl`에 lint 기록을 append한다.
