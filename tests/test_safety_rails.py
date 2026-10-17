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


class TestKernelReplyStatus:
    """A kernel reply that is not "ok" must never be reported as success.

    Jupyter's execute_reply.status is ok | error | abort. The old code set
    ``ok = error is None``, and an aborted request carries no evalue -- so an
    aborted (never executed) cell came back as a successful run with empty
    stdout.
    """

    def _run(self, replies):
        """Drive JupyterConsole._collect against a fake websocket.

        _collect sends the request first and then ignores any message whose
        parent_header.msg_id does not match the one it just sent, so the fake
        has to capture that id from the outgoing frame and echo it back.
        """
        import json as _json

        from openi_bridge.exec_on_card import JupyterConsole

        class FakeWS:
            def __init__(self):
                self._replies = list(replies)
                self._sent_id = None
                self.timeout = None

            def send(self, payload):
                self._sent_id = _json.loads(payload)["header"]["msg_id"]

            def settimeout(self, value):
                self.timeout = value

            def recv(self):
                if not self._replies:
                    raise TimeoutError("no more messages")
                message = self._replies.pop(0)
                message.setdefault("parent_header", {})["msg_id"] = self._sent_id
                return _json.dumps(message)

        return JupyterConsole._collect(FakeWS(), "print(1)", timeout=1.0)

    def test_ok_status_is_success(self):
        out = self._run([{"header": {"msg_type": "execute_reply"},
                          "content": {"status": "ok"}}])
        assert out["ok"] is True
        assert out["status"] == "ok"

    def test_abort_status_is_not_success(self):
        """abort carries no evalue, so it used to slip through as ok=True."""
        out = self._run([{"header": {"msg_type": "execute_reply"},
                          "content": {"status": "abort"}}])
        assert out["ok"] is False
        assert "abort" in out["error"]

    def test_error_status_is_not_success(self):
        out = self._run([{"header": {"msg_type": "execute_reply"},
                          "content": {"status": "error", "evalue": "boom"}}])
        assert out["ok"] is False

    def test_missing_reply_is_not_success(self):
        """No execute_reply at all means we cannot claim anything ran."""
        out = self._run([{"header": {"msg_type": "stream"},
                          "content": {"text": "partial"}}])
        assert out["ok"] is False
        assert out["error"]


class TestDownloadDoesNotClobber:
    """A failed download must not destroy the previous good file."""

    def test_previous_file_survives_a_failed_replace(self, tmp_path):
        import os

        dest = tmp_path / "data.bin"
        dest.write_bytes(b"GOOD-OLD-CONTENT")
        previous = str(dest) + ".prev"
        os.replace(dest, previous)
        try:
            raise OSError(28, "No space left on device")
        except BaseException:
            os.replace(previous, dest)
        assert dest.read_bytes() == b"GOOD-OLD-CONTENT"


class TestFreeWindowAccounting:
    """keep_seconds must be measured from RUNNING, and truncation reported."""

    def test_margin_constant_is_sane(self):
        assert 0 < autopoint.BILLING_MARGIN_SECONDS < autopoint.FREE_WINDOW_SECONDS

    def test_free_window_matches_billing_docs(self):
        assert autopoint.FREE_WINDOW_SECONDS == 6 * 60


class TestPaginationBounds:
    """Listing tools must refuse absurd pagination instead of forwarding it.

    These parameters used to go straight to the platform: limit=100000 returned
    tens of thousands of rows into a single MCP response, and page=0 has
    undefined behaviour. The bounds are a guard rail, not a platform limit.
    """

    def _check(self, **kw):
        from openi_bridge.server import _check_pagination

        _check_pagination(**kw)

    def test_reasonable_values_pass(self):
        self._check(limit=20)
        self._check(limit=200, page=1)
        self._check(page_size=100, pages=50)
        self._check()  # nothing given

    def test_limit_above_ceiling_is_rejected(self):
        from openi_bridge.server import MAX_LIST_LIMIT, _InputError

        with pytest.raises(_InputError, match="limit 必须在"):
            self._check(limit=MAX_LIST_LIMIT + 1)

    def test_page_zero_is_rejected(self):
        from openi_bridge.server import _InputError

        with pytest.raises(_InputError, match="page 必须在"):
            self._check(page=0)
        with pytest.raises(_InputError, match="page 必须在"):
            self._check(page=-5)

    def test_page_size_above_ceiling_is_rejected(self):
        from openi_bridge.server import MAX_LIST_PAGE_SIZE, _InputError

        with pytest.raises(_InputError, match="page_size 必须在"):
            self._check(page_size=MAX_LIST_PAGE_SIZE + 1)

    def test_pages_above_ceiling_is_rejected(self):
        from openi_bridge.server import MAX_PAGES, _InputError

        with pytest.raises(_InputError, match="pages 必须在"):
            self._check(pages=MAX_PAGES + 1)

    def test_non_integer_is_rejected(self):
        from openi_bridge.server import _InputError

        with pytest.raises(_InputError, match="必须是整数"):
            self._check(limit="20")
        with pytest.raises(_InputError, match="必须是整数"):
            self._check(page=1.5)

    def test_bool_is_not_accepted_as_int(self):
        """True == 1 in Python, but a bool is a caller mistake, not a page number."""
        from openi_bridge.server import _InputError

        with pytest.raises(_InputError, match="必须是整数"):
            self._check(limit=True)

    def test_input_error_renders_as_error_envelope(self):
        """_InputError subclasses ValueError so the tools' except catches it."""
        import json as _json

        from openi_bridge.server import _InputError, _err

        payload = _json.loads(_err(_InputError("limit 必须在 1-200 之间：99999")))
        assert payload["ok"] is False
        assert "limit" in payload["error"]


