# EventPilot v12 paper-method adaptations

## Sources and scope

The user supplied three papers. Their relevant methods motivate these implementations:

1. **JRDB-AVR**, arXiv:2609.35032v2, Appendix C: short factual CoT summaries and ReAct-style action/observation prompting.
2. **Reflexion: Language Agents with Verbal Reinforcement Learning** (Shinn et al., 2023): Actor, self-evaluator and verbal reflection memory. This release uses model self-evaluation, one of the feedback forms discussed by that paper.
3. **Plan-and-Solve Prompting: Improving Zero-Shot Chain-of-Thought Reasoning by Large Language Models** (Wang et al., ACL 2023): first plan and solve, then extract the final answer. We implement basic PS, not PS+.

This is an adaptation of reasoning workflows to recorded-page action/control prediction, not reproduction of the papers' original benchmarks or claimed gains. The exact English prompts are constants in `eventpilot/methods/paper.py`, and actual rendered prompts are retained in each episode's `calls` array.

## Methods

| CLI | Inference procedure per base request | What reaches the unchanged environment |
|---|---|---|
| direct | Original request, one model response | Original report/command |
| cot | Short factual summary (max 6 points), then final JSON extraction using the same permitted base input | Only final extracted JSON |
| react | Brief reason + one proposed command/report; real tool output arrives only after the command is committed | Unwrapped `action` object |
| reflexion | ReAct proposal; public-input self-evaluation; if rejected, bounded verbal reflection and another local proposal | First self-evaluator-passed candidate, otherwise last bounded trial |
| ps | Up to 6 plan items and provisional solution, then final extraction using the same permitted base input | Only final extracted JSON |

Every prefix, Decision and Actor request uses its chosen method. This is an end-to-end method intervention, not merely a new Decision label prompt. All methods use the same configured model, original tools, candidate retrieval, page states and external evaluation. The low-level engine has no automatic LGR plan compiler or repair routine.

## Reflexion adaptation and limits

This implementation uses **local action/report selection trials**. It does not reset and replay a full reference trajectory. With default 2 trials, each substantive request uses at most 5 model passes: proposal, evaluation, reflection, proposal, evaluation. If the first proposal passes self-evaluation, only 2 are needed. Actual public read commands execute immediately through the base environment; the resulting public tool evidence is available to the next proposal. No successful read, new observation, or backend outcome is synthesized by the method.

A failed self-evaluation does not advance the environment. Reflection memory persists only inside the current case and only within the same prefix/decision/actor role. Prefix or Actor can therefore not transfer omitted goals or history into Event-only Decision. Reflection memory has at most 3 entries; recent own action notes at most 4. Notes are fallible model hypotheses, never user orders or verified facts. Memory is fresh for each case; no cross-test-set learning or best-of-Gold trial choice occurs.

An evaluator `pass=true` is only a model judgment. The external benchmark can still mark that candidate wrong. At the trial cap the last proposal is committed whether or not the self-evaluator approves. Malformed final commands retain the base protocol's model-failure semantics. The method does not inspect private Gold, reference targets, future events, remaining-step counts, reward tables or scores. The method module has no corpus, dataset or evaluator import. It receives the current public request and returns one ordinary report/command to the original engine.

## Visibility and length

All auxiliary stages receive only the current base request and same-role method notes. The exact original public projection is preserved when it fits. If adding method instructions would exceed the 64,000-byte common limit, only public DOM/history previews are rebudgeted using the same role-scoped resource registry. Required tasks and delivered event texts are retained. Full originals remain accessible through the existing permitted `read_input` channel. Actor resources never enter a Decision registry. This is a byte bound, not a promise of a universal tokenizer/context-window size across providers.

The shared per-episode logical-request cap is 1,024, including prefix-cache reuse and every auxiliary stage. Base Actor command cap is 128. A request-cap termination is a known method failure with the registered denominator intact. Reflexion trials are fixed before inference, default 2 and restricted to 1–3; changing this value requires a new run root. No hidden-score-dependent early stopping is allowed.

## Fair comparison and reporting

Main uses Event + Goal + State (`state`) for every method. A previous v11 Main Full Trajectory score is not a matched Direct baseline for v12. Rerun Direct with the new information setting and identical model configuration. Main and ablation/state share a single episode when jointly registered. Non-ablation CF/Multi/Baseline retain their previous trajectory setting; no ablation includes trajectory.

The chosen methods intentionally use different numbers of model passes. Report accuracy/control/action metrics together with API call count, tokens, coverage and the fixed limits. This is equal-backbone/equal-public-information comparison, not equal-total-token inference. For stricter compute matching, preregister the same request/output budgets for every method and run a separate sensitivity analysis rather than silently tuning the winning method after inspecting results.

The launcher runs methods in a fixed order and jobs in a fixed seeded order. Cases run concurrently with one shared API semaphore per method. For repeated paper runs vary the registered repeat count before starting, retain all repetitions, and account for provider/model changes over time. A single run with temperature 0 does not establish statistical superiority or eliminate backend nondeterminism.

Engineering tests use synthetic stubs and private oracle fixtures solely to verify protocol and scoring preservation. Their scores are not model performance and must not be included in experimental tables.
