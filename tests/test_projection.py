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
                                                 "routes.json", "search.json", "stats.json"])

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

        for artifact in ("catalog.json", "graph.json", "routes.json", "search.json", "stats.json"):
            self.assertEqual(combined[artifact], split[artifact], artifact)
        self.assertEqual({k: v["sha256"] for k, v in combined["map.json"]["pages"].items()},
                         {k: v["sha256"] for k, v in split["map.json"]["pages"].items()})

    def test_build_writes_identical_files_twice(self) -> None:
        self.write_demo()
        llmwiki.build(self.ws)
        before = {p.name: p.read_bytes() for p in sorted((self.root / "index").glob("*.json"))}
        llmwiki.build(self.ws)
        after = {p.name: p.read_bytes() for p in sorted((self.root / "index").glob("*.json"))}
        self.assertEqual(before, after)
        self.assertEqual(sorted(before), ["catalog.json", "graph.json", "map.json",
                                          "routes.json", "search.json", "stats.json"])

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
        self.assertEqual(stats, {"pages": 0, "blocks": 0, "edges": 0, "unresolved_conflicts": 0})

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
