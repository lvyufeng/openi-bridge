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
    GET  /api/v1/ai_task/resource_usage?id=        live card/CPU usage
    GET  /api/v1/ai_task/debug_url?id=             Jupyter / SSH entry point
    GET  /api/v1/ai_task/visualize_url?id=         TensorBoard entry point
    GET  /api/v1/ai_task/creation/required         live specs, images, balance
    GET  /api/v1/ai_task/creation/image_by_spec    images usable for a spec
    GET  /api/v1/images/recommend                  platform recommended images
    GET  /api/v1/images/star                       images the account starred
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
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
from typing import Any, Dict, List, Mapping, Optional

import httpx

from .auth import Credentials, resolve_credentials
from .resources import VERIFIED_IMAGES

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
# 递归列目录时的文件数上限，避免误对超大主体做全量 crawl。
WALK_FILE_LIMIT = int(os.getenv("OPENI_WALK_FILE_LIMIT", "20000"))

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
            digest.update(block[: 1024 * 1024])
    return digest.hexdigest()


def _total_chunks(size: int, chunk_size: int = CHUNK_SIZE) -> int:
    if size <= 0:
        return 1
    return math.ceil(size / chunk_size)


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
        json_body: Any = None,
        timeout: Optional[float] = None,
    ) -> Any:
        return _wrap(
            _unwrap_text(self.request("PUT", path, json_body=json_body, timeout=timeout))
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

    # -------------------------------------------------------------------- repos

    def my_repos(self, limit: int = 50, page: int = 1) -> List[Dict[str, Any]]:
        """Repos owned by the token holder; used to pick the task code source."""
        return self.get_json("/user/repos", params={"limit": limit, "page": page})

    def search_repos(self, query: str, limit: int = 20) -> Dict[str, Any]:
        return self.get_json("/repos/search", params={"q": query, "limit": limit})

    def repo(self, subject_name: str) -> Dict[str, Any]:
        return self.get_json(f"/repos/{subject_name}")

    def repo_branches(self, subject_name: str) -> List[Dict[str, Any]]:
        """Branches of a repo; the create form needs one for the code version."""
        return self.get_json(f"/repos/{subject_name}/branches")

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
        """整仓文件树，一条请求拿全（/git/trees/{ref}?recursive=1）。

        比逐层翻 contents 快得多，适合先看清仓里有什么再决定读哪个文件；大仓的
        返回体可能很大，用 max_entries 截断并在结果里标 truncated。
        ref 留空用 HEAD（即默认分支的最新提交）。
        """
        tree_ref = (ref or "").strip() or "HEAD"
        data = self.get_json(
            f"/repos/{subject_name}/git/trees/{tree_ref}", params={"recursive": 1}
        ) or {}
        rows = data.get("tree") or []
        prefix = (path or "").strip().strip("/")
        if prefix:
            rows = [
                row
                for row in rows
                if (row.get("path") or "") == prefix
                or (row.get("path") or "").startswith(prefix + "/")
            ]
        truncated = bool(data.get("truncated")) or (
            max_entries > 0 and len(rows) > max_entries
        )
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
        tmp_path = f"{dest}.part"
        digest = hashlib.md5()
        size = 0
        try:
            with self._http.stream(
                "GET",
                f"/repos/{subject_name}/archive/{ref}.zip",
                headers={"Authorization": f"Bearer {self.creds.token}"},
                timeout=httpx.Timeout(30.0, read=120.0),
            ) as response:
                if response.status_code >= 400:
                    raise OpenIError(
                        f"打包下载失败 HTTP {response.status_code}：{subject_name}@{ref}",
                        status_code=response.status_code,
                        payload=response.read().decode("utf-8", "replace")[:200],
                    )
                with open(tmp_path, "wb") as handle:
                    for chunk in response.iter_bytes(1024 * 1024):
                        handle.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                        if max_bytes is not None and size > max_bytes:
                            raise OpenIError(
                                f"仓库打包超过 max_bytes={max_bytes} 字节，已中止下载。"
                            )
        except BaseException:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
        os.replace(tmp_path, dest)
        return {
            "repo": subject_name,
            "ref": ref,
            "path": dest,
            "bytes": size,
            "mb": round(size / 1024 / 1024, 2),
            "md5": digest.hexdigest(),
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
        while True:
            page = self.subject_files(subject_name, subject_type, parent, marker) or {}
            for row in page.get("file_list") or []:
                if (row.get("FileName") or "") == base:
                    return row
            next_marker = page.get("marker") or ""
            if not page.get("has_next") or not next_marker or next_marker == marker:
                return None
            marker = next_marker

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
            uploaded_chunks = list(chunks_info.get("chunks") or [])
        start_from = len(uploaded_chunks) or 1
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
        return {
            "subject": subject_name,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "uploaded_count": len(succeeded),
            "failed_count": len(results) - len(succeeded),
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
        digest = hashlib.md5()
        size = 0
        tmp_path = f"{dest_path}.part"
        # 单个文件可能十几 GB，所以总时长不限，但每次读要有超时：否则连接被
        # 静默挂住时这里会一直卡着，MCP 调用方看不到任何反馈。
        with httpx.stream(
            "GET", url, timeout=httpx.Timeout(30.0, read=120.0), follow_redirects=True
        ) as response:
            if response.status_code >= 400:
                raise OpenIError(
                    f"下载失败 HTTP {response.status_code}：{file_name}",
                    status_code=response.status_code,
                )
            with open(tmp_path, "wb") as handle:
                for chunk in response.iter_bytes(1024 * 1024):
                    handle.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
        os.replace(tmp_path, dest_path)
        return {"file": file_name, "bytes": size, "md5": digest.hexdigest(), "path": dest_path}

    def walk_subject_files(
        self, subject_name: str, subject_type: str, parent_dir: str = "",
        limit: int = WALK_FILE_LIMIT,
    ) -> List[Dict[str, Any]]:
        """递归列出主体里的全部文件：{name(仓内相对路径), size, mtime}。"""
        collected: List[Dict[str, Any]] = []

        def walk(directory: str) -> None:
            marker = ""
            while True:
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

    def task_action(self, action: str, task_id: Any, repo: Optional[str] = None) -> Any:
        """Run stop / restart / del on a task.

        All three accept id only; the repo scoped variants only exist for a
        subset and are not required.
        """
        if action not in ("stop", "restart", "del"):
            raise ValueError(f"unsupported action: {action}")
        return self.post_json(f"/ai_task/{action}", params={"id": task_id}, json_body={})

    def batch_delete(self, task_ids: List[Any]) -> Any:
        return self.post_json("/ai_task/batch_del", json_body={"id": list(task_ids)})

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

        spec_id must come from openi_get_creation_options for the same
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
                verified = VERIFIED_IMAGES.get(str(spec_id))
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

