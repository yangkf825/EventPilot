# Online v2.0.5 运行代码 / v2.0.4 数据协议

此文档描述源码包中的现行实现，不将候选数据或工程测试称为已经通过论文级验证的 benchmark。

## 决策与行为

| 层级 | 类别 | 含义 |
| --- | --- | --- |
| 一级 | IGNORE | 不执行事件对应的新任务，继续当前任务 |
| 一级 | DEFER | 记录待处理事件，先完成当前任务再处理 |
| 一级 | INTERRUPT | 暂停当前任务，立即对事件采取后续行动 |
| 二级 | HANDLE | 先完成事件任务，再恢复原任务 |
| 二级 | REPLAN | 接受有授权的目标修订，按更新后的目标继续 |
| 二级 | TERMINATE | 接受有授权的取消，停止被取消的原任务 |

二级标签只针对一级 Gold 为 INTERRUPT 的案例。FollowupAcc / FollowupF1 只在一级 INTERRUPT 判断正确且一级预测有效的条件集合计算，同时报告分母；该集合中的二级预测缺失或非法计为错误。JointFUAcc 评估所有应 INTERRUPT 案例的联合判断，避免仅靠条件指标掩盖漏报。

Gold、私有评估标签和事后检查结果不会作为 actor 输入。任务本来公开的验收要求和事件约束属于模型可见信息。事件执行任务的内容在不同处理时机之间保持一致；运行逻辑不依据 Gold 选择 Agent 的路径。

## 输入与实验

| 实验 | 输入条件 | 数据及对照 |
| --- | --- | --- |
| Main | trajectory | 多个模型比较，单事件 |
| Ablation | event-only / goal / state / trajectory | 每个被选模型都运行 4 个条件 |
| Counterfactual | state | 只改变 goal、state 或 event 的语义表达，完整组审计 |
| Multi-event | trajectory | 分阶段到达的事件、可见依赖和调度约束 |
| Baseline | trajectory | 无事件的原任务，保留原本已有的验收依赖 |
| Final-intent | trajectory | 一开始就给出最后修订目标 |

Event-only 只包含事件及实验提供的公共规则；goal 增加当前任务目标；state 再增加当前网页与任务状态；trajectory 再增加可用完整浏览器历史，包括所有记录的操作和观察。不同条件由代码按可见信息组装输入，公共决策规则保持一致；不是继续沿用早期 7 条静态实验的人工进度描述。

主实验与消融 trajectory 条件指向同一次 episode，其结果应一致。各模型及输入条件从冻结的共同检查点开始；审计并记录真实可见状态。浏览器网络记录仅用于复现已经观察到的准备前缀，后续操作继续使用网页交互；不把静态 Mind2Web 快照冒充任意分支模拟器。

完整历史采用无损共享引用编码，使用有记录的状态和操作，不填造演示。上下文及操作预算写入 run manifest，不能根据某模型表现临时更改。

## 指标和范围

| 表格 | 指标 |
| --- | --- |
| Main / Ablation / Counterfactual 通用 | Acc, MacroF1, FIR, MIR, FollowupAcc, FollowupF1, JointFUAcc |
| 上述实验的执行结果 | I_SR, D_SR, H_SR, R_SR, T_SR, MacroESR, MicroESR, EHS, TaskCompletion, Coverage |
| Counterfactual 特有 | OverallAcc, CFA, SFA, SemanticRobustAccuracy |
| Multi-event | EventF1, CompleteCaseAcc, PriorityAcc, CriticalEventRecall, FIR, ScheduleExactMatch, ActualPriorityAcc, ActualScheduleExactMatch, multi_ESR, Coverage |
| 行为诊断及各类分母 | EDR、延期与恢复行为、取消后的行为违规率 PTAR 等，保存在完整 metrics JSON |
| Baseline / Final-intent | TaskCompletion, MicroESR, Coverage |

Acc 按案例与 repeat 计算；MacroF1 为一级三分类的 macro-F1。FIR 为不应中断却被预测为 INTERRUPT 的比例，MIR 为应中断却未被预测为 INTERRUPT 的比例；两者越低越好。I/D/H/R/T_SR 分别对应五种 Gold 行为的执行成功率，MacroESR 对行为类别取平均，MicroESR 对注册案例取平均。

CFA 和 SFA 分别要求同一 pair 的全部成员判断正确，SemanticRobustAccuracy 要求同一 triplet 全部正确；OverallAcc 按 case 计算。多次 repeat 全部保留，不用 case ID 去重覆盖；协议组审计失败的结果不能作为已验证反事实成功。

EventF1 对所有事件汇总一级三分类；CompleteCaseAcc 要求整个 multi-event case 全部判断正确。PriorityAcc / ScheduleExactMatch 评估模型报告的调度，使用当阶段已经可见的事件；ActualPriorityAcc / ActualScheduleExactMatch 另评估实际执行顺序。CriticalEventRecall 检查关键事件的中断识别。

PTAR（Post-Termination Action Rate）统计取消后仍执行被取消任务行为的 episode 比例，不把完成取消确认本身算成违规。它在完整诊断指标中存在，不一定作为每张 comparison.csv 的默认列。

## 不可确定结果与统计

记录 API、站点、检查点、回放和 Judge 状态，区别执行失败与不可确定结果。严格注册口径存在未确定结果时会保持相应正式指标为 unknown；另外输出可评估子集指标和 Coverage。比较模型时使用共同可评估的案例与 repeat 范围，同时公开覆盖率，不能只去掉困难失败样本。

来源 family 是统计聚类单位，置信区间按 family 重采样，包含其中全部条件和 repeats。多个事件变体不被当成独立原任务。只有 15 个单事件来源 family 的候选规模，不足以证明广泛跨站点泛化。

Event-only 不可观察 goal/state 反事实区分信息，当前单事件 cohort 的可识别上限分析为 70%，goal 条件为 90%；见 `data/author_candidates/INPUT_IDENTIFIABILITY.json`。该诊断不能用来把信息缺失导致的错误直接解释成模型推理能力弱。

## 正式实验前应冻结的事项

人工审核 Gold、事件现实性及控制变量；验证任务和来源适配；独立校准 Judge；冻结共享准备策略、上下文及操作预算；检查所有被纳入的 live checkpoints，报告不可复现比例。`--strict-review` 可要求数据先完成代码规定的人工审核流程。

本包没有执行付费全规模实验，也没有把全部候选标记为人审或在线成功。运行说明见根目录 README，数据来源及许可见 THIRD_PARTY_NOTICES。

## v2.0.5 运行修复

默认每项运行一次。HAR 回放退出先停止旧页面加载、结束残留请求，再移除拦截并重新审计实际状态。操作、回放清理及资源关闭具有独立截止时间；失败不转为模型错误或成功。准备阶段有逐例日志、15 秒心跳与专门状态文件，超时继续下一例。数据、Gold、输入组和指标定义保持原版本。
