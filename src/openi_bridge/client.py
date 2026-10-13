"""HTTP client for the OpenI platform (openi.pcl.ac.cn).

The platform is a Gitea fork whose REST surface lives under /api/v1 and
authenticates with a personal access token sent as Authorization: Bearer.

Compute (云脑 / ai_task) endpoints are mixed:

    GET  /api/v1/user                         account behind the token
    GET  /api/v1/ai_task?id=                  single task detail
    GET  /api/v1/ai_task/brief?id=            task status summary
    GET  /api/v1/ai_task/log?id=              task log
    GET  /api/v1/ai_task/resource_usage?id=   live card/CPU usage
    GET  /api/v1/ai_task/debug_url?id=        Jupyter / SSH entry point
    GET  /api/v1/ai_task/visualize_url?id=    TensorBoard entry point
    GET  /api/v1/{owner}/{repo}/ai_task       tasks that belong to one repo
    POST /api/v1/{owner}/{repo}/ai_task/create    create + start a task
    POST /api/v1/ai_task/stop?id=             stop a task (stop billing)
    POST /api/v1/ai_task/restart?id=          restart a stopped task
    POST /api/v1/{owner}/{repo}/ai_task/del?id=   delete a task
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Mapping, Optional

import httpx

from .auth import Credentials, resolve_credentials

DEFAULT_USER_AGENT = "openi-bridge/0.1 (+mcp)"


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


class OpenIClient:
    """Synchronous client; the MCP server is single user and calls are cheap."""

    def __init__(
        self,
        token: Optional[str] = None,
        endpoint: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        self.creds: Credentials = resolve_credentials(token, endpoint)
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

    # ------------------------------------------------------------- compute tasks

    def sdk_code(
        self,
        job_type: str,
        compute_source: str = "openi",
        cluster_type: str = "openi",
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
        """GET /api/v1/ai_task?id= - full detail of one task."""
        return self.get_json("/ai_task", params={"id": task_id})

    def task_brief(self, task_id: Any) -> Dict[str, Any]:
        """GET /api/v1/ai_task/brief?id= - compact status, cheap to poll."""
        return self.get_json("/ai_task/brief", params={"id": task_id})

    def task_resource_usage(self, task_id: Any) -> Any:
        return self.get_json("/ai_task/resource_usage", params={"id": task_id})

    def task_node_info(self, task_id: Any) -> Any:
        return self.get_json("/ai_task/node_info", params={"id": task_id})

    def list_tasks(self, repo: Optional[str] = None) -> Any:
        """List compute tasks.

        The platform only exposes a JSON listing per repo
        (GET /api/v1/{owner}/{repo}/ai_task); the account wide list is a
        server rendered page without a JSON API.
        """
        if not repo:
            raise OpenIError(
                "list_tasks 需要 repo 参数（owner/name）。平台没有账号级任务列表 JSON 接口，"
                "请通过 openi_list_my_repositories 选择代码仓后再查询，"
                "或在网页 https://openi.pcl.ac.cn/cloudbrains 查看全部任务。"
            )
        result = self.get_json(f"/{repo}/ai_task")
        return result

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
        """Run stop / restart / del on a task."""
        if action not in ("stop", "restart", "del"):
            raise ValueError(f"unsupported action: {action}")
        path = f"/{repo}/ai_task/{action}" if (repo and action == "del") else f"/ai_task/{action}"
        return self.post_json(path, params={"id": task_id}, json_body={})

    def create_task(
        self,
        repo: str,
        task_name: str,
        job_type: str = "DEBUG",
        cluster: str = "openi",
        computer_res: Optional[str] = None,
        card_type: Optional[str] = None,
        card_count: int = 1,
        node_count: int = 1,
        image: Optional[str] = None,
        boot_file: Optional[str] = None,
        branch: Optional[str] = None,
        dataset: Optional[str] = None,
        model: Optional[str] = None,
        command: Optional[str] = None,
        duration_hours: Optional[int] = None,
        description: Optional[str] = None,
        has_internet: Optional[bool] = None,
        visualize: Optional[bool] = None,
        spec: Optional[str] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST /api/v1/{owner}/{repo}/ai_task/create.

        The field names mirror the web create form. They are the best
        available approximation of the JSON body and can be corrected through
        the extra mapping or by editing build_create_payload.
        """
        payload = build_create_payload(
            repo=repo,
            task_name=task_name,
            job_type=job_type,
            cluster=cluster,
            computer_res=computer_res,
            card_type=card_type,
            card_count=card_count,
            node_count=node_count,
            image=image,
            boot_file=boot_file,
            branch=branch,
            dataset=dataset,
            model=model,
            command=command,
            duration_hours=duration_hours,
            description=description,
            has_internet=has_internet,
            visualize=visualize,
            spec=spec,
            extra=extra,
        )
        result = self.post_json(f"/{repo}/ai_task/create", json_body=payload)
        if isinstance(result, dict):
            result.setdefault("_request_payload", payload)
        return result


def build_create_payload(
    *,
    repo: str,
    task_name: str,
    job_type: str,
    cluster: str,
    computer_res: Optional[str],
    card_type: Optional[str],
    card_count: int,
    node_count: int,
    image: Optional[str],
    boot_file: Optional[str],
    branch: Optional[str],
    dataset: Optional[str],
    model: Optional[str],
    command: Optional[str],
    duration_hours: Optional[int],
    description: Optional[str],
    has_internet: Optional[bool],
    visualize: Optional[bool],
    spec: Optional[str],
    extra: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Assemble the create-task request body from the create form fields."""
    payload: Dict[str, Any] = {
        "repo": repo,
        "task_name": task_name,
        "job_type": job_type,
        "compute_source": cluster,
        "cluster_type": cluster,
        "card_count": card_count,
        "node_count": node_count,
        "work_server_num": node_count,
    }
    optional = {
        "computer_res": computer_res or card_type,
        "card_type": card_type,
        "image": image,
        "image_url": image,
        "boot_file": boot_file,
        "branch_name": branch,
        "dataset": dataset,
        "dataset_list": [dataset] if dataset else None,
        "model": model,
        "model_list": [model] if model else None,
        "run_parameters": command,
        "time_limit": duration_hours,
        "task_descr": description,
        "has_internet": has_internet,
        "visualization": visualize,
        "spec": spec,
    }
    for key, value in optional.items():
        if value is not None:
            payload[key] = value
    if extra:
        payload.update(extra)
    return payload


def dump(value: Any) -> str:
    """Stable JSON rendering used by the MCP tools."""
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)
