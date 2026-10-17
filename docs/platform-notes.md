# 平台行为与实测记录

这份文档记录 openi-bridge 依赖的**平台接口行为**：哪些路由存在、返回什么形状、
有哪些坑。写在这里而不是 README，是因为它是给要改这个工具的人看的，不是给用它的人看的。

所有结论都来自真实令牌的实测，原始输出在 [`verified-outputs/`](verified-outputs/)。
抓取时间 2026-10-14 ~ 2026-10-17。

本工具基于平台前端实际调用的 REST 接口（`/api/v1/...`）实现，创建任务的请求体取自平台
源码 `entity.CreateReq`。已用真实令牌验证过的接口与行为：

- 账号与代码仓：`/user`、`/user/repos`（`POST` 建仓）、`DELETE /repos/{owner}/{repo}`
  删仓、`/repos/{owner}/{repo}`、`/branches`、`/right`（写权限）、`/contents` 目录列表，
  以及文件读写 `GET/POST/PUT /repos/{owner}/{repo}/contents/{path}`（POST 新建、PUT 覆盖
  需带旧 blob sha）。
- 代码仓的 git 面：`/git/trees/{ref}?recursive=1` 返回文件树，但**有隐藏分页**——
  一次最多回 1000 条并带 `truncated` / `page` / `total_count`，取整棵树要按 `page` 翻完
  （详见下面 `openi_repo_tree` 那条）。另有 `/commits` 提交历史、`/archive/{ref}.zip`
  整仓打包、`/issues`（GET 列表 / POST 新建）、`/issues/{n}/comments`（POST 评论）。
- 资源发现：`/ai_task/creation/required` 返回当前账号在指定 `job_type` + `compute_source`
  + `cluster_type` 下真实可用的规格列表（含数字 `spec_id`、卡型、卡数、CPU/内存、单价）、
  可用镜像、默认任务名、积分余额，以及 `can_create_more` / `not_stop_task_count` 等配额信息。
- 镜像目录：`/images/recommend`（平台推荐）与 `/images/star`（本账号收藏），其中的 `place`
  才是集群实际拉取的镜像地址。
- 算力资源目录：`/resources/ai_center/card_info/active` 是在线卡型（比
  `/resources/acc_card/list` 多出 CPU 的 INTEL XEON、GPGPU 的 N100/N260、GPU 的
  5090/A800），`/resources/ai_center/active` 给每个算力中心的城市与卡型。
- 任务模板：`GET /ai_task_template/list/{scope}`（`scope` 取 `public` / `all` /
  `created` / `collected`，返回体是 `{Page, PageSize, Total, Templates}`）、
  `GET /ai_task_template?id=<uuid>`（单条详情，响应是 `{code,msg,data}` 信封），
  收藏走 `PUT` / `DELETE /ai_task_template/collect?id=`。模板自带 JobType、Cluster、
  ComputeSource、AccCardType、AccCardsNum、RepoOwnerName/RepoName、BranchName、
  BootFile 与数据集/模型清单，但没有 spec_id，`openi_start_task_from_template` 是按
  `(compute_source, acc_card_type, acc_cards_num)` 去 `/ai_task/creation/required`
  的实时清单里配对的（实测 NPU MNIST 模板配到 spec 328，挂上 MnistDataset_mindspore
  开 DEBUG 任务能到 RUNNING，代码仓可以直接用模板自带的公开示例仓）。
- 任务读写：`/ai_task`、`/ai_task/brief`、`/ai_task/log`、`/ai_task/resource_usage`、
  `/ai_task/debug_url`、`/ai_task/visualize_url`、`/ai_task/self_endpoint_url`（在线推理
  的对外地址）、`/ai_task/my_list`（账号级任务列表）、`/{owner}/{repo}/ai_task`（按仓列表）。
- 任务观测：`/ai_task/log/download`（整份日志纯文本，不是 JSON）、
  `/ai_task/operation_profile`（调度事件时间线，每条含 reason/timestamp/message，时间戳
  是 UTC）、`/ai_task/node_info`（节点清单）、`/ai_task/loss`（训练 loss 曲线）、
  `/ai_task/output`（输出目录，一次一层，`parent_dir` 下翻）、`/ai_task/output/all`
  （一次返回递归全量清单，`FileName` 就是相对路径）、`/ai_task/output/download`
  （下载单个输出文件，平台先答 307 再跳到 OBS 签名直链，`file_name` 给纯文件名或
  相对路径都能命中）。
