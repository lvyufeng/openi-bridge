"""HTTP client for the OpenI platform (openi.pcl.ac.cn).

The platform is a Gitea fork whose REST surface lives under /api/v1 and
authenticates with a personal access token sent as Authorization: Bearer.

Compute (云脑 / ai_task) endpoints that this client uses:

    GET  /api/v1/user                              account behind the token
    GET  /api/v1/user/repos                        repos owned by the token
    POST /api/v1/user/repos                        create a code repo (slow, ~30s+)
    GET  /api/v1/repos/{owner}/{repo}/right        write permission on a repo
    GET  /api/v1/repos/{owner}/{repo}/contents     list a directory
    GET  /api/v1/repos/{owner}/{repo}/contents/{p} read or list one path
    POST /api/v1/repos/{owner}/{repo}/contents/{p} create a file (base64 body)
    PUT  /api/v1/repos/{owner}/{repo}/contents/{p} update a file (needs old sha)
    GET  /api/v1/repos/{owner}/{repo}/git/trees/{ref}  whole tree (recursive=1)
    GET  /api/v1/repos/{owner}/{repo}/commits      commit history
    GET  /api/v1/repos/{owner}/{repo}/archive/{ref}.zip  whole repo as a zip
    GET  /api/v1/repos/{owner}/{repo}/issues       issue list
    POST /api/v1/repos/{owner}/{repo}/issues       open an issue
    POST /api/v1/repos/{owner}/{repo}/issues/{n}/comments  comment on an issue
    DELETE /api/v1/repos/{owner}/{repo}            delete a code repo

The commits endpoint accepts a path filter but this deployment ignores it
(verified 2026-10: every path returns the full history), so it is not exposed.

File deletion (DELETE .../contents/{p}) is NOT wrapped: the platform route
hangs and always dies at the 60s proxy timeout (504/502) without removing
the file, so a tool around it could only ever lie. Overwrite the file or
delete the whole repo instead.
    GET  /api/v1/notifications                     account notifications
    GET  /api/v1/notifications/new                 unread notification count
    GET  /api/v1/ai_task/my_list                   every task of the account
    GET  /api/v1/ai_task?id=                       one task detail (with sync)
    GET  /api/v1/ai_task/brief?id=                 compact status
    GET  /api/v1/ai_task/log?id=                   task log
    GET  /api/v1/ai_task/log/download?id=          whole log as plain text
    GET  /api/v1/ai_task/operation_profile?id=     scheduling event timeline
    GET  /api/v1/ai_task/node_info?id=             multi-node task node list
    GET  /api/v1/ai_task/output?id=                task output dir (one level)
    GET  /api/v1/ai_task/output/all?id=            task output (recursive)
    GET  /api/v1/ai_task/output/download?id=       download one output file
    GET  /api/v1/ai_task/loss?id=                  training loss curve
    GET  /api/v1/ai_task/resource_usage?id=        live card/CPU usage
    GET  /api/v1/ai_task/debug_url?id=             Jupyter / SSH entry point
    GET  /api/v1/ai_task/visualize_url?id=         TensorBoard entry point
    GET  /api/v1/ai_task/self_endpoint_url?id=     ONLINEINFERENCE service URL
    GET  /api/v1/ai_task/creation/required         live specs, images, balance
    GET  /api/v1/ai_task/creation/image_by_spec    images usable for a spec
    GET  /api/v1/images/recommend                  platform recommended images
    GET  /api/v1/images/star                       images the account starred
    GET  /api/v1/resources/ai_center/card_info/active  card types online now
    GET  /api/v1/resources/ai_center/active        AI centers online now
    GET  /api/v1/ai_task_template/list/{scope}     task template square
    GET  /api/v1/ai_task_template?id=              one template (the only by-id route)
    PUT  /api/v1/ai_task_template/collect?id=      collect a template
    DELETE /api/v1/ai_task_template/collect?id=    uncollect a template
    POST /api/v1/{owner}/{repo}/ai_task/create     create + start a task
    POST /api/v1/ai_task/stop?id=                  stop a task (stop billing)
    POST /api/v1/ai_task/restart?id=               restart a stopped task
    POST /api/v1/ai_task/del?id=                   delete a stopped task
    POST /api/v1/ai_task/batch_del                 delete several stopped tasks

Data subjects (datasets / models) and their files:

    GET  /api/v1/dataset?dataset_name=             dataset detail incl. uuid
    GET  /api/v1/aimodel?aimodel_name=             model detail incl. uuid
    GET  /api/v1/dataset/files?dataset_name=       dataset file listing
    GET  /api/v1/aimodel/files?aimodel_name=       model file listing
    GET  /api/v1/dataset/list/{scope}              dataset square listing
    GET  /api/v1/aimodel/list/{scope}              model square listing
    GET  /api/v1/{dataset|aimodel}/file            301 to the presigned OBS url
    DELETE /api/v1/{dataset|aimodel}/file          delete one file or subdirectory
    GET  /api/v1/storage/summary                   account or subject quota
    POST /api/v1/dataset/create                    create an empty dataset
    POST /api/v1/aimodel/create                    create an empty model
    POST /api/v1/{dataset|aimodel}/batch_delete    delete datasets / models
    GET  /api/v1/upload/direct/get_upload_url      presigned PUT url (<=64MB)
    POST /api/v1/upload/direct/complete_upload     finalise direct uploads
    GET  /api/v1/upload/get_chunks                 resume info / uuid lookup
    GET  /api/v1/upload/new_multipart              start a multipart upload
    GET  /api/v1/upload/get_multipart_url          presigned PUT url per chunk
    POST /api/v1/upload/complete_multipart         finalise a multipart upload

The create body follows entity.CreateReq from the platform source. Required
keys are job_type, display_job_name, spec_id, compute_source and cluster.
Spec ids are discovered live through /ai_task/creation/required.

Task templates (the 任务模板 page) carry a whole runnable configuration - repo,
branch, boot file, card type and count, image, mounted datasets - but no
spec_id, so template_spec() matches (compute_source, acc_card_type,
acc_cards_num) against the same live spec list before creating a task.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import sys
import time
from typing import Any, Dict, List, Mapping, Optional

import httpx

from .auth import Credentials, resolve_credentials
from .resources import trusted_verified_image

DEFAULT_USER_AGENT = "openi-bridge/0.1 (+mcp)"
DEFAULT_CLUSTER = "C2Net"

# 数据集 / 模型（ckpt）文件上传的分片大小，与官方 openi SDK 保持一致。
CHUNK_SIZE = 64 * 1024 * 1024
# 文件不超过一片时走直传（/upload/direct/*），超过就走分片（/upload/*multipart*）。
DIRECT_UPLOAD_MAX = CHUNK_SIZE
# 关联主体类型：上传接口用字符串，subject_query 也用它区分数据集与模型。
SUBJECT_TYPES = {"dataset": "1", "model": "2"}
# 往代码仓提交单个文件的上限：contents API 用 base64 传，整个文件会进内存。
MAX_REPO_FILE_BYTES = 50 * 1024 * 1024
# 不落盘（返回文本/base64）时的默认读取上限。任务输出里可能是几十 GB 的 ckpt，
# 没有上限的内存读取会把 MCP 进程直接撑爆；要完整内容就给 save_to 落盘。
IN_MEMORY_DOWNLOAD_MAX_BYTES = int(
    os.getenv("OPENI_IN_MEMORY_MAX_BYTES", str(8 * 1024 * 1024))
)

# 落盘下载的超时与读写粒度。总时长不限（单个 ckpt 可能十几 GB），但每次读要有
# 超时，否则连接被静默挂住时调用方看不到任何反馈。
DOWNLOAD_TIMEOUT = httpx.Timeout(30.0, read=120.0)
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
# 平台算分片 md5 时每片只取前 1MB（见 _file_chunk_md5）。
_MD5_SAMPLE_BYTES = 1024 * 1024
# 默认请求超时（建仓、删仓这类慢接口会单独放宽）。
DEFAULT_REQUEST_TIMEOUT = 30.0

# /git/trees 的隐藏页大小与翻页上限（防止超大仓把一次工具调用拖到天荒地老）。
_TREE_PAGE_SIZE = 1000
MAX_TREE_PAGES = int(os.getenv("OPENI_MAX_TREE_PAGES", "50"))

# 数据集 / 模型广场的列表范围（对应平台前端 getCardList 的 tab url）。
SUBJECT_LIST_SCOPES = ("public", "owned", "collected", "collaborated")
# 列表排序取值（取自平台前端 datasets.sort 下拉）。
SUBJECT_LIST_ORDER = (
    "newest",
    "recentupdate",
    "downloadcount",
    "collections",
    "alias_asc",
    "alias",
)
SUBJECT_LIST_PAGE_SIZE = 30
# 平台把 page_size 上限压到 100：再大就静默退回默认的 12 条，所以这里显式拦住。
SUBJECT_LIST_MAX_PAGE_SIZE = 100
# 任务模板（任务模板页的卡片列表）取值范围。列表走 /ai_task_template/list/{scope}，
# 单条走 /ai_task_template?id=（列表接口的 id 参数是摆设，实测被忽略）。
TEMPLATE_LIST_SCOPES = ("public", "all", "created", "collected")
# 平台只认识这三个排序值，写别的会静默回落到默认排序，所以这里显式拦住。
TEMPLATE_LIST_ORDER = ("newest", "usecount", "collections")
# 递归列目录时的文件数上限，避免误对超大主体做全量 crawl。
WALK_FILE_LIMIT = int(os.getenv("OPENI_WALK_FILE_LIMIT", "20000"))
# marker 翻页的次数上限。正常分页靠 has_next / marker 前进收敛，但那是平台的
# 承诺；万一它一直回 has_next=true 且每次给一个新 marker，循环就永远不会结束。
# 加一个硬上限，让它变成一条明确的错误而不是一次挂死。
# 与 MAX_TREE_PAGES 分开：目录页比整树页小得多，合理上限本来就不同。
MAX_LIST_PAGES = int(os.getenv("OPENI_MAX_LIST_PAGES", "500"))

# 任务状态的终态与非终态。计费只在任务处于非终态（还在排队/准备/跑）时发生，
# 所以"任务是否还在烧积分"这个问题永远可以退回这一组取值来回答。
_TASK_TERMINAL_STATUSES = frozenset(
    {"STOPPED", "SUCCEEDED", "FAILED", "CREATED_FAILED", "DELETED", "KILLED"}
)
# stop / restart / del 各自"做完了"对应哪些状态。平台会先回一个过渡态
# （STOPPING / DELETING / QUEUED），只有走到这里列出的状态才算真的生效。
_TASK_ACTION_EXPECTED = {
    "stop": frozenset({"STOPPED", "CREATED_FAILED", "FAILED"}),
    "restart": frozenset(
        {"WAITING", "QUEUED", "PENDING", "PREPARING", "CREATING",
         "RUNNING", "STARTING", "STOPPED"}
    ),
    "del": frozenset({"DELETED"}),
}

# 平台前端对数据集 name / alias 的校验（services/... 与 create 页面一致）。
SUBJECT_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-_.]{0,98}[a-zA-Z0-9]$")
SUBJECT_ALIAS_RE = re.compile(r"^[\u4e00-\u9fa5a-zA-Z0-9\-_.]{0,100}$")

# 平台登记的数据集类别、任务与许可证取值，用于创建数据集时校验。
DATASET_CATEGORIES = (
    "computer_vision",
    "natural_language_processing",
    "speech_processing",
    "computer_vision_natural_language_processing",
    "machine_translation",
    "medical_imaging",
)
DATASET_TASKS = (
    "machine_translation",
    "question_answering_system",
    "information_retrieval",
    "knowledge_graph",
    "text_annotation",
    "text_categorization",
    "language_modeling",
    "speech_recognition",
    "information_extraction",
    "description_generation",
    "image_classification",
    "face_recognition",
    "image_search",
    "target_detection",
    "image_description_generation",
    "vehicle_license_plate_recognition",
    "medical_image_analysis",
    "unmanned",
    "unmanned_security",
    "drone",
    "vr_ar",
    "image_processing",
    "video_processing",
    "visual_input_system",
    "speech_coding",
    "speech_enhancement",
    "speech_synthesis",
    "ros_hmci_datasets",
)
DATASET_LICENSES = (
    "MIT",
    "GPL",
    "GFDL",
    "Apache 2.0",
    "CC-BY",
    "CC-BY-SA",
    "CC-BY-NC",
    "CC-BY-ND",
    "CC0: Public Domain",
    "CDLA Permissive 1.0",
    "Open Database License",
    "Research or commercial",
    "Research and commercial",
)

# Only the NVIDIA GPU path is broken: /ai_task/creation/required advertises two
# images and C2Net rejects both with "image not available" (verified 2026-10).
# The default below is a platform public image (the platform resolves it to
# cuda111_python37_pytorch191_public) verified to boot a real C2Net GPU task.
# The other accelerator families (NPU/GCU/MLU/DCU/...) do list working images,
# so this is only used for compute_source=GPU. Override with
# OPENI_DEFAULT_IMAGE_URL.
DEFAULT_IMAGE_URL = os.getenv(
    "OPENI_DEFAULT_IMAGE_URL",
    "swr.cn-south-1.myhuaweicloud.com/openi/cuda111_python37_pytorch191:v1",
)

# Compute sources whose advertised images cannot be pulled, checked live.
SOURCES_NEEDING_IMAGE_URL = frozenset({"GPU"})

# 任务输出（/ai_task/output）在平台侧是异步索引的：任务刚进 SUCCEEDED 时
# output.status 是 1、file_list 是空的，等一两分钟再查才变成 status 0 并列出文件
# （2026-10 实测，两次任务都在约两分钟后才刷出来）。所以输出工具要把
# “刚跑完查不到文件”当成还没索引，而不是任务没有输出。

# Platform validation (services/ai_task_service/task/opt_handler.go).
JOB_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-_]{1,34}[a-z0-9-]$")
IMAGE_URL_RE = re.compile(
    r"^(?:[a-zA-Z0-9.-]+(?::[0-9]+)?/)?[a-zA-Z0-9._-]+(?:/[a-zA-Z0-9._-]+)*"
    r"(?::[a-zA-Z0-9._-]+)?(?:@sha256:[a-fA-F0-9]{64})?$"
)

COMPUTE_SOURCES = (
    "GPU",
    "NPU",
    "GCU",
    "MLU",
    "DCU",
    "CPU",
    "ILUVATAR-GPGPU",
    "METAX-GPGPU",
    "BIREN-GPU",
)


class OpenIError(RuntimeError):
    """Raised when the platform returns an error."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


def _wrap(data: Any) -> Any:
    """Unwrap the {code, msg, data} envelope used by the custom endpoints."""
    if isinstance(data, Mapping) and "code" in data and "data" in data:
        code = data.get("code")
        if code not in (0, "0", None):
            raise OpenIError(f"platform error {code}: {data.get('msg')}", payload=data)
        return data["data"]
    # 少数接口（例如 /ai_task/export2aimodel 的权限层）回的是大写信封
    # {Code, Msg, Data}，同一个平台上两种拼法并存，这里一并拆掉。
    if isinstance(data, Mapping) and "Code" in data and "Data" in data:
        code = data.get("Code")
        if code not in (0, "0", None):
            raise OpenIError(f"platform error {code}: {data.get('Msg')}", payload=data)
        return data["Data"]
    return data


