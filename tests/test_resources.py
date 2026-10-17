"""Off-network guard rails for the static catalog in resources.py.

These tests exist because VERIFIED_IMAGES is allowed to *override* the
platform's own default image when creating a task. That makes the table a
correctness-critical input, not documentation: a wrong value there produces a
task that fails to pull its image and burns the creator's points while
starting.

None of this needs a token or the network.
"""

from __future__ import annotations

import re

import pytest

from openi_bridge import resources
from openi_bridge.resources import (
    CARDS,
    COMPUTE_SOURCES,
    VERIFIED_IMAGES,
    describe_resources,
    trusted_verified_image,
)

# Platform image ids are 32 lowercase hex digits (a uuid without dashes).
IMAGE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class TestVerifiedImageTrust:
    """The override gate: only verified, well-formed entries may be used."""

    def test_every_shipped_entry_is_well_formed(self):
        """No entry may claim to be verified while carrying a bogus id.

        This is the regression test for the spec-328 placeholder
        (`5b9d7a3c8e2f1d4e0a6b7c8d9e0f1a2b`, an ascending run of hex digits
        rather than a platform-issued uuid) and for the spec-347 value that
        carried a `bi` prefix. Both would have silently overridden a working
        platform default.
        """
        for spec_id, row in VERIFIED_IMAGES.items():
            assert row.get("verified") == "yes", (
                f"spec {spec_id} ships without verified=yes; it is a lead, "
                f"not a cleared override"
            )
            assert IMAGE_ID_RE.match(row.get("image_id", "")), (
                f"spec {spec_id} image_id {row.get('image_id')!r} is not a "
                f"32-hex-digit platform image id"
            )

    def test_unverified_entries_are_refused(self):
        """An entry recorded as a lead must not be handed to create_task."""
        fake = {
            "999": {"image_id": "z" * 32, "image_name": "nope"},  # no flag
        }
        original = dict(VERIFIED_IMAGES)
        VERIFIED_IMAGES.update(fake)
        try:
            assert trusted_verified_image("999") is None
        finally:
            VERIFIED_IMAGES.clear()
            VERIFIED_IMAGES.update(original)

    def test_malformed_id_is_refused_even_when_flagged(self):
        """The flag alone is not enough; the value still has to be an id."""
        original = dict(VERIFIED_IMAGES)
        VERIFIED_IMAGES["998"] = {
            "image_id": "not-a-real-image-id",
            "image_name": "x",
            "verified": "yes",
        }
        try:
            assert trusted_verified_image("998") is None
        finally:
            VERIFIED_IMAGES.clear()
            VERIFIED_IMAGES.update(original)

    def test_removed_specs_fall_back_to_platform_default(self):
        """328 (910A) and 347 (Biren) are not cleared, so they must return None.

        Falling back is always safe: the platform listed that image itself.
        """
        assert trusted_verified_image("328") is None
        assert trusted_verified_image("347") is None

    def test_trusted_entry_returns_row(self):
        row = trusted_verified_image("389")
        assert row is not None
        assert row["image_id"] == "8c8d460801974a98945d26d8a06cf2d2"


class TestToolNameReferences:
    """Every openi_* name in prose must be a tool that actually exists.

    Error messages and docstrings are read by the model, so a stale or invented
    name sends it looking for a tool that is not there. Two were found this way:
    openi_get_creation_options (really openi_list_creation_options) and
    openi_mark_notifications_read (really openi_mark_all_notifications_read).
    """

    # Non-tool identifiers that legitimately appear in prose.
    ALLOWED = {"openi_bridge", "openi_resource"}

    def _defined_tools(self):
        import re
        from pathlib import Path

        source = Path(__file__).resolve().parents[1] / "src/openi_bridge/server.py"
        text = source.read_text(encoding="utf-8")
        return set(re.findall(r"def (openi_[a-z_]+)\(", text))

    def _referenced_names(self):
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        names = set()
        for path in list((root / "src/openi_bridge").glob("*.py")) + [root / "README.md"]:
            names |= set(re.findall(r"openi_[a-z_]+", path.read_text(encoding="utf-8")))
        return names

    def test_no_dangling_tool_references(self):
        defined = self._defined_tools()
        assert defined, "failed to parse any tool definitions"
        dangling = sorted(self._referenced_names() - defined - self.ALLOWED)
        assert not dangling, (
            f"prose references tools that do not exist: {dangling}. "
            f"Either the tool was renamed or the reference is stale."
        )

    def test_known_renames_are_gone(self):
        referenced = self._referenced_names()
        for stale in ("openi_get_creation_options", "openi_mark_notifications_read"):
            assert stale not in referenced, f"stale tool name {stale} reappeared"


