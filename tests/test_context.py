"""자동 컨텍스트 주입 CLI: 검색 품질, 예산, hook 스키마, fail-open."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.support import (CONTEXT_SCRIPT, REPO, WorkspaceCase, llmwiki_context as ctx,
                           make_page)

# 로컬에 설치된 클라이언트가 실제로 보내는 UserPromptSubmit 페이로드 모양.
# claude 2.1.237 / codex-cli 0.148.0 에서 확인한 필드 집합이다.
CLAUDE_INPUT = {
    "session_id": "sess-1",
    "transcript_path": "/tmp/transcript.jsonl",
    "cwd": "/tmp",
    "permission_mode": "default",
    "hook_event_name": "UserPromptSubmit",
    "prompt": "",
}
# codex 0.148.0 user-prompt-submit.command.input 은 turn_id/model 이 필수다.
CODEX_INPUT = {
    "cwd": "/tmp",
    "hook_event_name": "UserPromptSubmit",
    "model": "gpt-5.6-sol",
    "permission_mode": "default",
    "prompt": "",
    "session_id": "sess-1",
    "transcript_path": None,
    "turn_id": "turn-1",
}
# codex 의 user-prompt-submit.command.output 은 additionalProperties:false 다.
CODEX_OUTPUT_KEYS = {"continue", "decision", "hookSpecificOutput", "reason", "stopReason",
                     "suppressOutput", "systemMessage"}

CORPUS = [
    make_page("폐기-icd-코드",
              "# 폐기 ICD 코드\n\n"
              "RefModel 기준으로 무효 처리된 RefCode-CM 코드 324건을 어떻게 노출할지의 정책이다.\n\n"
              "## 핵심 사실\n\n"
              "- 전체 모집단은 324건이며 Condition 도달 149건과 Observation 전용 175건으로 나뉜다.\n"
              "- 자체 개념 유지와 Maps to 참고 후보를 섞은 혼합 정책이 권고됐다.\n\n"
              "> ⚠️ 상충: OSE_DATA_03 문서는 149건이라고 적었고 전수 대조는 324건을 보고했다.\n",
              type="concept", projects=["beta"], tags=["폐기-코드", "샘플개념-코드"],
              sources=["source:beta-폐기-icd-코드-정책-비교", "user:2026-08-12"],
              summary="RefModel 무효 ICD 코드의 검색 노출 정책."),
    make_page("golden-set",
              "# Golden Set\n\n"
              "The golden set is the manually curated answer key for AlphaStd SDTM mapping "
              "regression checks.\n\n"
              "## Facts\n\n"
              "- Every release compares automated mapping output against the golden set.\n",
              type="concept", projects=["alpha"], tags=["검증", "mapping"],
              sources=["source:샘플노트-2026-07-16"],
              summary="AlphaStd SDTM 매핑 회귀 검증용 정답지."),
    make_page("배포-메모",
              "# 배포 메모\n\n"
              "api_key: sk-live-should-never-leak 를 설정에 넣지 말 것.\n",
              type="concept", projects=["beta"], summary="배포 시 주의사항."),
]


class ContextCase(WorkspaceCase):
    """이 파일의 in-process 검사는 **최후 스캔 경로**(`use_memory=False`)를 고정한다.

    색인이 없을 때의 기본 폴백은 정본에서 만든 메모리 색인(P/B/E)이고 그쪽은
    `tests/test_index.py` 가 다룬다. 훅 subprocess 검사는 기본 경로 그대로 돈다.
    """
    def setUp(self) -> None:
        super().setUp()
        self.write_pages(CORPUS, name="corpus.json")
        self.root_path = Path(self.root).resolve()

    def build(self, query: str, **options: object) -> tuple[str, object, list[dict]]:
        options.setdefault("use_memory", False)
        return ctx.build_context(self.root_path, query, **options)


# --------------------------------------------------------------------------- tokens
class TokenTest(ContextCase):
    def test_stopwords_and_short_tokens_are_dropped(self) -> None:
        self.assertEqual(ctx.query_tokens("이거 어떻게 해줘?"), [])
        self.assertEqual(ctx.query_tokens("what is this about"), [])

    def test_korean_particles_do_not_break_matching(self) -> None:
        # "코드는" 처럼 조사가 붙어 들어와도 정본의 "코드" 를 찾아야 한다.
        self.assertGreater(ctx.match_strength("코드는", "폐기 icd 코드"), 0.5)
        self.assertEqual(ctx.match_strength("코드", "폐기 icd 코드"), 1.0)

    def test_unrelated_korean_token_does_not_match(self) -> None:
        self.assertEqual(ctx.match_strength("파스타", "폐기 icd 코드"), 0.0)

    def test_latin_tokens_only_trim_to_four_characters(self) -> None:
        self.assertGreater(ctx.match_strength("mappings", "sdtm mapping"), 0.5)
        self.assertEqual(ctx.match_strength("mapper", "maps to"), 0.0)


# --------------------------------------------------------------------------- retrieval
class RetrievalTest(ContextCase):
    def test_korean_question_finds_the_right_page(self) -> None:
        result = ctx.retrieve(self.root_path, "폐기 ICD 코드는 몇 건인가요?")
        self.assertEqual(result.reason, "canonical")
        self.assertEqual(result.hits[0].doc.page_id, "page:폐기-icd-코드")

    def test_english_question_finds_the_right_page(self) -> None:
        result = ctx.retrieve(self.root_path, "what is the golden set for SDTM mapping?")
        self.assertEqual(result.hits[0].doc.page_id, "page:golden-set")

    def test_a_long_instruction_does_not_starve_the_answer(self) -> None:
        # 커버리지는 질문이 길수록 떨어진다. 정본을 정확히 짚은 질문이 뒤에 붙은
        # 지시문 때문에 통째로 버려지면 안 된다 (실사용에서 이걸로 주입이 끊겼다).
        short = "폐기 ICD 코드 324건 중 Condition 도달과 Observation 전용은 각각 몇 건이야?"
        long = (short + " 근거 page id 와 block id 를 붙여 3줄 이내로 답해라. "
                "표로 정리하지 말고 문장으로만 써라. 마지막에 확신도를 적어라. "
                "추측이면 추측이라고 밝히고, 모르면 모른다고 답해라. "
                "출처가 여러 개면 최신 것을 먼저 적어라.")
        self.assertEqual(ctx.retrieve(self.root_path, short).reason, "canonical")
        result = ctx.retrieve(self.root_path, long)
        self.assertEqual(result.reason, "canonical")
        self.assertEqual(result.hits[0].doc.page_id, "page:폐기-icd-코드")
        self.assertLess(result.coverage, ctx.MIN_COVERAGE,
                        "커버리지 문턱을 넘어서가 아니라 매치 개수로 통과해야 하는 케이스다")
        self.assertGreaterEqual(len(result.hits[0].matched), ctx.MIN_MATCHED)

    def test_unrelated_question_injects_nothing(self) -> None:
        text, result, pages = self.build("파스타 삶는 법 알려줘")
        self.assertEqual(text, "")
        self.assertEqual(pages, [])
        self.assertIn(result.reason, {"no-match", "below-threshold"})

    def test_greeting_has_no_content_tokens(self) -> None:
        _, result, _ = self.build("안녕하세요")
        self.assertEqual(result.reason, "no-content-tokens")

    def test_without_an_index_the_memory_index_is_the_default_fallback(self) -> None:
        text, result, _ = ctx.build_context(self.root_path, "폐기 ICD 코드는 몇 건인가요?")
        self.assertEqual(result.mode, "memory")
        self.assertEqual(result.fallback, "no-index")
        self.assertTrue(text.startswith("<llmwiki-context v=3>"))

    def test_the_scan_path_is_the_last_resort_and_says_so(self) -> None:
        text, result, _ = self.build("폐기 ICD 코드는 몇 건인가요?")
        self.assertEqual(result.mode, "scan")
        self.assertEqual(result.fallback, "no-index;memory-disabled")
        self.assertTrue(text.startswith("<llmwiki-context>\n"))

    def test_broken_shard_is_skipped_instead_of_raising(self) -> None:
        (self.root_path / "wiki" / "concepts" / "broken.json").write_text("{ nope",
                                                                         encoding="utf-8")
        result = ctx.retrieve(self.root_path, "폐기 ICD 코드")
        self.assertTrue(result.hits)

    def test_empty_corpus_is_not_an_error(self) -> None:
        empty = Path(self.root) / "nowhere"
        text, result, _ = ctx.build_context(empty, "폐기 ICD 코드")
        self.assertEqual(text, "")
        self.assertEqual(result.reason, "empty-corpus")


# --------------------------------------------------------------------------- projection
class ProjectionTest(ContextCase):
    def test_context_carries_page_identity_blocks_and_sources(self) -> None:
        text, _, pages = self.build("폐기 ICD 코드 정책")
        page = pages[0]
        self.assertEqual(page["id"], "page:폐기-icd-코드")
        self.assertEqual(page["type"], "concept")
        self.assertEqual(page["file"], "wiki/concepts/corpus.json")
        self.assertIn("source:beta-폐기-icd-코드-정책-비교", page["sources"])
        self.assertTrue(page["blocks"])
        for block in page["blocks"]:
            self.assertTrue(block["id"].startswith("block:"))
            self.assertTrue(block["kind"])
        self.assertIn("page:폐기-icd-코드", text)
        self.assertIn("wiki/concepts/corpus.json", text)
        self.assertIn("updated=", text)

    def test_headings_are_not_spent_on_the_budget(self) -> None:
        _, _, pages = self.build("폐기 ICD 코드 정책")
        kinds = {b["kind"] for b in pages[0]["blocks"]}
        self.assertNotIn("heading", kinds)

    def test_unresolved_conflict_is_surfaced(self) -> None:
        text, _, pages = self.build("폐기 ICD 코드 정책")
        page = pages[0]
        self.assertEqual(page["unresolved_conflicts"], 1)
        conflict = [b for b in page["blocks"] if b["kind"] == "conflict"]
        self.assertTrue(conflict)
        self.assertEqual(conflict[0]["resolution"], "unresolved")
        self.assertIn("미판정 상충", text)

    def test_conflict_ships_even_when_the_question_does_not_mention_it(self) -> None:
        _, _, pages = self.build("폐기 ICD 코드")
        self.assertTrue(any(b["kind"] == "conflict" for b in pages[0]["blocks"]))

    def test_credentials_are_masked(self) -> None:
        text, _, pages = self.build("배포 메모 주의사항")
        self.assertTrue(pages, "배포 메모 페이지가 잡혀야 마스킹을 검증할 수 있다")
        self.assertNotIn("sk-live-should-never-leak", text)
        self.assertIn("(접속 정보 생략)", text)

    def test_projection_never_ships_the_whole_page(self) -> None:
        _, _, pages = self.build("폐기 ICD 코드 정책")
        self.assertNotIn("history", pages[0])
        self.assertNotIn("links", pages[0])
        self.assertNotIn("block_order", pages[0])


# --------------------------------------------------------------------------- 고정 주입
class AlwaysTest(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.root_path = Path(self.root).resolve()
        self.write_pages([make_page(
            "ai-작업-규칙",
            "# AI 작업 규칙\n\n- 한국어로 답한다.\n- page 통짜 대신 block 만 집는다.\n",
            type="concept", projects=["공통"], summary="이 사람과 일하는 방식.")])
        self.write_pages([make_page("폐기-icd-코드", "# 폐기 ICD 코드\n\n폐기 ICD 코드는 324건이다.\n",
                                    type="concept", projects=["beta"])], name="other.json")

    def pin(self, *slugs: str, budget: int = 800) -> None:
        path = self.root_path / ctx.ALWAYS_CONFIG
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"always": list(slugs), "always_max_bytes": budget},
                                   ensure_ascii=False), encoding="utf-8")

    def build(self, query: str) -> str:
        return ctx.build_context(self.root_path, query)[0]

    def test_without_config_nothing_is_pinned(self) -> None:
        self.assertEqual(ctx.render_always(self.root_path), "")

    def test_a_pinned_page_rides_along_with_an_unrelated_question(self) -> None:
        self.pin("ai-작업-규칙")
        text = self.build("파스타 삶는 법 알려줘")
        self.assertIn("page:ai-작업-규칙", text)
        self.assertIn("한국어로 답한다", text)

    def test_a_pinned_page_is_not_repeated_by_the_search(self) -> None:
        self.pin("ai-작업-규칙")
        text = self.build("AI 작업 규칙에서 조회는 어떻게 하라고 했지?")
        self.assertEqual(text.count("page:ai-작업-규칙"), 1)

    def test_pinning_does_not_crowd_out_the_search_result(self) -> None:
        self.pin("ai-작업-규칙")
        text = self.build("폐기 ICD 코드는 몇 건인가요?")
        self.assertIn("page:ai-작업-규칙", text)
        self.assertIn("폐기-icd-코드", text)

    def test_the_pinned_share_cannot_swallow_the_whole_budget(self) -> None:
        # 설정이 크게 잡혀 있어도 검색 몫이 남아야 한다.
        self.pin("ai-작업-규칙", budget=100000)
        preamble = ctx.render_always(self.root_path, total=1000)
        self.assertLessEqual(len(preamble.encode("utf-8")), 500)

    def test_the_always_only_path_respects_the_cap(self) -> None:
        self.pin("ai-작업-규칙")
        preamble = ctx.render_always(self.root_path)
        self.assertEqual(ctx.render_always_only(self.root_path, preamble, max_bytes=10), "")

    def test_the_pinned_budget_is_respected(self) -> None:
        self.pin("ai-작업-규칙", budget=120)
        self.assertLessEqual(len(ctx.render_always(self.root_path).encode("utf-8")), 120)

    def test_a_missing_pinned_page_is_not_an_error(self) -> None:
        self.pin("없는-page")
        self.assertEqual(ctx.render_always(self.root_path), "")
        self.assertEqual(self.build("파스타 삶는 법"), "")

    def test_pinning_can_be_turned_off_per_call(self) -> None:
        self.pin("ai-작업-규칙")
        text = ctx.build_context(self.root_path, "파스타 삶는 법", use_always=False)[0]
        self.assertEqual(text, "")

    def test_the_hook_carries_the_pinned_page(self) -> None:
        self.pin("ai-작업-규칙")
        payload = json.dumps({"prompt": "파스타 삶는 법", "cwd": "/tmp",
                              "hook_event_name": "UserPromptSubmit"}, ensure_ascii=False)
        out, stats = ctx.run_hook(self.root_path, payload,
                                  max_bytes=ctx.MAX_BYTES, max_tokens=ctx.MAX_TOKENS)
        self.assertTrue(stats["injected"])
        self.assertIn("ai-작업-규칙", out)

# --------------------------------------------------------------------------- hint
class HintTest(ContextCase):
    """본문 문턱은 못 넘었지만 짚이는 데는 있는 질문에는 주소만 넘긴다."""

    def weak(self, query: str) -> tuple[str, object, list[dict]]:
        # 본문 문턱을 인위적으로 올려 '약한 매치' 상태를 만든다.
        return self.build(query, min_score=1000.0)

    def test_weak_match_ships_addresses_instead_of_silence(self) -> None:
        text, result, pages = self.weak("폐기 코드 이야기 좀 하자")
        self.assertTrue(result.reason.startswith("hint"))
        self.assertIn("page:폐기-icd-코드", text)
        self.assertTrue(pages)

    def test_hint_carries_no_block_text(self) -> None:
        text, _, pages = self.weak("폐기 코드 이야기 좀 하자")
        self.assertEqual([p["blocks"] for p in pages], [[] for _ in pages])
        self.assertNotIn("324건", text)

    def test_hint_tells_the_client_how_to_fetch_one_block(self) -> None:
        text, _, _ = self.weak("폐기 코드 이야기 좀 하자")
        self.assertIn("llmwiki_get", text)
        self.assertIn("통째로 읽지 마라", text)

    def test_hint_stays_small(self) -> None:
        text, _, _ = self.weak("폐기 코드 이야기 좀 하자")
        self.assertLessEqual(len(text.encode("utf-8")), 1400)

    def test_unrelated_question_gets_no_hint_either(self) -> None:
        text, result, _ = self.weak("파스타 삶는 법 알려줘")
        self.assertEqual(text, "")
        self.assertNotIn("hint", result.reason)

    def test_a_stray_number_is_not_a_signal(self) -> None:
        # 본문에 우연히 같은 숫자가 있다고 해서 주소를 흘리지 않는다.
        text, result, _ = self.weak("zzqq xxyy vvww 324")
        self.assertEqual(text, "")
        self.assertNotIn("hint", result.reason)

    def test_strong_match_still_ships_blocks(self) -> None:
        text, result, pages = self.build("폐기 ICD 코드는 몇 건인가요?")
        self.assertEqual(result.reason, "canonical")
        self.assertTrue(pages[0]["blocks"])
        self.assertIn("324", text)


# --------------------------------------------------------------------------- get
class GetTest(ContextCase):
    """조회는 언제나 필요한 object 단위. page 통짜는 명시할 때만."""

    def outline(self, selector: str = "폐기-icd-코드", **kwargs: object) -> dict:
        return ctx.get_page(self.root_path, selector, **kwargs)

    def test_default_is_an_outline_not_the_page(self) -> None:
        out = self.outline()
        self.assertEqual(out["id"], "page:폐기-icd-코드")
        self.assertEqual(out["block_count"], len(out["blocks"]))
        self.assertTrue(all(set(row) <= {"id", "kind", "preview", "refs", "resolution",
                                         "conflict"} for row in out["blocks"]))

    def test_outline_is_far_smaller_than_the_page(self) -> None:
        whole = json.dumps(self.outline(mode="page"), ensure_ascii=False)
        outline = json.dumps(self.outline(), ensure_ascii=False)
        self.assertLess(len(outline), len(whole) / 2)

    def test_named_block_comes_back_whole(self) -> None:
        block_id = self.outline()["blocks"][1]["id"]
        out = self.outline(blocks=[block_id])
        self.assertEqual([b["id"] for b in out["blocks"]], [block_id])
        self.assertIn("data", out["blocks"][0])

    def test_several_blocks_come_back_in_the_order_asked(self) -> None:
        ids = [row["id"] for row in self.outline()["blocks"][1:4]]
        out = self.outline(blocks=list(reversed(ids)))
        self.assertEqual([b["id"] for b in out["blocks"]], list(reversed(ids)))

    def test_block_id_tail_is_enough(self) -> None:
        block_id = self.outline()["blocks"][1]["id"]
        out = self.outline(blocks=[block_id.split(":", 2)[-1]])
        self.assertEqual([b["id"] for b in out["blocks"]], [block_id])

    def test_selector_can_carry_the_block(self) -> None:
        block_id = self.outline()["blocks"][1]["id"]
        out = self.outline(f"폐기-icd-코드#{block_id}")
        self.assertEqual([b["id"] for b in out["blocks"]], [block_id])

    def test_block_id_alone_resolves_its_page(self) -> None:
        block_id = self.outline()["blocks"][1]["id"]
        out = self.outline(block_id)
        self.assertEqual([b["id"] for b in out["blocks"]], [block_id])

    def test_slug_id_and_title_all_resolve(self) -> None:
        for selector in ("폐기-icd-코드", "page:폐기-icd-코드", "폐기 ICD 코드"):
            self.assertEqual(self.outline(selector)["id"], "page:폐기-icd-코드")

    def test_fields_narrow_the_page_header(self) -> None:
        out = self.outline(fields=["summary"])
        self.assertEqual(set(out) - {"block_count", "blocks", "file"}, {"summary"})

    def test_missing_block_is_reported_not_raised(self) -> None:
        out = self.outline(blocks=["block:없는:것"])
        self.assertEqual(out["missing"], ["block:없는:것"])
        self.assertEqual(out["blocks"], [])

    def test_missing_page_is_an_error_value(self) -> None:
        self.assertIn("page 없음", self.outline("nope")["error"])

    def test_unknown_mode_is_an_error_value(self) -> None:
        self.assertIn("unknown mode", self.outline(mode="everything")["error"])

    def test_credentials_are_masked_in_every_mode(self) -> None:
        for mode in ("outline", "page"):
            text = json.dumps(self.outline("배포-메모", mode=mode), ensure_ascii=False)
            self.assertNotIn("sk-live-should-never-leak", text)
            self.assertIn("접속 정보 생략", text)

    def test_cli_get_prints_the_same_projection(self) -> None:
        proc = self.context_cli("get", "폐기-icd-코드")
        self.assertEqual(json.loads(proc.stdout)["id"], "page:폐기-icd-코드")

    def test_cli_get_block_flag_selects_one_object(self) -> None:
        block_id = self.outline()["blocks"][1]["id"]
        proc = self.context_cli("get", "폐기-icd-코드", "--block", block_id)
        self.assertEqual([b["id"] for b in json.loads(proc.stdout)["blocks"]], [block_id])

    def test_cli_get_reports_a_missing_page_with_a_nonzero_exit(self) -> None:
        proc = self.context_cli("get", "nope", expect=1)
        self.assertIn("page 없음", json.loads(proc.stdout)["error"])


# --------------------------------------------------------------------------- budget
class BudgetTest(ContextCase):
    def test_byte_and_token_caps_are_never_exceeded(self) -> None:
        for max_bytes in (400, 900, 1500, 4000, 20000):
            text, _, _ = self.build("폐기 ICD 코드 정책 golden set mapping",
                                    max_bytes=max_bytes)
            self.assertLessEqual(len(text.encode("utf-8")), max_bytes, max_bytes)

    def test_token_cap_binds_independently(self) -> None:
        for max_tokens in (40, 120, 400, 2000):
            text, _, _ = self.build("폐기 ICD 코드 정책 golden set mapping",
                                    max_tokens=max_tokens)
            self.assertLessEqual(ctx.est_tokens(text), max_tokens, max_tokens)

    def test_token_estimate_is_conservative_for_korean(self) -> None:
        # 한글 1글자는 UTF-8 3바이트라 1토큰으로 센다(실제보다 크게 잡는 쪽).
        self.assertEqual(ctx.est_tokens("가나다"), 3)

    def test_truncation_is_announced_not_silent(self) -> None:
        text, _, pages = self.build("폐기 ICD 코드 정책 golden set mapping", max_bytes=1200)
        if len(pages) > 1 and text:
            self.assertIn("예산 초과로", text)

    def test_block_text_is_clipped(self) -> None:
        _, _, pages = self.build("폐기 ICD 코드 정책", max_block_chars=40)
        for block in pages[0]["blocks"]:
            self.assertLessEqual(len(block["text"]), 40)


# --------------------------------------------------------------------------- hook
class HookTest(ContextCase):
    def hook(self, payload: object, *, raw: str | None = None, cwd: Path | None = None,
             env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        stdin = raw if raw is not None else json.dumps(payload, ensure_ascii=False)
        return self.context_cli("hook", stdin=stdin, cwd=cwd, env=env)

    def test_claude_shaped_input_produces_injectable_output(self) -> None:
        proc = self.hook({**CLAUDE_INPUT, "prompt": "폐기 ICD 코드는 몇 건인가요?"})
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("폐기-icd-코드", payload["hookSpecificOutput"]["additionalContext"])

    def test_codex_shaped_input_produces_schema_valid_output(self) -> None:
        proc = self.hook({**CODEX_INPUT, "prompt": "폐기 ICD 코드는 몇 건인가요?"})
        payload = json.loads(proc.stdout)
        self.assertTrue(set(payload) <= CODEX_OUTPUT_KEYS, payload.keys())
        specific = payload["hookSpecificOutput"]
        self.assertEqual(set(specific), {"hookEventName", "additionalContext"})
        self.assertIsInstance(specific["additionalContext"], str)

    def test_both_clients_get_the_same_context(self) -> None:
        prompt = "폐기 ICD 코드는 몇 건인가요?"
        a = json.loads(self.hook({**CLAUDE_INPUT, "prompt": prompt}).stdout)
        b = json.loads(self.hook({**CODEX_INPUT, "prompt": prompt}).stdout)
        self.assertEqual(a["hookSpecificOutput"]["additionalContext"],
                         b["hookSpecificOutput"]["additionalContext"])

    def test_unrelated_prompt_emits_nothing(self) -> None:
        self.assertEqual(self.hook({**CLAUDE_INPUT, "prompt": "파스타 삶는 법 알려줘"}).stdout, "")

    def test_slash_command_is_skipped(self) -> None:
        self.assertEqual(self.hook({**CLAUDE_INPUT, "prompt": "/code-review 폐기 ICD 코드"}).stdout,
                         "")

    def test_already_injected_prompt_is_not_reinjected(self) -> None:
        for prompt in ("<llmwiki-context>기존</llmwiki-context> 폐기 ICD 코드",
                       "<llmwiki-context v=3>\nP x</llmwiki-context> 폐기 ICD 코드"):
            self.assertEqual(self.hook({**CLAUDE_INPUT, "prompt": prompt}).stdout, "", prompt)

    def test_malformed_stdin_never_blocks_the_prompt(self) -> None:
        for raw in ("", "   ", "not json", "{", "[]", "null", '"just a string"',
                    '{"prompt": null}', '{"prompt": ""}', '{}'):
            proc = self.hook(None, raw=raw)
            self.assertEqual(proc.returncode, 0, raw)
            self.assertEqual(proc.stdout, "", raw)

    def test_disable_switch_stops_all_injection(self) -> None:
        proc = self.hook({**CLAUDE_INPUT, "prompt": "폐기 ICD 코드는 몇 건인가요?"},
                         env={ctx.ENV_DISABLE: "1"})
        self.assertEqual(proc.stdout, "")

    def test_hook_works_from_any_cwd(self) -> None:
        for cwd in (Path("/"), Path("/tmp"), REPO):
            proc = self.hook({**CLAUDE_INPUT, "prompt": "폐기 ICD 코드는 몇 건인가요?"}, cwd=cwd)
            self.assertIn("폐기-icd-코드", proc.stdout, str(cwd))

    def test_budget_env_overrides_apply_to_the_hook(self) -> None:
        proc = self.hook({**CLAUDE_INPUT, "prompt": "폐기 ICD 코드는 몇 건인가요?"},
                         env={ctx.ENV_MAX_BYTES: "900"})
        payload = json.loads(proc.stdout or "{}")
        text = payload.get("hookSpecificOutput", {}).get("additionalContext", "")
        self.assertLessEqual(len(text.encode("utf-8")), 900)

    def test_stats_log_records_no_prompt_text(self) -> None:
        log = self.root_path / "hook.log"
        self.hook({**CLAUDE_INPUT, "prompt": "폐기 ICD 코드는 몇 건인가요?"},
                  env={ctx.ENV_LOG: str(log)})
        rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(rows[0]["injected"])
        self.assertEqual(rows[0]["client"], "claude")
        self.assertNotIn("몇 건인가요", json.dumps(rows, ensure_ascii=False))

    def test_codex_client_is_detected_from_turn_id(self) -> None:
        log = self.root_path / "codex.log"
        self.hook({**CODEX_INPUT, "prompt": "폐기 ICD 코드는 몇 건인가요?"},
                  env={ctx.ENV_LOG: str(log)})
        rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows[0]["client"], "codex")


# --------------------------------------------------------------------------- cli
class CliTest(ContextCase):
    def test_search_reports_scores_and_file_evidence(self) -> None:
        payload = json.loads(self.context_cli("search", "폐기 ICD 코드").stdout)
        self.assertEqual(payload["results"][0]["id"], "page:폐기-icd-코드")
        self.assertEqual(payload["results"][0]["file"], "wiki/concepts/corpus.json")
        self.assertGreater(payload["results"][0]["score"], 0)

    def test_context_json_reports_the_budget(self) -> None:
        payload = json.loads(self.context_cli("context", "폐기 ICD 코드", "--json").stdout)
        self.assertEqual(payload["bytes"], len(payload["text"].encode("utf-8")))
        self.assertGreaterEqual(payload["est_tokens"], 1)

    def test_doctor_resolves_the_repo_from_any_cwd(self) -> None:
        payload = json.loads(self.context_cli("doctor", cwd=Path("/")).stdout)
        self.assertEqual(payload["root"], str(self.root_path))
        self.assertEqual(payload["wiki_pages"], len(CORPUS))

    def test_script_is_executable_and_self_locating(self) -> None:
        self.assertTrue(os.access(CONTEXT_SCRIPT, os.X_OK))
        self.assertEqual(ctx.DEFAULT_ROOT, REPO)


# --------------------------------------------------------------------------- mcp
class McpTest(ContextCase):
    def rpc(self, *requests: dict) -> list[dict]:
        stdin = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in requests)
        proc = self.context_cli("mcp", stdin=stdin)
        return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]

    def test_initialize_and_tools_list(self) -> None:
        rows = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                        {"jsonrpc": "2.0", "method": "notifications/initialized"},
                        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(rows[0]["result"]["serverInfo"]["name"], "llmwiki")
        names = {t["name"] for t in rows[1]["result"]["tools"]}
        self.assertEqual(names, {"llmwiki_search", "llmwiki_context", "llmwiki_get"})

    def test_search_tool_returns_canonical_hits(self) -> None:
        rows = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "llmwiki_search",
                                    "arguments": {"query": "폐기 ICD 코드"}}})
        payload = json.loads(rows[0]["result"]["content"][0]["text"])
        self.assertEqual(payload["results"][0]["id"], "page:폐기-icd-코드")

    def test_get_tool_returns_an_outline_not_the_whole_page(self) -> None:
        rows = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "llmwiki_get",
                                    "arguments": {"selector": "폐기-icd-코드"}}})
        page = json.loads(rows[0]["result"]["content"][0]["text"])
        self.assertEqual(page["id"], "page:폐기-icd-코드")
        # 목차는 block 주소와 미리보기만 준다. 정본 block object 는 싣지 않는다.
        self.assertEqual(page["block_count"], len(page["blocks"]))
        self.assertTrue(all("data" not in row for row in page["blocks"]))
        self.assertNotIn("block_order", page)

    def test_get_tool_hands_back_the_named_block_object(self) -> None:
        rows = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "llmwiki_get",
                                    "arguments": {"selector": "폐기-icd-코드"}}})
        block_id = json.loads(rows[0]["result"]["content"][0]["text"])["blocks"][1]["id"]
        rows = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "llmwiki_get",
                                    "arguments": {"selector": "폐기-icd-코드",
                                                  "blocks": [block_id]}}})
        payload = json.loads(rows[0]["result"]["content"][0]["text"])
        self.assertEqual([b["id"] for b in payload["blocks"]], [block_id])
        self.assertIn("data", payload["blocks"][0])

    def test_get_tool_still_accepts_the_singular_block_argument(self) -> None:
        rows = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "llmwiki_get",
                                    "arguments": {"selector": "폐기-icd-코드"}}})
        block_id = json.loads(rows[0]["result"]["content"][0]["text"])["blocks"][1]["id"]
        rows = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "llmwiki_get",
                                    "arguments": {"selector": "폐기-icd-코드",
                                                  "block": block_id}}})
        payload = json.loads(rows[0]["result"]["content"][0]["text"])
        self.assertEqual([b["id"] for b in payload["blocks"]], [block_id])

    def test_get_tool_ships_the_whole_page_only_when_asked(self) -> None:
        rows = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "llmwiki_get",
                                    "arguments": {"selector": "폐기-icd-코드",
                                                  "mode": "page"}}})
        page = json.loads(rows[0]["result"]["content"][0]["text"])
        self.assertIn("block_order", page)
        self.assertIsInstance(page["blocks"], dict)

    def test_missing_page_is_reported_not_raised(self) -> None:
        rows = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "llmwiki_get",
                                    "arguments": {"selector": "nope"}}})
        self.assertIn("page 없음", rows[0]["result"]["content"][0]["text"])

    def test_unknown_method_is_an_error_not_a_crash(self) -> None:
        rows = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "resources/list"},
                        {"jsonrpc": "2.0", "id": 2, "method": "ping"})
        self.assertEqual(rows[0]["error"]["code"], -32601)
        self.assertEqual(rows[1]["result"], {})

    def test_garbage_lines_do_not_kill_the_server(self) -> None:
        proc = self.context_cli("mcp", stdin='not json\n\n{"jsonrpc":"2.0","id":9,'
                                            '"method":"ping"}\n')
        rows = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(rows[-1]["id"], 9)

    def test_mcp_exposes_no_write_tools(self) -> None:
        for tool in ctx.MCP_TOOLS:
            self.assertNotRegex(tool["name"], r"(write|edit|delete|ingest|build|log)")


# --------------------------------------------------------------------------- global wiring
class LegacyQmdMcpTest(ContextCase):
    """옛 qmd MCP 등록은 우리가 만든 것이 아니다 — 찾아서 떼는 명령을 알려 줄 뿐, 건드리지 않는다."""

    def setUp(self) -> None:
        super().setUp()
        self.home = self.root_path / "home"
        (self.home / ".codex").mkdir(parents=True)
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.addCleanup(lambda: os.environ.__setitem__("HOME", old_home or ""))

    def test_clean_machine_reports_nothing(self) -> None:
        self.assertEqual(ctx.legacy_qmd_mcp(self.root_path), [])
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {"llmwiki": {}}}), encoding="utf-8")
        (self.home / ".codex/config.toml").write_text("[mcp_servers.llmwiki]\ncommand = \"x\"\n", encoding="utf-8")
        self.assertEqual(ctx.legacy_qmd_mcp(self.root_path), [])
        report = ctx.verify(self.root_path, clients=())
        item = next(c for c in report["checks"] if c["check"] == "legacy-qmd-mcp")
        self.assertTrue(item["ok"])
        self.assertNotIn("warn", item)

    def test_finds_every_scope_and_names_the_removal(self) -> None:
        (self.home / ".claude.json").write_text(json.dumps({
            "mcpServers": {"qmd": {"command": "qmd", "args": ["mcp"]}},
            "projects": {"/work/a": {"mcpServers": {"qmd": {"command": "qmd"}}},
                         "/work/b": {"mcpServers": {"other": {}}}},
        }), encoding="utf-8")
        (self.root_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"qmd": {}}}), encoding="utf-8")
        (self.home / ".codex/config.toml").write_text(
            "[hooks.state]\nx = 1\n\n[mcp_servers.qmd]\ncommand = \"qmd\"\nargs = [\"mcp\"]\n", encoding="utf-8")
        found = ctx.legacy_qmd_mcp(self.root_path)
        self.assertEqual([f["remove"] for f in found], [
            "claude mcp remove --scope user qmd",
            "cd /work/a && claude mcp remove --scope local qmd",
            f"cd {self.root_path} && claude mcp remove --scope project qmd",
            "codex mcp remove qmd",
        ])
        self.assertEqual({f["client"] for f in found}, {"claude", "codex"})
        report = ctx.verify(self.root_path, clients=())
        item = next(c for c in report["checks"] if c["check"] == "legacy-qmd-mcp")
        self.assertTrue(item["ok"])          # 통과는 한다 — 떼는 건 사용자 몫
        self.assertTrue(item["warn"])
        self.assertIn("codex mcp remove qmd", item["detail"])
        # 아무것도 고치지 않았다
        self.assertIn("mcp_servers.qmd", (self.home / ".codex/config.toml").read_text(encoding="utf-8"))
        self.assertIn("qmd", json.loads((self.home / ".claude.json").read_text(encoding="utf-8"))["mcpServers"])

    def test_broken_config_files_do_not_raise(self) -> None:
        (self.home / ".claude.json").write_text("{not json", encoding="utf-8")
        (self.root_path / ".mcp.json").write_text("[]", encoding="utf-8")
        self.assertEqual(ctx.legacy_qmd_mcp(self.root_path), [])


class GlobalInstallTest(ContextCase):
    """전역 hook 설정에 필요한 계약 — 설치 스크립트가 이 모양을 만든다."""

    def test_hook_command_pins_absolute_interpreter_and_script(self) -> None:
        command = ctx.hook_command()
        self.assertIn(str(CONTEXT_SCRIPT), command)
        python = ctx.hook_python()
        self.assertTrue(python.startswith("/"), python)
        self.assertTrue(os.access(python, os.X_OK), python)
        self.assertIn(python, command)

    def test_install_appends_without_touching_existing_groups(self) -> None:
        path = self.root_path / "hooks.json"
        orca = {"hooks": [{"type": "command", "command": "orca-hook.sh", "timeout": 10}]}
        path.write_text(json.dumps({"hooks": {"UserPromptSubmit": [orca],
                                              "PreToolUse": [orca]}}), encoding="utf-8")
        report = ctx.install_hook(path, "MY-COMMAND", remove=False)
        config = json.loads(path.read_text(encoding="utf-8"))
        groups = config["hooks"]["UserPromptSubmit"]
        self.assertEqual(groups[0], orca)
        self.assertEqual(groups[1]["hooks"][0]["command"], "MY-COMMAND")
        self.assertEqual(config["hooks"]["PreToolUse"], [orca])
        self.assertTrue(Path(report["backup"]).exists())

    def test_install_is_idempotent(self) -> None:
        path = self.root_path / "hooks.json"
        command = ctx.hook_command()
        for _ in range(3):
            ctx.install_hook(path, command, remove=False)
        groups = json.loads(path.read_text(encoding="utf-8"))["hooks"]["UserPromptSubmit"]
        self.assertEqual(len(groups), 1)

    def test_remove_restores_the_original_groups(self) -> None:
        path = self.root_path / "hooks.json"
        orca = {"hooks": [{"type": "command", "command": "orca-hook.sh"}]}
        original = {"hooks": {"UserPromptSubmit": [orca]}, "statusLine": {"type": "command"}}
        path.write_text(json.dumps(original), encoding="utf-8")
        ctx.install_hook(path, ctx.hook_command(), remove=False)
        ctx.install_hook(path, ctx.hook_command(), remove=True)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)

    def test_guide_section_is_fenced_and_reversible(self) -> None:
        path = self.root_path / "AGENTS.md"
        path.write_text("# 기존 지침\n\n건드리면 안 되는 내용.\n", encoding="utf-8")
        ctx.install_guide(path, ctx.guide_body(self.root_path), remove=False)
        body = path.read_text(encoding="utf-8")
        self.assertIn("건드리면 안 되는 내용.", body)
        self.assertIn("llmwiki_json", body)
        ctx.install_guide(path, ctx.guide_body(self.root_path), remove=True)
        self.assertEqual(path.read_text(encoding="utf-8"),
                         "# 기존 지침\n\n건드리면 안 되는 내용.\n")

    def test_guide_install_is_idempotent(self) -> None:
        path = self.root_path / "AGENTS.md"
        for _ in range(3):
            ctx.install_guide(path, ctx.guide_body(self.root_path), remove=False)
        self.assertEqual(path.read_text(encoding="utf-8").count("llmwiki-context:start"), 1)

    def test_hook_command_fails_open_when_the_script_is_missing(self) -> None:
        # 저장소를 옮기거나 지워도 프롬프트가 막히면 안 된다.
        command = ctx.hook_command().replace(str(CONTEXT_SCRIPT), "/nonexistent/gone.py")
        proc = subprocess.run(["/bin/sh", "-c", command], input="{}", capture_output=True,
                              text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_hook_command_survives_a_python_crash(self) -> None:
        command = ctx.hook_command()
        proc = subprocess.run(["/bin/sh", "-c", command], input="\x00\xff garbage",
                              capture_output=True, text=True,
                              env={**os.environ, ctx.ENV_ROOT: "/nonexistent"})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
