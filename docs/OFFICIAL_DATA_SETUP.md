# Official Online-Mind2Web 数据配置

## 数据来源与版本

当前官方任务数据来自 `osunlp/Online-Mind2Web`，文件名 `Online_Mind2Web.json`。须在 [官方 Hugging Face 页面](https://huggingface.co/datasets/osunlp/Online-Mind2Web) 登录、完成访问申请并自行取得文件。公开仓库只存放作者事件配方，未打包 gated 任务原文。

本次构建所用 revision 为 `eacad896a84dc5b65e29b0b06e4699ab0544d701`，原文件 SHA256 为 `7dedf381531d423dc0fc48d21dc1d425655dd75f38b10211a880fa09102f257f`，包含 300 条官方任务、145 个入口 host。可用系统 `shasum -a 256 /path/to/Online_Mind2Web.json` 或 `sha256sum` 检查文件。若下载的是另一版本，不要填写本版本号；记录实际 revision 并重新验证配方引用的任务和网站。

## 导入和构建

在项目根目录、Python 环境激活后运行：

```bash
python scripts/official_online.py import \
  --input /path/to/Online_Mind2Web.json \
  --revision eacad896a84dc5b65e29b0b06e4699ab0544d701

python scripts/official_online.py build-events \
  --spec config/official_online/event_pilot_specs.json \
  --output data/official_online/events_pilot
```

导入生成：

- `data/official_online/raw/Online_Mind2Web.json`：完整原文件；
- `data/official_online/tasks.jsonl`：标准化任务，保留全部官方字段；
- `data/official_online/manifest.json`：数据 revision、原文件/标准化文件哈希与计数。

事件构建生成 `data/official_online/events_pilot/`，包含三类案例、source index、作者配方副本及注册 manifest。A 的 `confirmed_task` 和 B 的任务目标原样保留。未知 ID、非法 Gold、触发条件或约束会使构建失败。不自动覆盖已有注册目录；相同环境继续实验时直接使用已有数据，不必重新导入。

当前作者配方共 19 条：10 单事件、7 Counterfactual（1 Goal pair、1 State pair、1 Semantic triplet）、2 Multi-event。从单事件派生无事件 Baseline，从两条修订目标案例派生 Final-intent Baseline。全部实验单模型 1 repeat 注册 71 job。官方版本和保留的旧版 100/70/20 数据集是两个不同注册版本，不可拼接为同一研究结果。

## 真实网站与任务核验

```bash
python scripts/official_online.py check-sites \
  --output reports/official_sites --workers 3 --timeout 25

python scripts/official_online.py pairs \
  --availability reports/official_sites/availability.json \
  --output data/official_online/pair_candidates.json

python scripts/official_online.py probe-tasks \
  --plans config/official_online/task_path_plans.json \
  --output reports/official_path_checks
```

这些命令不调用收费模型。入口访问、配方诊断和无事件任务成功是不同结论：HTTP 200 不证明任务成功；同站点/词汇相关的 A/B 候选不证明依赖关系，也不自动获得 Gold。路径诊断使用显式作者路径，仅检查构建可行性，不可作为被测 Actor 的能力分数。

真实网站可能修改内容、页面路径或访问策略。配方中的页面触发词来自构建时的观察；失效应新增数据版本并重新审核，不在已注册运行中静默替换目标或降低覆盖率 gate。

## 存储与公开范围

Official 任务文件约百 KB；作者定义也很小。Python 环境、Chromium 缓存和运行录制占据主要空间。HAR/截图可能随执行次数增长，没有固定上限。Official 模式无需旧版数 GB Mind2Web HTML；仅复现旧版数据时才使用 `scripts/prepare_dataset.py --download --extract`。

`data/official_online/`、运行记录和本机配置均已 Git ignore。公开源码包含 `config/official_online/` 下的作者定义、来源 ID 和构建代码。不要解除 ignore 后批量上传原数据、身份会话或网页录制。