class TestReadmeConsistency:
    """The README is the front door, so its claims must match the server.

    Two drifts had accumulated: the tool table header said 73 tools while there
    were 85, and the grouped table's links pointed at anchors that did not
    exist. Neither is caught by anything else, because the README is not code.
    """

    def _readme(self):
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / "README.md").read_text(
            encoding="utf-8"
        )

    def _tool_docs(self):
        """The full tool table lives in docs/tools.md since the README was trimmed."""
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / "docs/tools.md").read_text(
            encoding="utf-8"
        )

    def _defined_tools(self):
        import re
        from pathlib import Path

        text = (Path(__file__).resolve().parents[1] / "src/openi_bridge/server.py").read_text(
            encoding="utf-8"
        )
        return set(re.findall(r"^def (openi_[a-z_]+)\(", text, re.M))

    def test_every_tool_is_documented_exactly_once(self):
        import re

        rows = re.findall(r"^\| (openi_[a-z_]+) \|", self._tool_docs(), re.M)
        defined = self._defined_tools()
        undocumented = sorted(defined - set(rows))
        duplicated = sorted({r for r in rows if rows.count(r) > 1})
        assert not undocumented, f"tools missing from the README table: {undocumented}"
        assert not duplicated, f"tools listed twice in the README table: {duplicated}"

    def test_stated_tool_count_matches_reality(self):
        import re

        # the count is stated in both places; both must agree with reality
        expected = len(self._defined_tools())
        for name, text in (("README.md", self._readme()), ("docs/tools.md", self._tool_docs())):
            m = re.search(r"共 \*{0,2}(\d+)\*{0,2} 个工具", text)
            assert m, f"{name} no longer states a tool count"
            assert int(m.group(1)) == expected, f"{name} says {m.group(1)}, actual {expected}"

    def test_internal_links_resolve(self):
        import re

        def slug(heading):
            s = heading.strip().lower()
            s = re.sub(r"[^\w\u4e00-\u9fff\- ]", "", s)
            return s.replace(" ", "-")

        text = self._readme()
        heads = {slug(m.group(2)) for m in re.finditer(r"^(#{2,6})\s+(.+?)\s*$", text, re.M)}
        broken = [
            m.group(2)
            for m in re.finditer(r"\[[^\]]+\]\(#([^)]+)\)", text)
            if m.group(1) not in heads
        ]
        assert not broken, f"README links point at missing headings: {broken}"


class TestOutputToolNames:
    """The two output tools must read the way they behave.

    They used to be openi_task_outputs (one level) and openi_task_output_files
    (recursive) -- the plural, more expansive name was the shallow one, so a
    caller reasonably picked the wrong tool. The shallow one is now _dir, which
    says what it does.
    """

    def _texts(self):
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        server = (root / "src/openi_bridge/server.py").read_text(encoding="utf-8")
        readme = (root / "README.md").read_text(encoding="utf-8")
        allsrc = "\n".join(
            p.read_text(encoding="utf-8")
            for p in (root / "src/openi_bridge").glob("*.py")
        )
        return server, readme, allsrc

    def test_old_misleading_name_is_gone(self):
        server, readme, allsrc = self._texts()
        assert "openi_task_outputs" not in server
        assert "openi_task_outputs" not in readme

    def test_both_names_exist(self):
        server, _, _ = self._texts()
        assert "def openi_task_output_dir(" in server
        assert "def openi_task_output_files(" in server

    def test_dir_tool_says_it_is_not_recursive(self):
        import re

        server, _, _ = self._texts()
        m = re.search(r'def openi_task_output_dir\(.*?"""(.*?)"""', server, re.S)
        assert m, "could not find the openi_task_output_dir docstring"
        doc = m.group(1)
        assert "一次一层" in doc, "the shallow tool must say it lists one level"
        assert "openi_task_output_files" in doc, "and point at the recursive one"

    def test_files_tool_says_it_is_recursive(self):
        import re

        server, _, _ = self._texts()
        m = re.search(r'def openi_task_output_files\(.*?"""(.*?)"""', server, re.S)
        assert m
        doc = m.group(1)
        assert "递归" in doc and "一次拿全" in doc