- 任务变更：`POST /{owner}/{repo}/ai_task/create`、`/ai_task/stop`、`/ai_task/restart`、
  `/ai_task/del`、`/ai_task/batch_del`。
- 任务产物落库：`POST /ai_task/export2dataset`（查询串收 `dataset_id` 与 `id`，表单体收
  `task_id` 与 `file_list_str`）与 `POST /ai_task/export2aimodel`（查询串收 `id`，表单体
  收 `name` / `alias` / `owner_id` / `aimodel_type` / `is_private` / `engine` / `label` /
  `license` / `task_id` / `file_list_str`）。两条都只认
  `application/x-www-form-urlencoded`，JSON 体会被反向代理直接 403；字段名按平台 Go 侧
  转蛇形，清单字段少了 `_str`（写成 `file_list`）平台会回 "file list is empty"。导出是
  异步的，进度查 `GET /dataset/get_export_process?id=` 与 `GET /aimodel/get_export_process?id=`。
- 代码仓 issue 编辑：`POST /repos/{owner}/{repo}/issues/{n}` 带 JSON 体（`state` / `title` /
  `body`）可改，返回 201 与更新后的 issue；Gitea 原生的 `PATCH` 被前置反向代理挡成空 400，
  `PUT` 与带 `_method=PATCH` 的 POST 都没注册（404），所以工具走的是这条 POST。
- 通知：`GET /notifications`、`GET /notifications/new`、`GET /notifications/threads/{id}`
  与 `/threads/{id}/comments`（正文）、`PUT /notifications?all=true`（全部标已读，回 205）。
- 数据集与模型：`GET /dataset?dataset_name=`、`GET /aimodel?aimodel_name=`（拿 UUID 与
  `can_edit_file`）、`GET /dataset/files`、`GET /aimodel/files`、`GET /storage/summary`；
  创建走 `POST /dataset/create` 与 `POST /aimodel/create`（JSON 体，`owner_id` 必须是整数），
  删除走 `POST /{dataset|aimodel}/batch_delete?dataset_ids=...`。
- 广场检索：`GET /dataset/list/{scope}` 与 `GET /aimodel/list/{scope}`，`scope` 取
  `public` / `owned` / `collected` / `collaborated`（对应平台数据集/模型页的四个 tab）。
  查询参数 `page`、`page_size`、`q`、`order_by`、`tags`、`tasks`、`license`、`recommend`；
  返回体是 `{datasets|aimodels, total, page, page_size}`。公开数据集体量约 1 万、
  模型约 2.9 万，`recommend=only` 时是首页那 40 个数据集 / 37 个模型。
- 文件清单：`GET /dataset/files` 与 `/aimodel/files` 单页最多 1000 行，用 `marker`
  翻页（响应里的 `has_next` / `marker`），每行是 `FileName`、`IsDir`、`Size`、`ModTime`。
- 从平台下载：`GET /dataset/file` 与 `/aimodel/file` 对托管在 OBS 上的文件返回
  **301**，`Location` 就是带签名的直链，直接对 OBS 发 GET 即可，无需再带令牌
  （工具内部走的就是这条路径）。整库打包另有
  `GET /{dataset|aimodel}/download`，返回一个含 `openi_resource.version` 的 zip。
- 文件上传：小文件走 `GET /upload/direct/get_upload_url` → `PUT` → `POST
  /upload/direct/complete_upload`；大于 64MB 走 `GET /upload/get_chunks` →
  `GET /upload/new_multipart` → 每片 `GET /upload/get_multipart_url` + `PUT` →
  `POST /upload/complete_multipart`。实测 70MB 文件分片上传成功，重复上传会命中
  `get_chunks` 的 `uploaded` 直接跳过（断点续传）。

需要注意：

- 上传到对象存储的 `PUT` 用的是预签名地址，签名把请求头也算进去了：带 `Authorization`
  或 `Content-Type` 都会返回 `SignatureDoesNotMatch`，所以这条请求只发文件体本身。
- 数据集/模型名（`name`）只能是字母/数字/连字符/下划线/点，字母数字开头结尾；`alias`
  （中文展示名）只接受中文/字母/数字/连字符/下划线/点，且不含空格。
- 分片的 md5 不是整文件 md5，而是「每个 64MB 分片取前 1MB」串起来算的值，与官方 openi
  SDK 一致；`get_file_md5` 用错会导致续传判定失败。
- 上传前会先查 `/storage/summary` 比对剩余配额；单账号默认 100GB。`/storage/summary`
  带 `subject_id` 时实测仍返回账号级汇总，所以按主体维度的剩余额度只能作参考。
