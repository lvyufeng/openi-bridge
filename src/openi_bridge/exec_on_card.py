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

# 平台 notebook 入口用的是它自己的证书链，Python 的默认信任库不一定认。默认仍然
# 校验证书；确实握手失败时用 OPENI_TLS_INSECURE=1 显式放行，这样"不校验"是一个
# 看得见的决定，而不是一个没人注意的默认值（token 和代码都要经过这条连接）。
TLS_INSECURE_ENV = "OPENI_TLS_INSECURE"


def _tls_insecure() -> bool:
    return os.getenv(TLS_INSECURE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _sslopt(base: str) -> Optional[Dict[str, Any]]:
    """wss 连接的 TLS 选项；ws:// 不需要。"""
    if not base.startswith("wss://"):
        return None
    import ssl as _ssl

    if _tls_insecure():
        return {"cert_reqs": _ssl.CERT_NONE, "check_hostname": False}
    return {"cert_reqs": _ssl.CERT_REQUIRED}


def _redact(exc: BaseException, secret: str) -> BaseException:
    """把异常文本里的 token 抹掉，保留异常类型。

    websocket-client 的报错经常带完整 URL，而 URL 上有一次性 token；这些文本会
    进 stdout / MCP transcript / 日志，所以必须在离开本模块之前处理掉。
    """
    if not secret:
        return exc
    text = str(exc).replace(secret, "<redacted>")
    try:
        return type(exc)(text)
    except Exception:  # noqa: BLE001 - 构造不出来的异常类型就退回通用异常
        return RuntimeError(text)


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
        verify: Optional[bool] = None,
    ) -> None:
        self.base = base_url.rstrip("/")
        self.token = token
        params = {"token": token} if token else None
        # 默认校验证书；只有显式设了 OPENI_TLS_INSECURE=1 才放行自签证书。
        if verify is None:
            verify = not _tls_insecure()
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
            headers: List[str] = []
            if self.token:
                # token 走 header 而不是查询串：查询串会被 websocket-client 写进
                # 异常文本，而那些文本会进入 result.stdout / report["error"]，
                # 也就是 MCP transcript、终端回滚和 cron 日志。
                headers = [f"Authorization: token {self.token}"]
                # 有些 Jupyter 部署只认查询串，header 不作为通用替代，所以同时
                # 保留查询串形式（平台实测接受 header，见 README 的验证记录）。
                ws_url += f"?token={self.token}"
            ws = websocket.create_connection(
                ws_url,
                timeout=timeout,
                header=headers or None,
                sslopt=_sslopt(self.base),
            )
            try:
                return self._collect(ws, code, timeout)
            finally:
                ws.close()
        except Exception as exc:
            # 兜底：任何异常文本里的 token 都抹掉再往外抛。
            raise _redact(exc, self.token) from None
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
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # 每次 recv 都设一个不超过剩余预算的超时：连接级 timeout 只约束握手和
            # 单次 socket 读，内核静默挂住时 recv 会一直阻塞，deadline 根本轮不到
            # 检查，调用方只能干等。
            try:
                ws.settimeout(max(0.5, remaining))
            except Exception:  # noqa: BLE001 - 不影响主流程
                pass
            try:
                message = json.loads(ws.recv())
            except Exception as exc:  # noqa: BLE001
                if time.monotonic() >= deadline:
                    break  # 到点了，交给下面的超时分支给结论
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
        # 只有 status == "ok" 才算跑成功。Jupyter 的 execute_reply.status 除了
        # ok/error 还有 "abort"（请求被丢弃、代码根本没执行），而 abort 不带
        # evalue，所以早期版本用 `ok = error is None` 会把它报成成功，返回一个
        # 空 stdout 的成功结果——调用方以为代码跑了，其实一行都没执行。
        if reply_status is None:
            error = error or (
                f"内核在 {timeout:.0f} 秒内没有回 execute_reply"
                "（超时或连接中断），无法确认代码是否执行完。"
            )
        elif reply_status != "ok" and not error:
            error = f"内核回的执行状态是 {reply_status!r}，代码没有正常跑完"
        return {
            "ok": reply_status == "ok" and error is None,
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
            cleanup = autopoint._stop_and_delete(client, task_id)
            report["cleanup"] = cleanup
            # 清理失败必须反映到 ok 上。只放在 report["cleanup"] 里的话，任何
            # 只检查 ok 的调用方都会把"留下一个正在计费的任务"当成一次成功。
            if not autopoint._cleanup_ok(cleanup):
                report["ok"] = False
                report["action"] = "cleanup_failed"
                report["error"] = (
                    "代码已执行，但任务没能确认清理干净，可能仍在运行并计费："
                    f"{cleanup.get('delete_error') or cleanup.get('warning') or cleanup}"
                )


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

