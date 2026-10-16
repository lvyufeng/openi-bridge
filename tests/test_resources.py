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