# openi-bridge

一个 MCP（Model Context Protocol）服务，用来直接使用 [OpenI 启智社区](https://openi.pcl.ac.cn)
提供的普惠算力：查询资源与积分、查看实时可开的规格（spec）与镜像、查看代码仓、
创建/停止/重启/删除计算任务、读取任务状态、日志和调试入口。

> 说明：www.openi.org.cn 只是社区门户网站，真正的算力平台是
> [openi.pcl.ac.cn](https://openi.pcl.ac.cn)（启智 AI 协作平台），本工具对接的是后者。

## 安装

~~~bash
python -m venv .venv
.venv/bin/pip install -e .
~~~

## 配置令牌

1. 打开 https://openi.pcl.ac.cn/user/settings/applications ，新建一个访问令牌（勾选需要的作用域）。
2. 设置环境变量：

~~~bash
export OPENI_TOKEN=你的令牌
# 可选，默认就是 https://openi.pcl.ac.cn
export OPENI_ENDPOINT=https://openi.pcl.ac.cn
~~~

令牌解析顺序：工具参数 → `~/.openi/token.json`（`openi login` 生成）→ 环境变量。
也可以把令牌写到 `~/.openi/token.json`：

~~~json
{ "endpoint": "https://openi.pcl.ac.cn", "token": "你的令牌" }
~~~

## 在 MCP 客户端里接入

Claude Desktop / Cursor 等支持 stdio MCP 的客户端：

~~~json
{
  "mcpServers": {
    "openi-bridge": {
      "command": "/绝对路径/openi-bridge/.venv/bin/python",
      "args": ["-m", "openi_bridge.server"],
      "env": { "OPENI_TOKEN": "你的令牌" }
    }
  }
}
~~~

Codex / 其他客户端可直接运行 `openi-bridge`（安装后生成的命令）。

## 怎么开一个任务

1. `openi_whoami` 确认令牌可用。
2. `openi_list_creation_options(job_type="DEBUG", compute_source="GPU")` 拿到实时的 `spec_id`、默认任务名和积分余额。
3. `openi_list_my_repositories` / `openi_list_repo_files` 选一个代码仓和分支。
4. `openi_start_task(repo="owner/name", spec_id=210)` 创建任务（不传镜像会用一个可拉起的默认镜像）。
5. `openi_get_task` / `openi_list_tasks` 查看状态，`openi_task_debug_url` 拿 Jupyter/SSH 入口。
6. 用完 `openi_stop_task(task_id=...)` 停止并停止计费，必要时 `openi_delete_task`。

## 镜像（重要）

平台在创建任务页给出的两个 `image_id`（tensorflow2.14、torch2.1.2）在 C2Net 上都会以
`image not available` 失败，`/ai_task/creation/image_by_spec` 也返回空。真正能被集群拉起的是
镜像地址，可以从 `openi_list_public_images` 拿到（平台推荐镜像的 `place` 字段），例如：

~~~text
dockerhub.pcl.ac.cn:5000/user-images/openi:SemanticSegmentEnv
swr.cn-south-1.myhuaweicloud.com/openi/cuda111_python37_pytorch191:v1
~~~

用法：把这个地址作为 `openi_start_task(image_url=...)` 传入。不传镜像时工具会自动使用默认镜像
`swr.cn-south-1.myhuaweicloud.com/openi/cuda111_python37_pytorch191:v1`（已验证能拉起 C2Net GPU 任务，
平台把它识别为 `cuda111_python37_pytorch191_public`），可以用环境变量 `OPENI_DEFAULT_IMAGE_URL` 覆盖。

## 提供的工具

| 工具 | 作用 |
| --- | --- |
| openi_whoami | 查看令牌对应的账号，确认令牌可用 |
| openi_list_resources | 列出集群、卡型、积分单价与计费规则（离线目录） |
| openi_list_creation_options | 列出当前账号实时可开的规格（含 spec_id）与镜像、余额 |
| openi_list_images_for_spec | 查询某个 spec 可用的镜像（C2Net 上通常为空） |
| openi_list_public_images | 列出可真正拉起的公共镜像地址（平台推荐 + 自己收藏） |
| openi_list_my_repositories | 列出自己的代码仓（创建任务要指定代码仓） |
| openi_search_repositories | 搜索公开代码仓 |
| openi_list_repo_files | 列出代码仓文件与分支，用于确定启动文件 |
| openi_get_task | 按任务 ID 查询详情与状态 |
| openi_list_tasks | 列出当前账号的全部任务（跨所有代码仓） |
| openi_list_repo_tasks | 列出某个代码仓下的所有任务 |
| openi_task_logs | 读取任务日志 |
| openi_task_resource_usage | 查看任务实时算力占用 |
| openi_task_debug_url | 获取调试任务的 Jupyter / SSH 入口 |
| openi_task_visualize_url | 获取训练任务的 TensorBoard 入口 |
| openi_start_task | 新建并启动计算任务（调试/训练/推理） |
| openi_start_task_raw | 用自定义 JSON 请求体创建任务（兜底/调试用） |
| openi_stop_task | 停止任务、释放算力、停止计费 |
| openi_restart_task | 重启已停止的任务 |
| openi_delete_task | 删除任务（需先停止） |
| openi_sdk_code | 生成容器内访问数据集/模型的示例代码 |

## 计费与约束

- 消耗积分 = 单价 × 卡时 × 节点数，每 6 分钟结算一次，不足 6 分钟不计费。
- 调试（DEBUG）任务通常有单次时长上限（例如 4 小时），到期自动结束。
- 积分余额和明细见 https://openi.pcl.ac.cn/reward/point 。
- 同一作业类型同时只能有一个运行/排队中的任务（新建前先停止旧任务）。
- 任务名以小写字母或数字开头，只能包含小写字母、数字、`_` 和 `-`，不能以 `_` 结尾，最长 36 字符。

## 实现说明与已知限制

本工具基于平台前端实际调用的 REST 接口（`/api/v1/...`）实现，创建任务的请求体取自平台源码 `entity.CreateReq`。已用真实令牌验证过的接口与行为：

- 账号与代码仓：`/user`、`/user/repos`、`/repos/{owner}/{repo}`、`/branches`、`/contents`。
- 资源发现：`/ai_task/creation/required` 返回当前账号在指定 `job_type` + `compute_source` + `cluster_type` 下真实可用的规格列表（含数字 `spec_id`、卡型、卡数、CPU/内存、单价）、可用镜像、默认任务名、积分余额，以及 `can_create_more` / `not_stop_task_count` 等配额信息。
- 镜像目录：`/images/recommend`（平台推荐）与 `/images/star`（本账号收藏），其中的 `place` 才是集群实际拉取的镜像地址。
- 任务读写：`/ai_task`、`/ai_task/brief`、`/ai_task/log`、`/ai_task/resource_usage`、`/ai_task/debug_url`、`/ai_task/visualize_url`、`/ai_task/my_list`（账号级任务列表）、`/{owner}/{repo}/ai_task`（按仓列表）。
- 任务变更：`POST /{owner}/{repo}/ai_task/create`、`/ai_task/stop`、`/ai_task/restart`、`/ai_task/del`、`/ai_task/batch_del`。

需要注意：

- 普通用户可用的集群是 `C2Net`（智算网络）。`cluster` 必须填 `C2Net`，`cluster_type` 查询参数同理；`openi` 之类的值会返回参数错误或空规格。
- 创建任务的必填字段是 `job_type`、`display_job_name`、`spec_id`（整数）、`compute_source`、`cluster`；`spec_id` 必须来自 `openi_list_creation_options`，否则会返回 Specification not available。
- 创建是异步的：`create` 立即返回任务 ID（状态 `PREPARING`），随后集群异步拉起。如果集群侧排队或拉起失败，任务会进入失败态，且不计费；用 `openi_get_task` 观察状态。
- DEBUG 任务不需要 `boot_file`；训练类任务需要以 `.py` 结尾的 `boot_file`。
- 账号级任务列表用的是 `/ai_task/my_list`，它在平台前端真实存在，但字段较少；更完整的信息用 `openi_get_task`。
- 镜像：`/ai_task/creation/required` 给出的 `image_id` 在本集群拉不起来（`image not available`），所以工具在未指定镜像时会改用 `OPENI_DEFAULT_IMAGE_URL`，把镜像地址作为 `image_url` 传下去；这也是平台上“外部镜像 / 推荐镜像”的实际工作方式。

