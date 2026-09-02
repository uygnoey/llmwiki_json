# Parity 하네스

`tools/parity/` 는 두 가지 질문에 숫자로 답한다.

1. **정본 build 는 정말 결정적인가** — 같은 입력에서 두 번 돌려 바이트가 같은가.
2. **다른 런타임으로 옮기면 어디가 갈라지는가** — 그리고 그 완화책이 실제로 통하는가.

두 번째는 Python 구현을 TypeScript 로 옮길지 판단하기 위한 것이다. 옮기지
않기로 하더라도 첫 번째는 계속 값어치가 있다.

## 구성

| 파일 | 하는 일 |
| --- | --- |
| `cases.json` | 두 런타임이 공유하는 case 목록. 입력·기대·완화책이 한곳에 있다 |
| `probe.py` | Python(정본) 답안지 |
| `probe.ts` | Bun(후보) 답안지 — `results` 는 순진한 JS, `mitigated` 는 완화책 적용 |
| `serialize.ts` | Python `json.dumps` 와 바이트가 같은 직렬화기 (완화책 참조 구현) |
| `casefold.ts` | Unicode 15.0 full case fold 전수 매핑 1,530자 (완화책 참조 구현) |
| `sweep.py` / `sweep.ts` | 값 공간 전체 훑기 — corpus 가 놓치는 구간 |
| `parity.py` | 드라이버 — `corpus`, `sweep`, `build` 세 명령 |

## 쓰는 법

```bash
python3 tools/parity/parity.py build     # build 산출물의 바이트 결정성
python3 tools/parity/parity.py corpus    # 두 런타임의 원시 의미론 대조
python3 tools/parity/parity.py sweep     # 값 공간 전체 훑기
python3 -m unittest tests.test_parity    # 위 셋을 시험으로 고정
```

`corpus` 는 Python 과 Bun 을 **각각 독립 실행**한다. 한쪽 출력을 다른 쪽 입력으로
쓰지 않는다 — 그래야 "같은 답"이 우연이 아님을 알 수 있다.

`build` 는 정본만 복사한 임시 저장소에서 돈다. 공식 `index/` 와
`viewer/public/data/` 는 절대 건드리지 않는다. 두 번의 cold build 를 대조한 뒤,
세 번째로 cold build → page 하나 편집 → `build --changed` 증분 → 같은 정본의
cold build 를 돌려 증분 발행본(`index/search.sqlite` 포함)의 바이트가 cold 와 같은지
본다. 바이트가 다르면 `llmwiki_index.logical_digest`(표 내용의 PK 순 지문) 까지 같은지
따로 적어 원인이 sqlite 파일 배치인지 내용인지 가른다. `index/search.work.*`(증분
작업 DB 와 상태 파일) 는 발행물이 아니라 지문에서 뺀다. 나중에 TS build 가 생기면
`--candidate 'bun tools/parity/build.ts'` 로 그대로 꽂으면 되고, 파일 목록·경로·
내용 sha256 을 전부 대조한다.

## case 를 늘릴 때

`cases.json` 에 `{id, kind, input, expect, mitigation}` 을 추가하면 두 probe 가
자동으로 답한다. `expect` 는 **지금 갈라지는지**를 기록하는 자리다. `diverge` 로
적었는데 시험이 통과하지 않으면 런타임이 바뀐 것이니, 어느 쪽이 낡았는지 먼저
확인해라. `mitigation` 은 비워 둘 수 없다 — 막는 법을 모르면 아직 case 가 아니라
미해결 질문이다.

`kind` 는 `json`, `json_pretty`, `sha256`, `nfc`, `casefold`, `sort`,
`codepoints`, `regex` 다. 새 kind 를 더하려면 두 probe 를 함께 고쳐야 한다.

## 읽는 법

`순진` 열은 관용적인 JS 로 짰을 때, `완화` 열은 `mitigation` 대로 짰을 때다.

- 순진 `≠` / 완화 `=` — 포팅하려면 이 완화책을 반드시 넣어야 한다.
- 순진 `=` / 완화 `=` — 그냥 옮겨도 되는 지점.
- 완화 `≠` — 일반 규칙으로 못 맞춘다. 포팅하려면 계약 자체를 고치고 정본을
  다시 만들어야 한다는 뜻이다.

시험이 보는 것은 두 가지뿐이다. 기록해 둔 `expect` 와 실제가 어긋나지 않는가,
그리고 완화 `≠` 가 하나도 남지 않았는가.

## corpus 만으로는 부족하다

이 하네스를 만들면서 실제로 겪은 일이라 적어 둔다.

첫 후보 구현은 손으로 고른 24개 case 를 **전부 통과**했다. 그런데 값 공간을
훑어 보니 `|v| >= 1e16` 에서 float 표기가 1,002/7,637 틀렸다 — JS `toString()`
이 그 구간에서 지수 없는 긴 십진수를 주는데, 그것을 지수 표기로 되돌릴 때
자리값 0 을 유효숫자로 끌고 왔기 때문이다. case 가 그 구간을 밟지 않아서
초록이었을 뿐이다. casefold 도 마찬가지로 `toLowerCase()` + 두 글자 치환이
`ß`, `ς` 만 담은 case 를 통과했지만 합자·그리스 iota subscript·체로키에서
깨졌다.

그래서 `sweep` 이 있다. corpus 는 **무엇을 막아야 하는지**를 사람이 읽을 수
있게 기록하는 곳이고, sweep 은 **정말 막았는지**를 값 공간으로 확인하는
곳이다. 둘 다 있어야 한다. 새 완화책을 넣을 때는 corpus case 만 늘리지 말고
sweep 표본이 그 구간을 덮는지도 확인해라.

손으로 고른 시험은 자기가 생각한 것만 시험한다.