- 文件本身落在华为 OBS（`obs.cn-south-222.ai.pcl.cn`）上，是持久化存储：上传成功后
  不随任务或会话消失，删除走 `openi_delete_subject_file`（单个文件或子目录）或
  `openi_delete_dataset` / `openi_delete_model`（整个库，`batch_delete`）。
- 删除单个文件走 `DELETE /{dataset|aimodel}/file?file_name=`，平台对**不存在的路径也
  返回 `{"code":0,"msg":"ok"}`**（删一个不存在的 `ghost.txt` 也回 ok，实测），只看响应
  就会把「根本没删」当成删掉了。工具因此先确认路径存在，删完再重新列一遍父目录，清单里
  确实没有了才报 `removed=true`，本来就没有则报 `existed=false`。
- 代码仓写入走 Gitea 风格的 `contents` 接口：新建用 `POST`、覆盖用 `PUT` 且必须带旧
  blob 的 sha（工具会先查一次 sha 再决定用哪个动词），因此**每个文件一个提交**。平台的
  多文件提交入口（`POST /repos/{owner}/{repo}/contents` 批量、`POST /git/blobs`）在这套
  部署上都是 404，所以没有批量提交这条路径。
- `openi_repo_tree` 走 `/git/trees/{ref}?recursive=1`。这个接口有隐藏分页：一次最多回
  1000 条，同时在返回里给 `truncated` / `page` / `total_count`（mindnlp 实测 2627 条，
  第一页只有 1000）。所以工具会翻完所有页再返回，`total_count` 是平台声明的总数；
  `max_entries`（默认 20000）是之后的硬截断，被它截时 `count < total_count` 且
  `truncated` 为 true。`openi_list_repo_commits` 走 `/commits`，
  它的 `path` 过滤参数在这套部署上是空操作（实测传任何 path 都返回全量历史），所以工具
  没有暴露这个参数；要看某个文件的内容直接 `openi_read_repo_file`。
- `openi_download_repo_archive` 走 `/archive/{ref}.zip`，边下边写盘，先落在
  `<目标>.part` 下完再改名，中途失败或超过 `max_bytes` 都会把临时文件删掉，不会留下
  半截 zip。
- 任务输出（`/ai_task/output*`）在平台侧是**异步索引**的：任务刚进 `SUCCEEDED` 时
  `output.status` 是 1、`file_list` 是空的，通常要等一两分钟才变成 `status=0` 并列出
  文件（实测 TASK-14 / TASK-13 都在约两分钟后才刷出来）。所以 `openi_task_output_dir` /
  `openi_task_output_files` 带 `ready` 标记：刚跑完看不到文件不代表任务没有输出，隔一会儿
  重试即可。输出目录里的子目录以 `IsDir=true` 标出，下载子目录里的文件用相对路径
  （如 `sub/mid.txt`）或者“文件名 + `parent_dir`”两种写法都行。
- 单文件走 base64（整个文件进内存），因此 `openi_push_repo_files` 有单文件上限
  `MAX_REPO_FILE_BYTES`（50MB）；更大的文件请改用 local git 推送。
- 通知只能“全部标已读”：Gitea 的 `PATCH /notifications/threads/{id}` 被前置反向代理
  拦截，同名的 `PUT` 与带 `_method=PATCH` 的 `POST` 都没注册（404），平台前端标单条走的是
  带 `_csrf` cookie 的网页表单，令牌方式用不了，所以没有做单条标记的接口。
- 没有做成工具的 `DELETE /repos/{owner}/{repo}/contents/{path}`：这条路由在平台上会一直
  挂到代理的 60 秒超时（504/502）且不删文件，包一层只会给出假的成功，所以故意不提供。
  要换内容就覆盖写，要清空就删掉整个仓。
- 建仓（`POST /user/repos`）与删仓（`DELETE /repos/{owner}/{repo}`）在平台侧都要跑几十秒
  （实测建仓 30 秒以上、删仓约 7 秒），工具默认给 180 秒超时；超时不代表失败，请回查
  `openi_list_my_repositories` 确认，不要重复建仓。
- 检索与下载用的是 REST 面（`/api/v1/...`），和积分那批 web 接口不同：这里带
  `Authorization: Bearer`，而不需要 Basic。注意 `httpx.Client(base_url=...)` 必须写成
  `"{endpoint}/api/v1"`，只填 endpoint 会让 `/aimodel/file` 之类返回 404。
