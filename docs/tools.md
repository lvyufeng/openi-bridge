# 工具全表

共 85 个工具，都是 `openi_` 前缀。按"你想干什么"分组；同一个工具只会出现在
最贴近它用途的那一组里。

> 分组速览见 [README 的工具部分](../README.md#工具)。


共 85 个工具，都是 `openi_` 前缀。按“你想干什么”分组：

| 分组 | 用途 | 工具数 |
| --- | --- | --- |
| [账号与积分](#账号与积分) | 先确认令牌能用、看还剩多少算力、今天的分领没领。 | 9 |
| [算力与规格](#算力与规格) | 平台现在有什么、你这个账号能开什么、余额够开多久。 | 6 |
| [开卡与跑代码](#开卡与跑代码) | 建任务、在卡上跑一段代码、停/重启/删（改状态的都会回读确认）。 | 7 |
| [看任务](#看任务) | 状态、日志、调度时间线、产物清单与下载、产物转存。 | 19 |
| [代码仓](#代码仓) | 建仓、读写文件、提交历史、issue 与仓库治理面、找别人的仓。 | 26 |
| [数据集与模型库](#数据集与模型库) | 建库、上传、检索、拉回本地、删文件。 | 14 |
| [任务模板](#任务模板) | 别人配好的一键开任务模板。 | 4 |

### 账号与积分

先确认令牌能用、看还剩多少算力、今天的分领没领。

| 工具 | 作用 |
| --- | --- |
| openi_whoami | 查看令牌对应的账号，确认令牌可用 |
| openi_points | 查看积分余额、单日获取上限与平台公布的完整获取规则 |
| openi_points_today | 看今天（北京时间）已经拿到多少分、算力那 10 分还在不在、还剩哪些每日项 |
| openi_points_history | 查看积分流水（获取/消耗明细，10 条一页，按天汇总） |
| openi_claim_daily_points | 需要时自动跑一遍"开任务→RUNNING→停止→删除"，把当天 10 分算力积分拿到，净消耗 0 分 |
| openi_autopoint_schedule | 生成 launchd plist 与 crontab 配置，把上面这条挂成每日定时任务 |
| openi_notifications | 查看账号通知（评论、@、issue），可只看未读并带未读总数 |
| openi_notification_detail | 看单条通知的完整内容（标题、仓库、最新评论链接）与评论正文（列表接口只给标题与链接） |
| openi_mark_all_notifications_read | 把所有未读通知一次标为已读（平台只提供“全部已读”，单条标记无可用接口） |

### 算力与规格

平台现在有什么、你这个账号能开什么、余额够开多久。

| 工具 | 作用 |
| --- | --- |
| openi_list_resources | 列出集群、卡型、积分单价与计费规则（离线目录） |
| openi_list_compute_resources | 查平台实时在线的算力中心（城市）与卡型（含离线目录没有的 CPU/5090/A800 等） |
| openi_list_creation_options | 列出当前账号实时可开的规格（含 spec_id）与镜像、余额 |
| openi_list_images_for_spec | 查询某个 spec 可用的镜像（C2Net 上通常为空） |
| openi_list_public_images | 列出可真正拉起的公共镜像地址（平台推荐 + 自己收藏） |
| openi_estimate_runtime | 按当前积分余额估算每个规格还能开多少机时（小时/天，已按 6 分钟向下取整） |

### 开卡与跑代码

建任务、在卡上跑一段代码、停/重启/删（改状态的都会回读确认）。

| 工具 | 作用 |
| --- | --- |
| openi_start_task | 新建并启动计算任务（调试/训练/推理） |
| openi_start_task_raw | 用自定义 JSON 请求体创建任务（兜底用；**默认只预览**，需显式 dry_run=False 才真建） |
| openi_run_on_card | 在云脑的一张卡上直接跑一段代码：建 DEBUG 任务 → 连它的 Jupyter 内核执行 → 收 stdout → 清理 |
| openi_stop_task | 停止任务、释放算力、停止计费（会回读确认状态真的变成已停止） |
| openi_restart_task | 重启已停止的任务（会回读确认任务确实离开了已停止状态） |
| openi_delete_task | 删除任务（需先停止；会回读确认任务确实消失了） |
| openi_sdk_code | 生成容器内访问数据集/模型的示例代码 |

### 看任务

状态、日志、调度时间线、产物清单与下载、产物转存。

| 工具 | 作用 |
| --- | --- |
| openi_get_task | 按任务 ID 查询详情与状态 |
| openi_list_tasks | 列出当前账号的全部任务（跨所有代码仓） |
| openi_list_repo_tasks | 列出某个代码仓下的所有任务 |
| openi_task_logs | 读取任务日志 |
| openi_task_events | 看任务的调度事件时间线（分配节点→拉镜像→建容器→启动→结束），定位为什么排队/失败 |
| openi_task_nodes | 列出任务实际跑在哪些节点上（多机任务多条） |
| openi_task_resource_usage | 查看任务实时算力占用 |
| openi_task_loss | 读取训练任务的 loss 曲线（epoch / loss 序列） |
| openi_task_debug_url | 获取调试任务的 Jupyter / SSH 入口 |
| openi_task_visualize_url | 获取训练任务的 TensorBoard 入口 |
| openi_task_service_url | 获取在线推理（ONLINEINFERENCE）任务的对外服务地址 |
| openi_task_output_dir | 列任务输出目录（**一次一层**，子目录用 parent_dir 下翻；不递归） |
| openi_task_output_files | **一次拿全**任务输出的所有文件（递归，name 即相对路径，可直接下载/导出） |
| openi_task_download_output | 下载任务输出的某个文件（流式落盘，或直接读进返回） |
| openi_task_download_output_archive | 把整个输出目录打包成一个 zip 下载（产物多时省事，流式落盘） |
| openi_task_download_log | 下载任务的完整日志（整段纯文本，非分页；读进返回时默认截到 256KB） |
| openi_task_export_to_dataset | 把任务输出文件搬进已有数据集：产物从“跟着任务走”变成数据集里的普通文件，可挂别的任务或被拉取 |
| openi_task_export_to_model | 把任务输出文件建成一个新模型库（就地新建，英文名全平台唯一），适合放 ckpt |
| openi_task_export_progress | 查上面两条导出的进度（{文件: 百分比}，可原地轮询等待） |

### 代码仓

建仓、读写文件、提交历史、issue 与仓库治理面、找别人的仓。

| 工具 | 作用 |
| --- | --- |
| openi_create_repository | 新建代码仓（owner 固定为当前账号），建完可直接推代码去跑任务 |
| openi_delete_repository | 删除代码仓（连同代码与提交历史，不可恢复） |
| openi_repo_permission | 查当前账号对某个代码仓的写权限（协作者列表不回权限，用这个判断） |
| openi_push_repo_files | 把本地文件/目录提交进代码仓，每文件一个提交，不需要本地 git |
| openi_read_repo_file | 读取仓内单个文件（可落盘），用于确认启动入口或超参 |
| openi_write_repo_file | 新建或覆盖仓内一个文本文件，产生一个提交 |
| openi_list_repo_files | 列出代码仓某个目录的文件（含类型/大小）与全部分支，用于确定启动文件 |
| openi_repo_tree | 拿到整仓文件树（路径/类型/大小），自动翻页取全，比逐层翻目录快 |
| openi_list_repo_commits | 列出提交历史（时间倒序），核对代码是不是真的推上去了 |
| openi_download_repo_archive | 把整仓打包成 zip 下载到本地；max_bytes 超限即中止，不留残包 |
| openi_create_repo_issue | 在代码仓里开 issue，返回 issue 号 |
| openi_list_repo_issues | 列出代码仓的 issue（open / closed / all） |
| openi_comment_repo_issue | 在指定 issue 下发表评论 |
| openi_update_repo_issue | 关闭 / 重开 issue，或改标题与正文（走 issue 路径上的 POST，平台的 PATCH 被反代拦截） |
| openi_list_repo_tags | 列出代码仓的 tag 及每个 tag 指向的提交 |
| openi_list_repo_releases | 列出代码仓的 release（含 tag、草稿/预发布标记与时间） |
| openi_list_repo_labels | 列出代码仓的标签（issue 分类用，含颜色） |
| openi_list_repo_collaborators | 列出代码仓的协作者（平台不回 permissions 字段，故不承诺权限） |
| openi_list_repo_stargazers | 列出收藏（star）某个代码仓的账号 |
| openi_list_my_repositories | 列出自己的代码仓（创建任务要指定代码仓） |
| openi_list_starred_repositories | 列出当前账号收藏的代码仓（别人的仓，找参考实现的入口） |
| openi_list_subscribed_repositories | 列出当前账号关注（watch）的代码仓 |
| openi_list_my_orgs | 列出当前账号加入的组织 / 团队 |
| openi_search_repositories | 搜索公开代码仓 |
| openi_search_topics | 按主题（topic）找仓，主题上带着仓数量 |
| openi_search_users | 按关键字找账号（人和组织），拿 login 拼全名 owner/name |

### 数据集与模型库

建库、上传、检索、拉回本地、删文件。

| 工具 | 作用 |
| --- | --- |
| openi_create_dataset | 新建空数据集（返回 UUID，可作 dataset_uuid_str） |
| openi_create_model | 新建空模型库（返回模型 ID，可作 pretrain_model_id_str） |
| openi_data_subject_info | 查数据集/模型详情，拿 UUID 与是否有写权限 |
| openi_upload_dataset_files | 把本地文件/目录上传到数据集（>64MB 自动分片续传，可 dry_run 先看清单） |
| openi_upload_model_files | 把本地文件/目录（ckpt 等）上传到模型库（同样支持 dry_run） |
| openi_storage_usage | 查看账号或单个主体的存储配额（上限/已用/剩余） |
| openi_search_datasets | 搜索平台托管的数据集（关键词/类别/任务/许可证/排序/只看推荐，返回体积与 UUID） |
| openi_search_models | 搜索平台托管的模型与 ckpt（关键词/标签/排序/推荐，返回体积、下载量、是否外链） |
| openi_list_subject_files | 列出任意数据集/模型库里的文件与子目录及大小（>1000 行用 marker 翻页） |
| openi_pull_dataset | 从 OpenI 拉取数据集文件到本地（单文件或整库，支持 dry_run 与 max_bytes） |
| openi_pull_model | 从 OpenI 拉取模型/ckpt 文件到本地（单文件或整库，支持断点跳过） |
| openi_delete_subject_file | 删除数据集/模型库里的一个文件或整个子目录（删完重新列清单核对，本来没有则报 existed=false） |
| openi_delete_dataset | 删除数据集（全名或 UUID，不可恢复） |
| openi_delete_model | 删除模型库（全名或 UUID，不可恢复） |

### 任务模板

别人配好的一键开任务模板。

| 工具 | 作用 |
| --- | --- |
| openi_list_task_templates | 浏览任务模板广场：别人配好的一键开任务模板（含代码仓、分支、启动文件、卡型卡数、镜像、数据集/模型挂载） |
| openi_get_task_template | 按模板 ID 查一个模板的完整配置 |
| openi_collect_task_template | 收藏 / 取消收藏任务模板 |
| openi_start_task_from_template | 照着模板开任务：仓/分支/启动文件/挂载自动带上，spec 由卡型+卡数实时配对 |
