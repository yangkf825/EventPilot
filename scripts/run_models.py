#!/usr/bin/env python3
"""EventArena decisions on recorded Mind2Web/browser trajectory checkpoints.

Examples:
  python3 scripts/run_models.py run --experiment main --only deepseek_flash
  python3 scripts/run_models.py run --experiment ablation --only deepseek_flash --dry-run
  python3 scripts/run_models.py compare --experiment main --run-root runs/pilot

No live API call is made by dry-run, evaluate, compare, or case-study.
"""

from __future__ import annotations

import argparse
import csv
import email.utils
import hashlib
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from metrics import DECISIONS, FOLLOW_UPS, _schedule_ok, score_counterfactual, score_multi, score_single


ROOT = Path(__file__).resolve().parents[1]
DATASETS = {
    "main": ROOT / "data/events" / "single_event.jsonl",
    "ablation": ROOT / "data/events" / "single_event.jsonl",
    "counterfactual": ROOT / "data/events" / "counterfactual.jsonl",
    "multi": ROOT / "data/events" / "multi_event.jsonl",
}
VIEWS = {"main": ("state",), "ablation": ("event-only", "goal", "state", "trajectory"),
         "counterfactual": ("state",), "multi": ("state",)}
SYSTEM_DECISION = (
    "You are a web agent deciding how to respond to an incoming event while working on an original task. "
    "Choose exactly one decision: IGNORE = the event needs no action; DEFER = finish the original task, "
    "then handle the event; INTERRUPT = respond to the event now before continuing the original task. "
    "Treat the supplied case facts as true. Do not infer hidden action counts. "
    "Reply with only IGNORE, DEFER, or INTERRUPT."
)
SYSTEM_FOLLOW_UP = (
    "The first-stage decision to respond to the event now is fixed. Choose exactly one follow-up: "
    "HANDLE = handle the event now, then resume the original goal with the same plan; "
    "REPLAN = handle the event now, then pursue the original goal with a revised plan; "
    "TERMINATE = handle the event now and permanently stop the original goal. "
    "All three handle the event first. Reply with only HANDLE, REPLAN, or TERMINATE."
)
SYSTEM_MULTI = (
    "You are a web agent responding to several events at one task checkpoint. For each event, choose "
    "IGNORE, DEFER, or INTERRUPT with the same definitions: no event action; finish the current task first; "
    "or respond to the event now. Also give a feasible execution schedule. "
    "Use event IDs and exactly one task node in the schedule. CURRENT_TASK means continue or finish the original "
    "goal; TERMINATE_TASK means abandon that goal after handling a cancellation event. Put INTERRUPT events "
    "before the task node and DEFER events after it. Omit IGNORE events from the schedule. "
    "Treat the supplied facts as true; do not invent hidden action counts. "
    'Return only JSON in the form {"decisions":{"E1":"INTERRUPT"},"schedule":["E1","CURRENT_TASK"]}. '
    "Include every listed event ID in decisions."
)
_LAST_REQUEST_AT: dict[str, float] = {}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        raise FileNotFoundError(path)
    rows = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_no}: expected JSON object")
            rows.append(value)
    return rows


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config.get("models"), list) or not config["models"]:
        raise ValueError("models.json must contain a nonempty models list")
    names = set()
    for model in config["models"]:
        name = model.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name) or name in names:
            raise ValueError("model names must be unique and filesystem-safe")
        names.add(name)
        if model.get("backend") == "baseline":
            if model.get("baseline") not in DECISIONS or model.get("follow_up_baseline", "HANDLE") not in FOLLOW_UPS:
                raise ValueError(f"{name}: invalid baseline labels")
        elif model.get("backend") == "chat-completions":
            if not all(isinstance(model.get(k), str) and model[k] for k in ("endpoint", "model", "api_key_env")):
                raise ValueError(f"{name}: endpoint, model, api_key_env required")
            endpoint = urlsplit(model["endpoint"])
            if endpoint.scheme not in ("http", "https") or not endpoint.hostname or endpoint.username or endpoint.password:
                raise ValueError(f"{name}: valid HTTP(S) endpoint without URL credentials required")
            if endpoint.scheme == "http" and model.get("allow_http") is not True:
                raise ValueError(f"{name}: HTTP endpoint requires explicit allow_http=true")
            if any(k in model for k in ("api_key", "token", "secret")):
                raise ValueError(f"{name}: credentials must be environment variables")
        else:
            raise ValueError(f"{name}: unsupported backend")
    return config


