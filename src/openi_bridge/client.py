"""HTTP client for the OpenI platform (openi.pcl.ac.cn).

The platform is a Gitea fork whose REST surface lives under /api/v1 and
authenticates with a personal access token sent as Authorization: Bearer.

Compute (云脑 / ai_task) endpoints that this client uses:

    GET  /api/v1/user                              account behind the token
    GET  /api/v1/user/repos                        repos owned by the token
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
# 递归列目录时的文件数上限，避免误对超大主体做全量 crawl。
WALK_FILE_LIMIT = 20000

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
    ) -> httpx.Response:
        if not self.creds.has_token:
            raise OpenIError(
                "NO_TOKEN: 没有找到启智令牌。请在 https://openi.pcl.ac.cn/user/settings/applications "
                "生成令牌，然后设置 OPENI_TOKEN 环境变量，或执行 openi login。"
            )
        headers = {"Authorization": f"Bearer {self.creds.token}"}
        response = self._http.request(
            method, path, params=params, json=json_body, data=data, headers=headers
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
    ) -> Any:
        return _wrap(_unwrap_text(self.request("POST", path, params=params, json_body=json_body)))

    # ------------------------------------------------------------------- users

    def current_user(self) -> Dict[str, Any]:
        """GET /api/v1/user - the account the token belongs to."""
        return self.get_json("/user")

    def user(self, username: str) -> Dict[str, Any]:
        return self.get_json(f"/users/{username}")

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

    def repo_contents(self, subject_name: str, ref: Optional[str] = None) -> Any:
        """Top-level file listing, used to discover a valid boot_file."""
        params = {"ref": ref} if ref else None
        return self.get_json(f"/repos/{subject_name}/contents", params=params)

    def default_branch(self, subject_name: str) -> Optional[str]:
        data = self.repo(subject_name)
        if isinstance(data, Mapping):
            return data.get("default_branch")
        return None

    # ----------------------------------------------------------- data subjects

    def dataset_files(self, dataset_name: str, parent_dir: str = "", marker: str = "") -> Any:
        params = {"dataset_name": dataset_name, "parent_dir": parent_dir, "page_size": 1000}
        if marker:
            params["marker"] = marker
        return self.get_json("/dataset/files", params=params)

    def model_files(self, model_name: str, parent_dir: str = "", marker: str = "") -> Any:
        params = {"aimodel_name": model_name, "parent_dir": parent_dir, "page_size": 1000}
        if marker:
            params["marker"] = marker
        return self.get_json("/aimodel/files", params=params)

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
        path = "/dataset/list" if subject_type == "dataset" else "/aimodel/list"
        params: Dict[str, Any] = {"page": int(page), "page_size": int(page_size)}
        if q:
            params["q"] = q
        if order_by:
            params["order_by"] = order_by
        if tags:
            params["tags"] = ",".join(tags)
        if tasks:
            params["tasks"] = ",".join(tasks)
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
        self, subject_name: str, subject_type: str, parent_dir: str = "", marker: str = ""
    ) -> Any:
        """列出数据集 / 模型库某个目录下的文件，含子目录（IsDir=True）。"""
        if subject_type == "dataset":
            return self.dataset_files(subject_name, parent_dir, marker)
        if subject_type == "model":
            return self.model_files(subject_name, parent_dir, marker)
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
        if subject_type == "dataset":
            path, param = "/dataset", "dataset_name"
        elif subject_type == "model":
            path, param = "/aimodel", "aimodel_name"
        else:
            raise OpenIError(f"subject_type 只能是 dataset 或 model：{subject_type!r}")
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
    ) -> Dict[str, Any]:
        """把 entries（expand_upload_paths 的结果）上传到数据集/模型。

        小文件走直传、大文件走分片，按官方 SDK 的规则各走一条通道；直传的文件
        最后统一补一次 complete_upload。返回每个文件的结果与汇总。
        """
        if subject_type not in SUBJECT_TYPES:
            raise OpenIError(f"subject_type 只能是 dataset 或 model：{subject_type!r}")
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
        if subject_type == "dataset":
            path, param = "/dataset/file", "dataset_name"
        elif subject_type == "model":
            path, param = "/aimodel/file", "aimodel_name"
        else:
            raise OpenIError(f"subject_type 只能是 dataset 或 model：{subject_type!r}")
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
        with httpx.stream("GET", url, timeout=None, follow_redirects=True) as response:
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
        include_resource_version: bool = False,
    ) -> Dict[str, Any]:
        """把平台托管的文件拉取到本地目录。

        file 为空时递归下载整个主体（保留目录结构），否则只下载该文件。
        dry_run=True 只列大小不下载。max_bytes 是总字节上限，超出直接报错，
        防止误拉几十 GB 的 ckpt。已存在且大小一致的文件默认跳过，便于续传。
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
            targets = [{"name": file, "size": None}]
            base_dir = ""
        else:
            targets = self.walk_subject_files(subject_name, subject_type, parent_dir)
            base_dir = parent_dir
            if not include_resource_version:
                targets = [t for t in targets if t["name"] != "openi_resource.version"]
        known_total = sum(t["size"] or 0 for t in targets)
        if max_bytes is not None and known_total > max_bytes:
            raise OpenIError(
                f"待下载 {known_total} 字节，超过 max_bytes={max_bytes}；"
                "请改用 file 指定单个文件，或调大 max_bytes。"
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
            dest_path = os.path.join(dest_root, inside)
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


def dump(value: Any) -> str:
    """Stable JSON rendering used by the MCP tools."""
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)

