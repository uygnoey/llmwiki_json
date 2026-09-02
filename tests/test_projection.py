"""Deterministic object/map projection."""
from __future__ import annotations

import json

from tests.support import WorkspaceCase, llmwiki, make_page


class ProjectionShapeTest(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_demo()
        self.payloads = llmwiki.project(self.ws)

    def test_emits_every_artifact(self) -> None:
        self.assertEqual(sorted(self.payloads), ["catalog.json", "graph.json", "map.json",
                                                 "routes.json", "stats.json"])

    def test_stats_count_pages_blocks_edges_and_conflicts(self) -> None:
        stats = self.payloads["stats.json"]
        self.assertEqual(stats["pages"], 3)
        self.assertEqual(stats["edges"], 2)  # alpha->beta, beta->alpha
        self.assertEqual(stats["unresolved_conflicts"], 1)
        self.assertEqual(stats["blocks"], sum(len(p["blocks"]) for p in self.ws.load_pages()))

    def test_graph_nodes_carry_degree_and_orphan_flags(self) -> None:
        nodes = {n["id"]: n for n in self.payloads["graph.json"]["nodes"]}
        self.assertEqual(nodes["page:alpha"]["outgoing"], 1)
        self.assertEqual(nodes["page:alpha"]["incoming"], 1)
        self.assertFalse(nodes["page:alpha"]["orphan"])
        self.assertTrue(nodes["page:gamma"]["orphan"])
        self.assertEqual(nodes["page:beta"]["unresolved_conflicts"], 1)

    def test_project_grouping_and_routes(self) -> None:
        nodes = {n["id"]: n for n in self.payloads["graph.json"]["nodes"]}
        self.assertEqual(nodes["page:alpha"]["group"], "beta")
        self.assertEqual(nodes["page:gamma"]["group"], "common")
        routes = self.payloads["routes.json"]
        self.assertEqual(routes["beta"], ["page:alpha", "page:beta"])
        self.assertEqual(routes["common"], ["page:gamma"])
        self.assertEqual(routes["alpha"], [])

    def test_multi_project_pages_land_in_multi_group(self) -> None:
        self.assertEqual(llmwiki.project_group(["beta", "alpha"], self.ws.load_groups()), "multi")
        self.assertEqual(llmwiki.project_group([], self.ws.load_groups()), "ungrouped")

    def test_map_addresses_every_page_and_block(self) -> None:
        mapping = self.payloads["map.json"]
        pages = self.ws.load_pages()
        self.assertEqual(sorted(mapping["pages"]), sorted(p["id"] for p in pages))
        self.assertTrue(all(entry["pointer"] == "" for entry in mapping["pages"].values()))
        self.assertEqual(len(mapping["blocks"]), sum(len(p["blocks"]) for p in pages))
        entry = mapping["blocks"][pages[0]["block_order"][0]]
        self.assertEqual(entry["page_id"], pages[0]["id"])
        self.assertTrue(entry["pointer"].startswith("/blocks/"))

    def test_map_paths_are_repo_relative(self) -> None:
        for entry in self.payloads["map.json"]["pages"].values():
            self.assertFalse(entry["source"].startswith("/"), entry["source"])
            self.assertTrue(entry["source"].startswith("wiki/"), entry["source"])

    def test_catalog_is_a_lightweight_row_per_page(self) -> None:
        row = self.payloads["catalog.json"][0]
        self.assertEqual(sorted(row), ["id", "projects", "slug", "sources", "summary",
                                       "tags", "title", "type", "updated"])


class DeterminismTest(WorkspaceCase):
    def test_repeated_projection_is_byte_identical(self) -> None:
        self.write_demo()
        first = json.dumps(llmwiki.project(self.ws), ensure_ascii=False, sort_keys=True)
        second = json.dumps(llmwiki.project(self.ws), ensure_ascii=False, sort_keys=True)
        self.assertEqual(first, second)

    def test_shard_layout_does_not_change_the_projection(self) -> None:
        pages = [make_page(slug, body, projects=["beta"]) for slug, body in
                 {"alpha": "# Alpha\n\n[[beta]]\n", "beta": "# Beta\n\n[[alpha]]\n"}.items()]
        self.write_pages(pages, name="all.json")
        combined = llmwiki.project(self.ws)

        (self.root / "wiki" / "concepts" / "all.json").unlink()
        self.write_pages([pages[1]], name="zz/one.json")
        self.write_pages([pages[0]], name="aa/two.json")
        split = llmwiki.project(self.ws)

        for artifact in ("catalog.json", "graph.json", "routes.json", "stats.json"):
            self.assertEqual(combined[artifact], split[artifact], artifact)
        self.assertEqual({k: v["sha256"] for k, v in combined["map.json"]["pages"].items()},
                         {k: v["sha256"] for k, v in split["map.json"]["pages"].items()})

    def test_build_writes_identical_files_twice(self) -> None:
        self.write_demo()
        llmwiki.build(self.ws)
        # search.work.* 는 증분 build 의 작업 DB 와 상태(시작 시각) 라 발행물이 아니다.
        before = {p.name: p.read_bytes() for p in sorted((self.root / "index").iterdir())
                  if p.is_file() and not p.name.startswith("search.work.")}
        llmwiki.build(self.ws)
        after = {p.name: p.read_bytes() for p in sorted((self.root / "index").iterdir())
                 if p.is_file() and not p.name.startswith("search.work.")}
        self.assertEqual(before, after)
        self.assertEqual(sorted(before), ["catalog.json", "graph.json", "map.json",
                                          "revision.json", "routes.json", "search.sqlite",
                                          "stats.json"])
        self.assertEqual(sorted(p.name for p in (self.root / "index").iterdir()
                                if p.name.startswith("search.work.")),
                         ["search.work.json", "search.work.sqlite"])

    def test_layout_coordinates_are_rounded_and_stable(self) -> None:
        self.write_demo()
        nodes = llmwiki.project(self.ws)["graph.json"]["nodes"]
        for node in nodes:
            self.assertEqual(node["x"], round(node["x"], 6))
            self.assertEqual(node["y"], round(node["y"], 6))


class BuildOutputTest(WorkspaceCase):
    def test_build_publishes_page_shards_for_the_app(self) -> None:
        self.write_demo()
        stats = llmwiki.build(self.ws)
        graph = self.read_json("viewer/public/data/graph.json")
        self.assertEqual(len(graph["nodes"]), stats["pages"])
        for node in graph["nodes"]:
            shard = self.root / "viewer" / "public" / "data" / node["data_url"]
            self.assertTrue(shard.exists(), node["data_url"])
            self.assertEqual(json.loads(shard.read_text(encoding="utf-8"))["id"], node["id"])

    def test_build_prunes_removed_page_shards(self) -> None:
        self.write_demo()
        llmwiki.build(self.ws)
        stale = self.root / "viewer" / "public" / "data" / "pages" / "removed.json"
        stale.write_text("{}", encoding="utf-8")
        llmwiki.build(self.ws)
        self.assertFalse(stale.exists())

    def test_empty_knowledge_builds_an_empty_projection(self) -> None:
        stats = llmwiki.build(self.ws)
        self.assertEqual({k: stats[k] for k in ("pages", "blocks", "edges", "unresolved_conflicts")},
                         {"pages": 0, "blocks": 0, "edges": 0, "unresolved_conflicts": 0})
        self.assertEqual(stats["mode"], "full")

    def test_duplicate_page_id_is_rejected(self) -> None:
        page = make_page("alpha", "# Alpha\n")
        self.write_pages([page], name="a.json")
        self.write_pages([page], name="b.json")
        with self.assertRaisesRegex(llmwiki.WikiError, "duplicate page id"):
            llmwiki.project(self.ws)

    def test_duplicate_block_id_across_pages_is_rejected(self) -> None:
        alpha = make_page("alpha", "# Alpha\n")
        beta = make_page("beta", "# Beta\n")
        stolen = next(iter(alpha["blocks"]))
        block = dict(alpha["blocks"][stolen])
        beta["blocks"][stolen] = block
        beta["block_order"].append(stolen)
        self.write_pages([alpha, beta])
        with self.assertRaisesRegex(llmwiki.WikiError, "duplicate block id"):
            llmwiki.project(self.ws)

    def test_invalid_page_blocks_the_build(self) -> None:
        page = make_page("alpha", "# Alpha\n")
        page["type"] = "nope"
        self.write_pages([page])
        with self.assertRaisesRegex(llmwiki.WikiError, "invalid type"):
            llmwiki.project(self.ws)


class LinkResolutionTest(WorkspaceCase):
    """[[링크]]가 표기 하나 다르다고 선이 사라지면 안 된다."""

    def graph(self) -> dict:
        return llmwiki.project(self.ws)["graph.json"]

    def edges(self) -> set[tuple[str, str]]:
        return {(edge["source"], edge["target"]) for edge in self.graph()["edges"]}

    def test_title_and_casing_variants_resolve_to_the_same_page(self) -> None:
        self.write_pages([
            make_page("alpha-platform", "# Alpha Platform\n\n첫 워크스트림.\n"),
            make_page("note", "# Note\n\n[[Alpha Platform]] 과 [[ALPHA_PLATFORM]] 과 [[page:alpha-platform]].\n"),
        ])
        self.assertEqual(self.edges(), {("page:note", "page:alpha-platform")})

    def test_source_reference_draws_an_edge(self) -> None:
        self.write_pages([make_page("handbook", "# Handbook\n\n원본.\n"),
                          make_page("note", "# Note\n\n본문.\n", sources=["source:handbook"])])
        self.assertEqual(self.edges(), {("page:note", "page:handbook")})

    def test_source_reference_outside_the_wiki_is_ignored(self) -> None:
        self.write_pages([make_page("note", "# Note\n\n본문.\n",
                                    sources=["user:2026-08-19", "raw:notes.md"])])
        self.assertEqual(self.edges(), set())

    def test_block_wikilink_without_a_links_entry_still_draws_an_edge(self) -> None:
        alpha = make_page("alpha", "# Alpha\n\n[[beta]] 참조.\n")
        alpha["links"] = []  # 손으로 쓴 JSON page 가 흔히 빠뜨리는 배열
        self.write_pages([alpha, make_page("beta", "# Beta\n\n본문.\n")])
        self.assertEqual(self.edges(), {("page:alpha", "page:beta")})

    def test_a_page_linked_and_cited_gets_one_edge(self) -> None:
        self.write_pages([make_page("handbook", "# Handbook\n\n원본.\n"),
                          make_page("note", "# Note\n\n[[handbook]] 참조.\n",
                                    sources=["page:handbook"])])
        self.assertEqual(len(self.graph()["edges"]), 1)

    def test_self_links_are_not_edges(self) -> None:
        self.write_pages([make_page("alpha", "# Alpha\n\n[[alpha]] 자기 자신.\n")])
        nodes = {node["id"]: node for node in self.graph()["nodes"]}
        self.assertEqual(self.edges(), set())
        self.assertTrue(nodes["page:alpha"]["orphan"])


class ProjectGroupTest(WorkspaceCase):
    """groups.json 이 모르는 프로젝트도 자기 그룹을 가진다 — 전부 '미분류'로 뭉치면 안 된다."""

    def test_unknown_project_gets_its_own_group_and_route(self) -> None:
        self.write_pages([make_page("note", "# Note\n\n본문.\n", projects=["Sandbox Lab"])])
        payloads = llmwiki.project(self.ws)
        node = payloads["graph.json"]["nodes"][0]
        self.assertEqual(node["group"], "sandbox-lab")
        group = payloads["graph.json"]["groups"]["project"]["sandbox-lab"]
        self.assertEqual(group["label"], "Sandbox Lab")
        self.assertEqual(group["match"], ["Sandbox Lab"])
        self.assertEqual(payloads["routes.json"]["sandbox-lab"], ["page:note"])

    def test_derived_groups_do_not_touch_the_config_file(self) -> None:
        self.write_pages([make_page("note", "# Note\n\n본문.\n", projects=["Sandbox Lab"])])
        before = self.ws.groups_path.read_bytes()
        llmwiki.build(self.ws)
        self.assertEqual(self.ws.groups_path.read_bytes(), before)

    def test_reserved_groups_stay_last(self) -> None:
        self.write_pages([make_page("note", "# Note\n\n본문.\n", projects=["Sandbox Lab"])])
        keys = list(llmwiki.project(self.ws)["graph.json"]["groups"]["project"])
        self.assertEqual(keys[-2:], ["multi", "ungrouped"])
