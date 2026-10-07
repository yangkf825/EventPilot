# 数据集配置与本地重建

本仓库包含 EventArena v2.0.4 的算法代码、作者构建配方，以及用于查看结构的 `data/author_candidates/`。公开候选定义包含 100 个 single-event 案例、70 个 counterfactual 案例、20 个 multi-event 案例；它们不是完整的 runtime dataset，不直接交给实验 runner。

Mind2Web 原始网页 HTML、原始测试任务文本、Python 环境和 Chromium 二进制均不放在 GitHub ZIP 中。因此源码压缩包很小；这不代表数据下载失败，也不代表运行时不需要磁盘空间。

## 1. 安装环境

在解压后的项目根目录执行。推荐 Python 3.12；以下为 macOS/Linux 命令：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
```

下载脚本需要系统 `curl`，可先执行 `curl --version` 检查；macOS 通常已预装，Linux 可通过系统包管理器安装，现代 Windows 可使用系统 `curl.exe` 或另行安装。下文的手动解压命令还需要 `unzip`；一步数据准备由 Python 完成本地解压。

Linux 如果缺少浏览器系统依赖，可使用 `python -m playwright install --with-deps chromium`；系统依赖安装需要相应权限。环境目录请放在支持普通 POSIX 文件权限及可执行文件的本地文件系统上。模型配置及运行命令见根目录 `README.md`。

## 2. 一步配置数据

首次使用、尚无官方原始数据时：

```bash
python scripts/prepare_dataset.py --download --extract
```

该命令完成以下工作：

1. 根据 `data/source_metadata/mind2web_metadata.json` 下载官方 Mind2Web 数据，固定 revision 为 `17ece8eb89862368edc0cc806acee6fca5163474`，核验文件大小及官方 LFS SHA256。
2. 在本地解压官方 `test.zip`，其官方密码为 `mind2web`。
3. 流式读取原始任务，只提取构建配方需要的 54 个任务的元数据到 `data/local/source_task_index.jsonl`，校验原任务目标的 SHA256。不会额外输出全部任务的 HTML 快照。
4. 调用 `build_online_v2.py`，将具有完整源任务关联的 runtime dataset 写入 `data/online_v2/`，比对公开作者候选的投影并进行结构验证。

本流程不调用付费 LLM API，不需要下载或 clone 官方 Mind2Web 的模型训练源码，也不需要申请官方 Online-Mind2Web 任务文件的访问权限。

## 3. 已有原始数据时

如果官方原始文件已经在项目的 `data/raw/Mind2Web/`，但尚未解压测试集：

```bash
python scripts/prepare_dataset.py --extract
```

如果原始 JSON 已下载并解压到上述目录：

```bash
python scripts/prepare_dataset.py
```

可以分别执行下载和解压步骤：

```bash
python scripts/download_data.py --scope all --workers 2
unzip -P mind2web data/raw/Mind2Web/test.zip -d data/raw/Mind2Web
python scripts/prepare_dataset.py
```

`--scope pilot` 仅下载一个小训练分片，不能保证包含这 54 个选定任务，不能用于完整重建本版本。

## 4. 数据目录

| 目录或文件 | 内容 | 是否上传 GitHub |
| --- | --- | --- |
| `data/author_candidates/` | 去除原始任务文本后的作者事件、适配任务和候选标签，供检查设计 | 是 |
| `data/source_metadata/source_task_ids.json` | 54 个源任务 ID、split、website 和原始目标 SHA256 | 是 |
| `data/source_metadata/mind2web_metadata.json` | 官方固定 revision、下载文件大小与校验信息 | 是 |
| `config/online_v2_authoring.py` | 任务适配、事件及约束的构建配方 | 是 |
| `data/raw/Mind2Web/` | 官方下载文件及本地解压的 JSON | 否，Git ignored |
| `data/local/` | 本地提取或处理的源任务资料 | 否，Git ignored |
| `data/online_v2/` | 本地重建的完整 runtime dataset 与源任务元数据 | 否，Git ignored |
| `data/normalized/` | 可选的离线任务索引和 HTML 快照 | 否，Git ignored |
| `runs/`、`logs/`、缓存及环境目录 | 模型输出、网页轨迹、HAR、日志与本机环境 | 否，Git ignored |

官方原始数据应具有以下结构：

```text
data/raw/Mind2Web/
├── data/train/train_0.json ... train_10.json
├── test.zip
├── test_task/test_task_*.json
├── test_website/test_website_*.json
└── test_domain/test_domain_*.json
```

公开仓库不携带原始 `source_task_index.jsonl` 或 `original_goal`。本地重建后，这些用于来源核验的字段才会恢复到 runtime dataset。请保持本地数据目录的 Git ignore 规则；新建仓库时不要使用 `git add -f` 将这些目录强制加入。

## 5. 验证与重建

准备成功后，可以单独运行验证：

```bash
python scripts/validate_online_v2.py
```

修改作者构建配方后，请优先再次运行 `python scripts/prepare_dataset.py`。如需直接调用 builder，须提供本地已有的源任务元数据；仅有公开候选定义时不能跳过来源恢复步骤。

结构验证确认字段、来源关联和控制变量契约，不等于人工 Gold 审核，也不等于所有 live website 的执行成功验证。

## 6. 可选：导出全部离线 HTML

在线 v2 实验不需要这一步。如果另做离线快照研究，可在原始 JSON 就绪后执行：

```bash
python -m eventarena.cli normalize
```

该命令把所有找到的任务及其 HTML 快照写入 `data/normalized/`，会增加磁盘占用。快照是历史人类演示记录，不是支持任意分支的网页模拟器；不能把离线快照回放称为真实在线交互。

官方完整 corpus 为 2,350 个任务（train 1,009；测试 1,341）。此前本机曾转换出 1,750 条，是部分训练数据加完整测试数据的历史处理结果，不能视为完整官方 corpus。本版本固定使用 54 个源任务（train 11；测试 43），其对应的多个作者案例不是 100 个独立原任务。

## 7. 磁盘空间

固定 metadata 对应的下载文件总计 **6,499,137,203 bytes，约 6.499 GB**，包括训练分片、加密测试 ZIP 和 README，不包括候选生成模型及原始视频 dump。测试 ZIP 解压后为 **6,107,912,752 bytes，约 6.108 GB**。同时保留下载文件和解压测试文件约需 12.607 GB。

建议至少预留 **25 GB** 用于数据准备、Python/Chromium 和临时文件；大规模实验的 HAR、截图和轨迹另需持续增加空间。全部离线 HTML 的 normalize 输出也是额外占用。以上是磁盘存储，不是 RAM 要求。

## 8. 来源、许可及实验边界

数据来自 [Mind2Web 官方数据集](https://huggingface.co/datasets/osunlp/Mind2Web)，原始数据许可为 CC BY 4.0。官方仓库为避免测试污染，明确要求：**“Please DO NOT redistribute the unzipped data files online.”** 见 [官方 Dataset Access](https://github.com/OSU-NLP-Group/Mind2Web#dataset-access)。本仓库因此提供官方下载与本地恢复流程，而不重新公开原始测试记录及 HTML。

公开作者事件是本项目新增的适配任务、约束与突发事件，不是官方 Mind2Web 提供的事件标注。本项目在线任务经过公共网站和只读研究适配，不能视为原始 Mind2Web 任务的等价复现，也不是官方 Online-Mind2Web benchmark。运行时 Full Trajectory 来自实际浏览器观察和操作，不由作者填造逐步日志。

所有 Gold 仍为作者候选，`human_reviewed=false`。独立人工审核、Judge 校准以及各网站检查点和任务执行验证仍须完成；代码可运行不保证任何特定模型分数或论文结论。第三方许可与正式论文引用见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。
