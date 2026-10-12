# EventPilot v6：冻结协议

## 任务与两层标签

测评真实网页 Agent 遇到事件后，是否识别适用范围、延迟代价、完成状态和授权变化，并实际执行正确行为。一级 IGNORE / DEFER / INTERRUPT；二级 HANDLE（处理附加任务后恢复）、REPLAN（采用授权修订，满足保留要求）、TERMINATE（接受撤回，停止已取消任务）。模型报告不会强迫 Actor 走 Gold 分支。

A 保留原官方目标；查询事件 B、相似历史查询 C 引用另一条官方任务。通知、审稿流程、授权、依赖是作者新增前提，不是官方数据自带的事件。任务有日期、对象或筛选时不能为了成功偷偷换成更简单查询。

## 难度与有效控制

全部 380 个 case 使用 120 个原任务，Main 200 使用 100 个。配对、派生、Baseline 和共享 Full 不能充当独立新任务。采用 source A website cluster bootstrap，同时报告 unique tasks、websites 和有效样本。

- Goal：相同事件、规则、实际页面；只改变原目标。用几个对象线索限定通知范围，判断当前任务是否属于该范围。
- State：相同目标、事件、A 页面；实际先完成同网站相似查询或所需精确查询。结果须经过独立验证；相似查询的证书不能证明另一个查询已完成。
- REPLAN：保留对象、筛选、原事实要求，同时修改交付物并增加逐项证据要求。原目标和新条件由一次联合 Judge 验证，避免相互矛盾的评价。
- Semantic：只改变通知措辞，保持任务、规则、执行定义、标签及 checkpoint 相同。等价性须人工核验。
- Multi-event：醒目的网页营销不产生用户授权，交付依赖优先于通知醒目程度，deferred obligation 需保留。stream 后续事件在第一次通知后的实际操作边界到达；不得泄漏未来事件。

easy / medium / hard 是构建分层，不是模型错误率分层。如果模型 Full / State 仍为高分，应如实报告。Event-only 的不可识别对用于信息消融，不能以不可推断的 Gold 证明模型不智能。

development / heldout_candidate 按网站划分，B / C 不跨分区。历史 Apple / Justice 开发样本归入 development。Counterfactual 是同源诊断，不声称与 Main 来源完全独立；若调参只在 development，并记录。

## 实验与指标

Main / Ablation：Acc、固定三类 MacroF1、FIR、MIR、条件 FollowupAcc / FollowupF1、JointFUAcc、I/D/H/R/T 分支成功率、Micro/Macro ESR、EHS、TaskCompletion、PTAR、Coverage。每个选定模型均做全部消融；Main 与 Full 共用 episode。

输入消融改变通知决策边界的预事件信息。后续执行给各组相同的当前操作工具与任务指令，之后观察均来自实际网页。这是决策上下文与预事件记忆消融，不能说 Event-only Actor 始终不知道任务。Full 日志可由 READ_CONTEXT 完整检索，预算固定公开。

Counterfactual：Overall Acc 按 140 个 case；CFA / SFA 各按 20 对同对全部正确；Semantic robust accuracy 按 20 个三元组同组三例全部正确。三类各自输出完整决策和执行指标。实际输入 invariant audit 失败时正式成绩不可判定，保留原始轨迹诊断。

Multi-event：EventF1、CompleteCaseAcc、PriorityAcc、CriticalEventRecall、FIR、ScheduleExactMatch、ActualPriorityAcc、ActualScheduleExactMatch、multi_ESR、Coverage。计划和实际动作独立评价；阶段 schedule 是当时可见范围的完整时序计划，已完成节点保留位置，不包含未来事件。batch / stream 分别报告。

Baseline / Final-intent：TaskCompletion、MicroESR、Coverage。Case D 使用实际 Multi-event 的通知、报告、动作、结果；可以展示成功或失败，但必须有真实证据。

## 未知值、区间与评审

所有注册案例保留分母。网络、权限、不可访问网页、不可判定 reset / Judge 保持 unknown，不作为模型错误；covered subset 的成绩不得写成全数据集成绩。报告 known / unknown、Coverage 和上下界。

网站聚类 bootstrap 为 2000 次、seed 20261012，仅完整可判定统计量给全注册集 CI。一次运行不能支持模型随机性稳定或重复实验方差结论。固定三个标签的 Macro-F1 在某 CF 类型缺少类别时有结构上限；提供 supported F1 并披露，不能以此制造低分。

固定 Judge 跨模型一致，但 Judge 本身是被测模型时存在 self-preference 风险，必须披露。需独立人工抽样核验，并记录不一致与裁决；不能自动填写 human_reviewed=true。
Judge 的证据输入也采用完整可检索接口，inline 预算 90000 字符、每次最多 12 次 exact read、每次最多 16000 字符；原始来源不裁剪。预算不足保持 unknown，不能猜测成功，完整请求和检索记录保留。

当前作者构建状态是 author_candidate_not_validated_execution，数据冻结、共享 preparation、实际模型结果和人工审查分别登记。运行前核验适用范围、依赖、标签、语义等价；运行后按标签、二级类型、难度、网站、结果分层抽取 Judge 证据并双人核验。修订标签应另建版本，对所有模型统一执行，不能只修改个别模型错例。

公开 source revision、checksum、recipe seed、案例哈希、代码指纹、精确 model ID、采样和操作预算、固定角色、网页访问时间。受限原数据需自行申请；不上传实际 Key、cookies、私人 HAR 或机密运行证据。自动运行不能保证论文接收或某个指定准确率。

参考：[Online-Mind2Web 官方项目](https://github.com/OSU-NLP-Group/Online-Mind2Web)。在线任务有效性随网站变化，本扩展协议不与官方排行榜分数直接等同。