def select_models(config: dict, only: str | None, experiment: str) -> list[dict]:
    if only:
        matches = [model for model in config["models"] if model["name"] == only]
        if not matches:
            raise ValueError(f"unknown model {only}")
    elif experiment == "ablation":
        backbone = config.get("ablation_backbone")
        matches = [model for model in config["models"] if model["name"] == backbone]
        if not matches:
            raise ValueError("ablation requires --only MODEL or ablation_backbone in models.json")
    else:
        matches = [model for model in config["models"] if model.get("enabled", False)]
    if not matches:
        raise ValueError("no models selected")
    if experiment == "ablation" and len(matches) != 1:
        raise ValueError("ablation must use exactly one backbone")
    return matches


def validate_cases(experiment: str, cases: list[dict]) -> None:
    if not cases:
        raise ValueError(f"{experiment}: empty dataset")
    ids = [case.get("case_id") for case in cases]
    if any(not isinstance(cid, str) or not cid for cid in ids) or len(ids) != len(set(ids)):
        raise ValueError(f"{experiment}: case IDs missing or duplicated")
    for case in cases:
        cid = case["case_id"]
        if not isinstance(case.get("goal"), str) or not isinstance(case.get("state"), dict) or not isinstance(case.get("trajectory"), list):
            raise ValueError(f"{cid}: goal, state, trajectory required")
        if not all(isinstance(case["state"].get(key), str) for key in ("url", "summary")):
            raise ValueError(f"{cid}: state url/summary required")
        if not all(isinstance(step, dict) and all(k in step for k in ("step", "url", "observation", "action"))
                   for step in case["trajectory"]):
            raise ValueError(f"{cid}: each trajectory step needs step/url/observation/action")
        if not case["trajectory"] or case["trajectory"][-1]["action"] is not None or case["trajectory"][-1]["url"] != case["state"]["url"]:
            raise ValueError(f"{cid}: trajectory must end at current state with no action yet")
        if experiment == "multi":
            events = case.get("events")
            gold = case.get("gold", {})
            if not isinstance(events, list) or len(events) < 2:
                raise ValueError(f"{cid}: multi case needs at least two events")
            event_ids = [event.get("id") for event in events]
            if len(event_ids) != len(set(event_ids)) or any(not isinstance(eid, str) for eid in event_ids):
                raise ValueError(f"{cid}: invalid event IDs")
            if set(gold.get("decisions", {})) != set(event_ids):
                raise ValueError(f"{cid}: each event needs Gold decision")
            for event in events:
                if not all(isinstance(event.get(k), str) for k in ("source", "text")):
                    raise ValueError(f"{cid}: event source/text required")
                item = gold["decisions"][event["id"]]
                if item.get("decision") not in DECISIONS or (
                    item.get("follow_up") not in FOLLOW_UPS if item.get("decision") == "INTERRUPT" else item.get("follow_up") is not None
                ):
                    raise ValueError(f"{cid}: invalid multi Gold labels")
            nodes = gold.get("required_nodes")
            edges = gold.get("precedence")
            if not isinstance(nodes, list) or len(nodes) != len(set(nodes)) or not isinstance(edges, list):
                raise ValueError(f"{cid}: required_nodes/precedence invalid")
            if any(not isinstance(edge, list) or len(edge) != 2 or not set(edge).issubset(nodes) for edge in edges):
                raise ValueError(f"{cid}: precedence references missing schedule node")
        else:
            event = case.get("event")
            gold = case.get("gold", {})
            if not isinstance(event, dict) or not all(isinstance(event.get(k), str) for k in ("source", "text")):
                raise ValueError(f"{cid}: event source/text required")
            if gold.get("decision") not in DECISIONS or (
                gold.get("follow_up") not in FOLLOW_UPS if gold.get("decision") == "INTERRUPT" else gold.get("follow_up") is not None
            ):
                raise ValueError(f"{cid}: invalid Gold labels")
            if experiment == "counterfactual" and (case.get("group_type") not in ("goal", "state", "semantic") or not case.get("group_id")):
                raise ValueError(f"{cid}: group_type/group_id required")


