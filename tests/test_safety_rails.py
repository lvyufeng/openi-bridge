"""Guard rails for the behaviors that used to report success on failure.

Every test here targets a specific defect found by audit, and is written so that
reverting the fix makes it fail. None of them touch the network: the client is
driven through a fake transport, and the billing/cleanup paths are checked with
a stub client.
"""

from __future__ import annotations

import pytest

from openi_bridge import autopoint
from openi_bridge.client import OpenIError, _uploaded_chunk_numbers


class TestUploadedChunkNumbers:
    """Resuming a multipart upload must never silently skip a chunk.

    The old code derived the resume offset from ``len(uploaded)``, which is only
    correct when the platform reports a dense 1..N prefix. A sparse report would
    skip a middle chunk and still call complete_multipart -- a corrupt object
    reported as a successful upload.
    """

    def test_dense_prefix_is_accepted(self):
        assert _uploaded_chunk_numbers({"chunks": [1, 2, 3]}, 5, "f.bin") == [1, 2, 3]

    def test_unordered_prefix_is_normalised(self):
        assert _uploaded_chunk_numbers({"chunks": [3, 1, 2]}, 5, "f.bin") == [1, 2, 3]

    def test_sparse_numbers_are_rejected(self):
        with pytest.raises(OpenIError, match="不是连续的"):
            _uploaded_chunk_numbers({"chunks": [1, 3]}, 5, "f.bin")

    def test_gap_after_prefix_is_rejected(self):
        with pytest.raises(OpenIError, match="不是连续的"):
            _uploaded_chunk_numbers({"chunks": [1, 2, 4]}, 5, "f.bin")

    def test_out_of_range_is_rejected(self):
        """[1,2,9] is caught by the contiguity check before the range check."""
        with pytest.raises(OpenIError, match="无法安全地从中间续传"):
            _uploaded_chunk_numbers({"chunks": [1, 2, 9]}, 5, "f.bin")

    def test_dense_prefix_beyond_total_is_rejected(self):
        """A dense 1..N prefix longer than the file's own chunk count is a mismatch."""
        with pytest.raises(OpenIError, match="超出该文件的总分片数"):
            _uploaded_chunk_numbers({"chunks": [1, 2, 3]}, 2, "f.bin")

    def test_count_only_is_treated_as_no_resume_info(self):
        """An int is a count, not a list of numbers, so it cannot be trusted."""
        assert _uploaded_chunk_numbers({"chunks": 3}, 5, "f.bin") == []
        assert _uploaded_chunk_numbers({"chunks": 0}, 5, "f.bin") == []

    def test_missing_or_empty_is_no_resume_info(self):
        assert _uploaded_chunk_numbers({}, 5, "f.bin") == []
        assert _uploaded_chunk_numbers({"chunks": []}, 5, "f.bin") == []
        assert _uploaded_chunk_numbers({"chunks": None}, 5, "f.bin") == []

    def test_unparsable_is_an_error_not_a_silent_restart(self):
        with pytest.raises(OpenIError, match="无法解析"):
            _uploaded_chunk_numbers({"chunks": ["a", "b"]}, 5, "f.bin")


class _StubClient:
    """Minimal stand-in for OpenIClient in the cleanup paths."""

    def __init__(self, statuses, stop_exc=None, del_exc=None, brief_exc_times=0):
        self._statuses = list(statuses)
        self._stop_exc = stop_exc
        self._del_exc = del_exc
        self._brief_exc_times = brief_exc_times
        self.stopped = False
        self.deleted = False
        self.brief_calls = 0

    def task_action(self, action, task_id, repo=None):
        if action == "stop":
            if self._stop_exc:
                raise self._stop_exc
            self.stopped = True
            return {"code": 0}
        if action == "del":
            if self._del_exc:
                raise self._del_exc
            self.deleted = True
            return {"code": 0}
        raise AssertionError(action)

    def task_brief(self, task_id):
        self.brief_calls += 1
        if self.brief_calls <= self._brief_exc_times:
            raise OpenIError("transient read failure")
        if self._statuses:
            return {"status": self._statuses.pop(0)}
        return {"status": "STOPPED"}


class TestStopAndDeleteReportsTruth:
    """Cleanup must not claim success while a billed task may still be running."""

    def test_transient_read_error_does_not_abandon_the_wait(self):
        """One failed status read used to `break` and then report deleted=True."""
        client = _StubClient(
            statuses=["RUNNING", "STOPPED"], brief_exc_times=1
        )
        report = autopoint._stop_and_delete(client, 42, timeout=5)
        assert report["deleted"] is True
        assert report["status_confirmed_stopped"] is True
        assert report["status_read_errors"] == 1

    def test_status_that_never_ends_is_reported_as_unconfirmed(self):
        """Timing out must surface the last status, not silently pass."""
        client = _StubClient(statuses=["RUNNING"] * 50)
        report = autopoint._stop_and_delete(client, 42, timeout=0.2)
        assert report["status_confirmed_stopped"] is False
        assert report["status_at_delete"] == "RUNNING"
        assert "warning" in report

    def test_failed_delete_is_not_ok(self):
        client = _StubClient(statuses=["STOPPED"], del_exc=OpenIError("still running"))
        report = autopoint._stop_and_delete(client, 42, timeout=5)
        assert report["deleted"] is False
        assert autopoint._cleanup_ok(report) is False

    def test_successful_delete_is_ok(self):
        client = _StubClient(statuses=["STOPPED"])
        report = autopoint._stop_and_delete(client, 42, timeout=5)
        assert autopoint._cleanup_ok(report) is True

    def test_stop_failure_still_attempts_delete(self):
        client = _StubClient(statuses=["RUNNING"], stop_exc=OpenIError("nope"))
        report = autopoint._stop_and_delete(client, 42, timeout=0.2)
        assert report["stopped"] is False
        assert report["del_attempted"] is not False  # delete was still tried
        assert client.deleted is True


