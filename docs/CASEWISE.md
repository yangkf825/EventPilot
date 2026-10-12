# 逐案例运行与条件成功率

本版只改变准备与实验的调度、初始可用性门槛及结果报告，原始任务、事件、Gold、四种信息输入、Actor 步数和 Judge 规则保持原样。它使用独立协议 `official_online_eventarena_v6_casewise`，不会覆盖原实验目录。

## 运行方式

不再等待整个 380-case pool 达到 90% 可用率。每个案例或控制组先做共同 preparation 和 independent reset audit，通过后立即进入所有选定模型、消融和 Baseline。独立组可并行准备，默认 2 路；同网站的 preparation 串行。原有 checkpoint 缓存和 Main / Full 共享机制保留。

配对 / 三元组 / batch-stream 对比必须整组通过准备；不得按被测模型成绩选择其中的一侧。Model 在可用案例上做错、超出步数或提前结束，仍属于被测失败。准备阶段不可用的案例只保存原因，不向每个模型重复发请求。运行阶段网络或 Judge 无法判定的案例保留 unknown，不回头改成“初始不可用”以剔除。

本版单 repeat。Final-intent 没有事件 checkpoint，保留全登记样本及其原有指标；它不会被其他实验的 checkpoint 条件筛选。

## 三个分母

- `RunnableCoverage = 起点可验证的案例数 / 全部登记案例数`。
- `EligibleSuccessRate = 成功案例数 / 起点可验证的案例数`。只要该分母中存在运行结果 unknown，点估计为 NA，并给上下界。
- `DeterminateEligibleSuccessRate = 成功 / (成功 + 已判定失败)`。这是已判定子集的比例，必须同时报告 `OutcomeCoverage = 已判定数 / 可运行数`。

例如登记 100 条、起点可用 60 条、完成判定的 50 条中 35 成功、15 失败、另有 10 条 unknown：可运行覆盖率 60%；已判定子集成功率 70%；结果覆盖率 83.33%。全部 60 条可运行案例的成功率介于 58.33% 和 75%，尚不能给单一精确值。此例仅用于解释公式，不是实验结果。

任务成功率 `TaskCompletion` 与包含事件处理和顺序要求的 `MicroESR` 分开报告。取消任务不要求完成原任务，TaskCompletion 的适用分母单列。各网站结果按该网站的案例计算，同时给出 unique tasks / websites，不能把同网站多个事件案例算成多个独立网站。

多个模型必须使用同一初始可运行 cohort；运行结果的可判定子集可能不同，应报告配对覆盖和共同可判定子集，不能直接用各自最有利子集排名。整体结论只适用于此次经验证的可用任务池，需要披露网站、标签和难度分布。

## 使用原准备记录

`EP_PREPARATION_FROM` 指向原 v6 run，可以复用已关闭浏览器、导出网络 archive 且绑定相同 case / 固定角色 / 预算 / 协议的事实记录。通过的 reset 证书也可复用。不会导入被测 Actor 结果；该来源必须尚未开始被测 Actor。缺少一侧、没有完成 capture 或不可兼容的记录继续重新准备。

先在旧终端按 Ctrl+C，待旧进程退出后再启动新协议。保留旧 run，源 HAR 仍由新 run 引用，不能删除或挪动它们。续跑必须保留同一个 preparation source 和结果目录。

```zsh
read -rs "GATEWAY_DS_API_KEY?DeepSeek 网关 API Key: "; echo
export GATEWAY_DS_API_KEY

# 在新版项目目录执行
EP_CREDENTIAL_FILE=/dev/null \
EP_PREP_MODEL=gateway_ds EP_JUDGE_MODEL=gateway_ds \
EP_PREPARATION_FROM=/path/to/original/v6/run \
zsh scripts/start_publication.zsh gateway_ds runs/deepseek_casewise

# 中断后继续，准备来源和固定角色保持一致
EP_CREDENTIAL_FILE=/dev/null \
EP_PREP_MODEL=gateway_ds EP_JUDGE_MODEL=gateway_ds \
EP_PREPARATION_FROM=/path/to/original/v6/run \
zsh scripts/start_publication.zsh gateway_ds runs/deepseek_casewise resume
```

`resume retry-infra` 会重新尝试先前不可用的准备 / 运行记录，不重采样已判定的模型失败。记录 cohort 的扩展和访问时间；不可用网站重复访问不能保证恢复。API 额度或存储故障仍会安全暂停。

## 结果

`runnable_comparison.csv` 是共同可用案例的全部实验指标；`runnable_counterfactual_goal/state/semantic.csv` 是反事实分表；`runnable_website_results.csv` 是按网站分层；`availability_report.json` 是分母、原因和 cohort 分布。原全登记分母表仍保留，`paper_tables/` 同时存放条件表与全登记表，并使用条件 cohort 的网站聚类 CI。

Headless 网站可访问不等于任务成功。`RunnableCoverage` 同时反映固定 preparer 能否构造真实插入状态与 reset 能否验证，不只是 HTTP 200 比率。分类、实际执行、接口、网站、准备和评价问题仍分开记录。
