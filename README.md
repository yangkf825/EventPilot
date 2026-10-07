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

其他模型对应 `GATEWAY_GLM_API_KEY`、`GATEWAY_KIMI_API_KEY`、`GATEWAY_GPT_API_KEY`。下文 `run_one_model.py` 单模型入口会自动隐藏输入缺少的 Key，也可以事先设置环境变量。Bash 可用 `read -r -s -p 'DeepSeek API Key: ' GATEWAY_DS_API_KEY; printf '\n'; export GATEWAY_DS_API_KEY`。

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

以下命令在项目根目录、Python 环境已经激活后运行。每条只测试一个模型，包含全部实验，每项 **1 repeat**，默认 2 workers，并自动创建带模型名和时间的独立结果目录。

DeepSeek：

```bash
python -u scripts/run_one_model.py \
  --provider deepseek --model deepseek-v4-flash --mode full --repeats 1
```

GLM：

```bash
python -u scripts/run_one_model.py \
  --provider glm --model glm-5.3 --mode full --repeats 1
```

Kimi：

```bash
python -u scripts/run_one_model.py \
  --provider kimi --model k3-256k --mode full --repeats 1
```

GPT：

```bash
python -u scripts/run_one_model.py \
  --provider gpt --model gpt-5.4 --mode full --repeats 1
```

`--model` 是发送给 API 的确切模型 ID，不是配置别名。可以替换成服务实际提供的任意 ID，例如 `deepseek-v4-pro`、`glm-5`、`glm-5-turbo`、`glm-5.1`、`glm-5.2`、`gpt-5.4-mini`、`gpt-6-astra` 等；这些名称只是命令示例，是否可调用由你的服务决定。

| `--provider` | 继承的配置别名 | API Key 环境变量 |
| --- | --- | --- |
| `deepseek` | `gateway_ds` | `GATEWAY_DS_API_KEY` |
| `glm` | `gateway_glm` | `GATEWAY_GLM_API_KEY` |
| `kimi` | `gateway_kimi` | `GATEWAY_KIMI_API_KEY` |
| `gpt` | `gateway_gpt` | `GATEWAY_GPT_API_KEY` |

入口继承 `models.gateway.json` 中对应模型的 endpoint、采样参数、预算和 Key 环境变量，只替换模型 ID，并保存到 `logs/model_config.json`；原配置文件不变。Key 只保留在运行进程的环境里。模型切换时若服务要求不同参数，应先按其接口要求调整本机配置。

例如指定 DeepSeek Pro 和固定结果目录：

```bash
python -u scripts/run_one_model.py \
  --provider deepseek --model deepseek-v4-pro \
  --mode full --repeats 1 --run-root runs/deepseek_v4_pro_full_once
```

准备正式运行前，可增加 `--dry-run` 查看注册数量；此模式不探测、不调用 API、不询问 Key。真实运行先探测指定模型 ID，通过后才开始实验；`--mode pilot` 可先运行少量案例。

```bash
python scripts/run_one_model.py \
  --provider glm --model glm-5.3 --mode full --repeats 1 --dry-run
```

这些单模型命令用当前模型同时准备检查点和担任 Judge，各结果目录独立准备；适合逐模型运行检查。正式跨模型比较使用下面共同 run 的命令，以固定检查点和 Judge。

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

### 断点续跑与异常重试

支持 `--resume`。程序复用已完成的作业，日志显示 `CACHED`；未完成或中断的作业建立新 attempt，从任务入口或可用的事件检查点重做该 episode。已经完成的 preparation 资格检查默认也会复用。续跑精度是**作业 / episode 级**，正在打开的网页标签和尚未完成的 API 响应不会跨进程延续。

例如续跑上面固定目录的 DeepSeek Pro 实验：

```bash
python -u scripts/run_one_model.py \
  --provider deepseek --model deepseek-v4-pro \
  --mode full --repeats 1 --run-root runs/deepseek_v4_pro_full_once --resume
```