def _event_lines(event: dict) -> str:
    return f"Incoming event from {event['source']}: {event['text']}"


def context_text(case: dict, view: str) -> str:
    if view == "event-only":
        return ""
    if view not in ("goal", "state", "trajectory"):
        raise ValueError(f"unknown view {view}")
    text = f"Original user task: {case['goal']}"
    if view == "state":
        state = case["state"]
        text += f"\n\nCurrent page URL: {state.get('url', '')}\nCurrent state: {state.get('summary', '')}"
        if state.get('recent_action') is not None:
            text += f"\nMost recent recorded interaction: {state['recent_action']}"
    elif view == "trajectory":
        # The complete authored prefix is supplied verbatim; no state summary,
        # future action, Gold label, or manual remaining-action estimate is added.
        text += "\n\nFull recorded pre-event execution history:"
        for step in case["trajectory"]:
            text += (f"\nStep {step['step']}\nURL: {step['url']}"
                     f"\nPage observation: {step['observation']}"
                     f"\nAgent action: {step['action'] if step['action'] is not None else 'No action yet (event arrives now)'}")
    if view in ("state", "trajectory") and case.get("rules"):
        text += "\n\nApplicable workflow rules:\n" + "\n".join(case["rules"])
    return text


def decision_messages(case: dict, view: str) -> list[dict]:
    prefix = context_text(case, view)
    content = f"{prefix}\n\n{_event_lines(case['event'])}" if prefix else _event_lines(case["event"])
    return [{"role": "system", "content": SYSTEM_DECISION},
            {"role": "user", "content": content + "\n\nDecision:"}]


def follow_up_messages(case: dict, view: str, event: dict | None = None, schedule: list | None = None) -> list[dict]:
    prefix = context_text(case, view)
    event = event or case["event"]
    content = f"{prefix}\n\n{_event_lines(event)}" if prefix else _event_lines(event)
    if case.get("events"):
        content += "\n\nOther events at the SAME checkpoint (no event action has executed yet):"
        for other in case["events"]:
            content += f"\n{other['id']}: {_event_lines(other)}"
        if schedule is not None:
            content += "\nProposed schedule (a proposal, not an observed state change): " + json.dumps(schedule)
    return [{"role": "system", "content": SYSTEM_FOLLOW_UP},
            {"role": "user", "content": content + "\n\nThe event will be handled now. What happens to the original task afterward?\nFollow-up:"}]


def multi_messages(case: dict, view: str | None = None) -> list[dict]:
    parts = [context_text(case, view or VIEWS['multi'][0]), "Events arriving at this checkpoint:"]
    for event in case["events"]:
        parts.append(f"{event['id']} — {_event_lines(event)}")
    parts.append("Return decisions for every event and a feasible schedule using event IDs plus CURRENT_TASK or TERMINATE_TASK.")
    return [{"role": "system", "content": SYSTEM_MULTI},
            {"role": "user", "content": "\n\n".join(parts)}]


def _json_object(raw: str) -> dict | None:
    value = raw.strip()
    if value.startswith("```") and value.endswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
        value = re.sub(r"\s*```$", "", value)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_label(raw: str, allowed: tuple[str, ...], field: str) -> str | None:
    direct = raw.strip().upper()
    if direct in allowed:
        return direct
    parsed = _json_object(raw)
    value = parsed.get(field) if parsed else None
    return value.strip().upper() if isinstance(value, str) and value.strip().upper() in allowed else None


