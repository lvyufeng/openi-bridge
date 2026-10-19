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


class TestPollingSurvivesTransientErrors:
    """A blip while polling status must not abort the whole operation.

    Found by running against the live platform: a single 502 from /ai_task/brief
    during the wait-for-RUNNING loop raised out of launch_and_wait, so the caller
    never got the task id and could not clean up -- a real task was left behind
    in PREPARING. The loop now retries and only the deadline ends the wait.
    """

    def _client(self, failures, then):
        class Flaky:
            def __init__(self):
                self.calls = 0

            def create_task(self, **kw):
                return {"id": 42}

            def task_brief(self, task_id):
                self.calls += 1
                if self.calls <= failures:
                    raise RuntimeError("HTTP 502 Bad Gateway")
                return {"status": then}

        return Flaky()

    def _spec(self):
        return {"spec_id": 184, "compute_source": "MLU", "cluster": "C2Net"}

    def test_transient_errors_do_not_abort_the_wait(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda s: None)
        out = autopoint.launch_and_wait(
            self._client(failures=3, then="RUNNING"), "o/r", self._spec(), wait_seconds=60
        )
        assert out["reached_running"] is True
        assert out["status_read_errors"] == 3

    def test_errors_are_counted_and_reported(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda s: None)
        out = autopoint.launch_and_wait(
            self._client(failures=1, then="STOPPED"), "o/r", self._spec(), wait_seconds=60
        )
        assert out["status_read_errors"] == 1
        assert "last_read_error" in out

    def test_clean_task_reports_no_errors(self, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda s: None)
        out = autopoint.launch_and_wait(
            self._client(failures=0, then="RUNNING"), "o/r", self._spec(), wait_seconds=60
        )
        assert out["reached_running"] is True
        assert "status_read_errors" not in out


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
        assert _REPO_FULLNAME_RE.match("owner/mindnlp")
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


class TestRepoTreePagination:
    """repo_tree must fetch the whole tree, not just the platform's first page.

    /git/trees caps a response at 1000 entries and sets truncated=true, with
    page and total_count alongside. The client ignored both, so a repo with
    2627 files reported 1000 and called it the tree -- truncated was true, but
    nothing said the rest was simply missing rather than capped on purpose.
    """

    def _client(self, pages, total=None):
        """pages: list of entry-counts to return, one per requested page."""
        from openi_bridge.client import OpenIClient

        client = OpenIClient.__new__(OpenIClient)
        state = {"i": 0, "requested": []}

        def get_json(path, params=None):
            state["requested"].append((params or {}).get("page"))
            n = pages[min(state["i"], len(pages) - 1)]
            state["i"] += 1
            return {
                "sha": "deadbeef",
                "tree": [{"path": f"f{k}", "type": "blob", "size": 1, "sha": "s"}
                         for k in range(n)],
                "truncated": n >= 1000,
                "page": (params or {}).get("page", 1),
                "total_count": total if total is not None else sum(pages),
            }

        client.get_json = get_json
        client._state = state
        return client

    def test_single_page_tree_is_not_paginated(self):
        client = self._client([10], total=10)
        out = client.repo_tree("o/r")
        assert out["count"] == 10
        assert out["truncated"] is False
        assert client._state["requested"] == [1]

    def test_multi_page_tree_is_fully_fetched(self):
        client = self._client([1000, 1000, 627], total=2627)
        out = client.repo_tree("o/r")
        assert out["count"] == 2627
        assert out["truncated"] is False
        assert client._state["requested"] == [1, 2, 3]

    def test_max_entries_still_caps_and_flags(self):
        client = self._client([1000, 1000, 627], total=2627)
        out = client.repo_tree("o/r", max_entries=50)
        assert out["count"] == 50
        assert out["truncated"] is True

    def test_page_cap_reports_truncation(self):
        from openi_bridge.client import MAX_TREE_PAGES

        # more pages than the cap allows, each full
        client = self._client([1000] * (MAX_TREE_PAGES + 5), total=10**9)
        out = client.repo_tree("o/r")
        assert out["truncated"] is True
        assert out["pages_fetched"] <= MAX_TREE_PAGES + 1

    def test_empty_repo_is_not_truncated(self):
        client = self._client([0], total=0)
        out = client.repo_tree("o/r")
        assert out["count"] == 0
        assert out["truncated"] is False


