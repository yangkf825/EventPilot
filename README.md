# EventArena–Mind2Web

用于测试 LLM Agent 在网页任务执行过程中，面对突发事件时的**判断、后续处理和任务执行能力**。本仓库提供 Online v2.0.5 实验实现（数据候选为 v2.0.4），以及基于 Mind2Web 来源任务构建的作者候选数据。

一级决策为 `IGNORE / DEFER / INTERRUPT`；选择 `INTERRUPT` 后，再判断 `HANDLE / REPLAN / TERMINATE`。主实验、消融、反事实和多事件实验均保留执行评估。

**这是研究开发版本。** 事件和 Gold 是作者构建的候选标注，目前 `human_reviewed=false`；结构验证通过不代表所有网站已完成运行验证，也不保证模型分数。项目并非官方 Mind2Web 或 Online-Mind2Web benchmark。

## 1. 压缩包里有什么

| 内容 | GitHub 包是否包含 |
| --- | --- |
| 实验代码、构建配方、验证脚本、回归测试 | 包含 |
| 100 条单事件、70 条反事实、20 条多事件的作者定义 | 包含，位于 `data/author_candidates/` |
| 官方来源 ID、split、website、SHA256、固定下载 revision | 包含 |
| Mind2Web 原始任务文本、HTML、测试 JSON | 不包含，按下文从官方获取并在本地恢复 |
| 完整可运行数据 `data/online_v2/` | 本地构建后生成，Git ignored |
| Python 环境、Chromium、API Key、本机网关配置、实验录屏和日志 | 不包含 |

事件定义是文本，压缩率较高，所以 ZIP 的磁盘大小很小。它不是完整 Mind2Web 的离线镜像。固定官方下载约 **6.499 GB**，测试 ZIP 解压另需约 **6.108 GB**；建议准备至少 **25 GB** 的磁盘空间，实验记录另计。这是磁盘存储大小，不是 RAM 用量。