- 广场列表的 `page_size` 上限是 100：传 200 会被平台静默压成默认的 12 条，所以工具
  在 1-100 之外直接报错。反过来，`tags` / `tasks` 传字符串会被 `",".join()` 按字符拆开
  （`tags=computer_vision` 变成 `c,o,m,...`），平台照样返回 200 但结果是 0 条——工具
  内部已把字符串按逗号切分，列表和逗号串两种写法都吃。
- 数据集接口支持 `tasks` 和 `license` 过滤（`license=MIT` 实测从 1 万条筛到 1099 条），
  但模型接口实测忽略这两个参数（传进去仍返回全量），所以 `openi_search_models` 没有
  它们，找模型请用 `q` 或 `tags`。
- 文件清单一页最多 1000 行，靠响应里的 `has_next` / `marker` 翻页；
  `openi_list_subject_files` 把 `marker` 透传出来了（20 万张图的目录要翻 200 页），
  递归下载走的是内部循环，不受影响。
- 重复上传同名文件是覆盖，不会出现两份（实测二次上传后目录里仍然只有一个 `a.txt`）；
  但 ≤64MB 的直传不做跳过判断，重传会把字节重发一遍，只有分片路径会命中
  `get_chunks` 的 `uploaded` 跳过已完成分片。
- 上传目录只跳过 `.DS_Store` / `Thumbs.db`，`.git` 之类的隐藏目录会一起传上去；
  传代码目录时注意先清理。
- 下载不做重试：单个文件失败会记在返回的 `failed` 里，重跑会跳过已完成文件，所以
  半途断掉直接再跑一次即可。下载的总时长不设上限（怕大 ckpt 被掐断），但每次读有
  120 秒超时，连接被静默挂住时不会无限等。
- 拉取时如果平台返回的文件名里含 `..`，写到目标目录之外的路径会被跳过并计入 `failed`。
- 目录遍历默认上限 20000 个文件（环境变量 `OPENI_WALK_FILE_LIMIT` 可调），
  `openi_pull_dataset` / `openi_pull_model` 另有 `max_files` 参数；文件更多的数据集
  必须显式放宽才拉得全，否则会直接报错而不是悄悄少拉。
- 外链模型（返回里 `external: true`，`external_name` / `external_url` 非空）在平台上
  没有实际文件，`openi_list_subject_files` 会是空、`openi_pull_model` 也拉不到东西；
  真正托管在自己对象存储里的模型 `external` 为 false。
- 大主体（例如 `Open_Dataset/imagenet`，143GB）会触发全量遍历，因此
  `openi_pull_dataset` / `openi_pull_model` 在 `max_bytes` 有值且未指定 `file` 时，
  先读主体的声明体积做一次拦截；真要全量拉请显式调大或省略 `max_bytes`。
- `openi_start_task` 的 `dataset_uuid_str` / `pretrain_model_id_str` 支持逗号分隔的
  `owner/name`，工具会先查回 UUID 再创建任务；挂载靠平台把数据迁到集群
  （任务会经过 `dataMigrating`），所以公开 ckpt 可以直接挂、不必先下载。
- 数据集/模型库的整库 zip 接口（`/{dataset|aimodel}/download`）没有单独做成工具：逐文件
  拉取已经能拿到同样的内容，还多出 `dry_run` 预览、按大小跳过已下载文件和断点续传的能力，
  而 zip 一次性把整包放进内存/磁盘、中途失败要重来。需要 zip 时可直接对该接口发请求。
  代码仓的打包下载是另一条路由，做成了 `openi_download_repo_archive`。
- 普通用户可用的集群是 `C2Net`（智算网络）。`cluster` 必须填 `C2Net`，`cluster_type`
  查询参数同理；`openi` 之类的值会返回参数错误或空规格。
- 任务模板按 ID 取详情只有 `GET /ai_task_template?id=<uuid>` 一条路：
  `/ai_task_template/detail`、`/ai_task_template/<uuid>` 都是 404，列表接口的 `id`
  参数也是摆设（传了照旧返回首页，实测过）；列表 `page_size` 上限同样是 100。公开
  模板约 1600 个、含私有约 2700 个。`order_by` 只认 `newest` / `usecount` /
  `collections`，写别的平台不报错、直接按默认顺序返回，所以工具显式拦住；`tags` 是按
  模板自己的标签精确匹配（大小写敏感，取值多为中文），不是广场数据集那种类别枚举。
- 模板的 `ImageUrl` 不一定是镜像地址：NPU 的 MNIST 模板给的是内部记录 UUID
  （`318c8cac-...`），当镜像地址发下去必然拉不起来。`openi_start_task_from_template`
  默认忽略模板镜像、改用实测跑通的镜像，要指定就用 `image_url`；模板确实固定了特殊
  镜像（如在线推理的 `openclaw-feishu-qwen3.5`）时才把 `use_template_image` 置 True。
  模板里的 `Parameters`（表单默认值）也没有自动映射到 `params`，需要时自己传。
