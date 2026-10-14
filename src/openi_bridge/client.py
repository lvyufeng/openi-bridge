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

The create body follows entity.CreateReq from the platform source. Required
keys are job_type, display_job_name, spec_id, compute_source and cluster.
Spec ids are discovered live through /ai_task/creation/required.
"""

from __future__ import annotations

import base64
import json
import os
import re
from typing import Any, Dict, List, Mapping, Optional

import httpx

from .auth import Credentials, resolve_credentials
from .resources import VERIFIED_IMAGES

DEFAULT_USER_AGENT = "openi-bridge/0.1 (+mcp)"
DEFAULT_CLUSTER = "C2Net"

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

    def dataset_files(self, dataset_name: str, parent_dir: str = "") -> Any:
        return self.get_json(
            "/dataset/files",
            params={"dataset_name": dataset_name, "parent_dir": parent_dir, "page_size": 1000},
        )

    def model_files(self, model_name: str, parent_dir: str = "") -> Any:
        return self.get_json(
            "/aimodel/files",
            params={"aimodel_name": model_name, "parent_dir": parent_dir, "page_size": 1000},
        )

    def storage_summary(self, subject_id: str, subject_type: str) -> Dict[str, Any]:
        return self.get_json(
            "/storage/summary",
            params={"subject_id": subject_id, "subject_type": subject_type},
        )

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


def dump(value: Any) -> str:
    """Stable JSON rendering used by the MCP tools."""
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)