def _unwrap_text(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def validate_job_name(name: str) -> str:
    """Raise if the display job name violates the platform pattern."""
    if not JOB_NAME_RE.match(name or ""):
        raise OpenIError(
            "display_job_name 只能是 3-36 位小写字母/数字/连字符/下划线，"
            f"且以字母或数字开头、以字母数字或连字符结尾：{name!r}"
        )
    return name


def validate_subject_name(name: str) -> str:
    """数据集 / 模型的 name（URL 里那段）必须符合平台规则。"""
    if not SUBJECT_NAME_RE.match(name or ""):
        raise OpenIError(
            "name 只能是 1-100 位字母/数字/连字符/下划线/点，"
            f"且以字母或数字开头和结尾：{name!r}"
        )
    return name


def validate_subject_alias(alias: str) -> str:
    """数据集 / 模型的 alias（中文展示名）可以为空，否则需符合平台规则。"""
    if alias and not SUBJECT_ALIAS_RE.match(alias):
        raise OpenIError(
            "alias 只能包含中文/字母/数字/连字符/下划线/点，且不超过 100 个字符："
            f"{alias!r}"
        )
    return alias


def _file_type_for_api(path: str) -> Optional[str]:
    """按 MIME 猜测文件类型；猜不出来就不带这个参数。"""
    import mimetypes

    mime_type, _ = mimetypes.guess_type(path)
    return mime_type


def _file_chunk_md5(path: str, chunk_size: int = CHUNK_SIZE) -> str:
    """平台用的不是整文件 md5，而是「每个 64MB 分片取前 1MB」串起来算的 md5。

    这个约定来自官方 openi SDK 的 get_file_md5，和分片续传的判定绑定。
    """
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block[: _MD5_SAMPLE_BYTES])
    return digest.hexdigest()


def _total_chunks(size: int, chunk_size: int = CHUNK_SIZE) -> int:
    if size <= 0:
        return 1
    return math.ceil(size / chunk_size)


def _uploaded_chunk_numbers(
    chunks_info: Mapping[str, Any], total: int, name: str
) -> List[int]:
    """取出续传接口报告的"已经传完的分片号"，并校验它是一个连续前缀。

    /upload/get_chunks 回的 ``chunks`` 有两种可能：已经传完的分片编号列表，或者
    只是一个数量。这个区别至关重要——调用方用它决定续传从第几片开始。按"长度"
    推导分片号只在编号恰好是 1..N 连续前缀时才成立；如果平台回的是稀疏编号
    （例如 [1, 3]），按长度会从第 3 片接着传、第 2 片永远缺失，而 complete 仍然
    成功，结果是一个内容损坏却报成功的对象。

    这里宁可报错也不猜：稀疏或越界的编号一律拒绝，让调用方重传整个文件。
    """
    raw = chunks_info.get("chunks")
    if not raw:
        return []
    if not isinstance(raw, (list, tuple)):
        # 只回了数量（int）时无法判断编号分布，当作"没有可信的续传信息"处理。
        # 这是安全的方向：重传已传过的分片只是浪费流量，漏传分片是数据损坏。
        return []
    try:
        numbers = sorted({int(n) for n in raw})
    except (TypeError, ValueError):
        raise OpenIError(f"{name} 的续传分片信息无法解析：{raw!r}") from None
    if not numbers:
        return []
    expected = list(range(1, len(numbers) + 1))
    if numbers != expected:
        raise OpenIError(
            f"{name} 的续传分片编号不是连续的 1..{len(numbers)}（平台返回 {numbers[:20]}）。"
            "无法安全地从中间续传，请重传该文件（或换一个 uuid 重新开始分片上传）。"
        )
    if total and numbers[-1] > total:
        raise OpenIError(
            f"{name} 的续传分片编号 {numbers[-1]} 超出该文件的总分片数 {total}，"
            "续传信息与本地文件不一致，请重传。"
        )
    return numbers


def _iter_chunks(path: str, chunk_size: int = CHUNK_SIZE, start_from: int = 1):
    """按分片大小顺序读出 (分片号, 数据)。空文件会给出一片空数据。"""
    if os.path.getsize(path) == 0:
        yield 1, b""
        return
    number = start_from
    with open(path, "rb") as handle:
        handle.seek((start_from - 1) * chunk_size)
        while True:
            data = handle.read(chunk_size)
            if not data:
                break
            yield number, data
            number += 1


def _stream_to_file(
    response_chunks: Any,
    dest: str,
    label: str,
    max_bytes: Optional[int] = None,
) -> Dict[str, Any]:
    """把一串字节块写进 dest，走"临时文件 + 改名"，失败不留半截也不毁旧文件。

    三个下载入口（仓打包、主体单文件、任务的输出/日志）原本各写了一遍这段逻辑，
    结果只有一处做了覆盖保护：另外两处的 os.replace 会无条件盖掉同名文件，中途
    失败还会留下一个 .part。这里收成一处，三条路径行为一致。

    覆盖语义是"要么换成新文件，要么保持旧文件不动"：先把已有文件挪到 .prev，
    新文件就位后再删 .prev；中途任何失败都把 .prev 换回去。刻意不删 ENOSPC 情况下
    的 .part——那时它是唯一完整的副本。
    """
    tmp_path = f"{dest}.part"
    digest = hashlib.md5()
    size = 0
    try:
        with open(tmp_path, "wb") as handle:
            for chunk in response_chunks:
                handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)
                if max_bytes is not None and size > max_bytes:
                    raise OpenIError(
                        f"{label} 超过 max_bytes={max_bytes} 字节，已中止下载。"
                    )
    except BaseException:
        # 只有在确实写坏了（或超限）时才删临时文件；errno 28（磁盘满）时它是
        # 唯一完整的副本，删掉等于把已经下到的内容也扔掉。
        if os.path.exists(tmp_path) and not _is_out_of_space():
            os.remove(tmp_path)
        raise

    previous = None
    if os.path.exists(dest):
        previous = f"{dest}.prev"
        os.replace(dest, previous)
    try:
        os.replace(tmp_path, dest)
    except BaseException:
        if previous and os.path.exists(previous):
            os.replace(previous, dest)  # 回滚，别把老文件也弄丢
        # 换名失败时 .part 还在：内容完整但位置不对，留着只会让下次下载多一个
        # 莫名其妙的分片。ENOSPC 除外——那时它是唯一的副本。
        if os.path.exists(tmp_path) and not _is_out_of_space():
            os.remove(tmp_path)
        raise
    if previous and os.path.exists(previous):
        os.remove(previous)
    return {
        "path": dest,
        "bytes": size,
        "md5": digest.hexdigest(),
    }


def _is_out_of_space() -> bool:
    """当前异常链里是不是磁盘写满（ENOSPC）。"""
    import errno

    exc = sys.exc_info()[1]
    while exc is not None:
        if getattr(exc, "errno", None) == errno.ENOSPC:
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _assert_safe_path(path: str) -> None:
    """挡掉会把请求重定向到别的端点的路径。

    很多 URL 是 f"/repos/{repo}/..." 这样拼出来的，而 repo 来自调用方。httpx 会
    把 "../" 规范化掉，所以 repo="../user" 会让 /repos/../user 真的打到 /api/v1/user
    —— 一个仓维度的调用变成了任意 API 调用（2026-10 实测确认）。

    入口处统一拦：路径里不允许出现 "." / ".." 这样的整段，也不允许换行。这一层
    在 request() 里，所以所有接口都覆盖到，不必给 20 处拼接各加一次判断。
    """
    if not path.startswith("/"):
        raise OpenIError(f"内部路径必须以 / 开头：{path!r}")
    if "\n" in path or "\r" in path:
        raise OpenIError(f"路径里不能有换行：{path!r}")
    for segment in path.split("/"):
        if segment in (".", ".."):
            raise OpenIError(
                f"路径里不允许出现 {segment!r} 段（会把请求打到别的端点）：{path!r}"
            )


def expand_upload_paths(paths: List[str], remote_dir: str = "") -> List[Dict[str, Any]]:
    """把本地文件/目录展开成待上传清单：{name(仓内路径), path, size}。

    目录会递归收集，仓内路径保留相对目录结构；remote_dir 会作为统一前缀，
    例如 remote_dir="ckpt/v1" 时 a.txt 会上传成 ckpt/v1/a.txt。
    """
    prefix = (remote_dir or "").strip("/")
    entries: List[Dict[str, Any]] = []
    for raw in paths:
        source = os.path.abspath(os.path.expanduser(raw))
        if not os.path.exists(source):
            raise OpenIError(f"本地路径不存在：{raw}")
        if os.path.isfile(source):
            name = os.path.basename(source)
            entries.append(
                {"name": f"{prefix}/{name}" if prefix else name, "path": source,
                 "size": os.path.getsize(source)}
            )
            continue
        base = source
        found = False
        for root, _dirs, files in os.walk(base):
            for filename in files:
                if filename in (".DS_Store", "Thumbs.db"):
                    continue
                full = os.path.join(root, filename)
                rel = os.path.relpath(full, base).replace(os.sep, "/")
                name = f"{prefix}/{rel}" if prefix else rel
                entries.append({"name": name, "path": full, "size": os.path.getsize(full)})
                found = True
        if not found:
            raise OpenIError(f"目录下没有可上传的文件：{raw}")
    return entries


def subject_endpoint(subject_type: str) -> tuple[str, str]:
    """把 dataset/model 映射成平台的接口路径与查询参数名。

    主体类接口（详情、文件清单、下载、删除文件）都共用这一对取值，
    集中在这里免得每个方法各写一遍。
    """
    if subject_type == "dataset":
        return "/dataset", "dataset_name"
    if subject_type == "model":
        return "/aimodel", "aimodel_name"
    raise OpenIError(f"subject_type 只能是 dataset 或 model：{subject_type!r}")