def parse_multi(raw: str, case: dict) -> dict | None:
    parsed = _json_object(raw)
    if not parsed:
        return None
    decisions = parsed.get("decisions")
    schedule = parsed.get("schedule")
    if not isinstance(decisions, dict):
        return None
    expected = {event["id"] for event in case["events"]}
    if set(decisions) != expected:
        return None
    normalized = {eid: value.strip().upper() if isinstance(value, str) else None for eid, value in decisions.items()}
    # A bad schedule is a scheduling error. Preserve independently parseable
    # event decisions so event metrics and conditional follow-ups remain valid.
    return {"decisions": normalized, "schedule": schedule}


def tls_context() -> ssl.SSLContext:
    custom = os.environ.get("EVENTARENA_CA_BUNDLE")
    if custom:
        return ssl.create_default_context(cafile=custom)
    mac_ca = Path("/etc/ssl/cert.pem")
    if sys.platform == "darwin" and mac_ca.is_file():
        return ssl.create_default_context(cafile=str(mac_ca))
    return ssl.create_default_context()


class ProviderError(RuntimeError):
    def __init__(self, category: str, http_status: int | None = None, provider_code: str | None = None):
        super().__init__(category)
        self.category = category
        self.http_status = http_status
        self.provider_code = provider_code

    def audit(self) -> dict[str, Any]:
        return {"category": self.category, "http_status": self.http_status, "provider_code": self.provider_code}


def _safe_code(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "", str(value))[:50]


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            deadline = email.utils.parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            return max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _pace(model: dict) -> None:
    interval = float(model.get("min_interval_s", 0))
    if interval < 0:
        raise ValueError("min_interval_s must be nonnegative")
    name = model["name"]
    previous = _LAST_REQUEST_AT.get(name)
    if previous is not None:
        wait = previous + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
    _LAST_REQUEST_AT[name] = time.monotonic()


