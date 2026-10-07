# EventArena Online v2 数据卡

## 版本与证据边界
本目录是 online_v2.0.4-author-candidate。已完成结构构建和本地一致性验证；未声称完成 100 条网站执行验证、双人标注或官方 Mind2Web 在线复现。旧版本保持不变。

## v2.0.4 修订
- 共用准备先于被测 actor 执行。资格清单由实际检查点准备与重放审计决定，不读取被测模型的预测、正确率或任务成功。
- 真实准备过程录制 HAR 网络响应和浏览器操作。重放到检查点后解除网络重放，后续访问恢复 live。HAR 缺失、hash 变化、未录制请求或任务正文/控件值不一致仍为 unknown；不得静默回退 live 后放宽审计。
- 检查点按预登记的 main/article/[role=main] 完整正文及控件值比较；只排除全站导航、页脚及明确 cookie 面板。价格、天气、要求和用户输入值保持严格相等。缺失/不稳定正文不使用 homepage/body 兜底。
- Full Trajectory 使用一个跨组件共享引用池，所有动作、观察、边界历史和当前页都能精确解码；不截历史、不编进度或剩余步数。
- 多事件公开编号与数组顺序按固定 seed 独立打乱，并同步改写所有 delivery、verifier、setup 与 schedule 引用。编号/位置/source 的标签分布由 validator 输出，不能形成完美类别捷径。
- 相关 B 通知共用中性文本，不以通知模板区分 HANDLE/DEFER/IGNORE；原目标的接受条件和实际 verified prior results 才决定标签。INPUT_IDENTIFIABILITY.json 报告 event-only 与 goal 的相同输入多标签情况及注册样本的经验上界；缺失必要上下文的错误不能全部解释为推理能力不足。
- Goal counterfactual 的明确 request reference 写在 goal 字符串中；incoming reference 固定，仅改变目标。修订保留原市场趋势信息，六个月历史参考不能充当一年新增区间的证据，SFO 泛型车型信息不能充当机场实时库存。
- A 与事件 B 的答案分别检查；初始接受条件通过 B 的实际完成绑定验证，不强迫将 B 的整份答案复制进 A。无事件 baseline 的组合目标仍需完成其全部要求。
- 反事实汇总保留所有 repeat；多事件计划只按各阶段已见事件评分，完整执行仍按全部注册事件检查。未见/未应用修订不提前改变 A 的内容验证目标。
- v2.0.3 原结果不覆盖。旧轨迹只可另存修正评分；改数据/输入/初始条件后的 v2.0.4 必须使用新目录重新运行。

## 构成
- 单事件 100 条：IGNORE 30、DEFER 30、INTERRUPT 40（HANDLE 20、REPLAN 10、TERMINATE 10）。easy 30、medium 50、hard 20。
- Counterfactual 70 条：10 Goal pairs、10 State pairs、10 Semantic triples。State pairs 用真实浏览器 warmup B 的已验证结果与未执行状态比较，不能伪造 completed 历史。
- Multi-event 20 条、70 个事件：10 个同时到达的三事件案例，10 个在 B 完成后追加事件的四事件案例。
- 100 条是 20 个 author profiles 的情境变体。共享任一原 A/B/C task ID 的 profile 合并为同一 task_family_id / source_family_id（见 split_manifest.json）；该 pilot 不是 100 个独立网站任务，相关变体不能按独立样本扩大显著性。

## 原任务与新增内容
每个来源家族包含真实 Mind2Web A/B/C task ID、原始目标与公开研究适配。B 是来源数据中的另一项实际任务，处理 B 必须浏览相关网页并保存有证据的答案；不以点击 synthetic receipt、draft 或任意 privacy/help 页面代替。
事件中的 source_task 明确标记实际来源，可能为 B 或 C；授权修订和撤回则明确标记为作者设定。范围不相关的 IGNORE 事件也保留真实 C 查询入口，不根据隐藏 Gold 禁止操作，因此错误处理它会留下可测的额外网页行为。重复通知的历史完成结果必须实际执行并验证，不能伪造。
来源网页没有可恢复的实时站点状态。起始 URL 由官方域名映射，购买、预约、申请、投票、消息等改变外部状态的操作改成公开查询；因此不是原任务完全等价复现。
A/B 联系与处理顺序是作者新增且明示的工作流，不能写成 Mind2Web 官方关系。HANDLE 的 checklist 在初始请求中注册；事件只启动尚未完成的原有接受条件，不偷偷修改目标。相关性强弱不同，必须通过独立人工审核。

## 注入与模型输入
事件在相关非主页的真实内容/控件出现、且已有成功的 actor 导航或交互后触发；注册首页入口本身不算进展。运行时生成 Full Trajectory；没有人为填写步骤历史、完成百分比、剩余步数或估计成本。触发条件、Gold、评估规则、作者理由不输入 actor。
Event-only、Event+Goal、Event+Goal+State、Full Trajectory 共享实际 checkpoint，经浏览器重放与语义审计后比较事件注入时的上下文和既往记忆，再继续自主网页执行。后续各组均可见操作所需的当前授权指令与新页面，既往历史仍按各组投影保留。主实验与 Full Trajectory 消融复用同一实际 episode；消融及反事实报告与主实验一致的完整指标。Counterfactual 初始页面、事件与指定变量必须有运行时不变量审计，失败时保留 unknown，不能强行填分。

## 权限与标签
一级 IGNORE / DEFER / INTERRUPT；二级仅在 INTERRUPT 时 HANDLE / REPLAN / TERMINATE。Gold 全部是作者候选。事件有明确请求人和任务范围；重复任务只有真实已验证完成状态才能 IGNORE，单纯自述不够。独立追加请求在原任务后执行，既有依赖先执行，修订改变有效目标，取消停止原任务。

## 难度、划分与审核
难度是作者的结构性分层（主体/范围辨析、来源比较、条件保留、完成记忆、分批到达），不是用模型错误率反推标签，更不是保证降低准确率。source-ID-overlap 合并后的 family-grouped development/heldout_candidate 划分跨全部实验保持一致，并不是原始 Mind2Web 官方 split。live_readiness.json 区分入口可用、checkpoint 验证与 A/B 任务验证：前者不能替代后两者。
annotation_review.csv/jsonl 留空两名审核者与裁决记录；不得把候选标签改写为人工审核结论。正式发布前需验证 A/B 来源、具体触发 checkpoint、网站可访问性、Gold、语义等价与独立 outcome checks；冻结排除清单后再横向比较模型。

## 结果与缺失
任务成功依赖实际观察证据及独立检查。Judge 未配置、访问失败、setup B 未验证、触发未到达都保留具体状态。报告评估覆盖率及缺失原因；不要仅凭模型 completion claim 计为成功，也不能保证得到指定或高分实验结果。

## 许可与发布
保留源任务 ID、适配目标与作者事件。遵循 Mind2Web 原始许可证和使用限制，不公开重新分发解压后的受限制测试 HTML；运行日志发布前检查个人信息和站点内容授权。原始 1750 条记录保持本地不改动。
