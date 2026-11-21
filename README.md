# EventPilot v12 — paper-method comparison

This release uses the same 460 approved English cases and frozen Mind2Web assets as v11. It changes experiment registration and adds four optional, train-free paper-method adaptations. It does not change Gold, case membership, candidate retrieval, scoring definitions or the underlying execution environment. Old v11 code and runs remain independent.

[v6 online release at its original commit](https://github.com/yangkf825/EventPilot/tree/09cd4be9e36d5e0cde829d55af5719c0c2768f02).

## What changes in v12

| Item | Setting |
|---|---|
| Main | 200 cases, **Event + Goal + State**, internal `state` |
| Main information ablation | Event-only / Event + Goal / Event + Goal + State |
| Separate Goal CF / State CF / Multi context ablations | Event-only / Event + Goal / Event + State / Event + Goal + State |
| Full Trajectory | **Removed from all ablations**; unchanged for non-ablation Counterfactual, Multi-event and native Baseline |
| Shared record | Main and ablation/state use one canonical episode when registered together |
| Methods | `direct`, `cot`, `react`, `reflexion`, `ps` |
| LGR | Not included or enabled in this release; historical implementation remains in v11 |

The information intervention applies to Decision. Actor still uses the common public execution history and tools. Each paper method transforms only its currently permitted request; Actor reasoning never leaks into reduced-information Decision prompts. Methods are independent of backbone selection.

## Install

Python **3.10+**, recommended 3.12. Clone or download this repository, then install the dependencies from the repository root. Model inference uses the API in your selected profile.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock.txt
```

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock.txt
```

The project includes 362 used Mind2Web tasks and 2,197 unchanged HTML snapshots in `corpus/snapshots.zip`. The normalized source index has 1,750 task entries; the metadata catalog has 560 entries. Unused metadata is not an experimental case. This is the required subset, not the entire original Mind2Web distribution. See [dataset construction](docs/DATASET.md) and [Mind2Web attribution](corpus/ATTRIBUTION.md).

Download [the v12.0.0 data archive and checksum](https://github.com/yangkf825/EventPilot/releases/tag/v12.0.0). Verify the archive, then unpack it from the repository root. The archive password is `mind2web`, following the upstream test-set distribution convention. This installs `data/`, the packed `corpus/` assets, and `review/`.

```bash
shasum -a 256 -c EventPilot-v12.0.0-data.zip.sha256
unzip -n EventPilot-v12.0.0-data.zip
```

On Linux, `sha256sum -c EventPilot-v12.0.0-data.zip.sha256` also verifies the download. On Windows, compare `Get-FileHash .\EventPilot-v12.0.0-data.zip -Algorithm SHA256` with the checksum file and extract with a ZIP tool that supports password-protected archives.

```bash
python scripts/setup_data.py --check-only
python scripts/build_dataset.py --validate-only
python -m unittest discover -s tests -v
```

Validation is local engineering evidence, not a model result. Do not use oracle fixture outputs as paper scores.

## Recommended: DeepSeek, five methods, Main only

Copy the model configuration from the repository root, then set the endpoint and exact model ID for `gateway_ds` in `models.local.json`. The `deepseek_official` profile can use the checked-in configuration.

```bash
cp -n models.json models.local.json
```

```zsh
export EP_RUN_ROOT="$PWD/runs/deepseek_main_five_methods_v12"
export EP_MODELS_CONFIG="$PWD/models.local.json"
export EP_DELIVERIES=sequential
export EP_WORKERS=8
export EP_API_CONCURRENCY=4
export EP_REPEATS=1
zsh scripts/start_methods.zsh gateway_ds main full
```

Enter the gateway Key at the hidden terminal prompt. Credentials are never written to config, manifests or logs. Default model IDs/endpoints are user-managed in `models.json`; availability is not inferred from their names. The above command registers **1,000 canonical episodes: 200 × 5 methods**, not 1,000 model calls. Prefix/action/evaluation/reflection calls are additional. Five methods run in order with independent records; cases within each method run concurrently. The API concurrency remains 4 overall, not 20. Method order is fixed and recorded.

Resume the same experiment (closed model failures are preserved; only infrastructure failures are retried):

```zsh
export EP_RUN_ROOT="$PWD/runs/deepseek_main_five_methods_v12"
export EP_MODELS_CONFIG="$PWD/models.local.json"
export EP_DELIVERIES=sequential
zsh scripts/start_methods.zsh gateway_ds main resume
```

Export without calling any API:

```zsh
export EP_RUN_ROOT="$PWD/runs/deepseek_main_five_methods_v12"
zsh scripts/start_methods.zsh gateway_ds main export
```

Use a new result root for a genuinely new experiment or a different code/data/model/method/budget/runtime configuration. Resume validates these identities; worker/API concurrency may change. A completed `direct` method is loaded from cache when resuming the five-method suite. Export also lists unstarted methods as pending.

## Run just one method or one backbone

Main only with Direct, using the gateway:

```zsh
export EP_METHODS=direct
export EP_RUN_ROOT="$PWD/runs/deepseek_main_direct_v12"
zsh scripts/start_methods.zsh gateway_ds main full
```

Replace `direct` with `cot`, `react`, `reflexion` or `ps` to run a method independently. Clear the selection with `unset EP_METHODS` before running all five. Each new experiment needs its own root. The equivalent single-method entry point is `EP_METHOD=cot zsh scripts/start.zsh gateway_ds full`; `EP_EXPERIMENTS` defaults to `main` for this entry point.

To use the official DeepSeek API instead, change the model alias and result root:

```zsh
export EP_METHODS=all
export EP_RUN_ROOT="$PWD/runs/deepseek_official_main_five_methods_v12"
EP_MODELS_CONFIG="$PWD/models.json" zsh scripts/start_methods.zsh deepseek_official main full
```

Other configured aliases: `gateway_glm`, `gateway_kimi`, `gateway_gpt`, `gateway_gpt61_sol`. Each prompts for its own environment Key and can run the same scopes and methods. Configure the exact model ID in your local configuration copy before starting a run. Keys are supplied through the configured `api_key_env` variable.

Portable Python command (Mac/Linux/Windows), with `GATEWAY_DS_API_KEY` set in your own terminal:

```bash
python -u scripts/run_methods.py --config models.local.json --only gateway_ds --methods all --experiments main --deliveries sequential --run-root runs/deepseek_main_five_methods_v12 --workers 8 --api-concurrency 4 --repeats 1
```

Append `--resume --retry-infra` to continue or use `--export-only` to export. In an interactive terminal this launcher also securely prompts if the Key is absent. `--methods cot` runs one method. `scripts/run.py --method cot` is the lower-level runner; its result root is a single method rather than a suite.

## Other experiments remain available

The standard complete set (Main, Main ablation, CF, Multi with three delivery modes, native baseline), Direct only:

```zsh
export EP_METHODS=direct
export EP_RUN_ROOT="$PWD/runs/deepseek_standard_full_direct_v12"
zsh scripts/start_methods.zsh gateway_ds full full
```

This registers 1,422 canonical episodes / 1,622 logical entries per method. Main/state aliases 200 entries without additional inference. Counterfactual includes Goal 50, State 50 and Semantic 60. Multi includes ORD / COND / RES / CAN, 25 each; CAN/Final intent stays inside Multi. `full` does not automatically add the independent context ablations.

Independent context ablations, Direct, sequential delivery:

```zsh
export EP_METHODS=direct
export EP_RUN_ROOT="$PWD/runs/deepseek_context_direct_v12"
zsh scripts/start_methods.zsh gateway_ds context full
```

This registers **800 episodes**, with Goal CF, State CF and Multi-event reported separately. To select another scope directly, use the Python launcher `--experiments` with any comma-separated selection of `main,ablation,counterfactual,multi,baseline,context_ablation_goal,context_ablation_state,context_ablation_multi`. Choose `--deliveries sequential` when a single delivery condition is intended; the standard full scope retains all three original delivery modes.

A local plan needs no credentials, calls no model and writes no run records:

```zsh
unset EP_METHODS
zsh scripts/start_methods.zsh gateway_ds main plan
```

Useful settings: `EP_WORKERS=8`, `EP_API_CONCURRENCY=4`, `EP_REPEATS=1`, `EP_MAX_COMMANDS=128`, `EP_CANDIDATE_LIMIT=50`, `EP_OUTPUT_TOKENS=8192`, `EP_MAX_MODEL_REQUESTS=1024`, `EP_REFLEXION_TRIALS=2`. Freeze these for a reported comparison. Each model request is capped at 64,000 UTF-8 bytes, with original permitted fields available through scoped public resources. Each episode has a common logical request limit, including auxiliary/cached requests; exceeding it is a known method failure, not an infrastructure unknown.

## Results and progress

```zsh
tail -n 10 -F "$EP_RUN_ROOT/logs/"*.log
```

Live output reports closed/registered episodes, active workers, calls and recent activity; completion does not mean success. `METHOD/progress.json` is the current method progress. `tables/method_completion.csv` reports all suite methods, including pending ones.

Per-method folders: `direct/`, `cot/`, `react/`, `reflexion/`, `ps/`. The outer `tables/` merges the same tables with a `method` column:

- `all_experiments.csv`, per-experiment comparisons and Goal/State/Semantic tables;
- `native_metrics.csv`, `paired_impact_summary.csv`, control/scheduling tables;
- `metrics_with_denominators.csv`, bootstrap intervals, `job_ledger.csv`;
- `efficiency_summary.csv`, `efficiency_by_case.csv`, `efficiency_by_stage.csv`;
- `suite_summary.json` and `method_completion.csv`.

Efficiency uses provider-reported tokens for the retained canonical attempt; prefix cache reuse is not billed again and shared Main/ablation records are not double-counted. Unknown token metadata remains empty. Summed request time includes queuing/cache waiting, so it is not wall-clock throughput. Provider-internal retries, interrupted or superseded attempts may incur additional billing not captured in retained-attempt totals. `api_calls` counts non-cached provider-adapter invocations, including failed ones, rather than internal HTTP retries. Raw calls preserve stage, trial, timestamps and returned usage for auditing.

Quota/auth/model-endpoint failures pause the suite; ordinary rate limits use bounded transport retries. A framework error stops the run and saves a traceback. Successful or known-wrong model predictions are not selectively rerun. Code, dataset, model configuration, Python/dependency versions and budgets are frozen in manifests; all predictions and attempts are retained.

## Scientific interpretation

These four methods are **EventPilot adaptations**, described precisely in [METHODS.md](docs/METHODS.md). Reflexion uses local proposal/self-evaluation/reflection trials with role-isolated memory; it does not use hidden Gold, reset complete task episodes, or select trials by private score. It must be labeled as this adaptation in a paper.

The environment is **offline staged event-conditioned teacher-forced action and control evaluation**. Recorded successors are supplied independently of predicted action correctness; a correct decision or reaching a recorded next page is not real website success. Event metrics and Mind2Web-style action metrics are unchanged. All original metrics remain exported, including weak results and unknown coverage; do not present a selected subset as the complete evaluation. Dataset approval does not establish inter-annotator agreement. Actual method improvements require the new model runs; this release supplies code and checks, not manufactured performance results.
