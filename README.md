# EventPilot v6

当前目录是 **Casewise 逐案例运行版**，协议为 `official_online_eventarena_v6_casewise`。通过共同起点检查的案例立即开始实验，失败案例保留原因；不再用全局 90% 门槛阻止整批实验。原任务 / 事件 / Gold 保持不变。默认 2 路独立准备、同网站 preparation 串行。详细命令和比例定义：[CASEWISE.md](docs/CASEWISE.md)。下面旧说明中的全局准备门槛只适用于存档 v6。

基于官方 [Online-Mind2Web](https://github.com/OSU-NLP-Group/Online-Mind2Web) 的在线网页事件扩展。保留原任务的官方 ID、目标、入口；新增事件、授权、依赖与 Gold 是作者设定。网页动作、状态、结果和 Full Trajectory 必须来自真实执行。

本版提供全规模数据构建与实验代码，**不附带尚未运行的成绩，不声称完成人工审核或保证论文接收**。旧版说明见 [v5 存档](docs/ARCHIVE_V5_README.md)，旧版成绩不能混入 v6。

## 数据与难度

| 实验 | 规模 | 用途 |
|---|---:|---|
| Main | 200 cases，100 个原任务、69 个网站 | 决策和真实后续执行 |
| Ablation | 同一批 200 × 4 inputs / model | Event-only、Event+Goal、Event+Goal+State、Full Trajectory |
| Counterfactual | 140 cases | 20 Goal pairs、20 State pairs、20 Semantic triplets |
| Multi-event | 40 cases，每例 3 events | 20 batch、20 stream |
| No-event Baseline | Main 派生 200 episodes / model | 相同 checkpoint，无事件 |
| Final-intent Baseline | 20 episodes / model | 修改后的完整任务从一开始给出 |

单事件：IGNORE 60、DEFER 60、INTERRUPT 80；INTERRUPT 下 HANDLE 40、REPLAN 20、TERMINATE 20。预登记难度为 easy 20、medium 80、hard 100。

难例包括对象线索限定的通知、同网站不同目标、实际完成的相似查询、部分修改保留旧要求、看似紧急的促销、隐含交付依赖、后续事件到达后重新排序。难度分层是构建属性，不是按模型错误率事后划分。不得为了低分而反改 Gold。

相同输入应做相同决策时，Event-only 的登记可识别上限为 80%，Goal-only 为 90%。这是缺失上下文造成的条件，**不是测得的准确率，也不是模型能力上限**。State / Full 的成绩必须实际运行获得。

## 安装、数据和 Key

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
cp models.example.json models.gateway.json
```

私有配置 `models.gateway.json` 填写 endpoint、精确 model ID 和 `api_key_env`；不填实际 Key。默认别名：`gateway_ds`、`gateway_glm`、`gateway_kimi`、`gateway_gpt`。
Key 更新时用 `zsh scripts/configure_api_keys.zsh --replace`，旧文件只作本机备份。H 盘交付版使用本机 SSD 的私有 Key 文件，配置脚本和运行脚本读取同一个位置。

自行申请官方 Hugging Face 访问权限并取得 `Online_Mind2Web.json`。固定 revision：`eacad896a84dc5b65e29b0b06e4699ab0544d701`；原文件 SHA256：`7dedf381531d423dc0fc48d21dc1d425655dd75f38b10211a880fa09102f257f`。公开包不重新分发受限任务文本。

```bash
python scripts/build_publication_dataset.py --input /path/to/Online_Mind2Web.json --output data/official_online/events_v6_final
python scripts/audit_publication_dataset.py
zsh scripts/configure_api_keys.zsh
```

生成三份 JSONL、来源索引、Gold 理由、manifest、统计和完整 `CASE_CATALOG.csv`。State 的 setup goal 是待真实执行的官方查询，不能当作已完成结果。已有 manifest 的目录不能覆盖；更换数据版本须新目录、新注册、新 run。

## 全规模运行

每个选定模型均完成全部消融。默认 repeat=1，两个并发 episode；固定 Preparation 为 `gateway_ds`，固定 Judge 为 `gateway_gpt`。即使只测 GLM / Kimi / DeepSeek，也需这两个固定角色的 Key。

```bash
# 四模型统一运行，共用 preparation
zsh scripts/start_publication.zsh gateway_ds,gateway_glm,gateway_kimi,gateway_gpt runs/v6_all

# 分别运行
zsh scripts/start_publication.zsh gateway_ds runs/v6_deepseek
zsh scripts/start_publication.zsh gateway_glm runs/v6_glm
zsh scripts/start_publication.zsh gateway_kimi runs/v6_kimi
zsh scripts/start_publication.zsh gateway_gpt runs/v6_gpt

# 指定精确 model ID，首次与续跑须相同
EP_MODEL_ID=deepseek-v4-pro zsh scripts/start_publication.zsh gateway_ds runs/v6_deepseek_pro
EP_MODEL_ID=glm-5.3 zsh scripts/start_publication.zsh gateway_glm runs/v6_glm_5_3
EP_MODEL_ID=k3-256k zsh scripts/start_publication.zsh gateway_kimi runs/v6_kimi_k3
EP_MODEL_ID=gpt-5.4 zsh scripts/start_publication.zsh gateway_gpt runs/v6_gpt_5_4
```

model ID 以你的服务实际支持为准。HTTP 404、授权失败、额度不足会暂停后续请求，保留不含 Key 的诊断。四模型共登记 5600 jobs；Main / Full 共享 800 个 episode，实际被测 episode 为 4800，另有共用 preparation。共享样本不能重复计为独立结果。
启动器会复制固定角色为 `publication_preparer` / `publication_judge`；`EP_MODEL_ID` 只改变被测 Actor，保留原固定角色的 model ID。

仅有 DeepSeek Key 时，可显式固定同一 DeepSeek 为 Judge；这是 self-judge 设置，需披露并独立人工核验，不可与固定 GPT Judge 的结果混并：

```bash
EP_JUDGE_MODEL=gateway_ds zsh scripts/start_publication.zsh gateway_ds runs/v6_ds_selfjudge
```

## 续跑与进度

保持原目录、模型列表、配置、数据、代码与固定角色。更换 Key 可以；改变 model ID、endpoint、评分算法应新开 run。

```bash
zsh scripts/start_publication.zsh gateway_ds,gateway_glm,gateway_kimi,gateway_gpt runs/v6_all resume
zsh scripts/start_publication.zsh gateway_ds,gateway_glm,gateway_kimi,gateway_gpt runs/v6_all resume retry-infra
tail -n 20 -F runs/v6_all/logs/publication.log
python scripts/run_official_online.py status --run-root runs/v6_all
```

`retry-infra` 补不可判定的基础设施失败，保留已可判定的模型失败。Actor 完成后仅 Judge 失败时只重新评价。存储使用唯一临时文件和本机待写 journal，失败后停止，恢复原 JSON 再续跑；不保证外置盘或操作系统永不故障。

共享 preparation 要求真实进展、已验证 setup 和独立 reset audit。覆盖低于 90% 会暂停，不把不可访问网站当作模型失败。gate 不读取被测模型成绩；所有候选失败和覆盖保留。更换任务池必须在被测采样前重新注册，不得按成绩挑选。

## 结果表

```bash
python scripts/export_publication.py --run-root runs/v6_all
```

正式完成后自动生成 `paper_tables/`：全部实验 CSV、反事实三类分表、`TABLES.md`、JSON、网站聚类 95% CI、按 split / 难度 / 到达方式分层结果。缺失或不可判定值为 NA；同时保留分母、unknown、Coverage、界限。Main / Ablation / Counterfactual 均有决策与执行指标，取消任务报告 PTAR。

Follow-up 仅在 Gold 与预测均为 INTERRUPT 的样本计算；条件分母和 JointFUAcc 一并报告。固定三类 Macro-F1 中缺失类按 0 处理，单一 CF 类型可能存在结构上限；另给 Gold-supported F1，不能把此上限解释为任务难度。

详细协议与人工审核要求：[PUBLICATION_V6.md](docs/PUBLICATION_V6.md)。自动执行不能替代 Gold 和 Judge 人工审核，真实模型分数须如实报告。