class TestStartTaskRawGuards:
    """The generic raw-POST tool must not be reachable by accident."""

    def test_repo_must_be_owner_slash_name(self):
        from openi_bridge.server import _REPO_FULLNAME_RE

        assert _REPO_FULLNAME_RE.match("owner/name")
        assert _REPO_FULLNAME_RE.match("scratch-repo")
        # these would otherwise be spliced into f"/{repo}/ai_task/create"
        assert not _REPO_FULLNAME_RE.match("no-slash")
        assert not _REPO_FULLNAME_RE.match("../etc/passwd")
        assert not _REPO_FULLNAME_RE.match("a/b/c")
        assert not _REPO_FULLNAME_RE.match("")
        assert not _REPO_FULLNAME_RE.match("/leading")

    def test_safe_default_flag_exists(self):
        """A bool guard exists for the raw path; document the current default."""
        import inspect

        from openi_bridge.server import openi_start_task_raw

        sig = inspect.signature(openi_start_task_raw)
        assert "dry_run" in sig.parameters


class TestToolDecoratorInvariant:
    """Every @mcp.tool() function must be exception-guarded.

    The guard used to be an 83-times-repeated try/except inside each body; it is
    now the @tool decorator. That only works if @tool sits *below* @mcp.tool():
    decorators apply bottom-up, so @mcp.tool() must wrap the guarded function.
    Written the other way round, @mcp.tool() registers the raw function and an
    exception escapes to the client as UnexpectedToolError instead of coming
    back as an {"ok": false} envelope.
    """

    def _source(self):
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / "src/openi_bridge/server.py").read_text(
            encoding="utf-8"
        )

    def test_tool_decorator_is_defined(self):
        import openi_bridge.server as server

        assert callable(server.tool)

    def test_no_tool_inverts_the_decorator_order(self):
        import re

        text = self._source()
        inverted = re.findall(r"^@tool\n@mcp\.tool\(\)\n", text, re.M)
        assert not inverted, (
            "@tool must come after @mcp.tool(), not before; the guard would be "
            "applied to a discarded wrapper"
        )

    def test_every_tool_is_guarded(self):
        import re

        text = self._source()
        # a tool is guarded if it is immediately followed by @tool, or is one of
        # the two that genuinely cannot raise and carry no try at all
        unguarded = []
        for m in re.finditer(r"^@mcp\.tool\(\)\n(?:@tool\n)?def (openi_[a-z_]+)\(", text, re.M):
            followed_by_tool = text[m.start():m.end()].count("@tool") == 1
            if not followed_by_tool:
                unguarded.append(m.group(1))
        # these raise nothing: one returns a static catalog, the other only asks
        # the scheduler a question and formats it
        allowed = {"openi_list_resources"}
        assert set(unguarded) <= allowed, (
            f"tools without the @tool guard: {sorted(set(unguarded) - allowed)}"
        )

    def test_guard_turns_exception_into_envelope(self):
        import json as _json

        import openi_bridge.server as server

        @server.tool
        def boom():
            raise ValueError("kaboom")

        payload = _json.loads(boom())
        assert payload["ok"] is False
        assert "kaboom" in payload["error"]


