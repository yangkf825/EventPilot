# Official v5 执行与恢复协议

当前协议为 `official_online_eventarena_v5`，复用冻结的 Online v2.0.6 工具、指标和输入实验设计。源任务与作者事件的注册、模型配置、预算、运行时版本及代码哈希写入 manifest。改变条件后必须建立新 run。

## 核心行为

- 原任务 A 保持官方目标和入口；研究任务事件 B 保持其对应官方目标。作者通知、目标修订和撤回显式区别于官方任务。
- 事件需要实际 Actor 进展及语义页面条件才触发。State Counterfactual 中的“已完成 B”通过真实执行和独立证据核验得到。
- 四种输入消融改变决策信息：Event-only、Event+Goal、Event+Goal+State、Full Trajectory。完整轨迹记录每个真实状态和操作，通过有索引的读取工具访问完整历史，不编造进度或剩余步数。
- Main 与 Ablation Full Trajectory 使用同一注册执行结果。Ablation、Counterfactual 仍继续执行任务并报告执行指标，而不只评分三分类。
- 共同 preparation 和独立 reset 审计先于被测 Actor。正文容器不可用、无法恢复、任务证据不足保持 unknown；默认覆盖率门槛 90%。不以 Gold 或模型成绩筛选资格。
- 未知结果与模型已知失败分别报告。被忽略或撤回的任务不会通过伪造“成功”来提高任务完成率；网页任务结果须有 durable result 和可核验观测。

## v5 修复内容

1. 原生 HAR replay 保持完整捕获前缀，并处理未完成请求的取消/结束，避免等待悬挂；恢复审计保留正文与控件的实质比较。
2. 当前页和 Full Trajectory 使用索引读取；阅读范围绑定输入条件，保留完整历史，不向 Actor 泄漏 Gold、标签理由或官方 reference length。
3. 作者约束 `text` 与 evaluator 的 `requirement` 在私有评分投影中显式兼容；重复 ID、错误 origin、冲突字段仍拒绝，不改变事件标签或模型输入。
4. 评分异常前保存 Actor trace。只对符合验证条件的已完成轨迹进行 evaluation-only 恢复，不重新运行 Actor 或抽样刷新成绩。
5. 未完成及 unknown 基础设施错误可 `--resume --retry-infra`；有效已评分结果含已知模型失败保持原样。

## 续跑边界

将原 Python 命令的参数、配置文件和 `--run-root` 保持不变，仅附加 `--resume`，必要时再加 `--retry-infra`。接口限额恢复后仍使用原 API/模型配置；改 API 服务或模型版本需新 run。

需要保存结果目录，以及其中引用的 HAR、preparation 和旧 run 目录。仅复制状态 JSON 无法完整恢复浏览器记录。进程重启后锁会重新检查；同一结果目录不应同时启动多个 runner。

历史 v4 的事实前缀可通过 `--preparation-from-run runs/your_v4_run` 导入新 v5 run：重新执行全部 v5 reset 审计，不导入被测 Actor 成绩。后续续跑仍保持同一参数。不能将任意旧版 manifest 手工改 hash 后续跑。

v5 仅对一个已知约束 schema 错误提供受限迁移：验证精确旧代码 fingerprint、相同任务和参数、保存旧 manifest/错误记录，然后只为已有完成轨迹排队评分。不能把该机制当作通用跨版本迁移。

## 评估与研究状态

Main、Ablation 和 Counterfactual 使用同一组决策/执行指标；Counterfactual 额外报告 CFA、SFA、Semantic Robust Accuracy 和按 case 的 Overall Accuracy。Multi-event 单独报告事件三分类、整例正确、优先级、关键事件召回、误打断和 schedule exact match。完整定义由冻结的 `eventarena/online_v2_metrics.py` 和 [原协议说明](PROTOCOL.md) 给出。

当前 Gold 为作者候选，未独立人工审核，官方 pilot 也集中于少量来源任务。工程修复不等于发表条件已经满足；扩充来源任务、人工审核、固定 Judge 校准和多模型重复实验需要另行开展。本项目分数不等于官方 WebJudge leaderboard 分数。