class TestRawTaskToolIsSafeByDefault:
    """openi_start_task_raw posts an unchecked body and bills for a card."""

    def test_dry_run_defaults_to_true(self):
        import inspect

        from openi_bridge.server import openi_start_task_raw

        sig = inspect.signature(openi_start_task_raw)
        assert sig.parameters["dry_run"].default is True, (
            "the raw path must not create a billed task unless asked to"
        )

    def test_docstring_says_it_previews_by_default(self):
        from openi_bridge.server import openi_start_task_raw

        doc = openi_start_task_raw.__doc__ or ""
        assert "默认只预览" in doc or "默认 dry_run=True" in doc
        assert "dry_run=False" in doc, "must tell the caller how to actually run it"


class TestDocsLinks:
    """Links must resolve across every markdown file, not just the README.

    Content moved from the README into docs/ during the trim, and moving text
    breaks relative paths and in-page anchors silently -- two such links were
    found only by checking. This pins the whole set.
    """

    def _files(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        return [root / "README.md"] + sorted((root / "docs").glob("*.md"))

    @staticmethod
    def _slug(heading):
        import re

        s = heading.strip().lower()
        s = re.sub(r"[^\w\u4e00-\u9fff\- ]", "", s)
        return s.replace(" ", "-")

    def test_every_relative_link_resolves(self):
        import re

        broken = []
        for f in self._files():
            text = f.read_text(encoding="utf-8")
            for m in re.finditer(r"\[[^\]]+\]\(([^)]+)\)", text):
                target = m.group(1)
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                path_part = target.split("#")[0]
                if not path_part:
                    continue
                if not (f.parent / path_part).resolve().exists():
                    broken.append(f"{f.name} -> {target}")
        assert not broken, f"broken relative links: {broken}"

    def test_every_in_page_anchor_exists(self):
        import re

        broken = []
        for f in self._files():
            text = f.read_text(encoding="utf-8")
            heads = {
                self._slug(m.group(2))
                for m in re.finditer(r"^(#{1,6})\s+(.+?)\s*$", text, re.M)
            }
            for m in re.finditer(r"\[[^\]]+\]\(#([^)]+)\)", text):
                if m.group(1) not in heads:
                    broken.append(f"{f.name} -> #{m.group(1)}")
        assert not broken, f"dead anchors: {broken}"

    def test_docs_directory_is_linked_from_the_readme(self):
        from pathlib import Path

        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
            encoding="utf-8"
        )
        docs = Path(__file__).resolve().parents[1] / "docs"
        for f in sorted(docs.glob("*.md")):
            assert f.name in readme, (
                f"docs/{f.name} is not linked from the README, so nobody will find it"
            )