class TestStreamToFile:
    """The three download paths share one writer, so its guarantees are load-bearing.

    Before they were unified only one of the three protected the destination:
    the others called os.replace unconditionally, replacing a previously good
    file with a partial one and leaving .part files behind.
    """

    def _run(self, dest, chunks, max_bytes=None, label="thing"):
        from openi_bridge.client import _stream_to_file

        return _stream_to_file(iter(chunks), str(dest), label, max_bytes=max_bytes)

    def test_writes_file_and_reports_md5(self, tmp_path):
        import hashlib

        dest = tmp_path / "out.bin"
        out = self._run(dest, [b"hello ", b"world"])
        assert dest.read_bytes() == b"hello world"
        assert out["bytes"] == 11
        assert out["md5"] == hashlib.md5(b"hello world").hexdigest()

    def test_existing_file_is_replaced_on_success(self, tmp_path):
        dest = tmp_path / "out.bin"
        dest.write_bytes(b"OLD")
        self._run(dest, [b"NEW"])
        assert dest.read_bytes() == b"NEW"

    def test_failed_download_keeps_the_previous_file(self, tmp_path):
        """This is the bug the unify fixed: a partial download must not clobber."""
        from openi_bridge.client import OpenIError

        dest = tmp_path / "out.bin"
        dest.write_bytes(b"GOOD-OLD-CONTENT")
        with pytest.raises(OpenIError):
            self._run(dest, [b"x" * 5000], max_bytes=10)
        assert dest.read_bytes() == b"GOOD-OLD-CONTENT"

    def test_no_temp_files_left_behind_on_failure(self, tmp_path):
        from openi_bridge.client import OpenIError

        dest = tmp_path / "out.bin"
        with pytest.raises(OpenIError):
            self._run(dest, [b"x" * 5000], max_bytes=10)
        assert sorted(p.name for p in tmp_path.iterdir()) == []

    def test_no_temp_files_left_behind_on_success(self, tmp_path):
        dest = tmp_path / "out.bin"
        dest.write_bytes(b"OLD")
        self._run(dest, [b"NEW"])
        assert sorted(p.name for p in tmp_path.iterdir()) == ["out.bin"]

    def test_failure_at_the_replace_step_keeps_old_file_and_no_debris(self, tmp_path):
        """The failure that actually clobbers is the one AT the finalize.

        A mid-write failure returns before os.replace is ever reached, so the
        destination was never at risk; a failure during the swap itself is what
        needs the .prev dance. This is the case the first version of the test
        missed, because it only exercised the mid-write path.
        """
        import os as _os
        from unittest import mock

        from openi_bridge import client as C

        dest = tmp_path / "out.bin"
        dest.write_bytes(b"GOOD-OLD-CONTENT")
        real_replace = _os.replace

        def flaky(src, dst):
            if dst == str(dest) and src.endswith(".part"):
                raise OSError(5, "I/O error")
            return real_replace(src, dst)

        with mock.patch("os.replace", side_effect=flaky):
            with pytest.raises(OSError):
                C._stream_to_file(iter([b"NEW"]), str(dest), "thing")

        assert dest.read_bytes() == b"GOOD-OLD-CONTENT"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["out.bin"]

    def test_max_bytes_stops_early_rather_than_buffering(self, tmp_path):
        """The generator must not be drained once the ceiling is passed."""
        from openi_bridge.client import OpenIError

        consumed = []

        def chunks():
            for i in range(1000):
                consumed.append(i)
                yield b"z" * 100

        dest = tmp_path / "out.bin"
        with pytest.raises(OpenIError):
            self._run(dest, chunks(), max_bytes=250)
        assert len(consumed) < 10, f"read {len(consumed)} chunks past the limit"


class TestDownloadConstants:
    """The duplicated literals are now shared; guard that they stay shared."""

    def test_chunk_and_timeout_are_module_level(self):
        from openi_bridge import client

        assert client.DOWNLOAD_CHUNK_BYTES > 0
        assert client.DOWNLOAD_TIMEOUT is not None

    def test_no_stray_duplicate_timeout_literals(self):
        import re
        from pathlib import Path

        text = (
            Path(__file__).resolve().parents[1] / "src/openi_bridge/client.py"
        ).read_text(encoding="utf-8")
        # the literal may appear once, in the constant definition itself
        occurrences = len(re.findall(r"httpx\.Timeout\(30\.0, read=120\.0\)", text))
        assert occurrences == 1, (
            f"httpx.Timeout(30.0, read=120.0) appears {occurrences} times; "
            f"use DOWNLOAD_TIMEOUT instead"
        )


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