# 用法与典型流程

大多数时候你不需要记工具名——直接用自然语言交代任务，助手会去调对应的工具。
这份文档把常见的说法和它们背后的工具列出来，也给出从零开一次任务的固定顺序。
下面这几类说法都能直接生效：

**看家底。** “我还剩多少积分，能开多久？”“今天拿满每日积分了吗？”助手会调
`openi_points` / `openi_points_today` / `openi_estimate_runtime`，最后这个用实时单价和
余额算出每个规格还能开多少机时。

**开卡。** “用一张 910B 开个调试任务，代码仓选 `scratch-repo-8`，跑完给我 Jupyter 地址。”
助手先 `openi_list_creation_options(job_type="DEBUG", compute_source="NPU")` 拿实时
`spec_id` 和镜像，再 `openi_start_task` 建任务，`openi_get_task` 跟状态，
`openi_task_debug_url` 取入口。

**起一个在线服务。** “把这个仓里的 `serve.py` 起成在线推理，给我一个外网地址。”
助手用 `openi_start_task(job_type="ONLINEINFERENCE", compute_source="GPU", boot_file="serve.py")`
建任务（在线推理只支持 GPU，当前账号同时只能开一个），等它 RUNNING 后用
`openi_task_service_url` 拿到 `https://<hex>.mlunotebook.openi.org.cn` 这样的地址。
容器里的服务要监听 8000 端口——平台默认把外部流量转到这个端口，且不会往容器注入
`PORT` / `SERVICE_PORT` 之类的环境变量，服务自己写死或按自己的约定读端口。这个地址在
服务预热好之前会返回 `code 99`（“The service is loading”），隔一会儿再取即可。在线
推理任务不会自动停，会一直计费，用完记得 `openi_stop_task` / `openi_delete_task`。

**临时跑一段代码。** “在云脑上跑 `print(torch.cuda.is_available())` 看看有没有卡”，或者
“开张 GCU 跑一下 `efsmi`”。助手调 `openi_run_on_card`，建任务、连 Jupyter 内核执行、
把 stdout 带回来、顺手清理，全过程你不用管任务 ID。

**传数据集和 ckpt。** “把 `./data` 传成一个数据集叫 `my-corpus`”，“把这个 20G 的
`ckpt/` 传到模型库 `llama-ft` 下”。助手先用 `openi_create_dataset` /
`openi_create_model` 建库（库名固定的 `当前账号/name` 形式），再用
`openi_upload_dataset_files` / `openi_upload_model_files` 传文件——小文件走直传、
超过 64MB 自动按 64MB 分片续传。传完返回里的 UUID 直接就是 `openi_start_task` 的
`dataset_uuid_str` / `pretrain_model_id_str`。“这个数据集还剩多少空间”用
`openi_storage_usage` 看。传文件时最好给绝对路径——`paths` 相对的是 MCP 服务进程的
工作目录，不一定是当前对话所在的目录。传之前可以 `dry_run=True` 先看一眼清单和总量。

**用平台上现成的东西。** “OpenI 上有没有能直接用的 llama 权重？”“给我找个图像分类的
数据集，拉到本地看看”。助手用 `openi_search_models` / `openi_search_datasets` 搜平台
托管的模型与数据集（关键词、类别、任务、按下载量/收藏数排序、只看官方推荐都可以），
返回里每项都带 `subject`（`owner/name`）、UUID 和体积；`openi_list_subject_files`
可以继续看某个库里的文件清单，`openi_pull_model` / `openi_pull_dataset` 把它拉到本地
（支持只拉单个文件、`dry_run` 先看体积、按大小跳过已完成文件、`max_bytes` 上限）。

如果不打算下载到本地，直接把库挂到云脑任务上更省事：`openi_start_task` 的
`dataset_uuid_str` / `pretrain_model_id_str` 既能填 UUID，也能直接填 `owner/name`
（逗号分隔多个），工具会自动换成 UUID——也就是说搜到一个公开 ckpt 后可以直接
`openi_start_task(..., pretrain_model_id_str="FoundationModel/llama_model_7B")`，
不用先把十几 GB 的权重拖下来。

**改代码再开卡。** “把这个目录传成一个新仓 `my-train`，把 `train.py` 里
`epochs=10` 改成 `epochs=1`，然后拿一张 V100 跑一遍。”助手用
`openi_create_repository` 建仓，`openi_push_repo_files` 把本地目录按原结构提交上去
（每个文件一个提交，不需要本地 git），`openi_read_repo_file` / `openi_write_repo_file`
读改写代码，再 `openi_start_task` 指定这个仓和分支。`openi_list_repo_files` 传
`path` 可以翻子目录。建仓和删仓在平台上都要跑几十秒，工具已经放宽了超时；超时不代表
失败，回查 `openi_list_my_repositories` 确认即可。想一次看全仓结构用 `openi_repo_tree`，
确认提交落地用 `openi_list_repo_commits`，整份备份到本地用 `openi_download_repo_archive`，
报问题用 `openi_create_repo_issue` / `openi_comment_repo_issue`。用完
`openi_delete_repository` 删仓。

**收拾现场。** “把我的调试任务都停掉/删掉。”助手用 `openi_list_tasks` 找到任务，再
`openi_stop_task` / `openi_delete_task`。

**收产出、查为什么排队。** “任务跑完了，把 output 里的结果拿下来”“这个任务怎么一直排队？”
助手用 `openi_task_output_dir` / `openi_task_output_files` 列任务产出的文件，
`openi_task_download_output` 把文件取下来（小文件直接读进返回，大文件给 `save_to` 流式
落盘），`openi_task_download_log` 取整份日志；任务排队、被抢占或失败时用
`openi_task_events` 看调度时间线（分配节点→拉镜像→建容器→启动→结束），
`openi_task_nodes` 看跑在哪些节点上，`openi_task_loss` 读训练 loss 曲线。注意任务输出在
平台侧是异步索引的，刚 SUCCEEDED 时可能还列不到文件，隔一两分钟重试。

想手动调工具或写脚本，命令行入口和 MCP 工具是一一对应的：

~~~bash
python -m openi_bridge.exec_on_card --code "print(1+1)"
python -m openi_bridge.exec_on_card --source GCU --code "!efsmi"
python -m openi_bridge.autopoint --status
python -m openi_bridge.install_mcp --apply
~~~

完整的 85 个工具见[工具全表](tools.md)（按用途分组）。从零创建任务的顺序是固定的：`openi_whoami`
→ `openi_list_creation_options` → 选代码仓和分支 → `openi_start_task` →
`openi_get_task` / `openi_task_debug_url` → 用完 `openi_stop_task`，必要时
`openi_delete_task`。想省掉“自己找仓、自己配 spec、自己挂数据集”这几步，就先用
`openi_list_task_templates` 挑一个别人跑通的模板，再 `openi_start_task_from_template`。
