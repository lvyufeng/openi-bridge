"""在云脑的卡上直接跑代码：建 DEBUG 任务 -> 用它的 Jupyter 内核执行 -> 清理。

平台没有给 SSH，也不接受批处理脚本，DEBUG 任务唯一稳定的交互入口是它自带的
JupyterLab：``GET /api/v1/ai_task/debug_url`` 返回一个带一次性 token 的地址，
拿这个地址既能走 Jupyter 的 REST（/api/kernels），也能连它的内核 WebSocket
（/api/kernels/{id}/channels）执行任意代码、把 stdout 收回来。

于是"用启智的卡跑一段代码"这件事就变成四步：

    1. 挑代码仓与规格（默认挑单价最低的，通常是 1 分/卡时的 GCU ENFLAME-T20）
    2. 建 DEBUG 任务并等它进入 RUNNING（排队与 PREPARING 不计费）
    3. 取 debug_url，等 Jupyter 就绪，建内核、发 execute_request、收输出
    4. 无论成功失败都在 finally 里停掉并删除任务

计费的只有 RUNNING 的那段时间，而不足 6 分钟不计费，所以一次"跑个 print / 跑个
厂商 smi"通常净消耗 0 分；要长时间跑就得自己算清账，或者用 --keep 把任务留着、
拿返回的 Jupyter 地址手动接着用。

用法：

    python -m openi_bridge.exec_on_card --code "print(1+1)"
    python -m openi_bridge.exec_on_card --script probe.py --source GCU
    python -m openi_bridge.exec_on_card --code "!npu-smi info" --spec-id 184
    python -m openi_bridge.exec_on_card --code "import torch; print(torch.cuda.is_available())" --keep

相关环境变量：

    OPENI_TOKEN                 令牌，见 README
    OPENI_EXEC_REPO             任务使用的代码仓 owner/name，默认取账号下第一个非空仓库
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from .client import COMPUTE_SOURCES as ALL_SOURCES
from .client import DEFAULT_CLUSTER, OpenIClient, dump
from . import autopoint

# 自动化建的执行任务统一用这个前缀，避免和每日领分的 autopoint-* 混淆。
# 定义放在 autopoint 里，回收逻辑（reap_leftovers）要认得它。
EXEC_TASK_PREFIX = autopoint.EXEC_TASK_PREFIX

READY_POLL_SECONDS = 8.0
DEBUG_URL_POLL_SECONDS = 5.0


def split_debug_url(url: str) -> Tuple[str, str]:
    """把 debug_url 拆成 (Jupyter 根地址, token)。

    平台返回的形式通常是 ``https://host/path/lab?token=xxxx``（或 /tree），
    Jupyter 的 API 要挂在根上，所以要去掉 /lab、/tree、/notebooks 这些后缀。
    """
    if not url:
        return "", ""
    base, _, query = url.strip().partition("?")
    token = ""
    for part in query.split("&"):
        if part.startswith("token="):
            token = part[len("token="):]
            break
    for suffix in ("/lab", "/tree", "/notebooks"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base.rstrip("/"), token


def _extract_url(data: Any) -> str:
    """debug_url 接口的返回在不同版本里有几种包法，逐个兼容。"""
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in ("url", "Url", "URL"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
        for key in ("data", "Data"):
            value = data.get(key)
            if isinstance(value, dict):
                inner = _extract_url(value)
                if inner:
                    return inner
    return ""


class JupyterConsole:
    """一个正在运行的云脑任务里那台 JupyterLab 的瘦客户端。"""

    def __init__(
        self,
        base_url: str,
        token: str = "",
        timeout: float = 60.0,
        verify: bool = False,
    ) -> None:
        self.base = base_url.rstrip("/")
        self.token = token
        params = {"token": token} if token else None
        self.client = httpx.Client(
            base_url=self.base,
            params=params,
            verify=verify,
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": "openi-bridge/0.1 (+exec)"},
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "JupyterConsole":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _xsrf(self) -> str:
        value = self.client.cookies.get("_xsrf")
        if value:
            return value
        # 有些部署只在访问页面时才下发 _xsrf cookie，先敲一下首页。
        try:
            self.client.get("/lab")
        except Exception:  # noqa: BLE001
            pass
        return self.client.cookies.get("_xsrf") or ""

    def request(
        self,
        method: str,
        path: str,
        json_body: Optional[Any] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        headers: Dict[str, str] = {}
        if method.upper() != "GET":
            token = self._xsrf()
            if token:
                headers["X-XSRFToken"] = token
        response = self.client.request(
            method, path, json=json_body, headers=headers, timeout=timeout
        )
        response.raise_for_status()
        text = response.text
        if not text.strip():
            return None
        try:
            return response.json()
        except ValueError:
            return text

    def wait_ready(
        self, timeout: float = 300.0, interval: float = READY_POLL_SECONDS
    ) -> Dict[str, Any]:
        """等 Jupyter 起来：任务 RUNNING 之后服务还要几秒到几十秒才通。"""
        started = time.monotonic()
        deadline = started + timeout
        last_error: Optional[str] = None
        while True:
            try:
                self.request("GET", "/api/status", timeout=20)
                return {"ready": True, "seconds": round(time.monotonic() - started, 1)}
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
            if time.monotonic() >= deadline:
                return {
                    "ready": False,
                    "seconds": round(time.monotonic() - started, 1),
                    "error": last_error,
                }
            time.sleep(interval)

    def kernel_name(self) -> str:
        try:
            specs = self.request("GET", "/api/kernelspecs", timeout=30)
        except Exception:  # noqa: BLE001
            return "python3"
        if isinstance(specs, dict):
            default = specs.get("default") or "python3"
            available = specs.get("kernelspecs") or {}
            if default in available:
                return default
            if available:
                return next(iter(available))
        return "python3"

    def execute(self, code: str, timeout: float = 300.0) -> Dict[str, Any]:
        """建内核跑一段代码，收 stdout / 结果 / 异常，然后删掉内核。"""
        try:
            import websocket  # websocket-client
        except ImportError as exc:  # pragma: no cover - 依赖缺失时给明确提示
            raise RuntimeError(
                "缺少 websocket-client，请先安装：pip install websocket-client"
            ) from exc

        name = self.kernel_name()
        kernel = self.request("POST", "/api/kernels", json_body={"name": name}, timeout=60)
        kernel_id = kernel.get("id") if isinstance(kernel, dict) else None
        if not kernel_id:
            raise RuntimeError(f"创建内核失败：{kernel!r}")
        try:
            ws_base = self.base.replace("https://", "wss://").replace("http://", "ws://")
            ws_url = f"{ws_base}/api/kernels/{kernel_id}/channels"
            if self.token:
                ws_url += f"?token={self.token}"
            ws = websocket.create_connection(
                ws_url, timeout=timeout, sslopt={"cert_reqs": ssl.CERT_NONE}
            )
            try:
                return self._collect(ws, code, timeout)
            finally:
                ws.close()
        finally:
            try:
                self.request("DELETE", f"/api/kernels/{kernel_id}", timeout=30)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _collect(ws: Any, code: str, timeout: float) -> Dict[str, Any]:
        message_id = uuid.uuid4().hex
        ws.send(
            json.dumps(
                {
                    "header": {
                        "msg_id": message_id,
                        "username": "openi-bridge",
                        "session": uuid.uuid4().hex,
                        "msg_type": "execute_request",
                        "version": "5.3",
                    },
                    "parent_header": {},
                    "metadata": {},
                    "buffers": [],
                    "channel": "shell",
                    "content": {
                        "code": code,
                        "silent": False,
                        "store_history": False,
                        "user_expressions": {},
                        "allow_stdin": False,
                        "stop_on_error": True,
                    },
                }
            )
        )
        chunks: List[str] = []
        error: Optional[str] = None
        reply_status: Optional[str] = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = json.loads(ws.recv())
            except Exception as exc:  # noqa: BLE001
                chunks.append(f"[ws error] {type(exc).__name__}: {exc}\n")
                break
            if message.get("parent_header", {}).get("msg_id") != message_id:
                continue
            msg_type = message.get("header", {}).get("msg_type")
            content = message.get("content") or {}
            if msg_type == "stream":
                chunks.append(content.get("text") or "")
            elif msg_type in ("execute_result", "display_data"):
                data = content.get("data") or {}
                if data.get("text/plain"):
                    chunks.append(str(data["text/plain"]) + "\n")
            elif msg_type == "error":
                error = f"{content.get('ename')}: {content.get('evalue')}"
                chunks.append(f"ERROR {error}\n")
            elif msg_type == "execute_reply":
                reply_status = content.get("status")
                if content.get("status") == "error" and not error:
                    error = content.get("evalue") or "execution failed"
                break
        return {
            "ok": error is None,
            "status": reply_status,
            "stdout": "".join(chunks),
            "error": error,
        }


def _resolve_spec(
    client: OpenIClient,
    job_type: str,
    compute_source: Optional[str],
    spec_id: Optional[int],
) -> Dict[str, Any]:
    """把 --spec-id/--source 翻译成一笔可用的规格；都没给就挑最便宜的。"""
    if spec_id is None:
        return autopoint._cheapest_spec(client, job_type, compute_source)
    sources = [compute_source] if compute_source else list(ALL_SOURCES)
    for source in sources:
        try:
            info = client.creation_info(job_type, source)
        except Exception:  # noqa: BLE001
            continue
        specs = (info.get("specs") or {}).get("all") or []
        for spec in specs:
            if int(spec.get("id") or 0) != int(spec_id):
                continue
            if not info.get("can_create_more", True):
                return {
                    "blocked": True,
                    "reason": "平台报告不能再创建任务（同时只能有 1 个 running/waiting 任务）",
                    "info": info,
                }
            return {
                "spec_id": spec.get("id"),
                "compute_source": spec.get("compute_resource") or source,
                "card_type": spec.get("acc_card_type"),
                "price_per_card_hour": spec.get("unit_price"),
                "cluster": DEFAULT_CLUSTER,
                "job_type": job_type,
            }
    return {"blocked": True, "reason": f"没找到 spec_id={spec_id}（{job_type}）"}


def _wait_debug_url(
    client: OpenIClient,
    task_id: Any,
    timeout: float = 120.0,
    interval: float = DEBUG_URL_POLL_SECONDS,
) -> str:
    """RUNNING 之后 debug_url 可能还要等一会儿才出来。"""
    deadline = time.monotonic() + timeout
    while True:
        try:
            url = _extract_url(client.debug_url(task_id))
            if url:
                return url
        except Exception:  # noqa: BLE001
            pass
        if time.monotonic() >= deadline:
            return ""
        time.sleep(interval)


def run_on_card(
    client: OpenIClient,
    code: str,
    *,
    repo: Optional[str] = None,
    spec_id: Optional[int] = None,
    compute_source: Optional[str] = None,
    job_type: str = "DEBUG",
    wait_seconds: float = 900.0,
    ready_timeout: float = 300.0,
    exec_timeout: float = 300.0,
    keep: bool = False,
    dry_run: bool = False,
    task_prefix: str = EXEC_TASK_PREFIX,
) -> Dict[str, Any]:
    """建任务、在卡上跑 code、清理，返回一个可序列化的报告。"""
    compute_source = compute_source or os.getenv("OPENI_EXEC_SOURCE") or None
    report: Dict[str, Any] = {"ok": False, "changed": False}

    repo = repo or os.getenv("OPENI_EXEC_REPO") or autopoint._pick_repo(client, None)
    if not repo:
        report["error"] = "找不到可用的代码仓来建任务（用 --repo 指定 owner/name）"
        return report
    report["repo"] = repo

    spec = _resolve_spec(client, job_type, compute_source, spec_id)
    if spec.get("blocked"):
        report["error"] = spec.get("reason")
        report["spec_blocked"] = spec
        return report
    report["spec"] = spec

    if dry_run:
        report.update({"ok": True, "action": "dry_run", "planned": {"repo": repo, **spec}})
        return report

    task_id: Optional[Any] = None
    debug_url = ""
    try:
        launch = autopoint.launch_and_wait(
            client, repo, spec, wait_seconds, name_prefix=task_prefix, job_type=job_type
        )
        task_id = launch.get("task_id")
        report["task"] = launch
        if not task_id:
            report["error"] = "创建任务后没有拿到任务 ID"
            return report
        if not launch.get("reached_running"):
            report["error"] = (
                f"任务在 {wait_seconds:.0f} 秒内没进入 RUNNING（当前状态 {launch.get('status')}）"
            )
            return report

        debug_url = _wait_debug_url(client, task_id)
        report["debug_url"] = debug_url
        if not debug_url:
            report["error"] = "任务已 RUNNING 但拿不到 debug_url"
            return report

        base, token = split_debug_url(debug_url)
        report["jupyter_host"] = base
        with JupyterConsole(base, token) as console:
            ready = console.wait_ready(ready_timeout)
            report["console"] = ready
            if not ready.get("ready"):
                report["error"] = f"Jupyter 没在 {ready_timeout:.0f} 秒内就绪：{ready.get('error')}"
                return report
            result = console.execute(code, exec_timeout)
        report["result"] = result
        report["ok"] = bool(result.get("ok"))
        report["changed"] = True
        return report
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {exc}"
        return report
    finally:
        if task_id and keep:
            report["kept_task_id"] = task_id
            if debug_url:
                report["action"] = "kept"
                report["note"] = (
                    "任务保留在运行中，拿 debug_url 打开 JupyterLab 继续用；"
                    "用完记得停掉并删除，否则按分钟计费。"
                )
        elif task_id:
            report["cleanup"] = autopoint._stop_and_delete(client, task_id)


def _read_code(args: argparse.Namespace) -> str:
    if args.script:
        with open(args.script, "r", encoding="utf-8") as handle:
            return handle.read()
    if args.code is not None:
        return args.code
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m openi_bridge.exec_on_card",
        description="在启智的云脑卡上跑一段代码：建 DEBUG 任务 -> Jupyter 内核执行 -> 清理。",
    )
    parser.add_argument("--code", default=None, help="要执行的代码（内联）")
    parser.add_argument("--script", default=None, help="要执行的代码文件路径")
    parser.add_argument("--repo", default=None, help="代码仓 owner/name")
    parser.add_argument("--source", default=None, help="算力类型，如 GCU/MLU/NPU/GPU")
    parser.add_argument("--spec-id", type=int, default=None, help="指定 spec_id（优先于 --source）")
    parser.add_argument("--job-type", default="DEBUG", help="任务类型，默认 DEBUG")
    parser.add_argument("--wait", type=float, default=900.0, help="等 RUNNING 的最长秒数")
    parser.add_argument("--ready-timeout", type=float, default=300.0, help="等 Jupyter 就绪的秒数")
    parser.add_argument("--exec-timeout", type=float, default=300.0, help="单段代码的执行上限秒数")
    parser.add_argument("--keep", action="store_true", help="跑完保留任务，打印 Jupyter 入口")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不建任务")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出完整报告")
    args = parser.parse_args(argv)

    code = _read_code(args)
    if not code.strip() and not args.dry_run:
        parser.error("没有要执行的代码：传 --code、--script，或用管道喂 stdin")

    with OpenIClient() as client:
        report = run_on_card(
            client,
            code,
            repo=args.repo,
            spec_id=args.spec_id,
            compute_source=args.source,
            job_type=args.job_type,
            wait_seconds=args.wait,
            ready_timeout=args.ready_timeout,
            exec_timeout=args.exec_timeout,
            keep=args.keep,
            dry_run=args.dry_run,
        )

    if args.json:
        print(dump(report))
    else:
        _print_human(report)
    return 0 if report.get("ok") else 1


def _print_human(report: Dict[str, Any]) -> None:
    spec = report.get("spec") or {}
    print(f"代码仓      {report.get('repo')}")
    if spec:
        print(f"规格        #{spec.get('spec_id')} {spec.get('card_type')} "
              f"{spec.get('price_per_card_hour')} 分/卡时（{spec.get('compute_source')}）")
    task = report.get("task") or {}
    if task.get("task_id"):
        print(f"任务        #{task['task_id']} 进入 RUNNING={task.get('reached_running')} "
              f"用时 {task.get('seconds')}s")
    console = report.get("console")
    if console:
        print(f"Jupyter     {'就绪' if console.get('ready') else '未就绪'} 用时 {console.get('seconds')}s")
    result = report.get("result")
    if result is not None:
        print("---- 输出 ----")
        sys.stdout.write(result.get("stdout") or "")
        if not (result.get("stdout") or "").endswith("\n"):
            print()
        print("---- 结束 ----")
        if result.get("error"):
            print(f"异常        {result['error']}")
    if report.get("cleanup"):
        print(f"清理        {report['cleanup']}")
    if report.get("kept_task_id"):
        print(f"任务保留    #{report['kept_task_id']}")
        print(f"Jupyter     {report.get('debug_url')}")
    if report.get("error"):
        print(f"结论        失败：{report['error']}")
    else:
        print(f"结论        {'成功' if report.get('ok') else report.get('action') or '完成'}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