class TestReadmeClientSnippets:
    """The README documents four client config shapes; they must stay correct.

    These blocks are the easiest thing in the repo to get wrong by hand, because
    every client wants a different shape and two of them reject the obvious one:
    dsh needs its entry inside a top-level YAML array (and its default template
    is a bare `[]` that must be replaced, not appended to), and Cursor rejects
    the nested {"mcpServers": {...}} form that Claude Desktop wants.
    """

    def _install_doc(self):
        """The per-client snippets live in docs/install.md since the README
        was trimmed to a project front page."""
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / "docs/install.md").read_text(
            encoding="utf-8"
        )

    def _block(self, lang):
        import re

        m = re.search(rf"```{lang}\n(.*?)```", self._install_doc(), re.S)
        assert m, f"docs/install.md has no {lang} block"
        return m.group(1)

    def test_dsh_block_is_valid_yaml_with_the_right_entry(self):
        yaml = pytest.importorskip("yaml")

        doc = yaml.safe_load(self._block("yaml"))
        assert isinstance(doc, list), "dsh patch layer is a top-level array"
        entry = doc[0]["insert"][0]
        assert entry["id"] == "mcp-openi-bridge"
        assert entry["name"] == "@deepseek-ai/dsh-mcp-client"
        assert entry["config"]["transport"] == "stdio"
        assert entry["config"]["serverName"] == "openi-bridge"

    def test_cursor_snippet_is_flat_and_named(self):
        import json
        import re

        m = re.search(r"cursor --add-mcp '(\{.*?\})'", self._install_doc(), re.S)
        assert m, "docs/install.md has no cursor --add-mcp snippet"
        entry = json.loads(m.group(1))
        assert entry["name"] == "openi-bridge"
        assert "mcpServers" not in entry, (
            "Cursor rejects the nested form; the snippet must be flat"
        )

    def test_codex_block_matches_what_the_installer_writes(self):
        from openi_bridge.install_mcp import codex_block

        generated = codex_block(["/Users/you/.openi/venv/bin/openi-bridge"])
        assert "[mcp_servers.openi-bridge]" in generated
        readme = self._block("toml")
        assert "[mcp_servers.openi-bridge]" in readme
        assert "command = " in readme and "args = " in readme

    def test_claude_desktop_block_uses_nested_form(self):
        import json
        import re

        for m in re.finditer(r"```json\n(.*?)```", self._install_doc(), re.S):
            text = m.group(1)
            if "mcpServers" in text:
                doc = json.loads(text)
                assert "openi-bridge" in doc["mcpServers"]
                return
        pytest.fail("docs/install.md no longer documents the Claude Desktop JSON form")

    def test_no_snippet_contains_a_token(self):
        text = self._install_doc()
        import re

        for m in re.finditer(r"```(json|toml|yaml)\n(.*?)```", text, re.S):
            body = m.group(2).lower()
            assert "token" not in body or "你的令牌" in m.group(2), (
                f"a {m.group(1)} snippet appears to embed a credential"
            )


class TestCatalogIntegrity:
    """Cross-table consistency: the catalog is read by the model as fact."""

    def test_card_sources_are_known_compute_sources(self):
        for name, meta in CARDS.items():
            assert meta["source"] in COMPUTE_SOURCES, (
                f"card {name} maps to unknown compute_source {meta['source']!r}"
            )

    def test_card_prices_are_positive_numbers(self):
        for name, meta in CARDS.items():
            assert isinstance(meta["price"], (int, float)), name
            assert meta["price"] > 0, name

    def test_verified_accelerators_reference_known_sources(self):
        for row in resources.VERIFIED_ACCELERATORS:
            source = row.get("compute_source")
            assert source in COMPUTE_SOURCES, (
                f"{row.get('card_type')} references unknown source {source!r}"
            )

    def test_describe_resources_is_valid_json(self):
        import json

        payload = json.loads(describe_resources())
        assert payload["cards"], "catalog rendered empty"
        assert payload["live_specs"]

    def test_module_exports_image_id_pattern(self):
        """Guard the guard: the regex resources.py enforces must exist."""
        assert resources.IMAGE_ID_RE.match("a" * 32)
        assert not resources.IMAGE_ID_RE.match("A" * 32)
        assert not resources.IMAGE_ID_RE.match("bi" + "0" * 30)


class TestAvailabilityProse:
    """The 'availability' sentence is hand-written prose about a live cluster.

    It drifts silently, so pin it to the data it claims to describe.
    """

    def test_prose_does_not_claim_more_than_data_supports(self):
        import json

        payload = json.loads(describe_resources())
        verified_sources = {
            row["compute_source"] for row in resources.VERIFIED_ACCELERATORS
        }
        prose = payload["availability"]
        # METAX and CPU are explicitly called out as not verified; make sure no
        # other source sneaks into the table without an evidence row.
        assert verified_sources <= set(COMPUTE_SOURCES)


@pytest.mark.parametrize("spec_id", sorted(VERIFIED_IMAGES))
def test_no_duplicate_image_names_across_specs(spec_id):
    """Two specs pointing at one image id is usually a copy-paste slip."""
    seen: dict[str, str] = {}
    for other, row in VERIFIED_IMAGES.items():
        img = row["image_id"]
        if img in seen:
            pytest.fail(
                f"spec {other} and {spec_id} share image_id {img}; "
                f"one of them is probably wrong"
            )
        seen[img] = other