其他四类单模型运行的续跑命令如下。把 `--run-root` 的示例路径换成**启动时显示的原结果目录**；如果原先用了不同模型 ID，也要把 `--model` 换回原 ID：

DeepSeek：

```bash
python -u scripts/run_one_model.py \
  --provider deepseek --model deepseek-v4-flash --mode full --repeats 1 \
  --run-root 'runs/ORIGINAL_DEEPSEEK_RUN' --resume
```

GLM：

```bash
python -u scripts/run_one_model.py \
  --provider glm --model glm-5.3 --mode full --repeats 1 \
  --run-root 'runs/ORIGINAL_GLM_RUN' --resume
```

Kimi：

```bash
python -u scripts/run_one_model.py \
  --provider kimi --model k3-256k --mode full --repeats 1 \
  --run-root 'runs/ORIGINAL_KIMI_RUN' --resume
```

GPT：

```bash
python -u scripts/run_one_model.py \
  --provider gpt --model gpt-5.4 --mode full --repeats 1 \
  --run-root 'runs/ORIGINAL_GPT_RUN' --resume
```

保持原代码、数据、Python/依赖版本、模型配置及所有注册参数一致。首次 `--mode pilot`，续跑保持 pilot；首次用了 `--workers 1` 或 `--repeats 3`，续跑也必须填写相同值。`--resume` 必须显式提供原 `--run-root`，不重新生成带新时间戳的目录。先停止原运行进程，再续跑同一目录。

每次续跑前仍会做模型连通性探测；Key 环境变量缺失时会重新隐藏输入。`logs/run.log` 追加进度，旧结果与 attempt 保留。`logs/model_probe.json` 为最近一次探测报告，历次探测输出追加在 `logs/probe.log` 中。

已经完成并记为接口/环境不可确定的作业，普通 `--resume` 也会跳过。确需重新尝试这类问题，使用 **`--resume --retry-infra`**，例如：

```bash
python -u scripts/run_one_model.py \
  --provider deepseek --model deepseek-v4-pro --mode full --repeats 1 \
  --run-root runs/deepseek_v4_pro_full_once --resume --retry-infra
```

该选项重新检查准备资格，重试被 runner 标记为基础设施失败或结果未验证的作业，保存旧 attempt；不会选择性重跑已计分的 Agent 错误或修改 Gold。常规中断续跑只需 `--resume`。如果中断发生在 PREP 阶段并留下非 ready 缓存，普通续跑仍提示 `checkpoint_unavailable` 时，也使用 `--resume --retry-infra` 重新尝试准备与资格审计。

如果首次直接使用 `run_online_v2.py`，继续使用原来的完整 Python 命令，加上 `--resume`；不要切换入口，因为此类目录没有 `logs/model_config.json`。前面的四模型共同 run 可这样续跑：

```bash
python -u scripts/run_online_v2.py run \
  --config models.gateway.json \
  --only gateway_ds,gateway_glm,gateway_kimi,gateway_gpt \
  --run-root runs/four_models_full \
  --experiments all --repeats 1 --workers 4 --headless \
  --prep-model gateway_ds --judge-model gateway_gpt --resume
```

完整选项见 `python scripts/run_one_model.py --help`。新数据版本或新代码应使用新目录，不混入旧结果。

准备阶段现在逐例打印 START、API 调用和回放复核状态，并每 15 秒输出心跳。`preparation_state.json` 保存当前案例和完成计数；回放退出、浏览器操作及关闭都有截止时间，超时保留为环境未确定。清理旧请求后再次严格核对检查点。

本次修订更改了代码哈希及默认 repeats，旧 v2.0.4 三次运行不能直接 resume；使用新目录。需要三次时显式设置 `--repeats 3`。

## 7. 进度与结果

另开终端、进入项目目录，查看实时状态；将示例中的 `runs/deepseek_full` 替换为启动时显示的实际结果目录：

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
