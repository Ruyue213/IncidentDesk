# IncidentDesk

[English](README.md) · [参与贡献](CONTRIBUTING.md) · [安全边界](SECURITY.md) · [路线与发布门槛](ROADMAP.md)

**面向本机服务的性能与可用性诊断 Agent：结论有可核对证据，操作需人工批准。**

IncidentDesk 连接你授权的本机服务，采集有边界的日志、健康状态、Prometheus 指标、源码与项目文档，形成可以打开引用核对的诊断报告。重启与配置修改先展示冻结的命令或差异，批准后再执行；关闭页面或重启应用后，任务、审批与回执仍可恢复。

**当前定位：本机工程 Beta。** 支持同一操作系统用户、同一台电脑，网页只监听 `127.0.0.1`；已验证的模型适配器是 DeepSeek。本版本面向本地进程服务开发者，远程服务器、Docker/Kubernetes 管理以及多人生产部署不在支持范围。引用校验说明证据来源可核对，不能证明模型的因果判断一定正确。

## 安装与首次使用

需要 **Python 3.12**、macOS 或 Linux 和 `ps`；暂不支持 Windows。发布 wheel 包含网页资源与明确标注的合成案例，安装用户无需 Node.js。

从本仓库 **Releases → Assets** 下载 wheel 和 `incidentdesk-constraints.txt`，放在同一目录后在独立环境安装。constraints 固定发布验收使用的依赖版本：

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --constraint ./incidentdesk-constraints.txt ./incident_desk-0.3.0b1-py3-none-any.whl
incidentdesk doctor
incidentdesk serve
```

打开 [本机工作台](http://127.0.0.1:8787)。`serve` 启动 API 与 worker，Ctrl+C 停止；需要后台运行时使用 `incidentdesk start`，查看与停止分别使用 `incidentdesk status`、`incidentdesk stop`。可以通过 `--port 8790` 更换本机端口。仅打开页面不会调用模型。

无需凭证的安装检查可以运行 `incidentdesk serve --offline`。这里使用的是**固定测试脚本模型**，只验证合成案例与执行流程，不是本地大模型推理，不能用它诊断真实项目。

### 诊断自己的服务

1. 打开“项目与模型设置”，登记项目目录、允许读取的日志相对路径和本地健康／指标端点，使用采集检查确认各数据源是否可用。
2. 填写 DeepSeek API 地址、模型标识和 Key，选择推理选项与费用上限。保存配置不等于连接成功；显式执行的工具调用连接测试会计费。
3. 创建“真实本机项目”任务，说明异常现象、受影响的业务请求及时间窗。打开报告引用，对照实际快照判断模型解释是否成立。
4. 对已完成任务继续追问。系统创建关联的新任务并重新采集证据，保留上一轮报告。
5. 如需托管启动／重启，先登记程序与参数数组；如需编辑配置，先登记允许修改的文本文件。操作展示具体命令／差异，批准绑定该次冻结方案，执行前再次复核。

启动命令是操作者登记的参数数组，例如 `["/absolute/project/bin/server", "--port", "18080"]`，模型不能自行生成任意 shell 命令执行。已有外部进程可以只读诊断，不会按进程名接管或终止。托管能力针对直接启动的单进程服务，暂不支持 supervisor/reloader 进程树管理。

[本机 API 文档](http://127.0.0.1:8787/docs) 提供完整项目登记、知识库、历史观测与任务契约。健康端点恢复不能独立证明业务恢复；有条件时应登记有边界的只读业务探针。

## 项目知识库与混合检索

知识来源限定在已登记项目和明确选择的 Markdown／文本路径。每次索引形成不可变版本，记录来源摘要、行号、片段身份与 embedding 指纹；诊断任务冻结所选版本，重建索引不会替换在途任务使用的来源。

支持词法 BM25、本地向量余弦检索与 RRF 融合。可选 embedding 为 `intfloat/multilingual-e5-small`，固定 revision `fd1525a9fd15316a2d503bf26ab031a61d056e98`，使用 384 维归一化向量和 E5 query／passage 前缀。推理不会临时联网下载模型，需要明确安装：

```sh
python -m pip install --constraint ./incidentdesk-constraints.txt './incident_desk-0.3.0b1-py3-none-any.whl[rag]'
incidentdesk embeddings status
incidentdesk embeddings download
```

可选依赖会安装 PyTorch 与 sentence-transformers；下载命令获取约 470 MB 的公开模型文件，模型遵循其上游许可证。本地 embedding 不代表诊断完全离线：检索到并选入上下文的片段仍可能发送给云端 LLM。

在项目设置中登记 `knowledge_paths` 与 `retrieval_mode`，保存后通过知识面板明确建立索引、试检索；也可以调用 `POST /api/projects/{id}/knowledge/index` 和 `/knowledge/search`。面板还提供历史采样与固定业务探针检查。这些操作不消耗 LLM 调用。缺少本地模型时会明确报告降级，不能将词法结果标称为已启用混合检索。这是面向项目文档的有界索引，并非企业级海量文档搜索平台。

## 工程闭环及边界

| 关注点 | 当前机制 | 实际边界 |
| --- | --- | --- |
| 证据 | 版本化快照、引用原文校验、项目知识索引版本 | 保留缺失信息与因果不确定性 |
| 时间 | 有界采集健康／指标历史，报告时间窗覆盖情况 | 开始采集前的历史无法补建 |
| 执行 | 持久任务队列、独立 worker、检查点恢复、事件游标 | 当前只有本机单用户、单 worker |
| 副作用 | 冻结预览、审批摘要、目标／文件复核、执行回执 | 外部操作中断后可能结果未知，不盲目重放 |
| 验证 | 进程身份、端口／健康检查、可选只读业务探针 | 仅覆盖已登记业务请求 |
| 费用 | 持久费用预留、单任务上限、固定模型／价格版本 | 本地估算不能替代供应商账单 |
| 交付 | 包内网页资源、安装后 CLI、独立环境发布检查 | 需要 Python 环境，暂非签名桌面安装器 |

核心技术包括 Python、FastAPI/Pydantic、Deep Agents/LangGraph、SQLite WAL／检查点／FTS5、本地 E5 embedding、React/TypeScript/Vite 和 SSE。工具范围与审批由模型外的代码执行；增加 Agent 数量本身不能提高可靠性。

## 数据位置、安全与维护

安装版在 macOS 默认使用 `~/Library/Application Support/IncidentDesk`，Linux 使用 `${XDG_STATE_HOME:-~/.local/state}/incidentdesk`。可通过 `INCIDENT_DESK_HOME=/absolute/private/path` 指定私有目录。源码版保留仓库原有 `.local` 与 `.env`；从其他目录启动不会自动发现该目录的 `.env`，也不会向安装包目录写业务状态。

API Key 位于后端版本化私有文件，仅使用操作系统用户权限保护，未加密。报告、检查点、项目位置和备份可能包含敏感信息，规则脱敏也可能遗漏业务隐私。接入真实数据前请阅读 [安全边界](SECURITY.md)。

```sh
incidentdesk stop
incidentdesk backup --archive /absolute/private-backups/incidentdesk.zip
incidentdesk restore --archive /absolute/private-backups/incidentdesk.zip
incidentdesk start
```

恢复前还需停止平台托管服务。备份保留凭证与数据库，排除运行时进程所有权和已下载的 embedding 权重，要求应用版本一致。目标非空时需显式 `--replace`，系统先保存旧状态备份；外部项目文件需要另外备份。恢复后如需向量检索，应重新明确下载固定版本模型。备份未加密、未签名，必须私密保存。

## 从源码运行与验证

```sh
uv sync --frozen --extra rag --group dev
npm ci --prefix frontend
npm run build --prefix frontend
uv run --frozen --extra rag incidentdesk doctor
uv run --frozen --extra rag incidentdesk serve
```

需要 embedding 时，每次源码 `uv run` 都保留 `--extra rag`：uv 会同步所选依赖，省略可能卸载未选择的可选包。仅使用词法检索时，可一致省略该 extra，也无需下载模型。

```sh
uv run --frozen --extra rag ruff check .
uv run --frozen --extra rag ruff format --check .
uv run --frozen --extra rag pytest -q
npm test --prefix frontend
uv build --out-dir dist
uv export --frozen --all-extras --no-dev --no-emit-project --no-hashes --output-file dist/incidentdesk-constraints.txt
uv run --frozen --extra rag python scripts/check_release.py --wheel dist/incident_desk-0.3.0b1-py3-none-any.whl --constraints dist/incidentdesk-constraints.txt
```

发布检查在全新虚拟环境与无关工作目录安装 wheel，验证包内网页和静态资源、登记合成项目、完成脚本模型任务，再验证停止与备份／恢复；不调用供应商，不操作已有真实服务。CI 在 Linux 与 macOS 执行无需真实模型的检查。真实付费模型与具体目标服务集成需要另行显式验证；仅编写了 CI 文件不能视为对应平台已经验收。

## 贡献与产品方向

项目聚焦本机服务开发者的可复现排障和审批操作闭环。当前没有生产诊断准确率、自动修复成功率、完整离线推理或所有兼容接口通用性的承诺；其他供应商需要独立的工具回合与 usage 验证。

欢迎提供最小合成故障复现、可衡量的检索对比、有边界业务探针的服务适配器，以及操作中断后的恢复测试。具体门槛见 [路线图](ROADMAP.md)，提交方式见 [贡献说明](CONTRIBUTING.md)。

Apache-2.0；[第三方依赖、模型和网页资源](THIRD_PARTY_NOTICES.md) 保留各自许可证与声明。

## 验证材料

参见[工程验收记录](docs/release/verification.md)、[冻结检索对照](evals/retrieval/README.md)和[产品支持边界](docs/PRODUCT.md)。公开结果区分脚本模型、真实组件与真实云模型，不将小型检索基准当作业务诊断准确率。