官方要求不要在线重新分发解压后的测试数据，因此这里公开作者新增定义与来源校验信息，通过官方数据在本地还原完整运行数据。参见 [数据配置说明](docs/DATA_SETUP.md) 和 [官方 Dataset Access](https://github.com/OSU-NLP-Group/Mind2Web#dataset-access)。

## 2. 环境安装

解压后进入本项目目录。以下命令适用于 macOS / Linux，使用 **Python 3.12**：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-online-v2.lock.txt
python -m playwright install chromium
curl --version
```

`requirements-online-v2.lock.txt` 固定直接依赖的已测试版本；`requirements.txt` 提供兼容版本范围。下载脚本需要系统 `curl`。Linux 缺少浏览器系统依赖时执行 `python -m playwright install --with-deps chromium`。

Windows 可使用 Python 3.12 和 PowerShell，环境激活命令为 `.venv\Scripts\Activate.ps1`；Windows 若缺少 IANA 时区数据库，另安装 `python -m pip install tzdata`。后续 shell 的变量和续行语法需要对应转换。

源码不依赖指定盘符、外置磁盘运行镜像或预先配置的个人绝对路径。默认使用 Playwright 安装的 Chromium；若使用已有浏览器缓存，显式设置 `PLAYWRIGHT_BROWSERS_PATH`。

## 3. 获取与构建数据

首次使用执行：

```bash
python scripts/check_public_data.py
python scripts/prepare_dataset.py --download --extract
python scripts/validate_online_v2.py
```

准备脚本会下载固定 revision 的官方训练分片和测试 ZIP，验证文件校验值，在本地解压，然后流式提取配方使用的 **54 个来源任务元数据**，重建 `data/online_v2/` 并与公开作者候选逐项比对。此流程不调用付费模型，也不需要申请官方 Online-Mind2Web 的 gated 任务文件。

如果官方文件已经下载并解压：

```bash
python scripts/prepare_dataset.py --raw-dir /path/to/your/Mind2Web
```

如果只有下载文件、测试 ZIP 尚未解压，增加 `--extract`。默认原始数据目录为 `data/raw/Mind2Web/`。只下载 pilot 分片不足以重建本版本。详细目录、分步命令及可选离线 HTML 导出见 [DATA_SETUP.md](docs/DATA_SETUP.md)。

| 实验数据 | 数量 | 用途 |
| --- | --- | --- |
| Single-event | 100 cases | 主实验、4 种输入消融、无事件 baseline |
| Counterfactual | 70 cases | Goal：10 pairs；State：10 pairs；Semantic：10 triplets |
| Multi-event | 20 cases / 70 events | 连续事件判断、调度及 Case D |
| Final-intent baseline | 从单事件中派生 10 cases | 直接给出最终修订目标的对照 |

单事件一级标签为 30 `IGNORE`、30 `DEFER`、40 `INTERRUPT`；后者包含 20 `HANDLE`、10 `REPLAN`、10 `TERMINATE`。难度比例为 easy 30 / medium 50 / hard 20。这 100 个案例来自 54 个来源任务和 15 个来源 family，并非 100 个独立原任务。

`data/author_candidates/` 用于公开查看作者定义，**不能直接作为 runner 的 `--data-dir`**。完整来源信息在本地 `data/online_v2/` 恢复；请保持该目录的 Git ignore。

## 4. 配置模型和 API

```bash
cp models.example.json models.gateway.json
```

编辑 `models.gateway.json` 中每个模型的：

- `endpoint`：实际完整的 Chat Completions URL，例如 `https://YOUR_HOST/v1/chat/completions`，路径以服务方文档为准。
- `model`：服务实际支持的模型 ID，替换 `REPLACE_WITH_EXACT_MODEL_ID`。
- `api_key_env`：该模型密钥所在的环境变量名称。
- `enabled` 和 API 参数：按服务支持情况设置。某些服务需要 `max_completion_tokens`，另一些需要 `max_tokens`，不要同时发送不兼容参数。

模型配置名 `gateway_ds / gateway_glm / gateway_kimi / gateway_gpt` 只是本项目使用的别名，不指定模型版本；网关配置及密钥不会随源码公开。更多说明见 [config/README.md](config/README.md)。

macOS 默认 zsh 中，可隐藏输入 DeepSeek 密钥：

```zsh
read -rs "GATEWAY_DS_API_KEY?DeepSeek API Key: "; echo
export GATEWAY_DS_API_KEY
```

运行多个模型时，对应设置 `GATEWAY_GLM_API_KEY`、`GATEWAY_KIMI_API_KEY`、`GATEWAY_GPT_API_KEY`。Bash 可用 `read -r -s -p 'DeepSeek API Key: ' GATEWAY_DS_API_KEY; printf '\n'; export GATEWAY_DS_API_KEY`。

`.env.example` 仅展示变量名；runner **不自动加载 `.env`**，需要在运行进程的环境中设置变量。不要将密钥写进 Python、README 或公开 JSON。`models.gateway.json` 和 `.env` 已被 Git ignore。

## 5. 先检查，再运行

本地回归测试不调用模型 API：

```bash
python -m pytest -q
```

若尚未准备官方数据，依赖本地完整数据的测试会明确显示为 skipped；代码、浏览器 fixture、公开候选和来源恢复逻辑测试仍会执行。准备数据后可执行全部数据测试。

检查全部作业注册，**不会调用 API**：

```bash
python scripts/run_online_v2.py run \
  --config models.gateway.json --only gateway_ds \
  --run-root runs/dry_run \
  --experiments all --repeats 1 --workers 2 --headless --dry-run
```

检查模型连通性，**会发起少量 API 请求**：

```bash
python scripts/probe_gateway.py \
  --config models.gateway.json --only gateway_ds --timeout 60
```

先运行包含不同决策的少量案例：

```bash
python -u scripts/run_online_v2.py run \
  --config models.gateway.json --only gateway_ds \
  --run-root runs/deepseek_pilot \
  --experiments all --repeats 1 --workers 2 --headless \
  --case-ids ON2_S_026,ON2_S_027,ON2_S_028,ON2_S_029,ON2_S_034 \
  --cf-group-ids G07,T07,P07 \
  --multi-case-ids ON2_M_06,ON2_M_13 \
  --prep-model gateway_ds --judge-model gateway_ds
```

这个 pilot 采用同一模型准备检查点与判断结果，仅供工程检查。论文实验需要固定 preparation 和 Judge 配置、校准 Judge，并独立人工复核；自评结果不能直接当作无偏的模型能力排名。网页可达也不代表任务检查点可复现。

## 6. 全部实验运行命令

只运行 DeepSeek，所有实验、每项 1 repeat：

```bash
python -u scripts/run_online_v2.py run \
  --config models.gateway.json --only gateway_ds \
  --run-root runs/deepseek_full \
  --experiments all --repeats 1 --workers 4 --headless \
  --prep-model gateway_ds --judge-model gateway_ds
```

四个模型在同一个 run 中运行，配置好四个 API 环境变量后：

```bash
python -u scripts/run_online_v2.py run \
  --config models.gateway.json \
  --only gateway_ds,gateway_glm,gateway_kimi,gateway_gpt \
  --run-root runs/four_models_full \
  --experiments all --repeats 1 --workers 4 --headless \
  --prep-model gateway_ds --judge-model gateway_gpt
```

`--workers 4` 表示同时运行最多 4 个 episode，不保证每个模型固定占一个 worker。跨模型运行采用共同准备检查点，固定使用同一个 Judge；上述四模型命令选择 GPT 作为 Judge，仍需检查它对不同 actor 的评估偏差，也可改成单独配置的 Judge。

`all` 包含 `main,ablation,counterfactual,multi,baseline,final_intent`。100 条全规模、1 repeat 下，每个模型注册 **700 个作业**。主实验和消融的 Full Trajectory 条件共享同一 episode，避免把重复采样当成不同条件；实际 actor episode 数最多 600，API 请求数通常更多。四模型注册 2,800 个作业。

续跑同一个 DeepSeek run，保持原数据、代码、配置与所有注册参数一致：

```bash
python -u scripts/run_online_v2.py run \
  --config models.gateway.json --only gateway_ds \
  --run-root runs/deepseek_full \
  --experiments all --repeats 1 --workers 4 --headless \
  --prep-model gateway_ds --judge-model gateway_ds --resume
```

新数据版本或新代码应使用新目录。只有确定需要重试基础设施失败时才加 `--retry-infra`；它不会选择性重跑已计分的 Agent 错误。

准备阶段现在逐例打印 START、API 调用和回放复核状态，并每 15 秒输出心跳。`preparation_state.json` 保存当前案例和完成计数；回放退出、浏览器操作及关闭都有截止时间，超时保留为环境未确定。清理旧请求后再次严格核对检查点。

本次修订更改了代码哈希及默认 repeats，旧 v2.0.4 三次运行不能直接 resume；使用新目录。需要三次时显式设置 `--repeats 3`。

## 7. 进度与结果

另开终端、进入项目目录，查看实时状态：

```bash
python scripts/run_online_v2.py status --run-root runs/deepseek_full
```

完成后重新汇总及计算来源 family cluster bootstrap：

```bash
python scripts/run_online_v2.py evaluate --run-root runs/deepseek_full
python scripts/analyze_online_v2.py --run-root runs/deepseek_full --resamples 1000
```

主要输出：

```text
runs/deepseek_full/
├── manifest.json               # 固定数据、代码、模型与预算
├── registered_data/            # 本次注册的案例
├── main/comparison.csv
├── ablation/comparison.csv
├── counterfactual/comparison.csv
├── counterfactual/goal_comparison.csv
├── counterfactual/state_comparison.csv
├── counterfactual/semantic_comparison.csv
├── multi/comparison.csv
├── baseline/comparison.csv
├── final_intent/comparison.csv
└── statistics/                 # analyze 命令生成的区间和差异
```

每个 episode 保留决策、执行记录、评估与基础设施状态；Case D 候选从多事件失败中导出。详细指标、输入条件和统计口径见 [实验协议](docs/PROTOCOL.md)。`None` / 空值代表不可确定的结果，不能替换为成功或简单删除后报告正式分数；同时查看 Coverage 和可配对样本范围。

## 8. 上传 GitHub

把 **ZIP 解压后的目录内容** 上传到仓库，根目录保留本 README；无需把 ZIP 本身作为仓库源码文件上传。不要上传本地生成的数据、运行环境或 API 配置。

若使用 Git，先在 GitHub 创建自己的空仓库，再在解压的项目根目录执行：

```bash
git init
git add .
git status --short
git commit -m "Add EventArena Mind2Web benchmark source"
git branch -M main
git remote add origin https://github.com/YOUR_USER/YOUR_REPO.git
git push -u origin main
```

将 URL 中的占位符改为自己的仓库。提交前查看 `git status`，确认没有密钥、`data/raw/`、`data/local/`、`data/online_v2/` 或 `runs/`。不要使用 `git add -f` 覆盖本仓库的忽略规则。

## 来源与许可

原始 Mind2Web 数据使用 CC BY 4.0；官方 Mind2Web 源码的 MIT 许可与数据许可不同。本项目新增代码和作者标注的许可尚未由项目所有者指定，不能自动沿用 upstream MIT。正式引用和第三方声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

本仓库的网页任务采用公共网站研究适配，明确记录原任务来源和改动。Gold 是评估参考，不提供给被测 actor；Full Trajectory 来自实际浏览器执行和观察，而不是人为写好的操作日志。