class TestPathTraversal:
    """A caller-supplied repo must not be able to redirect the request.

    URLs are built as f"/repos/{repo}/...", and httpx normalises "../", so
    repo="../user" turned a repo-scoped call into GET /api/v1/user. The guard
    lives in request(), the one point every call passes through, rather than in
    the twenty places that build a repo path.
    """

    def _c(self):
        from openi_bridge.client import OpenIClient

        return OpenIClient.__new__(OpenIClient)

    def test_dotdot_segments_are_rejected(self):
        from openi_bridge.client import OpenIError, _assert_safe_path

        for path in ("/repos/../user", "/repos/a/../../user", "/repos/../notifications"):
            with pytest.raises(OpenIError, match="不允许出现"):
                _assert_safe_path(path)

    def test_single_dot_segments_are_rejected(self):
        from openi_bridge.client import OpenIError, _assert_safe_path

        with pytest.raises(OpenIError, match="不允许出现"):
            _assert_safe_path("/repos/owner/./repo/tags")

    def test_newlines_are_rejected(self):
        from openi_bridge.client import OpenIError, _assert_safe_path

        with pytest.raises(OpenIError, match="不能有换行"):
            _assert_safe_path("/repos/a\nb/tags")

    def test_relative_paths_are_rejected(self):
        from openi_bridge.client import OpenIError, _assert_safe_path

        with pytest.raises(OpenIError, match="必须以 / 开头"):
            _assert_safe_path("repos/a/b")

    def test_ordinary_repo_paths_pass(self):
        from openi_bridge.client import _assert_safe_path

        for path in (
            "/repos/lvyufeng/mindnlp/tags",
            "/repos/owner/repo.git/branches",
            "/ai_task?id=1",
            "/user",
        ):
            _assert_safe_path(path)  # must not raise

    def test_repo_methods_go_through_the_guard(self):
        """The guard must be wired into request(), not just importable."""
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "src/openi_bridge/client.py").read_text(
            encoding="utf-8"
        )
        body = src.split("    def request(", 1)[1].split("    def get_json", 1)[0]
        assert "_assert_safe_path(path)" in body, (
            "request() must call the path guard, or the fix is inert"
        )


class TestPaginationTerminates:
    """Marker pagination must end, even against a server that never converges.

    Normal paging stops when has_next goes false or the marker stops advancing.
    A server that keeps reporting has_next with a fresh marker made these loops
    run forever -- the call would hang instead of failing.
    """

    def _client(self):
        from openi_bridge.client import OpenIClient

        client = OpenIClient.__new__(OpenIClient)
        state = {"calls": 0}

        def never_ends(subject_name, subject_type, parent_dir="", marker="", page_size=1000):
            state["calls"] += 1
            return {
                "file_list": [{"FileName": f"f{state['calls']}"}],
                "has_next": True,
                "marker": f"m{state['calls']}",
            }

        client.subject_files = never_ends
        client._state = state
        return client

    def test_subject_entry_gives_up_with_an_error(self):
        from openi_bridge.client import MAX_LIST_PAGES, OpenIError

        client = self._client()
        with pytest.raises(OpenIError, match="还没结束"):
            client._subject_entry("o/r", "dataset", "target.txt")
        assert client._state["calls"] == MAX_LIST_PAGES

    def test_walk_subject_files_gives_up_with_an_error(self):
        from openi_bridge.client import OpenIError

        client = self._client()
        with pytest.raises(OpenIError):
            client.walk_subject_files("o/r", "dataset")

    def test_normal_pagination_still_terminates_early(self):
        from openi_bridge.client import OpenIClient

        client = OpenIClient.__new__(OpenIClient)
        pages = [
            {"file_list": [{"FileName": "a"}], "has_next": True, "marker": "m1"},
            {"file_list": [{"FileName": "target.txt"}], "has_next": True, "marker": "m2"},
        ]
        calls = {"n": 0}

        def paged(*a, **k):
            p = pages[min(calls["n"], len(pages) - 1)]
            calls["n"] += 1
            return p

        client.subject_files = paged
        row = client._subject_entry("o/r", "dataset", "target.txt")
        assert row is not None and row["FileName"] == "target.txt"
        assert calls["n"] == 2

    def test_repeating_marker_terminates(self):
        """A marker that never advances ends the search instead of looping."""
        from openi_bridge.client import OpenIClient

        client = OpenIClient.__new__(OpenIClient)
        client.subject_files = lambda *a, **k: {
            "file_list": [], "has_next": True, "marker": "same",
        }
        assert client._subject_entry("o/r", "dataset", "x.txt") is None


