# EventPilot

**EventPilot: Event-Aware Hierarchical Task Scheduling for Web Agents**。

用于评估 LLM Agent 在网页任务执行中遇到新事件时的决策、后续处理、调度及真实执行表现。一级决策为 `IGNORE / DEFER / INTERRUPT`；选择 `INTERRUPT` 后，二级决策为 `HANDLE / REPLAN / TERMINATE`。

当前入口是基于官方 **Online-Mind2Web** 原任务的事件扩展，协议为 `official_online_eventarena_v5`。官方原任务 A 的目标和入口保持原样；新任务型事件优先引用另一条官方任务 B。目标修改、撤回和通知明确属于作者新增事件。事件按实际页面与动作触发，Full Trajectory 来自实际浏览器日志。

## 当前数据和代码范围

| 版本 | 数据 | 当前规模 | 运行入口 |
| --- | --- | --- | --- |
| 当前 Official v5 | 官方 Online-Mind2Web + 作者事件 | 10 单事件、7 Counterfactual、2 Multi-event；19 个独立事件案例 | `scripts/run_official_online.py` |
| 保留的 Online v2.0.6 | Mind2Web 来源任务的作者适配与事件候选 | 100 单事件、70 Counterfactual、20 Multi-event | `scripts/run_online_v2.py` |

Official v5 的单模型全部实验、1 repeat 注册 **71 个 job**，包含 Main、四种输入的 Ablation、Counterfactual、Multi-event、无事件 Baseline 和 Final-intent Baseline。Main 与 Ablation 的 Full Trajectory 共用执行结果，注册 job 数不等于独立执行数。当前官方版案例集中在少量 Apple / Justice 任务上，不能作为已经完成的 100 条官方事件 benchmark。

**事件 Gold 尚未完成独立人工审核，`human_reviewed=false`。** 测试通过证明工程回归检查通过，不代表所有真实网站当前可用，也不保证模型实验分数。这里使用 EventArena 的证据评估规则，分数不能直接与官方 Online-Mind2Web 排行榜比较。

完整源码包包含两个版本的核心模块、作者构建配方、配置模板、测试和文档。API Key、本机网关地址、Python/Chromium 二进制、原始数据、HAR、截图和实验日志不包含；按下面的说明在本地配置即可。

## 1. 安装环境

macOS / Linux，建议 Python 3.12：

```bash
git clone https://github.com/yangkf825/EventPilot.git
cd EventPilot
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-online-v2.lock.txt
python -m playwright install chromium
```

Linux 缺少浏览器系统库时，将最后一条改为 `python -m playwright install --with-deps chromium`。Windows 用 `.venv\Scripts\Activate.ps1` 激活；必要时安装 `tzdata`，并将后续 shell 环境变量语法改为 PowerShell。Python CLI 不依赖 zsh、H 盘或外置盘镜像。

两套实验核心文件保持已注册字节和 fingerprint；只有公开包装和便捷启动脚本做可移植配置。历史手工采集入口 `python -m eventarena` 保留在源码中，使用它时须显式设置 `EA_M2W_RUNTIME` 指向自己的 runtime。本文的 Official / v2 实验入口不使用该历史默认值。

## 2. 配置官方数据集