class OpenIClient:
    """Synchronous client; the MCP server is single user and calls are cheap."""

    def __init__(
        self,
        token: Optional[str] = None,
        endpoint: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        self.creds: Credentials = resolve_credentials(token, endpoint)
        self.timeout = timeout
        self._http = httpx.Client(
            base_url=f"{self.creds.endpoint}/api/v1",
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"},
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "OpenIClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- transport

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Any = None,
        data: Optional[Mapping[str, Any]] = None,
        raise_for_status: bool = True,
        timeout: Optional[float] = None,
    ) -> httpx.Response:
        _assert_safe_path(path)
        if not self.creds.has_token:
            raise OpenIError(
                "NO_TOKEN: 没有找到启智令牌。请在 https://openi.pcl.ac.cn/user/settings/applications "
                "生成令牌，然后设置 OPENI_TOKEN 环境变量，或执行 openi login。"
            )
        headers = {"Authorization": f"Bearer {self.creds.token}"}
        # 少数接口（建仓、删仓）在平台上要跑几十秒，允许调用方单独放宽超时。
        kwargs: Dict[str, Any] = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        response = self._http.request(
            method, path, params=params, json=json_body, data=data, headers=headers, **kwargs
        )
        if not raise_for_status:
            return response
        if response.status_code in (401,):
            raise OpenIError(
                f"UNAUTHORIZED ({response.status_code}): 令牌无效或已过期",
                status_code=response.status_code,
                payload=response.text[:400],
            )
        if response.status_code == 403:
            raise OpenIError(
                "FORBIDDEN (403): 令牌权限不足，或该资源不属于当前账号",
                status_code=403,
                payload=response.text[:400],
            )
        if response.status_code >= 400:
            raise OpenIError(
                f"HTTP {response.status_code} for {method} {path}: {response.text[:300]}",
                status_code=response.status_code,
                payload=response.text[:400],
            )
        return response

    def get_json(self, path: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        return _wrap(_unwrap_text(self.request("GET", path, params=params)))

    def post_json(
        self,
        path: str,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Any = None,
        timeout: Optional[float] = None,
    ) -> Any:
        return _wrap(
            _unwrap_text(
                self.request("POST", path, params=params, json_body=json_body, timeout=timeout)
            )
        )

    def put_json(
        self,
        path: str,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Any = None,
        timeout: Optional[float] = None,
    ) -> Any:
        return _wrap(
            _unwrap_text(
                self.request("PUT", path, params=params, json_body=json_body, timeout=timeout)
            )
        )

    def delete_json(
        self,
        path: str,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Any = None,
        timeout: Optional[float] = None,
    ) -> Any:
        return _wrap(
            _unwrap_text(
                self.request(
                    "DELETE", path, params=params, json_body=json_body, timeout=timeout
                )
            )
        )

    def post_form(
        self,
        path: str,
        params: Optional[Mapping[str, Any]] = None,
        data: Optional[Mapping[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """POST 一个 application/x-www-form-urlencoded 请求。

        导出类接口（/ai_task/export2dataset、/ai_task/export2aimodel）只认表单体，
        传 JSON 体会被平台前置的反向代理直接 403。参数名按平台 Go 侧的字段名转
        蛇形：TaskId 写 task_id、FileListStr 写 file_list_str。
        """
        return _wrap(
            _unwrap_text(self.request("POST", path, params=params, data=data, timeout=timeout))
        )

    # ------------------------------------------------------------------- users

    def current_user(self) -> Dict[str, Any]:
        """GET /api/v1/user - the account the token belongs to."""
        return self.get_json("/user")

    def user(self, username: str) -> Dict[str, Any]:
        return self.get_json(f"/users/{username}")

    def notifications(self, limit: int = 20, page: int = 1, unread: bool = False) -> Any:
        """GET /notifications - 通知列表（含所属仓库的上下文）。"""
        params: Dict[str, Any] = {"limit": limit, "page": page}
        if unread:
            params["unread"] = "true"
        return self.get_json("/notifications", params=params)

    def unread_notification_count(self) -> Any:
        """GET /notifications/new - 未读通知数量。"""
        return self.get_json("/notifications/new")

    def notification_thread(self, thread_id: Any) -> Dict[str, Any]:
        """GET /notifications/threads/{id} - 单条通知的完整内容。

        列表接口（notifications）只给标题和链接，正文、最新一条评论、订阅状态都
        在这里。线程 id 就是列表里每项的 id。
        """
        row = self.get_json(f"/notifications/threads/{thread_id}") or {}
        subject = row.get("subject") if isinstance(row.get("subject"), Mapping) else {}
        repository = row.get("repository") if isinstance(row.get("repository"), Mapping) else {}
        return {
            "id": row.get("id"),
            "unread": row.get("unread"),
            "pinned": row.get("pinned"),
            "updated_at": row.get("updated_at"),
            "subject_type": subject.get("type"),
            "title": subject.get("title"),
            "url": (subject.get("url") or "").replace("/api/v1/repos/", "/"),
            "latest_comment_url": (subject.get("latest_comment_url") or "").replace(
                "/api/v1/repos/", "/"
            ),
            "repo": repository.get("full_name"),
        }

    def notification_thread_comments(self, thread_id: Any) -> List[Dict[str, Any]]:
        """GET /notifications/threads/{id}/comments - 通知线程里的评论正文。"""
        rows = self.get_json(f"/notifications/threads/{thread_id}/comments") or []
        if isinstance(rows, Mapping):
            rows = rows.get("comments") or []
        out: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            out.append(
                {
                    "id": row.get("id"),
                    "body": row.get("body"),
                    "author": (row.get("user") or {}).get("login")
                    if isinstance(row.get("user"), Mapping)
                    else None,
                    "created_at": row.get("created_at"),
                }
            )
        return out

    def mark_notifications_read(self, thread_id: Optional[Any] = None) -> Any:
        """把未读通知一次全部标为已读（PUT /notifications?all=true）。

        平台只开放了“全部已读”这一条接口：Gitea 那套单条标记走的
        PATCH /notifications/threads/{id} 被前置反向代理挡掉，同名的 PUT /
        POST(带 _method=PATCH) 都没有注册（404）。前端标单条是走带 _csrf
        cookie 的网页表单，令牌方式用不了，所以这里只做全清。
        传 thread_id 只是想标单条时，直接抛错说明限制，不静默失败。
        """
        if thread_id is not None:
            raise OpenIError(
                "平台不提供“标记单条通知已读”的接口（PATCH 被反向代理拦截、"
                "PUT/POST 路由未注册），只能用 openi_mark_all_notifications_read 全部标已读。"
            )
        return self.put_json("/notifications", params={"all": "true"})

    # -------------------------------------------------------------------- repos

    def my_repos(self, limit: int = 50, page: int = 1) -> List[Dict[str, Any]]:
        """Repos owned by the token holder; used to pick the task code source."""
        return self.get_json("/user/repos", params={"limit": limit, "page": page})

    def search_repos(self, query: str, limit: int = 20) -> Dict[str, Any]:
        return self.get_json("/repos/search", params={"q": query, "limit": limit})

    def repo(self, subject_name: str) -> Dict[str, Any]:
        return self.get_json(f"/repos/{subject_name}")

    def repo_branches(self, subject_name: str) -> List[Dict[str, Any]]:
        """GET /repos/{name}/branches - 分支清单（建任务要填 branch_name）。

        平台每条分支带的东西非常多：commit 里有完整 PGP 签名、签名原文、作者邮箱，
        一条就能到几 KB。这里只留选分支真正需要的几个字段——名字、最新提交、是否
        保护分支、当前账号能不能推。
        """
        rows = self.get_json(f"/repos/{subject_name}/branches") or []
        if isinstance(rows, Mapping):
            rows = rows.get("branches") or []
        out: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            commit = row.get("commit") if isinstance(row.get("commit"), Mapping) else {}
            author = commit.get("author") if isinstance(commit.get("author"), Mapping) else {}
            message = (commit.get("message") or "").strip().splitlines()
            out.append(
                {
                    "name": row.get("name"),
                    "commit_sha": commit.get("id"),
                    "commit_message": message[0] if message else None,
                    "commit_time": row.get("timestamp") or commit.get("timestamp"),
                    "author": author.get("username") or author.get("name"),
                    "protected": row.get("protected"),
                    "user_can_push": row.get("user_can_push"),
                }
            )
        return out

    def repo_tags(self, subject_name: str) -> List[Dict[str, Any]]:
        """GET /repos/{name}/tags - 代码仓的 tag 列表（每项含指向的 commit）。

        用来确认某个版本打没打过 tag、以及 tag 落在哪个提交上。注意 mirror 仓
        （从 GitHub 同步过来的那种）上的 ref 是平台只读的，删不掉也推不动。
        """
        rows = self.get_json(f"/repos/{subject_name}/tags") or []
        if isinstance(rows, Mapping):
            rows = rows.get("tags") or []
        out: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            commit = row.get("commit") if isinstance(row.get("commit"), Mapping) else {}
            out.append(
                {
                    "name": row.get("name"),
                    "id": row.get("id"),
                    "commit_sha": commit.get("sha"),
                    "commit_url": commit.get("url"),
                }
            )
        return out

    def repo_releases(self, subject_name: str) -> List[Dict[str, Any]]:
        """GET /repos/{name}/releases - 代码仓的 release（含附件）。"""
        rows = self.get_json(f"/repos/{subject_name}/releases") or []
        if isinstance(rows, Mapping):
            rows = rows.get("releases") or []
        out: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            author = row.get("author") if isinstance(row.get("author"), Mapping) else {}
            out.append(
                {
                    "id": row.get("id"),
                    "tag_name": row.get("tag_name"),
                    "name": row.get("name"),
                    "draft": row.get("draft"),
                    "prerelease": row.get("prerelease"),
                    "created_at": row.get("created_at"),
                    "published_at": row.get("published_at"),
                    "author": author.get("login"),
                }
            )
        return out

    def repo_labels(self, subject_name: str) -> List[Dict[str, Any]]:
        """GET /repos/{name}/labels - 代码仓的标签（issue 分类用）。"""
        rows = self.get_json(f"/repos/{subject_name}/labels") or []
        if isinstance(rows, Mapping):
            rows = rows.get("labels") or []
        return [
            {"id": r.get("id"), "name": r.get("name"), "color": r.get("color")}
            for r in rows
            if isinstance(r, Mapping)
        ]

    def repo_collaborators(self, subject_name: str) -> List[Dict[str, Any]]:
        """GET /repos/{name}/collaborators - 协作者清单。

        平台这个接口实际回的字段是 id / login / full_name / email / is_active /
        last_login，**没有 permissions**（2026-10-17 实测 OpenModelZoo/ELMo）。
        所以这里不再编一个恒为 null 的 permissions 字段，改成把平台真的给了的
        权限相关字段透出来；哪天平台开始回 permissions，也会被带上。
        """
        rows = self.get_json(f"/repos/{subject_name}/collaborators") or []
        if isinstance(rows, Mapping):
            rows = rows.get("collaborators") or []
        out: List[Dict[str, Any]] = []
        for r in rows:
            if not isinstance(r, Mapping):
                continue
            entry: Dict[str, Any] = {
                "login": r.get("login"),
                "full_name": r.get("full_name"),
                "is_active": r.get("is_active"),
            }
            if "permissions" in r:  # 平台当前不回，回了就透传
                entry["permissions"] = r.get("permissions")
            out.append(entry)
        return out

    def repo_stargazers(self, subject_name: str, limit: int = 50, page: int = 1) -> List[Dict[str, Any]]:
        """GET /repos/{name}/stargazers - 收藏该仓的账号。"""
        rows = self.get_json(
            f"/repos/{subject_name}/stargazers", params={"limit": limit, "page": page}
        ) or []
        if isinstance(rows, Mapping):
            rows = rows.get("stargazers") or []
        return [
            {"login": r.get("login"), "full_name": r.get("full_name")}
            for r in rows
            if isinstance(r, Mapping)
        ]

    def account_repos(self, scope: str, limit: int = 50, page: int = 1) -> List[Dict[str, Any]]:
        """账号维度的仓列表：scope 取 starred（收藏）/ subscriptions（关注）。

        只列自己 owner 的仓用 my_repos（/user/repos），这里补的是“别人的仓”。
        """
        paths = {
            "starred": "/user/starred",
            "subscriptions": "/user/subscriptions",
        }
        if scope not in paths:
            raise OpenIError(f"scope 只能是 starred 或 subscriptions：{scope!r}")
        rows = self.get_json(paths[scope], params={"limit": limit, "page": page}) or []
        if isinstance(rows, Mapping):
            rows = rows.get("data") or []
        out: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            owner = row.get("owner") if isinstance(row.get("owner"), Mapping) else {}
            out.append(
                {
                    "full_name": row.get("full_name"),
                    "owner": owner.get("login"),
                    "private": row.get("private"),
                    "description": row.get("description"),
                    "updated_at": row.get("updated_at"),
                }
            )
        return out

    def my_orgs(self) -> List[Dict[str, Any]]:
        """GET /user/orgs - 当前账号加入的组织/团队。"""
        rows = self.get_json("/user/orgs") or []
        if isinstance(rows, Mapping):
            rows = rows.get("data") or []
        return [
            {
                "username": r.get("username") or r.get("name"),
                "full_name": r.get("full_name"),
                "description": r.get("description"),
            }
            for r in rows
            if isinstance(r, Mapping)
        ]

    def search_topics(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """GET /topics/search - 按主题找仓，主题上带仓数量。"""
        data = self.get_json("/topics/search", params={"q": query, "limit": limit}) or {}
        rows = data.get("topics") if isinstance(data, Mapping) else []
        return [
            {
                "name": r.get("topic_name"),
                "repo_count": r.get("repo_count"),
            }
            for r in (rows or [])
            if isinstance(r, Mapping)
        ]

    def search_users(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """GET /users/search - 按关键字找人。"""
        data = self.get_json("/users/search", params={"q": query, "limit": limit}) or {}
        rows = data.get("data") if isinstance(data, Mapping) else []
        return [
            {
                "login": r.get("login"),
                "full_name": r.get("full_name"),
            }
            for r in (rows or [])
            if isinstance(r, Mapping)
        ]

    def repo_contents(
        self, subject_name: str, ref: Optional[str] = None, path: str = ""
    ) -> Any:
        """列目录（默认根目录），用于发现可用的 boot_file 或翻看代码结构。"""
        params = {"ref": ref} if ref else None
        clean = (path or "").strip("/")
        suffix = f"/{clean}" if clean else ""
        return self.get_json(f"/repos/{subject_name}/contents{suffix}", params=params)

    def repo_permission(self, subject_name: str) -> Any:
        """GET /repos/{owner}/{repo}/right - 当前账号对该仓的写权限。"""
        return self.get_json(f"/repos/{subject_name}/right")

    def create_repo(
        self,
        name: str,
        description: str = "",
        private: bool = False,
        auto_init: bool = True,
        readme: str = "Default",
        default_branch: str = "master",
        timeout: float = 180.0,
    ) -> Dict[str, Any]:
        """POST /user/repos - 建一个代码仓（owner 固定是当前账号）。

        平台建仓要跑几十秒（实测 30s 以上），所以默认给 180s 的超时；
        超时也不代表失败，调用方应回查 /user/repos 确认是否已建出来。
        """
        body: Dict[str, Any] = {
            "name": name,
            "description": description,
            "private": private,
            "auto_init": auto_init,
        }
        if auto_init:
            body["readme"] = readme or "Default"
            body["default_branch"] = default_branch or "master"
        return self.post_json("/user/repos", json_body=body, timeout=timeout)

    def delete_repo(self, subject_name: str, timeout: float = 180.0) -> Any:
        """DELETE /repos/{owner}/{repo} - 删掉一个代码仓（平台侧同样偏慢）。"""
        return self.delete_json(f"/repos/{subject_name}", timeout=timeout)

    def repo_file(self, subject_name: str, path: str, ref: Optional[str] = None) -> Any:
        """读仓库里的单个文件，返回 Gitea contents 结构（content 是 base64）。"""
        params = {"ref": ref} if ref else None
        return self.get_json(f"/repos/{subject_name}/contents/{path.strip('/')}", params=params)

    def repo_file_sha(
        self, subject_name: str, path: str, ref: Optional[str] = None
    ) -> Optional[str]:
        """文件已存在时返回其 blob sha（更新文件必须带上），否则 None。"""
        try:
            data = self.repo_file(subject_name, path, ref=ref)
        except OpenIError:
            return None
        return data.get("sha") if isinstance(data, Mapping) else None

    def write_repo_file(
        self,
        subject_name: str,
        path: str,
        content: Any,
        message: str = "",
        branch: Optional[str] = None,
        ref: Optional[str] = None,
    ) -> Dict[str, Any]:
        """新建或覆盖仓库里的一个文件，走 contents API（不需要 git）。

        Gitea 的语义是新建用 POST、更新用 PUT 且必须带旧 blob 的 sha，所以这里
        先查一次 sha 再决定用哪个动词。每次调用产生一个提交。
        """
        if isinstance(content, str):
            content = content.encode("utf-8")
        elif isinstance(content, bytearray):
            content = bytes(content)
        if not isinstance(content, bytes):
            raise OpenIError("content 必须是 bytes 或 str。")
        clean = path.strip("/")
        if not clean:
            raise OpenIError("path 不能为空。")
        body: Dict[str, Any] = {
            "content": base64.b64encode(content).decode("ascii"),
            "message": message or f"update {clean}",
        }
        if branch:
            body["branch"] = branch
        sha = self.repo_file_sha(subject_name, clean, ref=ref)
        if sha:
            body["sha"] = sha
            return self.put_json(f"/repos/{subject_name}/contents/{clean}", json_body=body)
        return self.post_json(f"/repos/{subject_name}/contents/{clean}", json_body=body)

    def read_repo_file(
        self, subject_name: str, path: str, ref: Optional[str] = None, max_bytes: int = 0
    ) -> Dict[str, Any]:
        """读仓库文件并解码。返回 {path, sha, size, bytes}，max_bytes>0 时超限即拒绝。"""
        data = self.repo_file(subject_name, path, ref=ref)
        if not isinstance(data, Mapping):
            raise OpenIError(f"{path} 不是一个文件（可能是目录）。")
        if data.get("type") and data.get("type") != "file":
            raise OpenIError(f"{path} 不是文件（type={data.get('type')}）。")
        size = int(data.get("size") or 0)
        if max_bytes and size > max_bytes:
            raise OpenIError(
                f"{path} 有 {size} 字节，超过 max_bytes={max_bytes}。"
                "确认要读请调大 max_bytes，或改用 save_to 存到本地。"
            )
        encoded = data.get("content") or ""
        try:
            blob = base64.b64decode(encoded)
        except Exception as exc:  # noqa: BLE001 - 内容不是合法 base64 时给个清楚的错
            raise OpenIError(f"{path} 的内容不是 base64：{exc}") from exc
        return {"path": data.get("path") or path, "sha": data.get("sha"), "size": size, "bytes": blob}

    def push_repo_files(
        self,
        subject_name: str,
        entries: List[Mapping[str, Any]],
        message: str = "",
        branch: Optional[str] = None,
        dry_run: bool = False,
        max_bytes: Optional[int] = None,
        max_file_bytes: int = MAX_REPO_FILE_BYTES,
    ) -> Dict[str, Any]:
        """把 expand_upload_paths 的结果逐个提交进代码仓，每个文件一个提交。

        contents API 走的是 base64，文件会被整个读进内存，所以单个文件有上限
        （默认 50MB），整批总量可以用 max_bytes 再卡一道。
        """
        if dry_run:
            total = sum(int(e["size"]) for e in entries)
            return {
                "repo": subject_name,
                "dry_run": True,
                "file_count": len(entries),
                "total_bytes": total,
                "files": [
                    {"path": e["name"], "size": int(e["size"])} for e in entries
                ],
            }
        total = sum(int(e["size"]) for e in entries)
        if max_bytes is not None and total > max_bytes:
            raise OpenIError(
                f"待提交总量 {total} 字节超过 max_bytes={max_bytes}，已中止。"
            )
        right = self.repo_permission(subject_name)
        if isinstance(right, Mapping) and right.get("right") not in ("write", "admin"):
            raise OpenIError(
                f"当前账号对 {subject_name} 没有写权限（right={right.get('right')}）。"
            )
        results: List[Dict[str, Any]] = []
        for entry in entries:
            name = entry["name"]
            size = int(entry["size"])
            record: Dict[str, Any] = {"path": name, "size": size}
            try:
                if size > max_file_bytes:
                    raise OpenIError(
                        f"{name} 有 {size} 字节，超过单文件上限 {max_file_bytes}。"
                        "contents API 会把文件整个读进内存，大文件请改用 git 推送。"
                    )
                with open(entry["path"], "rb") as handle:
                    blob = handle.read()
                if len(blob) != size:
                    raise OpenIError(f"{name} 读取字节数 {len(blob)} 与预期 {size} 不一致。")
                existed = self.repo_file_sha(subject_name, name) is not None
                self.write_repo_file(
                    subject_name, name, blob, message=message or f"update {name}", branch=branch
                )
                record["ok"] = True
                record["action"] = "update" if existed else "create"
            except Exception as exc:  # noqa: BLE001 - 逐文件汇报失败
                record["ok"] = False
                record["error"] = str(exc)
            results.append(record)
        succeeded = [r for r in results if r.get("ok")]
        return {
            "repo": subject_name,
            "ok": len(succeeded) == len(results),
            "committed_count": len(succeeded),
            "failed_count": len(results) - len(succeeded),
            "committed_bytes": sum(r["size"] for r in succeeded),
            "files": results,
        }

    def default_branch(self, subject_name: str) -> Optional[str]:
        data = self.repo(subject_name)
        if isinstance(data, Mapping):
            return data.get("default_branch")
        return None

    def repo_tree(
        self, subject_name: str, ref: str = "", path: str = "", max_entries: int = WALK_FILE_LIMIT
    ) -> Dict[str, Any]:
        """整仓文件树（/git/trees/{ref}?recursive=1）。

        比逐层翻 contents 快得多，适合先看清仓里有什么再决定读哪个文件。
        ref 留空用 HEAD（即默认分支的最新提交）。

        平台对这个接口有隐藏分页：一次最多回 1000 条，同时在返回里给
        ``truncated``、``page`` 与 ``total_count``（mindnlp 实测 2627 条，第一页
        只有 1000）。所以这里会翻页取全，而不是把第一页当成整棵树——早先的实现
        就是后者，一个 2627 文件的仓只能看到前 1000 个。
        max_entries 仍是硬上限（在取全之后再截断），避免超大仓把返回撑爆。
        """
        tree_ref = (ref or "").strip() or "HEAD"
        rows: List[Mapping[str, Any]] = []
        data: Mapping[str, Any] = {}
        page = 1
        page_limit_hit = False
        while True:
            data = self.get_json(
                f"/repos/{subject_name}/git/trees/{tree_ref}",
                params={"recursive": 1, "page": page},
            ) or {}
            batch = data.get("tree") or []
            rows.extend(r for r in batch if isinstance(r, Mapping))
            total = data.get("total_count")
            # 停止条件：这一页没满（没有更多）、拿满了 total、或该接口不再分页。
            if not batch or not data.get("truncated"):
                break
            if isinstance(total, int) and total and len(rows) >= total:
                break
            if len(batch) < _TREE_PAGE_SIZE:
                break
            page += 1
            if page > MAX_TREE_PAGES:
                page_limit_hit = True
                break
        prefix = (path or "").strip().strip("/")
        if prefix:
            rows = [
                row
                for row in rows
                if (row.get("path") or "") == prefix
                or (row.get("path") or "").startswith(prefix + "/")
            ]
        # truncated 只表示"返回的比仓里实际有的少"。平台每页都带 truncated=true
        # （它表示"这一页不是全部"），翻完页之后那个标志就不再说明问题了，所以
        # 这里只认两件事：是不是被 max_entries 截了，以及有没有因为页数上限没取完。
        total = data.get("total_count")
        complete_tree = not page_limit_hit and (
            len(rows) >= total
            if isinstance(total, int) and total
            else True
        )
        truncated_by_cap = max_entries > 0 and len(rows) > max_entries
        truncated = truncated_by_cap or not complete_tree
        if max_entries > 0:
            rows = rows[:max_entries]
        entries = [
            {
                "path": row.get("path"),
                "type": row.get("type"),
                "size": row.get("size"),
                "sha": row.get("sha"),
            }
            for row in rows
        ]
        return {
            "repo": subject_name,
            "ref": tree_ref,
            "sha": data.get("sha"),
            "count": len(entries),
            "truncated": truncated,
            # 平台声明的总数：count < total_count 说明是被 max_entries 截的，
            # 而不是仓里就这么多文件。
            "total_count": total,
            "pages_fetched": page,
            "entries": entries,
        }

    def repo_commits(
        self, subject_name: str, ref: str = "", limit: int = 20, page: int = 1
    ) -> List[Dict[str, Any]]:
        """提交历史，按时间倒序。

        平台这套部署忽略了 path 过滤参数（实测：传任何 path 都返回全量历史），
        所以这里没有暴露该参数，免得给出看起来筛过、其实没筛的结果。
        """
        params: Dict[str, Any] = {"limit": int(limit), "page": int(page)}
        if ref:
            params["sha"] = ref
        rows = self.get_json(f"/repos/{subject_name}/commits", params=params) or []
        out: List[Dict[str, Any]] = []
        for row in rows:
            commit = row.get("commit") or {}
            author = commit.get("author") or {}
            out.append(
                {
                    "sha": row.get("sha"),
                    "short_sha": (row.get("sha") or "")[:10],
                    "message": (commit.get("message") or "").strip(),
                    "author": author.get("name") or (row.get("author") or {}).get("login"),
                    "date": author.get("date"),
                    "parents": [p.get("sha") for p in row.get("parents") or []],
                }
            )
        return out

    def download_repo_archive(
        self,
        subject_name: str,
        save_to: str,
        ref: str = "",
        max_bytes: Optional[int] = None,
    ) -> Dict[str, Any]:
        """把整个代码仓打包成 zip 下载到本地（/repos/{o}/{r}/archive/{ref}.zip）。

        整包边走边写盘，先落在 <目标>.part，下完再改名，中途失败不会留下半截 zip。
        max_bytes 是大小上限，超了立刻中止并删掉临时文件。ref 留空用默认分支。
        """
        if not self.creds.has_token:
            raise OpenIError("NO_TOKEN: 没有找到启智令牌。")
        ref = (ref or "").strip() or self.default_branch(subject_name) or "master"
        if ref.endswith(".zip"):
            ref = ref[: -len(".zip")]
        dest = os.path.abspath(os.path.expanduser(save_to))
        if os.path.isdir(dest):
            dest = os.path.join(dest, f"{subject_name.split('/')[-1]}-{ref}.zip")
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with self._http.stream(
            "GET",
            f"/repos/{subject_name}/archive/{ref}.zip",
            headers={"Authorization": f"Bearer {self.creds.token}"},
            timeout=DOWNLOAD_TIMEOUT,
        ) as response:
            if response.status_code >= 400:
                raise OpenIError(
                    f"打包下载失败 HTTP {response.status_code}：{subject_name}@{ref}",
                    status_code=response.status_code,
                    payload=response.read().decode("utf-8", "replace")[:200],
                )
            written = _stream_to_file(
                response.iter_bytes(DOWNLOAD_CHUNK_BYTES),
                dest,
                f"仓库打包 {subject_name}@{ref}",
                max_bytes=max_bytes,
            )
        return {
            "repo": subject_name,
            "ref": ref,
            "path": written["path"],
            "bytes": written["bytes"],
            "mb": round(written["bytes"] / 1024 / 1024, 2),
            "md5": written["md5"],
        }

    def create_repo_issue(self, subject_name: str, title: str, body: str = "") -> Dict[str, Any]:
        """在代码仓里开一个 issue（/issues）。"""
        data = self.post_json(
            f"/repos/{subject_name}/issues",
            json_body={"title": title, "body": body or ""},
        )
        return _slim_issue(data or {})

    def repo_issues(
        self, subject_name: str, state: str = "open", limit: int = 20, page: int = 1
    ) -> List[Dict[str, Any]]:
        """列代码仓的 issue，state 取 open / closed / all。"""
        rows = self.get_json(
            f"/repos/{subject_name}/issues",
            params={"state": state, "limit": int(limit), "page": int(page)},
        ) or []
        return [_slim_issue(row) for row in rows]

    def comment_repo_issue(
        self, subject_name: str, index: Any, body: str
    ) -> Dict[str, Any]:
        """在某个 issue 下发表评论（/issues/{index}/comments）。"""
        data = self.post_json(
            f"/repos/{subject_name}/issues/{index}/comments",
            json_body={"body": body},
        ) or {}
        return {
            "id": data.get("id"),
            "url": data.get("html_url"),
            "body": data.get("body"),
            "created_at": data.get("created_at"),
        }

    def update_repo_issue(
        self,
        subject_name: str,
        index: Any,
        state: Optional[str] = None,
        title: Optional[str] = None,
        body: Optional[str] = None,
    ) -> Dict[str, Any]:
        """改一个已有 issue（POST /repos/{name}/issues/{index}）。

        state 取 open / closed，用来关闭或重开；title / body 改标题与正文。
        平台前置的反向代理把 PATCH 一律挡掉（回空 400），编辑走的是同一个 issue
        路径上的 POST + JSON 体。三个字段至少要给一个，空改动会被平台拒绝。
        """
        payload: Dict[str, Any] = {}
        if state is not None:
            if state not in ("open", "closed"):
                raise OpenIError(f"state 只能是 open 或 closed：{state!r}")
            payload["state"] = state
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        if not payload:
            raise OpenIError("至少要给 state / title / body 里的一个，平台不接受空改动。")
        data = self.post_json(f"/repos/{subject_name}/issues/{index}", json_body=payload)
        return _slim_issue(data or {})

    # ----------------------------------------------------------- data subjects

    def dataset_files(
        self, dataset_name: str, parent_dir: str = "", marker: str = "", page_size: int = 1000
    ) -> Any:
        """列目录。单次最多 1000 行，响应里的 has_next/marker 用来翻页。"""
        params = {"dataset_name": dataset_name, "parent_dir": parent_dir, "page_size": int(page_size)}
        if marker:
            params["marker"] = marker
        return self.get_json("/dataset/files", params=params)

    def model_files(
        self, model_name: str, parent_dir: str = "", marker: str = "", page_size: int = 1000
    ) -> Any:
        params = {"aimodel_name": model_name, "parent_dir": parent_dir, "page_size": int(page_size)}
        if marker:
            params["marker"] = marker
        return self.get_json("/aimodel/files", params=params)

    def _subject_entry(
        self, subject_name: str, subject_type: str, file_name: str
    ) -> Optional[Mapping[str, Any]]:
        """按仓内相对路径在它的父目录里找一条清单行（文件或目录），没有则 None。"""
        target = (file_name or "").strip().strip("/")
        parent = os.path.dirname(target)
        base = os.path.basename(target)
        if not base:
            return None
        marker = ""
        for _ in range(MAX_LIST_PAGES):
            page = self.subject_files(subject_name, subject_type, parent, marker) or {}
            for row in page.get("file_list") or []:
                if (row.get("FileName") or "") == base:
                    return row
            next_marker = page.get("marker") or ""
            if not page.get("has_next") or not next_marker or next_marker == marker:
                return None
            marker = next_marker
        raise OpenIError(
            f"列目录翻了 {MAX_LIST_PAGES} 页还没结束（目录：{parent or '/'}），"
            "平台的分页标记可能没有前进。请用 parent_dir 缩小范围后重试。"
        )

    def delete_subject_file(
        self,
        subject_name: str,
        subject_type: str,
        file_name: str,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """删除数据集/模型里的一个文件，或者连同内容删掉整个子目录。

        平台对不存在的路径也回 {"code":0,"msg":"ok"}（实测），只看响应就把"根本
        没删掉"当成删掉了，所以这里删完重新列一遍父目录：路径确实不在清单里才算
        成功。删之前先确认它存在，才能区分"删掉了"和"本来就没有"。
        """
        path, param = subject_endpoint(subject_type)
        target = (file_name or "").strip().strip("/")
        if not target:
            raise OpenIError("file_name 不能为空。")
        before = self._subject_entry(subject_name, subject_type, target)
        if before is None:
            return {
                "file": target,
                "existed": False,
                "removed": False,
                "reason": "清单里没有这个文件或目录，平台也不会报错；没有删掉任何东西。",
            }
        is_dir = bool(before.get("IsDir"))
        self.delete_json(
            f"{path}/file",
            params={param: subject_name, "file_name": target},
            timeout=timeout,
        )
        return {
            "file": target,
            "existed": True,
            "is_dir": is_dir,
            "size": None if is_dir else before.get("Size"),
            "removed": self._subject_entry(subject_name, subject_type, target) is None,
        }

    def search_subjects(
        self,
        subject_type: str,
        q: str = "",
        scope: str = "public",
        order_by: str = "",
        tags: Optional[List[str]] = None,
        tasks: Optional[List[str]] = None,
        license: Optional[str] = None,
        recommend: bool = False,
        page: int = 1,
        page_size: int = SUBJECT_LIST_PAGE_SIZE,
    ) -> Dict[str, Any]:
        """在数据集 / 模型广场里检索（平台自己托管的公开或收藏主体）。

        对应前端广场页调用的 GET /{dataset|aimodel}/list/{scope}，返回体是
        {total, page, page_size, datasets|aimodels}。scope 取 public / owned /
        collected / collaborated：public 是平台公开托管的全部数据集或模型，
        owned 是当前账号的，collected 是收藏的。q 为关键词（名称 / 别名模糊匹配），
        order_by 见 SUBJECT_LIST_ORDER，recommend=True 只返回官方推荐。
        """
        if subject_type not in SUBJECT_TYPES:
            raise OpenIError(f"subject_type 只能是 dataset 或 model：{subject_type!r}")
        if scope not in SUBJECT_LIST_SCOPES:
            raise OpenIError(f"scope 只能是 {'/'.join(SUBJECT_LIST_SCOPES)}：{scope!r}")
        if order_by and order_by not in SUBJECT_LIST_ORDER:
            raise OpenIError(
                f"order_by 只能是 {'/'.join(SUBJECT_LIST_ORDER)} 或留空：{order_by!r}"
            )
        page_size = int(page_size)
        if not 1 <= page_size <= SUBJECT_LIST_MAX_PAGE_SIZE:
            raise OpenIError(
                f"page_size 必须在 1-{SUBJECT_LIST_MAX_PAGE_SIZE} 之间：{page_size}。"
                f"平台对更大的取整页会静默回退到 12 条，所以这里不替你猜。"
            )
        path = "/dataset/list" if subject_type == "dataset" else "/aimodel/list"
        params: Dict[str, Any] = {"page": int(page), "page_size": page_size}
        if q:
            params["q"] = q
        if order_by:
            params["order_by"] = order_by
        tags_csv, tasks_csv = _csv(tags), _csv(tasks)
        if tags_csv:
            params["tags"] = tags_csv
        if tasks_csv:
            params["tasks"] = tasks_csv
        if license:
            params["license"] = license
        if recommend:
            params["recommend"] = "only"
        data = self.get_json(f"{path}/{scope}", params=params) or {}
        rows = data.get("datasets") or data.get("aimodels") or []
        return {
            "total": int(data.get("total") or 0),
            "page": int(data.get("page") or page),
            "page_size": int(data.get("page_size") or page_size),
            "items": [summarize_subject(row, subject_type) for row in rows],
        }

    def subject_files(
        self, subject_name: str, subject_type: str, parent_dir: str = "", marker: str = "",
        page_size: int = 1000,
    ) -> Any:
        """列出数据集 / 模型库某个目录下的文件，含子目录（IsDir=True）。"""
        if subject_type == "dataset":
            return self.dataset_files(subject_name, parent_dir, marker, page_size)
        if subject_type == "model":
            return self.model_files(subject_name, parent_dir, marker, page_size)
        raise OpenIError(f"subject_type 只能是 dataset 或 model：{subject_type!r}")

    def storage_summary(
        self, subject_id: Optional[str] = None, subject_type: Optional[str] = None
    ) -> Dict[str, Any]:
        """GET /storage/summary。

        带 subject_id/subject_type 时返回该数据集/模型的配额明细；都不带时返回
        当前账号的总体配额（storage_limit / used_storage / remaining_storage）。
        """
        params: Dict[str, Any] = {}
        if subject_id is not None:
            params["subject_id"] = subject_id
        if subject_type is not None:
            params["subject_type"] = subject_type
        return self.get_json("/storage/summary", params=params or None)

    def subject_info(self, subject_name: str, subject_type: str) -> Dict[str, Any]:
        """数据集/模型的详情，含 uuid(id) 与当前账号是否有写权限。

        subject_type 取 "dataset" 或 "model"；subject_name 是 owner/name。
        """
        path, param = subject_endpoint(subject_type)
        return self.get_json(path, params={param: subject_name})

    def subject_id(self, subject_name: str, subject_type: str) -> str:
        """取数据集/模型的 uuid，可直接作为任务的 dataset_uuid_str / 模型 id。"""
        info = self.subject_info(subject_name, subject_type) or {}
        subject_id = info.get("id")
        if not subject_id:
            raise OpenIError(
                f"找不到 {subject_type} {subject_name}，或当前账号无权访问。"
            )
        return str(subject_id)

    def _can_edit(self, info: Mapping[str, Any], subject_type: str) -> bool:
        if subject_type == "dataset":
            return bool(info.get("can_edit_file"))
        permission = info.get("permission") or {}
        return bool(permission.get("can_edit_file") or info.get("can_edit_file"))

    def create_dataset(
        self,
        name: str,
        alias: Optional[str] = None,
        is_private: bool = False,
        categories: Optional[List[str]] = None,
        tasks: Optional[List[str]] = None,
        licenses: str = "MIT",
        owner_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """POST /dataset/create，新建一个空数据集，返回 code:0 时的 data。"""
        validate_subject_name(name)
        validate_subject_alias(alias or "")
        if owner_id is None:
            owner_id = int(self.current_user().get("id"))
        payload: Dict[str, Any] = {
            "name": name,
            "alias": alias or name,
            "is_private": bool(is_private),
            "tags": list(categories or []),
            "tasks": list(tasks or []),
            "licenses": licenses,
            "owner_id": int(owner_id),
        }
        return self.post_json("/dataset/create", json_body=payload)

    def create_model(
        self,
        name: str,
        alias: Optional[str] = None,
        is_private: bool = False,
        engine: int = 0,
        label: str = "",
        license: str = "",
        owner_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """POST /aimodel/create，新建一个空模型库（用来放 ckpt）。"""
        validate_subject_name(name)
        validate_subject_alias(alias or "")
        if owner_id is None:
            owner_id = int(self.current_user().get("id"))
        payload: Dict[str, Any] = {
            "name": name,
            "alias": alias or name,
            "aimodel_type": 1,
            "is_private": bool(is_private),
            "engine": int(engine),
            "label": label,
            "license": license,
            "owner_id": int(owner_id),
        }
        return self.post_json("/aimodel/create", json_body=payload)

    def delete_subjects(self, subject_type: str, subject_ids: List[str]) -> Any:
        """POST /{dataset|aimodel}/batch_delete?{dataset_ids|aimodel_ids}=..."""
        if subject_type == "dataset":
            param, ids_path = "dataset_ids", "/dataset/batch_delete"
        elif subject_type == "model":
            param, ids_path = "aimodel_ids", "/aimodel/batch_delete"
        else:
            raise OpenIError(f"subject_type 只能是 dataset 或 model：{subject_type!r}")
        joined = ",".join(str(i) for i in subject_ids)
        return self.post_json(ids_path, params={param: joined})

    # ------------------------------------------------------------ file upload

    def _put_to_url(self, url: str, data: bytes, content_type: Optional[str] = None) -> Optional[str]:
        """PUT 文件/分片到预签名地址。

        预签名 URL 的签名把请求头也算进去了，多带 Authorization 或 Content-Type
        都会让对象存储报 SignatureDoesNotMatch，所以这里只发文件体本身。
        """
        response = httpx.put(url, content=data, timeout=self.timeout)
        if response.status_code >= 400:
            raise OpenIError(
                f"PUT 上传失败 HTTP {response.status_code}: {response.text[:200]}",
                status_code=response.status_code,
            )
        return response.headers.get("ETag")

    def _upload_direct(self, subject_id: str, subject_type: str, entry: Mapping[str, Any]) -> None:
        file_type = _file_type_for_api(entry["path"])
        params = {
            "file_name": entry["name"],
            "subject_id": subject_id,
            "subject_type": SUBJECT_TYPES[subject_type],
            "size": entry["size"],
        }
        if file_type:
            params["file_type"] = file_type
        data = self.get_json("/upload/direct/get_upload_url", params=params) or {}
        url = data.get("url")
        if not url:
            raise OpenIError(f"直传地址获取失败：{entry['name']}")
        with open(entry["path"], "rb") as handle:
            payload = handle.read()
        self._put_to_url(url, payload, file_type)

    def _complete_direct(self, subject_id: str, subject_type: str, names: List[str]) -> None:
        if not names:
            return
        self.post_json(
            "/upload/direct/complete_upload",
            params={"subject_id": subject_id, "subject_type": SUBJECT_TYPES[subject_type]},
            json_body={"file_name_list": names},
        )

    def _upload_multipart(self, subject_id: str, subject_type: str, entry: Mapping[str, Any]) -> None:
        name, path, size = entry["name"], entry["path"], entry["size"]
        file_type = _file_type_for_api(path)
        md5 = _file_chunk_md5(path)
        chunks_info = self.get_json(
            "/upload/get_chunks",
            params={
                "md5": md5,
                "file_name": name,
                "subject_id": subject_id,
                "subject_type": SUBJECT_TYPES[subject_type],
            },
        ) or {}
        if chunks_info.get("uploaded"):
            return
        uuid = chunks_info.get("uuid")
        if not uuid:
            params = {
                "md5": md5,
                "file_name": name,
                "subject_id": subject_id,
                "subject_type": SUBJECT_TYPES[subject_type],
                "size": size,
                "total_chunk_counts": _total_chunks(size),
            }
            if file_type:
                params["file_type"] = file_type
            new = self.get_json("/upload/new_multipart", params=params) or {}
            uuid = new.get("uuid")
            if not uuid:
                raise OpenIError(f"创建分片上传失败：{name}")
            uploaded_chunks: List[int] = []
        else:
            uploaded_chunks = _uploaded_chunk_numbers(chunks_info, _total_chunks(size), name)
        # 只有"已经传完 1..N 连续一片前缀"才能用长度推下一个分片号。平台如果回的是
        # 稀疏编号，按长度推导会跳过分片却照常 complete，得到一个内容损坏但报成功的
        # 对象——所以上面先校验，这里只做单纯的续传续点。
        start_from = (len(uploaded_chunks) + 1) if uploaded_chunks else 1
        for number, chunk in _iter_chunks(path, start_from=start_from):
            url_data = self.get_json(
                "/upload/get_multipart_url",
                params={"uuid": uuid, "chunk_number": number, "size": len(chunk)},
            ) or {}
            url = url_data.get("url")
            if not url:
                raise OpenIError(f"分片 {number} 上传地址获取失败：{name}")
            self._put_to_url(url, chunk)
        self.post_json("/upload/complete_multipart", params={"uuid": uuid})

    def upload_files(
        self,
        subject_name: str,
        subject_type: str,
        entries: List[Mapping[str, Any]],
        skip_storage_check: bool = False,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """把 entries（expand_upload_paths 的结果）上传到数据集/模型。

        小文件走直传、大文件走分片，按官方 SDK 的规则各走一条通道；直传的文件
        最后统一补一次 complete_upload。返回每个文件的结果与汇总。
        dry_run=True 时不建连接，只回显会上传哪些文件、多大、各走哪条通道。
        """
        if subject_type not in SUBJECT_TYPES:
            raise OpenIError(f"subject_type 只能是 dataset 或 model：{subject_type!r}")
        if dry_run:
            total = sum(int(e["size"]) for e in entries)
            return {
                "subject": subject_name,
                "subject_type": subject_type,
                "dry_run": True,
                "file_count": len(entries),
                "total_bytes": total,
                "total_gb": _gb(total),
                "files": [
                    {
                        "name": e["name"],
                        "size": int(e["size"]),
                        "mode": "direct" if int(e["size"]) <= DIRECT_UPLOAD_MAX else "multipart",
                    }
                    for e in entries
                ],
            }
        info = self.subject_info(subject_name, subject_type) or {}
        if not info.get("id"):
            raise OpenIError(f"找不到 {subject_type} {subject_name}，或当前账号无权访问。")
        if not self._can_edit(info, subject_type):
            raise OpenIError(f"当前账号没有向 {subject_name} 写入文件的权限。")
        subject_id = str(info["id"])

        total_size = sum(int(e["size"]) for e in entries)
        storage_warning = None
        try:
            summary = self.storage_summary(subject_id, SUBJECT_TYPES[subject_type])
            remaining = int(summary.get("remaining_storage") or 0)
            limit = int(summary.get("storage_limit") or 0)
            if not skip_storage_check and limit != -1 and remaining < total_size:
                raise OpenIError(
                    f"剩余配额不足：剩余 {remaining} 字节，本次需要 {total_size} 字节。"
                )
        except OpenIError:
            raise
        except Exception as exc:  # noqa: BLE001 - 配额查询失败不阻塞上传
            storage_warning = f"配额查询失败，已跳过：{exc}"

        results: List[Dict[str, Any]] = []
        direct_names: List[str] = []
        for entry in entries:
            record = {
                "name": entry["name"],
                "size": int(entry["size"]),
                "mode": "direct" if int(entry["size"]) <= DIRECT_UPLOAD_MAX else "multipart",
            }
            try:
                if record["mode"] == "direct":
                    self._upload_direct(subject_id, subject_type, entry)
                    direct_names.append(entry["name"])
                else:
                    self._upload_multipart(subject_id, subject_type, entry)
                record["ok"] = True
            except Exception as exc:  # noqa: BLE001 - 逐文件汇报失败
                record["ok"] = False
                record["error"] = str(exc)
            results.append(record)
        self._complete_direct(subject_id, subject_type, direct_names)

        succeeded = [r for r in results if r.get("ok")]
        failed = [r for r in results if not r.get("ok")]
        return {
            "subject": subject_name,
            "subject_type": subject_type,
            "subject_id": subject_id,
            # 顶层 ok：全部文件都成功才算成功。只有 failed_count 的话，调用方
            # result["ok"] 会 KeyError，或者把 {failed_count: N} 当成没问题。
            "ok": not failed,
            "uploaded_count": len(succeeded),
            "failed_count": len(failed),
            "uploaded_bytes": sum(r["size"] for r in succeeded),
            "storage_warning": storage_warning,
            "files": results,
        }

    # ---------------------------------------------------------- file download

    def file_download_url(
        self, subject_name: str, subject_type: str, file_name: str, parent_dir: str = ""
    ) -> str:
        """解析平台托管文件的直链（OBS 预签名 URL）。

        /dataset/file 与 /aimodel/file 对托管在对象存储上的文件返回 301，
        Location 就是带签名的下载地址，无需再带令牌。
        """
        path, param = subject_endpoint(subject_type)
        path = f"{path}/file"
        if not self.creds.has_token:
            raise OpenIError("NO_TOKEN: 没有找到启智令牌。")
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Authorization": f"Bearer {self.creds.token}",
        }
        params = {param: subject_name, "file_name": file_name, "parent_dir": parent_dir}
        with httpx.Client(
            base_url=f"{self.creds.endpoint}/api/v1",
            timeout=self.timeout,
            follow_redirects=False,
            headers=headers,
        ) as client:
            response = client.get(path, params=params)
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            if location:
                return location
        raise OpenIError(
            f"取下载地址失败 HTTP {response.status_code}：{file_name}",
            status_code=response.status_code,
            payload=response.text[:200],
        )

    def download_file(
        self,
        subject_name: str,
        subject_type: str,
        file_name: str,
        dest_path: str,
        parent_dir: str = "",
    ) -> Dict[str, Any]:
        """下载一个托管文件到本地 dest_path，返回字节数与 md5（下载后校验用）。"""
        url = self.file_download_url(subject_name, subject_type, file_name, parent_dir)
        dest_dir = os.path.dirname(os.path.abspath(dest_path))
        os.makedirs(dest_dir, exist_ok=True)
        # 单个文件可能十几 GB，所以总时长不限，但每次读要有超时：否则连接被
        # 静默挂住时这里会一直卡着，MCP 调用方看不到任何反馈。
        with httpx.stream(
            "GET", url, timeout=DOWNLOAD_TIMEOUT, follow_redirects=True
        ) as response:
            if response.status_code >= 400:
                raise OpenIError(
                    f"下载失败 HTTP {response.status_code}：{file_name}",
                    status_code=response.status_code,
                )
            written = _stream_to_file(
                response.iter_bytes(DOWNLOAD_CHUNK_BYTES), dest_path, file_name
            )
        return {
            "file": file_name,
            "bytes": written["bytes"],
            "md5": written["md5"],
            "path": written["path"],
        }

    def walk_subject_files(
        self, subject_name: str, subject_type: str, parent_dir: str = "",
        limit: int = WALK_FILE_LIMIT,
    ) -> List[Dict[str, Any]]:
        """递归列出主体里的全部文件：{name(仓内相对路径), size, mtime}。"""
        collected: List[Dict[str, Any]] = []

        def walk(directory: str) -> None:
            marker = ""
            for _ in range(MAX_LIST_PAGES):
                page = self.subject_files(subject_name, subject_type, directory, marker) or {}
                for row in page.get("file_list") or []:
                    name = row.get("FileName") or ""
                    child = f"{directory}/{name}".strip("/") if directory else name
                    if row.get("IsDir"):
                        walk(child)
                    else:
                        if len(collected) >= limit:
                            raise OpenIError(
                                f"文件数超过 {limit}，已停止遍历。请改用 file 指定单个文件，"
                                "或先用 parent_dir 缩小范围、用 dry_run 先看清单。"
                            )
                        collected.append(
                            {
                                "name": child,
                                "size": int(row.get("Size") or 0),
                                "mod_time": row.get("ModTime") or "",
                            }
                        )
                next_marker = page.get("marker") or ""
                if not page.get("has_next") or not next_marker or next_marker == marker:
                    break
                marker = next_marker
            else:
                # for-else: 走满 MAX_LIST_PAGES 还没 break，说明分页没收敛。
                # 静默返回半份清单比报错更糟——调用方会以为"就这些文件"。
                raise OpenIError(
                    f"列目录翻了 {MAX_LIST_PAGES} 页还没结束（目录：{directory or '/'}）。"
                    "平台的分页标记可能没有前进；请用 parent_dir 缩小范围后重试。"
                )

        walk(parent_dir)
        return collected

    def _remote_file_size(
        self, subject_name: str, subject_type: str, file_name: str
    ) -> Optional[int]:
        """查单个文件在平台上的大小，用于 dry_run 预览与 max_bytes 拦截。

        平台没有「按文件名查元数据」的接口，只能在它所在的目录里翻页找。查不到
        就返回 None，让调用方退回到「不知道大小」的行为。
        """
        try:
            row = self._subject_entry(subject_name, subject_type, file_name)
        except Exception:  # noqa: BLE001 - 只是预览/拦截用的信息，查不到不算失败
            return None
        if row is None or row.get("IsDir"):
            return None
        return int(row.get("Size") or 0)

    def pull_subject(
        self,
        subject_name: str,
        subject_type: str,
        dest_dir: str,
        file: Optional[str] = None,
        parent_dir: str = "",
        dry_run: bool = False,
        overwrite: bool = False,
        max_bytes: Optional[int] = None,
        max_files: Optional[int] = None,
        include_resource_version: bool = False,
    ) -> Dict[str, Any]:
        """把平台托管的文件拉取到本地目录。

        file 为空时递归下载整个主体（保留目录结构），否则只下载该文件。
        dry_run=True 只列大小不下载。max_bytes 是总字节上限，超出直接报错，
        防止误拉几十 GB 的 ckpt。已存在且大小一致的文件默认跳过，便于续传。
        max_files 覆盖遍历时的文件数上限（默认 WALK_FILE_LIMIT，可用环境变量
        OPENI_WALK_FILE_LIMIT 调），文件特别多的数据集需要它才能列全。
        数据集里自带的 openi_resource.version 默认跳过。
        """
        if subject_type not in SUBJECT_TYPES:
            raise OpenIError(f"subject_type 只能是 dataset 或 model：{subject_type!r}")
        dest_root = os.path.abspath(os.path.expanduser(dest_dir))
        # 先按主体总大小做一次廉价的上限拦截，避免对超大主体做全量遍历。
        if max_bytes is not None and not file:
            try:
                info = self.subject_info(subject_name, subject_type) or {}
                declared = int(info.get("size") or 0)
            except Exception:  # noqa: BLE001 - 查不到就退回到遍历后再校验
                declared = 0
            if declared > max_bytes:
                raise OpenIError(
                    f"{subject_name} 共 {declared} 字节，超过 max_bytes={max_bytes}。"
                    "请用 file 指定单个文件，或调大 max_bytes。"
                )
        if file:
            # file 是相对主体根目录的仓内路径，例如 "imagenet/README.md"。
            targets = [{"name": file, "size": self._remote_file_size(subject_name, subject_type, file)}]
            base_dir = ""
        else:
            targets = self.walk_subject_files(
                subject_name, subject_type, parent_dir, limit=max_files or WALK_FILE_LIMIT
            )
            base_dir = parent_dir
            if not include_resource_version:
                targets = [t for t in targets if t["name"] != "openi_resource.version"]
        known_total = sum(t["size"] or 0 for t in targets)
        if max_bytes is not None and known_total > max_bytes:
            if file:
                hint = "调大 max_bytes，或者去掉这个参数"
            else:
                hint = "改用 file 指定单个文件，或用 parent_dir 缩小范围，或调大 max_bytes"
            raise OpenIError(
                f"待下载 {known_total} 字节，超过 max_bytes={max_bytes}；"
                f"{hint}。"
            )
        plan = {
            "subject": subject_name,
            "subject_type": subject_type,
            "dest_dir": dest_root,
            "file_count": len(targets),
            "total_bytes": known_total,
            "total_gb": _gb(known_total),
        }
        if dry_run:
            return {**plan, "dry_run": True, "files": targets}

        downloaded: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        for target in targets:
            rel = target["name"]
            inside = rel[len(base_dir) :].lstrip("/") if base_dir and rel.startswith(base_dir) else rel
            dest_path = os.path.normpath(os.path.join(dest_root, inside))
            # 文件名来自平台元数据，别让 ".." 之类的相对路径写到目标目录外面。
            if dest_path != dest_root and not dest_path.startswith(dest_root + os.sep):
                failed.append({"file": rel, "error": f"路径越出目标目录，已跳过：{inside!r}"})
                continue
            try:
                if (
                    not overwrite
                    and os.path.isfile(dest_path)
                    and target["size"] is not None
                    and os.path.getsize(dest_path) == target["size"]
                ):
                    skipped.append({"file": rel, "bytes": target["size"], "reason": "already-present"})
                    continue
                record = self.download_file(
                    subject_name, subject_type, os.path.basename(rel), dest_path,
                    parent_dir=os.path.dirname(rel),
                )
                record["file"] = rel
                downloaded.append(record)
            except Exception as exc:  # noqa: BLE001 - 逐文件汇报失败
                failed.append({"file": rel, "error": str(exc)})
        return {
            **plan,
            "ok": not failed,
            "downloaded_count": len(downloaded),
            "skipped_count": len(skipped),
            "failed_count": len(failed),
            "downloaded_bytes": sum(r["bytes"] for r in downloaded),
            "files": downloaded,
            "skipped": skipped,
            "failed": failed,
        }

    # ------------------------------------------------------------ reward points

    def _web_get(self, path: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        """GET on the web surface (outside /api/v1) using HTTP Basic auth.

        The reward/points endpoints are served by the web app, not the REST API:
        a Bearer token there just redirects to the sign-in page, while Basic auth
        with the token as the password (any username) returns JSON. The envelope
        is {Code, Msg, Data} with capital letters, unlike the REST API.
        """
        if not self.creds.has_token:
            raise OpenIError(
                "NO_TOKEN: 没有找到启智令牌。请在 https://openi.pcl.ac.cn/user/settings/applications "
                "生成令牌，然后设置 OPENI_TOKEN 环境变量，或执行 openi login。"
            )
        auth = base64.b64encode(f"openi:{self.creds.token}".encode()).decode()
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json",
            "Authorization": f"Basic {auth}",
        }
        with httpx.Client(base_url=self.creds.endpoint, timeout=self.timeout, headers=headers) as web:
            response = web.get(path, params=params)
        if response.status_code != 200:
            raise OpenIError(
                f"HTTP {response.status_code} for GET {path}",
                status_code=response.status_code,
                payload=response.text[:300],
            )
        payload = _unwrap_text(response)
        if isinstance(payload, Mapping):
            code = payload.get("Code")
            if code not in (0, "0", None):
                raise OpenIError(f"platform error {code}: {payload.get('Msg')}", payload=payload)
            if "Data" in payload:
                return payload["Data"]
        return payload

    def points_account(self) -> Dict[str, Any]:
        """Available / total earned / total consumed compute points."""
        return self._web_get("/reward/point/account")

    def points_rules(self) -> Dict[str, Any]:
        """Official earning rules: per-action points, cycle, per-cycle limits."""
        return self._web_get("/reward/point/rule/config")

    def points_records(self, operate: str = "INCREASE", page: int = 1) -> Dict[str, Any]:
        """Point ledger, 10 rows per page. operate: INCREASE or DECREASE."""
        return self._web_get(
            "/reward/point/record/list",
            params={"Operate": operate.upper(), "Page": page},
        )

    # --------------------------------------------------------- creation options

    def creation_info(
        self,
        job_type: str = "DEBUG",
        compute_source: str = "GPU",
        cluster_type: str = DEFAULT_CLUSTER,
        visualize_required: bool = False,
        repo: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /ai_task/creation/required.

        Returns the live, account specific create form data: available specs
        (with their numeric spec_id), usable images, branches, the default
        display name, points balance and whether a new task may be created.
        """
        params: Dict[str, Any] = {
            "job_type": job_type,
            "compute_source": compute_source,
            "cluster_type": cluster_type,
            "visualize_required": str(bool(visualize_required)).lower(),
        }
        if repo:
            # The platform scopes branch lookup to the repo when present.
            params["repo"] = repo
        return self.get_json("/ai_task/creation/required", params=params)

    def specs(
        self,
        job_type: str = "DEBUG",
        compute_source: str = "GPU",
        cluster_type: str = DEFAULT_CLUSTER,
        visualize_required: bool = False,
        repo: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Just the spec list from creation_info."""
        info = self.creation_info(job_type, compute_source, cluster_type, visualize_required, repo)
        return list((info.get("specs") or {}).get("all") or [])

    def images_for_spec(
        self,
        spec_id: int,
        job_type: str = "DEBUG",
        compute_source: str = "GPU",
        cluster_type: str = DEFAULT_CLUSTER,
        has_internet: int = 2,
        visualize_required: bool = False,
    ) -> Dict[str, Any]:
        return self.get_json(
            "/ai_task/creation/image_by_spec",
            params={
                "job_type": job_type,
                "compute_source": compute_source,
                "cluster_type": cluster_type,
                "spec_id": spec_id,
                "has_internet": has_internet,
                "visualize_required": str(bool(visualize_required)).lower(),
            },
        )

    def recommended_images(self) -> Dict[str, Any]:
        """GET /images/recommend - images the platform recommends."""
        return self.get_json("/images/recommend")

    def starred_images(self) -> Dict[str, Any]:
        """GET /images/star - images this account starred; all known to work."""
        return self.get_json("/images/star")

    def compute_resources(self) -> Dict[str, Any]:
        """平台上线的算力中心与卡型清单（比离线目录更全、更新）。

        GET /resources/ai_center/card_info/active 给的是全平台在线卡型（比
        /resources/acc_card/list 多出 CPU、GPGPU 的 N100/N260、GPU 的 5090/A800），
        GET /resources/ai_center/active 给的是每个算力中心所在城市与卡型。
        两者和 /ai_task/creation/required 不同：这里回答“平台有什么”，不回答
        “我这个账号现在能开哪些规格”。
        """
        cards = self.get_json("/resources/ai_center/card_info/active") or {}
        centers = self.get_json("/resources/ai_center/active") or {}
        return {
            "acc_card_types": cards.get("list") or [],
            "ai_centers": centers.get("list") or [],
        }

    # ------------------------------------------------------------- compute tasks

    def sdk_code(
        self,
        job_type: str,
        compute_source: str = "GPU",
        cluster_type: str = DEFAULT_CLUSTER,
        visualize_required: bool = False,
    ) -> Dict[str, Any]:
        return self.get_json(
            "/ai_task/generate_sdk_code",
            params={
                "job_type": job_type,
                "compute_source": compute_source,
                "cluster_type": cluster_type,
                "visualize_required": str(visualize_required).lower(),
            },
        )

    def task(self, task_id: Any) -> Dict[str, Any]:
        """GET /api/v1/ai_task?id= - full detail of one task (triggers sync)."""
        return self.get_json("/ai_task", params={"id": task_id})

    def task_brief(self, task_id: Any) -> Dict[str, Any]:
        """GET /api/v1/ai_task/brief?id= - compact status, cheap to poll."""
        return self.get_json("/ai_task/brief", params={"id": task_id})

    def task_resource_usage(self, task_id: Any) -> Any:
        return self.get_json("/ai_task/resource_usage", params={"id": task_id})

    def task_node_info(self, task_id: Any) -> Any:
        return self.get_json("/ai_task/node_info", params={"id": task_id})

    def task_operation_profile(self, task_id: Any) -> Any:
        """GET /api/v1/ai_task/operation_profile?id= - 任务调度事件时间线。

        平台按“分配节点 → 拉镜像 → 建容器 → 启动 → 结束”逐条记录，每条含
        message / name / reason / timestamp / action，时间戳是 UTC。任务卡在
        WAITING、被抢占或异常退出时，这里能看出卡在哪一步、为什么。
        """
        return self.get_json("/ai_task/operation_profile", params={"id": task_id})

    def task_output(self, task_id: Any, parent_dir: str = "") -> Any:
        """GET /api/v1/ai_task/output?id= - 列出任务输出目录（一次一层）。

        parent_dir 是相对输出根的子目录，留空看根目录。返回 output.file_list 每项
        有 FileName / IsDir / Size / ModTime。注意 output.status 为 1 时平台还在
        索引，file_list 通常为空；任务跑完后要等一两分钟才刷得出文件。
        """
        params: Dict[str, Any] = {"id": task_id}
        if parent_dir:
            params["parent_dir"] = parent_dir
        return self.get_json("/ai_task/output", params=params)

    def task_output_all(self, task_id: Any) -> Any:
        """GET /api/v1/ai_task/output/all?id= - 输出目录的完整递归清单。

        和 task_output 不同，这里一次给全（含子目录），每项的 FileName 就是相对
        输出根的完整路径，可直接用作 task_output_download 的 file_name。
        """
        return self.get_json("/ai_task/output/all", params={"id": task_id})

    def task_output_download(
        self,
        task_id: Any,
        file_name: str,
        parent_dir: str = "",
        save_to: Optional[str] = None,
        max_bytes: Optional[int] = None,
    ) -> Dict[str, Any]:
        """下载任务输出里的一个文件（GET /ai_task/output/download）。

        file_name 可以是纯文件名（再配 parent_dir），也可以是相对输出根的完整
        路径（如 "sub/mid.txt"，此时 parent_dir 留空），两种都能命中。给 save_to
        就流式落盘并回大小与 md5；不给则把内容读进内存（能按 UTF-8 解码就当文本，
        否则给 base64），max_bytes 是内存里的截断上限。
        """
        params: Dict[str, Any] = {"id": task_id, "file_name": file_name}
        if parent_dir:
            params["parent_dir"] = parent_dir
        return self._download(
            "/ai_task/output/download",
            params,
            save_to=save_to,
            max_bytes=max_bytes,
            label=file_name,
        )

    def task_output_download_all(
        self,
        task_id: Any,
        save_to: Optional[str] = None,
        max_bytes: Optional[int] = None,
    ) -> Dict[str, Any]:
        """整包下载任务输出（GET /ai_task/output/download/all?id=）。

        平台把整个输出目录打成一个 zip 直接回（Content-Type 是
        application/octet-stream，不走 307 跳 OBS 签名直链那条路）。产物多的时候
        比逐个文件拉省事；任务没产物时它会回一个只有 end-of-central-directory 记
        录的空 zip（约 22 字节），那不是错误。

        一定要给 save_to：整包可能很大，这条接口是流式落盘的，不适合读进内存。
        max_bytes 给上是硬上限，超了立即中止并删掉半截包。
        """
        return self._download(
            "/ai_task/output/download/all",
            {"id": task_id},
            save_to=save_to,
            max_bytes=max_bytes,
            label=f"task-{task_id}-output.zip",
        )

    def task_log_download(
        self,
        task_id: Any,
        save_to: Optional[str] = None,
        max_bytes: Optional[int] = None,
    ) -> Dict[str, Any]:
        """整份任务日志（GET /ai_task/log/download?id=）。

        和分页的 task_log 不同，这个接口一次性返回完整日志纯文本，适合把日志整段
        留存或喂给别的工具分析。给 save_to 就写文件，否则把文本带回内存；max_bytes
        在落盘时是硬上限、在内存里是截断上限（截断时带 truncated 标记）。
        """
        return self._download(
            "/ai_task/log/download",
            {"id": task_id},
            save_to=save_to,
            max_bytes=max_bytes,
            label=f"task-{task_id}.log",
        )

    def task_loss(self, task_id: Any) -> Any:
        """GET /api/v1/ai_task/loss?id= - 训练任务的 loss 曲线。

        返回 [{epoch, loss}, ...]；任务没上报 loss 时是空列表。
        """
        return self.get_json("/ai_task/loss", params={"id": task_id})

    # --------------------------------------------------------------- task output

    def export_task_to_dataset(
        self, task_id: Any, dataset_id: str, file_list: Any
    ) -> Dict[str, Any]:
        """把任务输出文件搬到已有的数据集里（POST /ai_task/export2dataset）。

        这是任务产物落库的通道：训练跑完的输出（ckpt、metrics、生成语料）本来只
        存在于该任务的输出目录里，跟着任务走；导出之后就是数据集里的普通文件，
        可以挂到别的任务上、被别人拉取，也不受任务清理影响。

        参数不是常规 JSON：查询串收 dataset_id 与 id，表单体收 task_id 与
        file_list_str（平台 Go 字段名 FileListStr 的蛇形写法）。JSON 体会被平台
        前置的反向代理直接 403，所以这里固定走 post_form。

        file_list 是相对输出根的路径，用逗号分隔，即 task_output_all() 里每项的
        FileName；子目录要连同父目录一起写（"ckpt/model.bin"）。导出是异步的，
        返回的是进度 id，用 export_progress("dataset", id) 查进度。
        """
        files = _csv(file_list)
        if not files:
            raise OpenIError("file_list 不能为空，先看 openi_task_output_files 拿到路径。")
        data = self.post_form(
            "/ai_task/export2dataset",
            params={"dataset_id": dataset_id, "id": task_id},
            data={"task_id": task_id, "file_list_str": files},
        )
        return {
            "subject_type": "dataset",
            "subject_id": dataset_id,
            "task_id": task_id,
            "file_list": files,
            "process_id": (data or {}).get("id") if isinstance(data, Mapping) else data,
        }

    def export_task_to_model(
        self,
        task_id: Any,
        name: str,
        file_list: Any,
        alias: Optional[str] = None,
        license: str = "",
        label: str = "",
        engine: int = 0,
        is_private: bool = False,
        owner_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """把任务输出文件建成一个新模型库（POST /ai_task/export2aimodel）。

        和导出到数据集不同，这个接口不是“放进已有的库”，而是就地新建一个模型，
        所以 name 必须是当前账号下还不存在的英文名。查询串收 id，表单体收
        name / alias / owner_id / aimodel_type / is_private / engine / label /
        license / task_id / file_list_str（默认字段 file_list 会被平台无视，
        报 "file list is empty"）。

        返回的 process_id 用 export_progress("model", id) 查进度。
        """
        validate_subject_name(name)
        validate_subject_alias(alias or "")
        files = _csv(file_list)
        if not files:
            raise OpenIError("file_list 不能为空，先看 openi_task_output_files 拿到路径。")
        if owner_id is None:
            owner_id = int(self.current_user().get("id"))
        data = self.post_form(
            "/ai_task/export2aimodel",
            params={"id": task_id},
            data={
                "name": name,
                "alias": alias or name,
                "owner_id": int(owner_id),
                "aimodel_type": 0,
                "is_private": "true" if is_private else "false",
                "engine": int(engine),
                "label": label or "",
                "license": license or "",
                "task_id": task_id,
                "file_list_str": files,
            },
        )
        data = data if isinstance(data, Mapping) else {}
        return {
            "subject_type": "model",
            "subject_id": data.get("id") or name,
            "name": data.get("name") or name,
            "task_id": task_id,
            "file_list": files,
            "process_id": data.get("process_id"),
        }

    def export_progress(self, subject_type: str, process_id: str) -> Dict[str, Any]:
        """查一次导出进度（GET /{dataset|aimodel}/get_export_process?id=）。

        返回 {file: 百分比}：100 表示该文件导出完成，-1 / -2 / -3 是失败
        （源文件读不到、目标已存在同名文件、超出存储限额），0 是刚开始。
        """
        if subject_type == "dataset":
            path = "/dataset/get_export_process"
        elif subject_type == "model":
            path = "/aimodel/get_export_process"
        else:
            raise OpenIError(f"subject_type 只能是 dataset 或 model：{subject_type!r}")
        raw = self.get_json(path, params={"id": process_id}) or {}
        if not isinstance(raw, Mapping):
            return {"files": {}, "finished": False, "raw": raw}
        files = {k: v for k, v in raw.items() if k != "##type##"}
        done = [k for k, v in files.items() if v == 100]
        failed = [k for k, v in files.items() if v in (-1, -2, -3)]
        return {
            "files": files,
            "finished": bool(files) and len(done) + len(failed) == len(files),
            "succeeded": done,
            "failed": failed,
        }

    def wait_export(
        self, subject_type: str, process_id: str, wait_seconds: int = 60, interval: float = 3.0
    ) -> Dict[str, Any]:
        """轮询导出进度直到全部结束或等满 wait_seconds，返回最后一次进度。"""
        deadline = time.monotonic() + max(0, int(wait_seconds))
        progress = self.export_progress(subject_type, process_id)
        while not progress["finished"] and time.monotonic() < deadline:
            time.sleep(interval)
            progress = self.export_progress(subject_type, process_id)
        progress["process_id"] = process_id
        progress["subject_type"] = subject_type
        return progress

    def _download(
        self,
        path: str,
        params: Mapping[str, Any],
        *,
        save_to: Optional[str] = None,
        max_bytes: Optional[int] = None,
        label: str = "",
    ) -> Dict[str, Any]:
        """流式下载一个返回原始文件（非 JSON）的接口。

        给 save_to 时边走边写 <目标>.part，下完再改名，中途失败不留半截文件，
        同时算出 md5；不给 save_to 时把内容读进内存：能按 UTF-8 解码就当文本
        返回，否则给 base64。max_bytes 在落盘时是硬上限（超了立即中止），在内存
        模式里是截断上限（截断时 truncated 为 True）。
        """
        if not self.creds.has_token:
            raise OpenIError("NO_TOKEN: 没有找到启智令牌。")
        headers = {"Authorization": f"Bearer {self.creds.token}"}
        timeout = DOWNLOAD_TIMEOUT
        if save_to:
            dest = os.path.abspath(os.path.expanduser(save_to))
            if os.path.isdir(dest) or save_to.endswith(os.sep):
                os.makedirs(dest, exist_ok=True)
                dest = os.path.join(dest, os.path.basename(label) or "download.bin")
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            with self._http.stream(
                "GET", path, params=params, headers=headers, timeout=timeout
            ) as response:
                if response.status_code >= 400:
                    raise OpenIError(
                        f"下载失败 HTTP {response.status_code}：{path}",
                        status_code=response.status_code,
                        payload=response.read().decode("utf-8", "replace")[:200],
                    )
                written = _stream_to_file(
                    response.iter_bytes(DOWNLOAD_CHUNK_BYTES),
                    dest,
                    label or path,
                    max_bytes=max_bytes,
                )
            return {
                "path": written["path"],
                "bytes": written["bytes"],
                "mb": round(written["bytes"] / 1024 / 1024, 3),
                "md5": written["md5"],
            }
        # 内存模式也必须边读边停：任务输出里的 ckpt 能有几十 GB，先 response.content
        # 再截断等于限制还没生效就先把进程撑爆了。所以这里流式读取，够了就断开。
        chunks: List[bytes] = []
        total = 0
        truncated = False
        with self._http.stream(
            "GET", path, params=params, headers=headers, timeout=timeout
        ) as response:
            if response.status_code >= 400:
                raise OpenIError(
                    f"下载失败 HTTP {response.status_code}：{path}",
                    status_code=response.status_code,
                    payload=response.read().decode("utf-8", "replace")[:200],
                )
            # 没给 max_bytes 时也要有个上限，避免一次调用把整个大文件读进内存。
            budget = max_bytes if max_bytes is not None else IN_MEMORY_DOWNLOAD_MAX_BYTES
            for chunk in response.iter_bytes(DOWNLOAD_CHUNK_BYTES):
                if total + len(chunk) > budget:
                    chunks.append(chunk[: budget - total])
                    total = budget
                    truncated = True
                    break
                chunks.append(chunk)
                total += len(chunk)
        blob = b"".join(chunks)
        result: Dict[str, Any] = {"bytes": len(blob)}
        try:
            result["text"] = blob.decode("utf-8")
        except UnicodeDecodeError:
            result["base64"] = base64.b64encode(blob).decode("ascii")
        if truncated:
            result["truncated"] = True
            result["truncated_at_bytes"] = total
            result["hint"] = (
                "内容超过上限已被截断；要完整拿到请给 save_to 落盘"
                "（落盘模式没有这个限制，且不会把内容读进内存）。"
            )
        return result

    def list_tasks(self, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        """GET /api/v1/ai_task/my_list - every task of the account."""
        return self.get_json("/ai_task/my_list", params={"page": page, "page_size": page_size})

    def repo_tasks(self, repo: str) -> Any:
        """Tasks that belong to one repo (owner/name)."""
        return self.get_json(f"/{repo}/ai_task")

    def task_log(
        self,
        task_id: Any,
        lines: int = 200,
        order: str = "desc",
        base_line: Any = -1,
        node_id: Optional[Any] = None,
        file_name: Optional[str] = None,
        repo: Optional[str] = None,
    ) -> Any:
        """Read task logs. Both /ai_task/log and /{repo}/ai_task/log exist."""
        params: Dict[str, Any] = {"id": task_id, "lines": lines, "order": order, "base_line": base_line}
        if node_id is not None:
            params["node_id"] = node_id
        if file_name:
            params["file_name"] = file_name
        path = f"/{repo}/ai_task/log" if repo else "/ai_task/log"
        return self.get_json(path, params=params)

    def debug_url(self, task_id: Any, repo: Optional[str] = None) -> Any:
        """Jupyter / SSH entry point of a running debug task."""
        path = f"/{repo}/ai_task/debug_url" if repo else "/ai_task/debug_url"
        return self.get_json(path, params={"id": task_id})

    def visualize_url(self, task_id: Any) -> Any:
        """TensorBoard entry point of a training task."""
        return self.get_json("/ai_task/visualize_url", params={"id": task_id})

    def self_endpoint_url(self, task_id: Any) -> Any:
        """GET /api/v1/ai_task/self_endpoint_url?id= - ONLINEINFERENCE 的对外地址。

        只有 ONLINEINFERENCE 任务才有对外地址，平台返回
        {"url": "https://<hex>.mlunotebook.openi.org.cn"}。这个地址在任务还是
        WAITING 时就已返回，但那时服务没起来、连不上；预热期间该接口也会返回
        {"code": 99, "msg": "The service is loading, please try again later."}，
        隔一会儿重试即可。非在线推理任务拿到的是空 url。
        """
        return self.get_json("/ai_task/self_endpoint_url", params={"id": task_id})

    def service_url(self, task_id: Any) -> Dict[str, Any]:
        """在线推理服务的对外地址，附带平台自动生成的 end_point 与端口。

        先用 /ai_task/self_endpoint_url 拿真实可访问的 URL（服务没起来时它会返回
        code 99，这里把状态原样带回去让人知道该重试），同时读任务详情里的
        end_point / port 作为兜底信息。注意容器里的服务必须监听任务详情的
        port 字段（在线推理固定 8000），平台不会往容器注入 PORT /
        SERVICE_PORT 之类的环境变量。
        """
        info: Dict[str, Any] = {"task_id": task_id}
        try:
            data = self.task(task_id)
        except OpenIError:
            data = None
        task = data.get("task") if isinstance(data, Mapping) and isinstance(data.get("task"), Mapping) else data
        if isinstance(task, Mapping):
            info["status"] = task.get("status")
            info["port"] = task.get("port")
            info["end_point"] = task.get("end_point")
            info["app_name"] = task.get("app_name")
        try:
            url_data = self.self_endpoint_url(task_id)
        except OpenIError as exc:
            info["url_error"] = str(exc)
            return info
        if isinstance(url_data, Mapping):
            if "url" in url_data:
                info["url"] = url_data.get("url")
            # 平台错误信封是 {code, msg}（加载中时 code 为 99）。
            if "code" in url_data:
                info["code"] = url_data.get("code")
                info["msg"] = url_data.get("msg")
        else:
            info["url"] = url_data
        return info

    def task_action(self, action: str, task_id: Any, repo: Optional[str] = None) -> Any:
        """Run stop / restart / del on a task.

        All three accept id only; the repo scoped variants only exist for a
        subset and are not required.

        This only reports that the request was accepted. Use
        :meth:`task_action_verified` when the caller must know whether the task
        actually reached the new state — these calls are billed while a task is
        still RUNNING, so a silent no-op here means paying for nothing.
        """
        if action not in ("stop", "restart", "del"):
            raise ValueError(f"unsupported action: {action}")
        return self.post_json(f"/ai_task/{action}", params={"id": task_id}, json_body={})

    def task_action_verified(
        self,
        action: str,
        task_id: Any,
        wait_seconds: float = 30.0,
        interval: float = 2.0,
    ) -> Dict[str, Any]:
        """Run stop/restart/del and confirm the task actually changed state.

        The platform's POST returns success as soon as the request is accepted;
        the task then walks through transitional states (STOPPING, DELETING,
        QUEUED...). Stamping ``ok: true`` on that response tells the caller
        nothing about whether the card was actually released.

        So after the mutation this polls ``/ai_task/brief`` until the status
        reaches a state consistent with the action, and reports what it saw.
        ``ok`` is true only when the transition was observed before the deadline;
        on timeout it is false and ``status_at_timeout`` says where the task is,
        because a task left in RUNNING is still burning points.
        """
        if action not in ("stop", "restart", "del"):
            raise ValueError(f"unsupported action: {action}")
        before = self._safe_task_status(task_id)
        payload = self.task_action(action, task_id)
        expected = _TASK_ACTION_EXPECTED.get(action, set())

        deadline = time.monotonic() + max(0.0, float(wait_seconds))
        status = before
        polls = 0
        while True:
            status = self._safe_task_status(task_id)
            polls += 1
            if not status:
                # del removes the task entirely: "gone" is the success signal.
                if action == "del":
                    return {
                        "ok": True, "action": action, "task_id": task_id,
                        "status_before": before, "status": None, "polls": polls,
                        "note": "任务已不存在，删除完成。",
                    }
                break
            if status.upper() in expected:
                return {
                    "ok": True, "action": action, "task_id": task_id,
                    "status_before": before, "status": status, "polls": polls,
                    "payload": payload,
                }
            if action == "del" and status.upper() in _TASK_TERMINAL_STATUSES:
                # The listing no longer serves the row but it is not an error.
                return {
                    "ok": True, "action": action, "task_id": task_id,
                    "status_before": before, "status": status, "polls": polls,
                    "note": "任务已离开列表，删除完成。",
                }
            if time.monotonic() >= deadline:
                break
            time.sleep(max(0.1, float(interval)))

        return {
            "ok": False, "action": action, "task_id": task_id,
            "status_before": before, "status_at_timeout": status, "polls": polls,
            "payload": payload,
            "error": (
                f"{wait_seconds:g}s 内任务状态没有变为 {'/'.join(sorted(expected)) or '终态'}"
                f"（现在仍是 {status!r}）。若为 stop 且状态仍为 RUNNING，任务仍在计费，"
                f"请稍后再查 openi_get_task。"
            ),
        }

    def _safe_task_status(self, task_id: Any) -> str:
        """Read a task's status, tolerating the shapes the brief endpoint uses."""
        try:
            brief = self.task_brief(task_id)
        except Exception:  # noqa: BLE001 - a transient read must not mask the action
            return ""
        if isinstance(brief, Mapping):
            for key in ("status", "Status", "task_status", "TaskStatus"):
                value = brief.get(key)
                if isinstance(value, str) and value:
                    return value
        return ""

    def batch_delete(self, task_ids: List[Any]) -> Any:
        return self.post_json("/ai_task/batch_del", json_body={"id": list(task_ids)})

    # ------------------------------------------------------------ task templates

    def list_task_templates(
        self,
        scope: str = "public",
        q: str = "",
        job_type: str = "",
        compute_source: str = "",
        tags: Optional[List[str]] = None,
        order_by: str = "",
        page: int = 1,
        page_size: int = SUBJECT_LIST_PAGE_SIZE,
    ) -> Dict[str, Any]:
        """任务模板广场（对应平台“任务模板”页的卡片列表）。

        GET /ai_task_template/list/{scope}，scope 取 public / all / created /
        collected；返回体是 {Page, PageSize, Total, Templates}。每行自带创建任务
        需要的全部字段（JobType、Cluster、ComputeSource、AccCardType、
        AccCardsNum、BranchName、BootFile、数据集与模型清单），所以挑一个模板
        就能直接开任务，不需要再查详情。

        tags 按模板自己的标签做精确匹配（大小写敏感，取值多为中文，如“调试任务”），
        和广场数据集那种类别枚举不是一回事。
        """
        if scope not in TEMPLATE_LIST_SCOPES:
            raise OpenIError(f"scope 只能是 {'/'.join(TEMPLATE_LIST_SCOPES)}：{scope!r}")
        if order_by and order_by not in TEMPLATE_LIST_ORDER:
            raise OpenIError(
                f"order_by 只能是 {'/'.join(TEMPLATE_LIST_ORDER)} 或留空：{order_by!r}。"
                f"平台对未知取值不报错，直接按默认顺序返回，所以这里不替你猜。"
            )
        page_size = int(page_size)
        if not 1 <= page_size <= SUBJECT_LIST_MAX_PAGE_SIZE:
            raise OpenIError(
                f"page_size 必须在 1-{SUBJECT_LIST_MAX_PAGE_SIZE} 之间：{page_size}。"
            )
        params: Dict[str, Any] = {"page": int(page), "page_size": page_size}
        if q:
            params["q"] = q
        if job_type:
            params["job_type"] = job_type
        if compute_source:
            params["compute_source"] = compute_source
        if order_by:
            params["order_by"] = order_by
        tags_csv = _csv(tags)
        if tags_csv:
            params["tags"] = tags_csv
        data = self.get_json(f"/ai_task_template/list/{scope}", params=params) or {}
        rows = data.get("Templates") or []
        return {
            "total": int(data.get("Total") or 0),
            "page": int(data.get("Page") or page),
            "page_size": int(data.get("PageSize") or page_size),
            "items": [summarize_task_template(row) for row in rows],
        }

    def task_template(self, template_id: str) -> Dict[str, Any]:
        """单个任务模板的完整详情：GET /ai_task_template?id=<uuid>。

        列表接口不认识 id 参数（传了照旧返回首页），也没有
        /ai_task_template/detail 之类的路由，只有这一个按 ID 取详情的入口。
        """
        if not template_id or not str(template_id).strip():
            raise OpenIError("template_id 不能为空")
        data = self.get_json("/ai_task_template", params={"id": str(template_id).strip()})
        if not isinstance(data, Mapping):
            raise OpenIError(f"没有拿到模板详情：{template_id!r}")
        return dict(data)

    def collect_task_template(self, template_id: str, collect: bool = True) -> Any:
        """收藏 / 取消收藏任务模板（PUT / DELETE /ai_task_template/collect?id=）。"""
        if not template_id or not str(template_id).strip():
            raise OpenIError("template_id 不能为空")
        params = {"id": str(template_id).strip()}
        if collect:
            return self.put_json("/ai_task_template/collect", params=params)
        return self.delete_json("/ai_task_template/collect", params=params)

    def template_spec(
        self,
        template: Mapping[str, Any],
        compute_source: Optional[str] = None,
        cluster: Optional[str] = None,
    ) -> Dict[str, Any]:
        """把模板换算成当前账号真正可开的 spec。

        模板自己不带 spec_id（它记的是卡型与卡数），所以这里按
        (compute_resource, acc_card_type, acc_cards_num) 去
        /ai_task/creation/required 的实时清单里配对。对不上就报错，不猜。
        """
        source = compute_source or str(template.get("ComputeSource") or "")
        cluster_type = cluster or str(template.get("Cluster") or DEFAULT_CLUSTER)
        job_type = str(template.get("JobType") or "DEBUG")
        info = self.creation_info(job_type, source, cluster_type)
        specs = ((info.get("specs") or {}).get("all")) or []
        want = (source, template.get("AccCardType"), template.get("AccCardsNum"))
        for spec in specs:
            got = (
                spec.get("compute_resource"),
                spec.get("acc_card_type"),
                spec.get("acc_cards_num"),
            )
            if got == want:
                return dict(spec)
        listed = ", ".join(
            f"{s.get('acc_card_type')}x{s.get('acc_cards_num')}" for s in specs
        ) or "（空）"
        raise OpenIError(
            f"模板要的 {want[0]}/{want[1]}x{want[2]} 在当前账号的 {job_type} 可用规格里不存在。"
            f"可用的有：{listed}"
        )

    # ------------------------------------------------------------- create task

    def create_task(
        self,
        repo: str,
        spec_id: int,
        display_job_name: Optional[str] = None,
        compute_source: str = "GPU",
        cluster: str = DEFAULT_CLUSTER,
        job_type: str = "DEBUG",
        work_server_number: int = 1,
        branch_name: Optional[str] = None,
        image_id: Optional[str] = None,
        image_name: Optional[str] = None,
        image_url: Optional[str] = None,
        boot_file: Optional[str] = None,
        params: Optional[str] = None,
        dataset_uuid_str: Optional[str] = None,
        pretrain_model_id_str: Optional[str] = None,
        description: Optional[str] = None,
        label_names: Optional[str] = None,
        has_internet: int = 2,
        time_limit: Optional[int] = None,
        visualize_required: bool = False,
        auto_save: bool = False,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /api/v1/{owner}/{repo}/ai_task/create.

        spec_id must come from openi_list_creation_options for the same
        job_type/compute_source/cluster.

        When the caller supplies no image, the platform's own default image is
        used for every accelerator family except NVIDIA GPU, whose two listed
        images C2Net cannot pull; there DEFAULT_IMAGE_URL is sent as image_url
        instead. For the specs whose platform-listed images were verified on
        C2Net (see resources.VERIFIED_IMAGES) that known-good image id wins over
        the platform's first entry. The response reports which path was taken in
        _image_source.

        branch_name is the code version the task checks out. Omitting it makes
        C2Net fail the task with "Fail to load code" (CREATED_FAILED) for every
        family other than NVIDIA GPU, so when the caller does not pass one we
        fall back to the repo's default branch. That branch is reported in
        _branch_name.

        job_type=ONLINEINFERENCE（在线推理 / 模型体验）只支持 GPU：NPU 的规格
        清单为空，开了也起不来。它的 images 为 None，所以不传镜像时会走
        DEFAULT_IMAGE_URL（实测能拉起）。平台默认对外端口是 8000，容器里的服务
        必须监听这个端口（PORT / SERVICE_PORT 之类的环境变量不会被注入）。
        /ai_task/creation/required 里 limit_count 通常为 1，即同时只能有一个在线
        推理任务。自定义访问路径与端口的字段名是 endpoint / port，但平台对这两
        个值校验很严（实测直接传会被拒），所以这里只透传 extra，不替调用方拼。
        任务 RUNNING 之后用 self_endpoint_url() / service_url() 取对外地址。
        """
        image_source = "explicit"
        branch_source = "explicit"
        needs_info = display_job_name is None or not (image_id or image_url) or not branch_name
        info: Optional[Dict[str, Any]] = None
        if needs_info:
            info = self.creation_info(job_type, compute_source, cluster, repo=repo)
        if display_job_name is None and info is not None:
            display_job_name = info.get("display_job_name")
        if not branch_name:
            if info is None:
                info = self.creation_info(job_type, compute_source, cluster, repo=repo)
            branch_name = info.get("default_branch") or self.default_branch(repo)
            if branch_name:
                branch_source = "repo_default_branch"
        if not image_id and not image_url:
            images = (info or {}).get("images") or []
            if compute_source.upper() in SOURCES_NEEDING_IMAGE_URL or not images:
                image_url = DEFAULT_IMAGE_URL
                image_source = "fallback_image_url"
            else:
                verified = trusted_verified_image(str(spec_id))
                if verified:
                    image_id = verified["image_id"]
                    image_name = image_name or verified.get("image_name")
                    image_source = "verified_image"
                else:
                    image_id = images[0].get("image_id")
                    image_name = image_name or images[0].get("image_name")
                    image_source = "platform_default_image"
        validate_job_name(display_job_name or "")
        if image_url and not IMAGE_URL_RE.match(image_url):
            raise OpenIError(f"image_url 不是合法的镜像地址: {image_url!r}")

        payload: Dict[str, Any] = {
            "job_type": job_type,
            "display_job_name": display_job_name,
            "spec_id": int(spec_id),
            "compute_source": compute_source,
            "cluster": cluster,
            "work_server_number": work_server_number,
            "has_internet": has_internet,
            "visualize_required": bool(visualize_required),
            "auto_save": bool(auto_save),
        }
        optional = {
            "branch_name": branch_name,
            "image_id": image_id,
            "image_name": image_name,
            "image_url": image_url,
            "boot_file": boot_file,
            "params": params,
            "dataset_uuid_str": dataset_uuid_str,
            "pretrain_model_id_str": pretrain_model_id_str,
            "description": description,
            "label_names": label_names,
            "time_limit": time_limit,
        }
        for key, value in optional.items():
            if value not in (None, ""):
                payload[key] = value
        if extra:
            payload.update(extra)

        result = self.post_json(f"/{repo}/ai_task/create", json_body=payload)
        if isinstance(result, dict):
            result.setdefault("_request_payload", payload)
            result.setdefault("_image_source", image_source)
            result.setdefault("_branch_name", payload.get("branch_name"))
            result.setdefault("_branch_source", branch_source)
        return result


def summarize_spec(spec: Mapping[str, Any]) -> Dict[str, Any]:
    """Trim a raw spec row down to the fields a caller needs to choose one."""
    return {
        "spec_id": spec.get("id"),
        "compute_resource": spec.get("compute_resource"),
        "card_type": spec.get("acc_card_type"),
        "card_count": spec.get("acc_cards_num"),
        "cpu_cores": spec.get("cpu_cores"),
        "mem_gb": spec.get("mem_gi_b"),
        "gpu_mem_gb": spec.get("gpu_mem_gi_b"),
        "price_per_card_hour": spec.get("unit_price"),
        "has_internet": spec.get("has_internet"),
        "visualization": spec.get("enable_visualization"),
        "ai_center": spec.get("ai_center_code"),
    }


def _gb(size: Any) -> Optional[float]:
    """字节转 GiB，保留两位小数；无法解析时返回 None。"""
    try:
        return round(int(size) / 1024 / 1024 / 1024, 2)
    except (TypeError, ValueError):
        return None


def _csv(value: Any) -> str:
    """把调用方给的过滤条件拼成平台要的逗号串。

    模型经常把列表参数写成 "computer_vision" 这样的字符串，直接 join 会按字符
    拆开（tags=c,o,m,...）而平台照样返回 200，于是静默搜出 0 条。这里对字符串
    按逗号切分，两种写法都能用。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        parts = value.split(",")
    else:
        parts = list(value)
    return ",".join(str(p).strip() for p in parts if str(p).strip())


TEMPLATE_IMAGE_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}$")


def template_image_url(row: Mapping[str, Any]) -> Optional[str]:
    """模板的 ImageUrl 字段有时是镜像地址，有时只是一个内部记录 UUID。

    NPU 的 MNIST 模板给的是 318c8cac-2011-44c8-acee-21d6a1c138ff 这种 UUID，
    拿它当镜像地址发给平台必然拉不起来；OpenClaw 模板给的则是
    <host>:<port>/default-workspace/... 这种真地址（内网 registry）。这里只认后者。
    """
    value = str(row.get("ImageUrl") or "").strip()
    if not value or TEMPLATE_IMAGE_UUID_RE.match(value):
        return None
    if not IMAGE_URL_RE.match(value):
        return None
    return value


def summarize_task_template(row: Mapping[str, Any]) -> Dict[str, Any]:
    """把一行任务模板压成“挑一个来开任务”需要的字段。"""
    owner = row.get("RepoOwnerName") or ""
    name = row.get("RepoName") or ""
    raw_params = row.get("Parameters")
    try:
        parameters = json.loads(raw_params) if isinstance(raw_params, str) and raw_params else raw_params
    except ValueError:
        parameters = raw_params
    return {
        "template_id": row.get("ID"),
        "name": row.get("Name"),
        "description": row.get("Description"),
        "tags": row.get("Tags") or [],
        "job_type": row.get("JobType"),
        "compute_source": row.get("ComputeSource"),
        "cluster": row.get("Cluster"),
        "acc_card_type": row.get("AccCardType"),
        "acc_cards_num": row.get("AccCardsNum"),
        "cpu_cores": row.get("CpuCores"),
        "mem_gi_b": row.get("MemGiB"),
        "gpu_mem_gi_b": row.get("GPUMemGiB"),
        "image_id": row.get("ImageID"),
        "image_name": row.get("ImageName"),
        "image_url": template_image_url(row),
        "image_url_raw": row.get("ImageUrl"),
        "repo": f"{owner}/{name}" if owner and name else None,
        "repo_deleted": bool(row.get("IsRepoDeleted")),
        "branch": row.get("BranchName"),
        "boot_file": row.get("BootFile") or None,
        "parameters": parameters if parameters else None,
        "has_internet": row.get("HasInternet"),
        "visualize_required": bool(row.get("VisualizeRequired")),
        "datasets": [
            {
                "uuid": d.get("DatasetID"),
                "name": d.get("DatasetName"),
                "alias": d.get("DatasetAlias"),
                "owner": d.get("OwnerName"),
                "deleted": bool(d.get("IsDeleted")),
            }
            for d in (row.get("DatasetLists") or [])
        ],
        "models": [
            {
                "id": m.get("ModelID"),
                "name": m.get("ModelName"),
                "alias": m.get("ModelAlias"),
                "owner": m.get("OwnerName"),
                "deleted": bool(m.get("IsDeleted")),
            }
            for m in (row.get("ModelLists") or [])
        ],
        "use_count": row.get("UseCount"),
        "collections": row.get("NumCollections"),
        "recommend": bool(row.get("Recommend")),
        "collected": bool(row.get("IsCollected")),
        "can_edit": bool(row.get("CanEdit")),
        "author": (row.get("Owner") or {}).get("Name") if isinstance(row.get("Owner"), Mapping) else None,
        "updated_unix": row.get("UpdatedUnix"),
    }


def summarize_subject(row: Mapping[str, Any], subject_type: str) -> Dict[str, Any]:
    """把广场列表里的一行压成调用方挑选主体需要的字段。"""
    owner = row.get("owner_name") or row.get("creator_name") or ""
    name = row.get("name") or row.get("alias") or ""
    summary: Dict[str, Any] = {
        "subject": f"{owner}/{name}" if owner and name else name,
        "subject_type": subject_type,
        "id": row.get("id"),
        "alias": row.get("alias"),
        "owner": owner,
        "size_bytes": row.get("size"),
        "size_gb": _gb(row.get("size")),
        "downloads": row.get("download_count"),
        "stars": row.get("num_stars") or row.get("collected_count"),
        "recommend": bool(row.get("recommend")),
        "updated_unix": row.get("updated_unix") or row.get("created_unix"),
    }
    if subject_type == "dataset":
        summary["tags"] = row.get("tags") or []
        summary["tasks"] = row.get("tasks") or []
    else:
        summary["label"] = row.get("label") or ""
        summary["external"] = bool(row.get("external_name") or row.get("external_url"))
    return summary


def _slim_issue(row: Mapping[str, Any]) -> Dict[str, Any]:
    """把 issue 压成调用方要的字段（号、标题、状态、链接、作者、评论数）。"""
    return {
        "number": row.get("number"),
        "title": row.get("title"),
        "state": row.get("state"),
        "url": row.get("html_url"),
        "author": (row.get("user") or {}).get("login"),
        "comments": row.get("comments"),
        "created_at": row.get("created_at"),
        "body": row.get("body"),
    }


def dump(value: Any) -> str:
    """Stable JSON rendering used by the MCP tools."""
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)