- `openi_start_task_from_template` 不填 `repo` 时用模板自带的仓，公开模板多是示例仓
  （如 `OpenIOSSG/OpenI_Cloudbrain_Example`），实测可以直接在上面开任务，不必先建仓。
- 创建任务的必填字段是 `job_type`、`display_job_name`、`spec_id`（整数）、
  `compute_source`、`cluster`；`spec_id` 必须来自 `openi_list_creation_options`，否则会
  返回 Specification not available。
- 创建是异步的：`create` 立即返回任务 ID（状态 `PREPARING`），随后集群异步拉起。如果集群
  侧排队或拉起失败，任务会进入失败态，且不计费；用 `openi_get_task` 观察状态。
- DEBUG 任务不需要 `boot_file`；训练类任务需要以 `.py` 结尾的 `boot_file`。
- 在线推理（`job_type=ONLINEINFERENCE`）**只支持 GPU**：同样的 job_type 配
  `compute_source=NPU` 时 `/ai_task/creation/required` 返回的规格清单是空的，开不了。
  GPU 下实测给的是 A100 与 V100 两档（spec 237 / 210，单价 9 / 6 分/卡时），`images`
  字段为 `None`，所以不指定镜像时会走 `image_url` 默认镜像（实测能拉起）。配额上
  `limit_count` 通常是 1，即同一时刻只能有一个在线推理任务（`can_create_more` 反映
  当前是否还能开）。
- 在线推理任务把外部流量转到容器内的 **8000 端口**（任务详情的 `port` 字段就是这个值，
  `end_point` 是平台自动生成的一段 hex）。平台**不会**向容器注入 `PORT` / `SERVICE_PORT`
  之类的端口环境变量（实测 `env_PORT` 为 null），服务必须自己监听 8000。任务 RUNNING
  后 `GET /ai_task/self_endpoint_url?id=` 返回真实地址（形如
  `https://<hex>.mlunotebook.openi.org.cn`）。这个地址在任务还是 WAITING 时就已返回，
  但那会儿服务没起来、连不上；预热期间该接口也可能返回 `code 99`
  （"The service is loading"），隔一会儿重试即可。实测在这条地址上 GET 与 POST 都能
  收到容器里服务的响应。
- 在线推理的自定义访问路径与端口在请求体里的字段名是 `endpoint` / `port`，但平台对
  这两个值校验很严（只传 `port` 报 2030 "Custom path and port must be filled in"，
  再补 `endpoint` 又变成 9009 系统错误），所以工具没有把它们做成参数，需要时用
  `extra_fields` 自己拼，并接受可能被拒。
- 在线推理任务**不会自动停止**，会一直计费；训练任务里那套 `time_limit` 自动停止在这
  条路径上没有用。实测过程中 `openi_task_logs` 在任务运行期间返回 "The AI task is still
  running"（推理任务不落日志），`openi_task_resource_usage` 返回 6003 "Does not support
  displaying resource usage data yet"，`visualize_url` 返回空串——这几点都是平台行为，
  不是工具的问题。
- 账号级任务列表用的是 `/ai_task/my_list`，它在平台前端真实存在，但字段较少；更完整的信息
  用 `openi_get_task`。
- 镜像：只有 NVIDIA GPU 的登记 `image_id` 在 C2Net 上拉不起来（`image not available`），
  工具对 GPU 会改用 `OPENI_DEFAULT_IMAGE_URL` 并把镜像地址作为 `image_url` 传下去；其余
  算力的 `image_id` 可直接使用。
- `branch_name`：除 NVIDIA GPU 外的算力在创建时都需要它，缺失会以 `Fail to load code`
  （`CREATED_FAILED`）失败；工具默认取代码仓的默认分支。
- 可用性：`GPU` / `NPU` / `GCU` / `MLU` / `DCU` / `ILUVATAR-GPGPU` / `BIREN-GPU` 都已开卡
  验证（见上面的实测表）；`METAX-GPGPU`（沐曦 C500，spec 333）有规格但从未调度到卡——
  连续 9 次 DEBUG 任务里 7 次卡在 `centerPending`、2 次 `CREATED_FAILED`，且任务详情里的
  `ai_center_code` 是 `openi-mlu`，看起来没有真正的沐曦资源池；`CPU` 是唯一拿不到规格的
  `compute_source`。