在 [官方 Hugging Face 数据页](https://huggingface.co/datasets/osunlp/Online-Mind2Web) 登录并完成访问申请，自行下载 `Online_Mind2Web.json`。当前数据版本和导入方法见 [Official 数据配置](docs/OFFICIAL_DATA_SETUP.md)。不要把 Token 或官方 gated 原始文件提交到 GitHub。

```bash
python scripts/official_online.py import \
  --input /path/to/Online_Mind2Web.json \
  --revision eacad896a84dc5b65e29b0b06e4699ab0544d701

python scripts/official_online.py build-events \
  --spec config/official_online/event_pilot_specs.json \
  --output data/official_online/events_pilot
```

`--revision` 应填实际下载文件所属的版本；本次发布使用的原文件 SHA256 为 `7dedf381531d423dc0fc48d21dc1d425655dd75f38b10211a880fa09102f257f`。导入保存完整原记录及哈希，构建时逐项保留官方 A/B 目标。不自动覆盖已有事件注册；重建不同版本时使用新目录。

导入和构建不调用收费模型。Official 模式不需要下载数 GB 的旧 Mind2Web HTML 数据。旧版 100 条方案另见 [旧版使用说明](README_LEGACY.md) 和 [Mind2Web 数据配置](docs/DATA_SETUP.md)。

## 3. 配置模型

```bash
cp models.example.json models.gateway.json
```

编辑每条模型的 `endpoint` 和 `model`，填写服务方支持的完整 Chat Completions URL 和准确模型 ID。`gateway_ds / gateway_glm / gateway_kimi / gateway_gpt` 是本项目配置别名，与模型版本无关。Key 从 `api_key_env` 指定的环境变量读取；配置文件和 Key 不提交。

zsh 隐藏输入四个 Key（只运行一个模型时只需设置相关 Key，以及选定的 preparation / Judge Key）：

```zsh
read -rs "GATEWAY_DS_API_KEY?DeepSeek API Key: "; echo; export GATEWAY_DS_API_KEY
read -rs "GATEWAY_GLM_API_KEY?GLM API Key: "; echo; export GATEWAY_GLM_API_KEY
read -rs "GATEWAY_KIMI_API_KEY?Kimi API Key: "; echo; export GATEWAY_KIMI_API_KEY
read -rs "GATEWAY_GPT_API_KEY?GPT API Key: "; echo; export GATEWAY_GPT_API_KEY
```

`.env.example` 只说明变量名，代码不自动加载 `.env`。某些模型要求 `max_completion_tokens`，其他模型要求 `max_tokens`，按 API 文档选用。自定义模型与服务的配置方法见 [模型配置](config/README.md)。

## 4. 检查和无事件运行

本地回归测试不调用收费 API；未安装原始数据时，依赖原始数据的检查会明确 skipped：

```bash
python scripts/check_public_data.py
python -m pytest -q
```

GitHub Actions 测试配置作为可选示例保存在 `config/github_actions_tests.example.yml`。若希望在每次 push 自动运行测试，自行将其复制为 `.github/workflows/tests.yml` 并提交；源码同步默认不启用该工作流。

网页入口检查不调用模型；访问成功只证明入口可用，不证明任务成功：

```bash
python scripts/official_online.py check-sites \
  --output reports/official_sites --workers 3 --timeout 25
```

探测模型会发起少量 API 请求：

```bash
python scripts/probe_gateway.py \
  --config models.gateway.json --only gateway_ds --timeout 60
```

先运行官方无事件任务，保留可用性报告与同目录的 manifest：

```bash
python -u scripts/official_online.py baseline \
  --config models.gateway.json --only gateway_ds --judge-model gateway_ds \
  --task-ids d71be72aa25c3eab8eea47a0e60382e2,bf3b311cc8dce16d3de844f4b5875dfd,828c2d98616a9478d5864d847d5a1b28 \
  --availability reports/official_sites/availability.json \
  --run-root runs/official_no_event_ds --headless
```

## 5. 各模型单独运行全部 Official 实验

以下命令包含全部六组实验，每项 **1 repeat**。在已经激活环境、导入数据、配置模型和 Key 后运行。使用同一固定 preparation / Judge 便于比较；这里示例固定为 `gateway_ds`，因此其他模型仍需 DeepSeek Key。论文中应校准固定 Judge 并人工复核，避免将自评结果直接当作无偏模型排名。

DeepSeek：

```bash
python -u scripts/run_official_online.py run --config models.gateway.json \
  --only gateway_ds --prep-model gateway_ds --judge-model gateway_ds \
  --data-dir data/official_online/events_pilot --experiments all \
  --single-limit 10 --cf-groups 1 --multi-limit 2 --repeats 1 --workers 1 \
  --run-root runs/official_ds_v5 --headless
```

GLM：

```bash
python -u scripts/run_official_online.py run --config models.gateway.json \
  --only gateway_glm --prep-model gateway_ds --judge-model gateway_ds \
  --data-dir data/official_online/events_pilot --experiments all \
  --single-limit 10 --cf-groups 1 --multi-limit 2 --repeats 1 --workers 1 \
  --run-root runs/official_glm_v5 --headless
```

Kimi：

```bash
python -u scripts/run_official_online.py run --config models.gateway.json \
  --only gateway_kimi --prep-model gateway_ds --judge-model gateway_ds \
  --data-dir data/official_online/events_pilot --experiments all \
  --single-limit 10 --cf-groups 1 --multi-limit 2 --repeats 1 --workers 1 \
  --run-root runs/official_kimi_v5 --headless
```

GPT：

```bash
python -u scripts/run_official_online.py run --config models.gateway.json \
  --only gateway_gpt --prep-model gateway_ds --judge-model gateway_ds \
  --data-dir data/official_online/events_pilot --experiments all \
  --single-limit 10 --cf-groups 1 --multi-limit 2 --repeats 1 --workers 1 \
  --run-root runs/official_gpt_v5 --headless
```

不同目录会分别准备真实检查点；正式对比推荐在一个共同 run 中选择所有模型，共享经过审计的事实前缀：

```bash
python -u scripts/run_official_online.py run --config models.gateway.json \
  --only gateway_ds,gateway_glm,gateway_kimi,gateway_gpt \
  --prep-model gateway_ds --judge-model gateway_ds \
  --data-dir data/official_online/events_pilot --experiments all \
  --single-limit 10 --cf-groups 1 --multi-limit 2 --repeats 1 --workers 2 \
  --run-root runs/official_four_models_v5 --headless
```

只检查注册、确认规模且不调用 API：在以上任意命令后加 `--dry-run`。各模型均运行四种输入消融：Event-only、Event+Goal、Event+Goal+State、Full Trajectory。消融影响事件决策可见信息；实际 Actor 继续操作网页所需的观测和操作工具保持一致。

## 6. 断点续跑和进度

**保留原运行命令及同一个 `--run-root`**，在原命令末尾增加 `--resume`。普通 resume 跳过已登记结束的作业；若接口限额恢复、网络恢复后需要重试 unknown 的基础设施错误，同时增加 `--retry-infra`。有效已评分结果（含模型自身失败）保留，不重新抽样刷分。

DeepSeek 上述目录的续跑示例：

```bash
python -u scripts/run_official_online.py run --config models.gateway.json \
  --only gateway_ds --prep-model gateway_ds --judge-model gateway_ds \
  --data-dir data/official_online/events_pilot --experiments all \
  --single-limit 10 --cf-groups 1 --multi-limit 2 --repeats 1 --workers 1 \
  --run-root runs/official_ds_v5 --headless --resume --retry-infra
```

GLM / Kimi / GPT 使用各自原命令和原目录，并添加相同选项。续跑要求数据、代码、模型配置、预算、Judge 和注册参数一致。换 API 服务或模型版本应新建结果目录。历史 v4 前缀迁移和 v5 已知评分错误的受限修复见 [Official 协议说明](docs/OFFICIAL_PROTOCOL.md)。不要手改 manifest hash。

```bash
python scripts/run_official_online.py status --run-root runs/official_ds_v5
tail -n 8 -F runs/official_ds_v5/preparation_progress.jsonl
```

共同 preparation 完成后执行真实 reset 审计，覆盖率默认至少 90% 才启动被测 Actor。网站不可用、损坏记录和证据不足保留为 unknown，不能计为成功。评分异常会保存已执行的 Actor trace，符合恢复条件时只重试评分。长期恢复需要保留结果目录以及被引用的 HAR/旧前缀目录。

## 7. 结果、源码打包与许可

结果目录包含 manifest、逐步轨迹、各实验 comparison / 指标文件与 Case D 记录。指标与含 unknown 时的覆盖率报告见 [评估协议](docs/PROTOCOL.md)；当前官方执行改进见 [Official v5](docs/OFFICIAL_PROTOCOL.md)。

重新生成可公开的完整源码 ZIP：

```bash
python scripts/package_github.py --output releases/EventPilot_Official_v5.zip
```

打包采用文件白名单并检查密钥和本机路径，生成逐文件 SHA256 manifest 与 ZIP 校验文件。原始数据、运行结果与环境不会被自动加入。

项目新编代码遵循仓库现有 [MIT License](LICENSE)。官方数据、第三方软件和访问网页材料各有其自身条款，参见 [第三方来源说明](THIRD_PARTY_NOTICES.md)。官方来源：[Online-Mind2Web](https://github.com/OSU-NLP-Group/Online-Mind2Web)、[Mind2Web](https://github.com/OSU-NLP-Group/Mind2Web)。
