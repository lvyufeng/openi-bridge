# openi-bridge

一个 MCP（Model Context Protocol）服务，用来直接使用 [OpenI 启智社区](https://openi.pcl.ac.cn)
提供的普惠算力：查询资源与积分、查看代码仓、创建/停止/重启/删除计算任务、
读取任务状态、日志和调试入口。

> 说明：`www.openi.org.cn` 只是社区门户网站，真正的算力平台是
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

## 提供的工具

| 工具 | 作用 |
| --- | --- |
| openi_whoami | 查看令牌对应的账号，确认令牌可用 |
| openi_list_resources | 列出集群、卡型、积分单价与计费规则 |
| openi_list_my_repositories | 列出自己的代码仓（创建任务要指定代码仓） |
| openi_search_repositories | 搜索公开代码仓 |
| openi_list_repo_files | 列出代码仓文件与分支，用于确定启动文件 |
| openi_get_task | 按任务 ID 查询详情与状态 |
| openi_list_tasks | 列出某个代码仓下的所有任务 |
| openi_task_logs | 读取任务日志 |
| openi_task_resource_usage | 查看任务实时算力占用 |
| openi_task_debug_url | 获取调试任务的 Jupyter / SSH 入口 |
| openi_task_visualize_url | 获取训练任务的 TensorBoard 入口 |
| openi_start_task | 新建并启动计算任务（调试/训练/推理） |
| openi_start_task_raw | 用自定义 JSON 请求体创建任务（兜底/调试用） |
| openi_stop_task | 停止任务、释放算力、停止计费 |
| openi_restart_task | 重启已停止的任务 |
| openi_delete_task | 删除任务 |
| openi_sdk_code | 生成容器内访问数据集/模型的示例代码 |

## 计费与约束

- 消耗积分 = 单价 × 卡时 × 节点数，每 6 分钟结算一次，不足 6 分钟不计费。
- 调试（DEBUG）任务通常有单次时长上限（例如 4 小时），到期自动结束。
- 积分余额和明细见 https://openi.pcl.ac.cn/reward/point 。
- 任务名以小写字母或数字开头，只能包含小写字母、数字、`_` 和 `-`，不能以 `_` 结尾，最长 36 字符。

## 实现说明与已知限制

本工具基于平台前端实际调用的 REST 接口（`/api/v1/...`）实现。已确认可用的接口包括
`/user`、`/user/repos`、`/ai_task`、`/ai_task/brief`、`/ai_task/log`、
`/ai_task/debug_url`、`/ai_task/resource_usage`、`/{owner}/{repo}/ai_task` 以及
`POST /{owner}/{repo}/ai_task/create`、`/ai_task/stop`、`/ai_task/restart`。

需要注意：

- 账号级「全部任务」列表在平台上只有服务端渲染的网页，没有 JSON 接口，因此
  `openi_list_tasks` 需要指定代码仓；全部任务请在 https://openi.pcl.ac.cn/cloudbrains 查看。
- 创建任务的请求体字段取自网页创建表单，尚未用真实令牌做过端到端验证。
  如果 `openi_start_task` 返回字段相关的错误，可先用 `openi_start_task_raw` 传入自定义 JSON，
  再把正确字段名补进 `client.build_create_payload`。