def request_model(model: dict, messages: list[dict], config: dict, stage: str, case: dict) -> tuple[str, Any]:
    if model["backend"] == "baseline":
        if stage == "follow_up":
            return model.get("follow_up_baseline", "HANDLE"), None
        if stage == "multi_decision":
            decision = model["baseline"]
            event_ids = [event["id"] for event in case["events"]]
            if decision == "IGNORE":
                schedule = ["CURRENT_TASK"]
            elif decision == "DEFER":
                schedule = ["CURRENT_TASK", *event_ids]
            else:
                schedule = [*event_ids, "CURRENT_TASK"]
            return json.dumps({"decisions": dict.fromkeys(event_ids, decision), "schedule": schedule}), None
        return model["baseline"], None
    key = os.environ.get(model["api_key_env"])
    if not key:
        raise ValueError(f"set {model['api_key_env']} in the environment before running")
    payload = {"model": model["model"], "messages": messages}
    for name in ("temperature", "max_tokens", "max_completion_tokens", "top_p"):
        if model.get(name) is not None:
            payload[name] = model[name]
    payload.update(model.get("extra_body", {}))
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    retries = int(model.get("retries", config.get("retries", 2)))
    timeout = int(model.get("timeout", config.get("timeout", 90)))
    for attempt in range(retries + 1):
        _pace(model)
        req = urllib.request.Request(model["endpoint"], data=body, method="POST",
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=tls_context()) as response:
                try:
                    data = json.load(response)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ProviderError("invalid_json") from exc
            if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or not data["choices"]:
                raise ProviderError("missing_choices")
            first = data["choices"][0]
            message = first.get("message") if isinstance(first, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
            if not isinstance(content, str):
                raise ProviderError("nontext_content")
            return content, data.get("usage")
        except urllib.error.HTTPError as exc:
            try:
                data = json.load(exc)
            except (ValueError, OSError):
                data = {}
            detail = data.get("error", {}) if isinstance(data, dict) else {}
            code = detail.get("code", data.get("code")) if isinstance(detail, dict) else None
            if exc.code not in (429, 500, 502, 503, 504) or attempt == retries:
                raise ProviderError("http_error", exc.code, _safe_code(code)) from exc
            retry_after = _retry_after_seconds(exc.headers.get("Retry-After") if exc.headers else None)
            if retry_after is not None:
                max_wait = float(model.get("max_retry_after_s", config.get("max_retry_after_s", 60)))
                if retry_after > max_wait:
                    raise ProviderError("retry_after_exceeds_limit", exc.code, _safe_code(code)) from exc
                time.sleep(max(retry_after, min(8, 2 ** attempt)))
                continue
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == retries:
                raise ProviderError(type(exc).__name__.lower()) from exc
        time.sleep(min(8, 2 ** attempt))
    raise AssertionError("unreachable")


def response_key(view: str, case_id: str, stage: str, event_id: str | None = None) -> tuple:
    return (view, case_id, stage, event_id)


def load_responses(path: Path) -> dict[tuple, dict]:
    if not path.is_file():
        return {}
    return {response_key(row["view"], row["case_id"], row["stage"], row.get("event_id")): row
            for row in read_jsonl(path)}


def _append_jsonl(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _execute_call(model: dict, config: dict, case: dict, view: str, stage: str,
                  event_id: str | None, messages: list[dict], run_dir: Path,
                  records: dict[tuple, dict]) -> dict:
    key = response_key(view, case["case_id"], stage, event_id)
    old = records.get(key)
    if old and old.get("api_ok"):
        return old
    prompt_sha = sha256(stable_json(messages).encode("utf-8"))
    _append_jsonl(run_dir / "prompts.jsonl", {"view": view, "case_id": case["case_id"],
                                          "stage": stage, "event_id": event_id,
                                          "messages": messages, "sha256": prompt_sha})
    started = time.monotonic()
    error = None
    usage = None
    raw = None
    try:
        raw, usage = request_model(model, messages, config, stage, case)
    except ProviderError as exc:
        error = exc.audit()
    if stage == "multi_decision":
        parsed = parse_multi(raw, case) if raw is not None else None
    else:
        parsed = parse_label(raw, FOLLOW_UPS if stage == "follow_up" else DECISIONS,
                             "follow_up" if stage == "follow_up" else "decision") if raw is not None else None
    row = {"view": view, "case_id": case["case_id"], "stage": stage, "event_id": event_id,
           "api_ok": error is None, "error": error, "raw": raw, "parsed": parsed,
           "usage": usage, "duration_s": round(time.monotonic() - started, 3),
           "prompt_sha256": prompt_sha, "at_utc": datetime.now(timezone.utc).isoformat()}
    _append_jsonl(run_dir / "responses.jsonl", row)
    records[key] = row
    return row


def manifest_for(experiment: str, data_path: Path, config: dict, model: dict) -> dict:
    stable = {
        "experiment": experiment,
        "dataset_path": str(data_path.resolve()),
        "dataset_sha256": sha256(data_path.read_bytes()),
        "model_config": model,
        "run_config": {k: v for k, v in config.items() if k != "models"},
        "prompt_code_sha256": sha256(Path(__file__).read_bytes()),
        "scoring_code_sha256": sha256(Path(__file__).with_name("metrics.py").read_bytes()),
        "views": list(VIEWS[experiment]),
    }
    return {**stable, "created_at_utc": datetime.now(timezone.utc).isoformat()}


def _same_manifest(existing: dict, expected: dict) -> bool:
    return {k: v for k, v in existing.items() if k != "created_at_utc"} == {
        k: v for k, v in expected.items() if k != "created_at_utc"
    }


def evaluate_dir(experiment: str, cases: list[dict], run_dir: Path) -> dict:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    records = load_responses(run_dir / "responses.jsonl")
    views = VIEWS[experiment]
    metrics = {}
    for view in views:
        if experiment in ("main", "ablation"):
            metrics[view] = score_single(cases, records, view)
        elif experiment == "counterfactual":
            metrics[view] = score_counterfactual(cases, records, view)
        else:
            metrics[view] = score_multi(cases, records, view)
    report = {"manifest": manifest, "metrics": metrics}
    (run_dir / "metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    keys = sorted({key for values in metrics.values() for key in values})
    with (run_dir / "metrics.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["view", *keys])
        writer.writeheader()
        for view, values in metrics.items():
            writer.writerow({"view": view, **{key: stable_json(value) if isinstance(value, (dict, list)) else value
                                                 for key, value in values.items()}})
    for view, values in metrics.items():
        print(f"{manifest['model_config']['name']:<22} {experiment:<15} {view:<11} {stable_json(values)}")
    return report


def run_one(experiment: str, cases: list[dict], data_path: Path, config: dict,
            model: dict, run_root: Path, resume: bool) -> None:
    run_dir = run_root / experiment / model["name"]
    manifest = manifest_for(experiment, data_path, config, model)
    if resume:
        if not (run_dir / "manifest.json").is_file():
            raise ValueError(f"resume requested but no run exists: {run_dir}")
        old_manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        if not _same_manifest(old_manifest, manifest):
            raise ValueError(f"{run_dir}: dataset, model, config, or prompt code changed; use a new run root")
    else:
        run_dir.mkdir(parents=True, exist_ok=False)
        (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    records = load_responses(run_dir / "responses.jsonl")
    views = VIEWS[experiment]
    print(f"Running {model['name']} / {experiment}: {len(cases)} cases × {len(views)} view(s) -> {run_dir}")
    total = len(cases) * len(views)
    done = 0
    for view in views:
        for case in cases:
            done += 1
            stage = "multi_decision" if experiment == "multi" else "decision"
            messages = multi_messages(case) if experiment == "multi" else decision_messages(case, view)
            primary = _execute_call(model, config, case, view, stage, None, messages, run_dir, records)
            print(f"[{done}/{total}] {view} {case['case_id']}: "
                  f"{primary['parsed'] if primary['error'] is None else primary['error']}", flush=True)
            if experiment in ("main", "ablation"):
                if case["gold"]["decision"] == "INTERRUPT" and primary["parsed"] == "INTERRUPT":
                    _execute_call(model, config, case, view, "follow_up", None,
                                  follow_up_messages(case, view), run_dir, records)
            elif experiment == "multi" and isinstance(primary.get("parsed"), dict):
                for event in case["events"]:
                    eid = event["id"]
                    gold = case["gold"]["decisions"][eid]["decision"]
                    pred = primary["parsed"]["decisions"].get(eid)
                    if gold == pred == "INTERRUPT":
                        _execute_call(model, config, case, view, "follow_up", eid,
                                      follow_up_messages(case, view, event, primary["parsed"].get("schedule")), run_dir, records)
    evaluate_dir(experiment, cases, run_dir)


def _dataset_path(args: argparse.Namespace) -> Path:
    return Path(args.data).expanduser().resolve() if args.data else DATASETS[args.experiment]


def _config_path(args: argparse.Namespace) -> Path:
    return Path(args.config).expanduser().resolve() if args.config else ROOT / "models.json"


def run_command(args: argparse.Namespace) -> None:
    data_path = _dataset_path(args)
    config = load_config(_config_path(args))
    cases = read_jsonl(data_path)
    validate_cases(args.experiment, cases)
    models = select_models(config, args.only, args.experiment)
    if args.dry_run:
        n_primary = len(cases) * len(VIEWS[args.experiment])
        print(f"DRY RUN: {args.experiment}; dataset={data_path}; sha256={sha256(data_path.read_bytes())}")
        for model in models:
            suffix = "; follow-up calls depend on correct INTERRUPT predictions" if args.experiment != "counterfactual" else ""
            print(f"model={model['name']} primary_calls={n_primary}{suffix}")
        sample = multi_messages(cases[0]) if args.experiment == "multi" else decision_messages(cases[0], VIEWS[args.experiment][0])
        print("First prompt (Gold omitted):")
        print(json.dumps(sample, ensure_ascii=False, indent=2))
        return
    for model in models:
        if model["backend"] == "chat-completions" and not os.environ.get(model["api_key_env"]):
            raise ValueError(f"set {model['api_key_env']} in the environment before running")
    run_root = Path(args.run_root).expanduser().resolve() if args.run_root else ROOT / "runs" / "pilot_v3"
    for model in models:
        run_one(args.experiment, cases, data_path, config, model, run_root, args.resume)


def evaluate_command(args: argparse.Namespace) -> None:
    cases = read_jsonl(_dataset_path(args))
    validate_cases(args.experiment, cases)
    run_root = Path(args.run_root).expanduser().resolve()
    dirs = [run_root / args.experiment / args.only] if args.only else sorted((run_root / args.experiment).iterdir())
    for run_dir in dirs:
        if run_dir.is_dir() and (run_dir / "manifest.json").is_file():
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            if manifest["dataset_sha256"] != sha256(_dataset_path(args).read_bytes()):
                raise ValueError(f"dataset changed since {run_dir} ran")
            if manifest.get("scoring_code_sha256") != sha256(Path(__file__).with_name("metrics.py").read_bytes()):
                raise ValueError(f"scoring code changed since {run_dir} ran; use its original code version")
            evaluate_dir(args.experiment, cases, run_dir)


def compare_command(args: argparse.Namespace) -> None:
    run_root = Path(args.run_root).expanduser().resolve()
    reports = []
    for path in sorted((run_root / args.experiment).glob("*/metrics.json")):
        report = json.loads(path.read_text(encoding="utf-8"))
        for view, metrics in report["metrics"].items():
            reports.append({"model": report["manifest"]["model_config"]["name"],
                            "experiment": args.experiment, "view": view,
                            "dataset_sha256": report["manifest"]["dataset_sha256"], **metrics})
    if not reports:
        raise ValueError(f"no metrics found under {run_root / args.experiment}")
    hashes = {row["dataset_sha256"] for row in reports}
    prompt_hashes = {report["manifest"].get("prompt_code_sha256")
                     for path in sorted((run_root / args.experiment).glob("*/metrics.json"))
                     for report in [json.loads(path.read_text(encoding="utf-8"))]}
    scoring_hashes = {report["manifest"].get("scoring_code_sha256")
                      for path in sorted((run_root / args.experiment).glob("*/metrics.json"))
                      for report in [json.loads(path.read_text(encoding="utf-8"))]}
    if (len(hashes) != 1 or len(prompt_hashes) != 1 or len(scoring_hashes) != 1
            or None in prompt_hashes or None in scoring_hashes):
        raise ValueError("cannot compare models run on different dataset, prompt-code, or scoring-code versions")
    output = Path(args.output).expanduser().resolve() if args.output else run_root / args.experiment / "comparison.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    keys = sorted({key for row in reports for key in row})
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        for row in reports:
            writer.writerow({key: stable_json(value) if isinstance(value, (dict, list)) else value for key, value in row.items()})
    print(f"Saved {output}")


def case_study_command(args: argparse.Namespace) -> None:
    """Export Case D only: correct event decisions and follow-ups, wrong schedule."""
    if args.max_cases < 1:
        raise ValueError("max-cases must be positive")
    run_root = Path(args.run_root).expanduser().resolve()
    data_path = Path(args.data).expanduser().resolve() if args.data else DATASETS["multi"]
    cases = read_jsonl(data_path)

    def model_order(model_dir: Path) -> tuple[bool, str]:
        manifest_path = model_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        return manifest.get("model_config", {}).get("backend") == "baseline", model_dir.name

    selected = []
    for model_dir in sorted((run_root / "multi").glob("*"), key=model_order):
        manifest_path = model_dir / "manifest.json"
        response_path = model_dir / "responses.jsonl"
        if not manifest_path.is_file() or not response_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("dataset_sha256") != sha256(data_path.read_bytes()):
            raise ValueError(f"case-study source dataset changed since {model_dir} ran")
        records = load_responses(response_path)
        for case in cases:
            cid = case["case_id"]
            row = records.get((VIEWS["multi"][0], cid, "multi_decision", None))
            if not row or not row.get("api_ok") or not isinstance(row.get("parsed"), dict):
                continue
            prediction = row["parsed"]
            decisions = prediction.get("decisions")
            if not isinstance(decisions, dict) or any(
                decisions.get(eid) != gold["decision"] for eid, gold in case["gold"]["decisions"].items()
            ):
                continue
            follow_outputs = []
            for event in case["events"]:
                eid = event["id"]
                gold = case["gold"]["decisions"][eid]
                if gold["decision"] == "INTERRUPT":
                    follow = records.get((VIEWS["multi"][0], cid, "follow_up", eid))
                    if not follow or not follow.get("api_ok") or follow.get("parsed") != gold["follow_up"]:
                        break
                    follow_outputs.append({"event_id": eid, "raw_output": follow.get("raw"),
                                           "prediction": follow.get("parsed")})
            else:
                schedule = prediction.get("schedule")
                required = case["gold"]["required_nodes"]
                precedence = case["gold"]["precedence"]
                if _schedule_ok(schedule, required, precedence):
                    continue
                nodes = [node for node in schedule if isinstance(node, str)] if isinstance(schedule, list) else []
                missing = sorted(set(required) - set(nodes))
                extra = sorted(set(nodes) - set(required))
                duplicates = sorted({node for node in nodes if nodes.count(node) > 1})
                positions = {node: i for i, node in enumerate(nodes)}
                violated = [[before, after] for before, after in precedence
                            if before not in positions or after not in positions or positions[before] >= positions[after]]
                mechanism = ("invalid_schedule_format" if not isinstance(schedule, list) or len(nodes) != len(schedule)
                             else "wrong_schedule_nodes" if missing or extra or duplicates
                             else "precedence_violation")
                selected.append({"case_study": "D", "category": "multi_event_scheduling",
                                 "case_id": cid, "model": model_dir.name,
                                 "goal": case["goal"], "state": case["state"], "events": case["events"],
                                 "gold_event_decisions": case["gold"]["decisions"],
                                 "gold_schedule_constraints": {"required_nodes": required, "precedence": precedence},
                                 "gold_rationale": case.get("annotation", {}).get("rationale"),
                                 "raw_output": row.get("raw"), "prediction": prediction,
                                 "follow_up_outputs": follow_outputs,
                                 "failure_mechanism": mechanism, "missing_nodes": missing,
                                 "extra_nodes": extra, "duplicate_nodes": duplicates,
                                 "violated_precedence": violated})
                if len(selected) >= args.max_cases:
                    break
        if len(selected) >= args.max_cases:
            break
    output = Path(args.output).expanduser().resolve() if args.output else run_root / "case_studies.jsonl"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for row in selected:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"Saved {len(selected)} Case D multi-event scheduling failure(s) to {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Run main, ablation, counterfactual, or multi experiment")
    run.add_argument("--experiment", choices=tuple(DATASETS), required=True)
    run.add_argument("--config", default=None)
    run.add_argument("--data", default=None, help="Override dataset JSONL")
    run.add_argument("--only", default=None, help="Run one configured model, even if disabled")
    run.add_argument("--run-root", default=None)
    run.add_argument("--resume", action="store_true", help="Retry API failures and continue incomplete runs")
    run.add_argument("--dry-run", action="store_true", help="Print plan and first prompt without calling API or writing files")
    run.set_defaults(func=run_command)
    evaluate = sub.add_parser("evaluate", help="Recompute metrics from saved responses")
    evaluate.add_argument("--experiment", choices=tuple(DATASETS), required=True)
    evaluate.add_argument("--data", default=None)
    evaluate.add_argument("--run-root", required=True)
    evaluate.add_argument("--only", default=None)
    evaluate.set_defaults(func=evaluate_command)
    compare = sub.add_parser("compare", help="Create a model-comparison CSV for one experiment")
    compare.add_argument("--experiment", choices=tuple(DATASETS), required=True)
    compare.add_argument("--run-root", required=True)
    compare.add_argument("--output", default=None)
    compare.set_defaults(func=compare_command)
    study = sub.add_parser("case-study", help="Export Case D: multi-event scheduling failures only")
    study.add_argument("--run-root", required=True)
    study.add_argument("--data", default=None, help="Override multi-event dataset JSONL")
    study.add_argument("--max-cases", type=int, default=1)
    study.add_argument("--output", default=None)
    study.set_defaults(func=case_study_command)
    args = parser.parse_args()
    try:
        args.func(args)
    except (ValueError, FileNotFoundError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