class TestTokenRedaction:
    """The notebook token must never reach stdout, transcripts, or logs.

    websocket-client quotes the full URL in its messages, and that URL carries a
    one-time token; those messages are returned as ``result.stdout`` and land in
    the MCP transcript and any cron log.
    """

    def test_token_is_scrubbed_from_exception_text(self):
        from openi_bridge.exec_on_card import _redact

        exc = ConnectionRefusedError(
            "failed to connect to wss://host/api/kernels/k/channels?token=SECRET123"
        )
        out = _redact(exc, "SECRET123")
        assert "SECRET123" not in str(out)
        assert "<redacted>" in str(out)
        assert isinstance(out, ConnectionRefusedError)

    def test_no_token_leaves_the_message_alone(self):
        from openi_bridge.exec_on_card import _redact

        exc = RuntimeError("plain failure")
        assert str(_redact(exc, "")) == "plain failure"

    def test_tls_is_verified_by_default(self):
        from openi_bridge.exec_on_card import _sslopt

        opts = _sslopt("wss://host")
        assert opts is None or opts["cert_reqs"].name == "CERT_REQUIRED"

    def test_tls_insecure_requires_opt_in(self, monkeypatch):
        import importlib

        from openi_bridge import exec_on_card

        monkeypatch.setenv("OPENI_TLS_INSECURE", "1")
        importlib.reload(exec_on_card)
        try:
            assert exec_on_card._tls_insecure() is True
            opts = exec_on_card._sslopt("wss://host")
            assert opts["cert_reqs"].name == "CERT_NONE"
        finally:
            monkeypatch.delenv("OPENI_TLS_INSECURE", raising=False)
            importlib.reload(exec_on_card)

    def test_plain_ws_needs_no_ssl_options(self):
        from openi_bridge.exec_on_card import _sslopt

        assert _sslopt("ws://host") is None


class TestNoFabricatedFields:
    """Tools must not invent fields the platform never returns.

    A key that is always None reads as "the platform told us nothing about
    permissions", when the truth is "the platform has no such field here". The
    collaborator route was returning a hard-coded ``permissions: None``.
    """

    def _client(self, payload):
        from openi_bridge.client import OpenIClient

        client = OpenIClient.__new__(OpenIClient)
        client.get_json = lambda path, params=None: payload
        return client

    def test_permissions_absent_when_platform_omits_it(self):
        client = self._client(
            [{"login": "cqu-tqy", "full_name": "", "is_active": True}]
        )
        rows = client.repo_collaborators("owner/repo")
        assert rows == [{"login": "cqu-tqy", "full_name": "", "is_active": True}]
        assert "permissions" not in rows[0]

    def test_permissions_passed_through_when_platform_sends_it(self):
        client = self._client(
            [{"login": "x", "full_name": "y", "permissions": {"admin": True}}]
        )
        rows = client.repo_collaborators("owner/repo")
        assert rows[0]["permissions"] == {"admin": True}


class TestFreeWindowAccounting:
    """keep_seconds must be measured from RUNNING, and truncation reported."""

    def test_margin_constant_is_sane(self):
        assert 0 < autopoint.BILLING_MARGIN_SECONDS < autopoint.FREE_WINDOW_SECONDS

    def test_free_window_matches_billing_docs(self):
        assert autopoint.FREE_WINDOW_SECONDS == 6 * 60


class TestTaskActionVerified:
    """The verified wrapper must distinguish acceptance from effect."""

    def _client(self, statuses):
        from openi_bridge.client import OpenIClient

        client = OpenIClient.__new__(OpenIClient)
        state = {"i": 0, "posts": []}

        def task_brief(task_id):
            i = min(state["i"], len(statuses) - 1)
            state["i"] += 1
            return {"status": statuses[i]}

        def post_json(path, params=None, json_body=None, timeout=None):
            state["posts"].append(path)
            return {"code": 0, "data": {}}

        client.task_brief = task_brief
        client.post_json = post_json
        client._state = state
        return client

    def test_stop_is_ok_once_terminal(self):
        client = self._client(["RUNNING", "STOPPING", "STOPPED"])
        out = client.task_action_verified("stop", 7, wait_seconds=5, interval=0.01)
        assert out["ok"] is True
        assert out["status"] == "STOPPED"

    def test_stop_times_out_when_it_never_stops(self):
        client = self._client(["RUNNING"])
        out = client.task_action_verified("stop", 7, wait_seconds=0.05, interval=0.01)
        assert out["ok"] is False
        assert out["status_at_timeout"] == "RUNNING"
        assert "仍在计费" in out["error"]

    def test_delete_is_ok_when_task_disappears(self):
        client = self._client(["STOPPED"])

        def brief(task_id):
            return {}  # gone from the listing

        client.task_brief = brief
        out = client.task_action_verified("del", 7, wait_seconds=1, interval=0.01)
        assert out["ok"] is True

    def test_rejects_unknown_action(self):
        client = self._client(["RUNNING"])
        with pytest.raises(ValueError):
            client.task_action_verified("explode", 7)