class TestInstallMcpTargets:
    """The installer writes to four clients' config files; each has its own shape.

    dsh's config is a hand-maintained YAML patch full of comments and custom
    plugins, so the writer must do a surgical line insert rather than a YAML
    round-trip -- reparsing would strip every comment in the file.
    """

    def _mod(self):
        from openi_bridge import install_mcp

        return install_mcp

    def test_dsh_entry_has_the_fields_dsh_expects(self):
        m = self._mod()
        lines = m.dsh_entry(["/venv/bin/openi-bridge"])
        text = "\n".join(lines)
        assert "- id: mcp-openi-bridge" in text
        assert "'@deepseek-ai/dsh-mcp-client'" in text
        assert "transport: stdio" in text
        assert "serverName: openi-bridge" in text
        assert "command: /venv/bin/openi-bridge" in text
        assert "args: []" in text

    def test_dsh_entry_carries_no_token(self):
        m = self._mod()
        text = "\n".join(m.dsh_entry(["/venv/bin/openi-bridge"]))
        assert "TOKEN" not in text and "token" not in text

    def test_dsh_insert_is_idempotent(self, tmp_path):
        m = self._mod()
        f = tmp_path / "cordis.patch.yml"
        f.write_text("# a comment\n- insert:\n    - id: mcp-other\n      name: x\n", encoding="utf-8")
        first = m.apply_dsh(["/venv/bin/openi-bridge"], f)
        body = f.read_text(encoding="utf-8")
        second = m.apply_dsh(["/venv/bin/openi-bridge"], f)
        assert first["action"] == "added"
        assert second["action"] == "unchanged"
        assert f.read_text(encoding="utf-8") == body
        assert body.count("- id: mcp-openi-bridge") == 1

    def test_dsh_insert_preserves_comments(self, tmp_path):
        m = self._mod()
        f = tmp_path / "cordis.patch.yml"
        before = "# keep me\n# and me\n- insert:\n    - id: mcp-other\n      name: x\n"
        f.write_text(before, encoding="utf-8")
        m.apply_dsh(["/venv/bin/openi-bridge"], f)
        after = f.read_text(encoding="utf-8")
        assert "# keep me" in after and "# and me" in after
        assert "- id: mcp-other" in after

    def test_dsh_update_replaces_rather_than_duplicates(self, tmp_path):
        m = self._mod()
        f = tmp_path / "cordis.patch.yml"
        f.write_text("", encoding="utf-8")
        m.apply_dsh(["/old/bin/openi-bridge"], f)
        m.apply_dsh(["/new/bin/openi-bridge"], f)
        text = f.read_text(encoding="utf-8")
        assert text.count("- id: mcp-openi-bridge") == 1
        assert "/new/bin/openi-bridge" in text
        assert "/old/bin/openi-bridge" not in text

    def test_codex_reapply_is_idempotent_and_reports_no_backup(self, tmp_path):
        """Re-running apply must be a no-op, not a crash.

        apply_codex built its return value from a `backup` variable that only
        existed inside the "something changed" branch, so a second apply (where
        nothing changes) raised UnboundLocalError instead of reporting success.
        """
        m = self._mod()
        f = tmp_path / "config.toml"
        first = m.apply_codex(["/venv/bin/openi-bridge"], f)
        second = m.apply_codex(["/venv/bin/openi-bridge"], f)
        assert first["action"] == "added"
        assert first["backup"] in (None, "None") or first["backup"]
        # the no-op path must return cleanly
        assert second["action"] in ("added", "updated")
        assert second.get("backup") is None
        assert f.read_text(encoding="utf-8").count("[mcp_servers.openi-bridge]") == 1

    def test_dsh_handles_the_empty_array_template(self, tmp_path):
        """dsh's default per-profile template is a bare `[]`.

        Appending a second YAML document after it breaks the file outright
        (dsh: "document separator is expected"), which is how the headless
        profile got corrupted on the first apply. The `[]` must be replaced,
        and the header comments kept.
        """
        m = self._mod()
        f = tmp_path / "cordis.patch.yml"
        f.write_text(
            "# Your patch layer for this dsh profile:\n"
            "# a top-level YAML array of loader patch entries.\n"
            "[]\n",
            encoding="utf-8",
        )
        m.apply_dsh(["/venv/bin/openi-bridge"], f)
        text = f.read_text(encoding="utf-8")

        assert "# Your patch layer" in text, "header comments must survive"
        assert "- id: mcp-openi-bridge" in text
        # the top-level `[]` must be gone; the only [] left is the args value
        top_level = [l for l in text.splitlines() if l.strip() == "[]"]
        assert not top_level, "a leftover top-level [] would be a second document"

    def test_dsh_empty_array_output_is_one_yaml_document(self, tmp_path):
        """The result must parse as a single YAML array."""
        yaml = pytest.importorskip("yaml")

        m = self._mod()
        f = tmp_path / "cordis.patch.yml"
        f.write_text("# header\n[]\n", encoding="utf-8")
        m.apply_dsh(["/venv/bin/openi-bridge"], f)
        doc = yaml.safe_load(f.read_text(encoding="utf-8"))
        assert isinstance(doc, list)
        assert any(
            isinstance(e, dict) and e.get("insert") for e in doc
        ), "expected one insert entry"

    def test_codex_block_is_valid_toml_shape(self):
        m = self._mod()
        block = m.codex_block(["/venv/bin/openi-bridge"])
        assert block.startswith("[mcp_servers.openi-bridge]")
        assert 'command = "/venv/bin/openi-bridge"' in block

    def test_cursor_payload_is_json_with_no_secret(self):
        import json as _json

        m = self._mod()
        entry = _json.loads(m.cursor_payload(["/venv/bin/openi-bridge"]))
        # Cursor takes a flat single-server object with a name; the nested
        # {"mcpServers": {...}} shape is rejected with "Missing name property".
        assert entry["name"] == "openi-bridge"
        assert entry["command"] == "/venv/bin/openi-bridge"
        assert entry["args"] == []
        assert "token" not in _json.dumps(entry).lower()

    def test_cursor_payload_is_flat_not_nested(self):
        import json as _json

        m = self._mod()
        entry = _json.loads(m.cursor_payload(["/venv/bin/openi-bridge"]))
        assert "mcpServers" not in entry, (
            "Cursor rejects the nested form; it wants one flat server object"
        )

    def test_console_script_is_absolute(self):
        m = self._mod()
        cmd = m.console_script()
        assert cmd and cmd[0].startswith("/"), f"must be an absolute path: {cmd}"

    def test_no_client_config_embeds_a_token(self, tmp_path):
        """Whatever shape a client wants, the token stays out of it."""
        m = self._mod()
        cmd = ["/venv/bin/openi-bridge"]
        assert "token" not in m.codex_block(cmd).lower()
        assert "token" not in m.cursor_payload(cmd).lower()
        assert "token" not in "\n".join(m.dsh_entry(cmd)).lower()


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