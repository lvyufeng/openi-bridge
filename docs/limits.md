# 参数上限与防呆


列表类工具（通知、任务、仓、issue、数据集/模型、文件清单、模板）的 `limit` /
`page` / `page_size` / `pages` 有一层防呆上限：`limit` ≤ 200、`page` ≤ 1000、
`page_size` ≤ 100、`pages` ≤ 50。这些不是平台限制，而是因为这个工具把平台响应
原样交给模型——不设上限时一个 `limit=100000` 就能把几万条记录塞进一次返回。
要更多数据请翻页，不要一次要完。

工具自己翻页时也有内部上限，防止平台的异常响应把一次调用拖死：`openi_repo_tree`
最多翻 50 页（`MAX_TREE_PAGES`），主体目录的 marker 翻页最多 500 页
（`MAX_LIST_PAGES`）。正常分页靠 `has_next`/`marker` 收敛，这两个上限只在平台
"一直说还有下一页、但标记不前进"时生效——那时会报一条明确的错误，而不是挂住。
两个值都可以用环境变量覆盖（`OPENI_MAX_TREE_PAGES` / `OPENI_MAX_LIST_PAGES`）。

`openi_start_task_raw` 是自定义 JSON 创建任务的兜底入口。因为它不做字段校验、
拼错的字段平台未必会拒（可能建出一个配置不对却照样计费的任务），它**默认
`dry_run=True` 只回显请求体**，要真的创建必须显式传 `dry_run=False`。
`repo` 必须是 `owner/name`、`payload_json` 必须是非空 JSON 对象。
