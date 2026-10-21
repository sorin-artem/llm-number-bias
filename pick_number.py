#!/usr/bin/env python3
"""Ask independent number-picking questions through OpenRouter; keep every result."""

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

from dotenv import load_dotenv
from openai import APIError, OpenAI

from python_tool import DEFAULT_IMAGE, DockerPython, PYTHON_TOOL


BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODELS = [
    "openai/gpt-6.1-sol",
    "anthropic/claude-opus-5.5",
    "x-ai/grok-4.7",
    "google/gemini-4-argon",
    "meta/muse-wonder",
]
PROMPT = "Pick a number between 1 and 10"
CSV_FIELDS = [
    "run_id", "sample", "model", "returned_model", "provider", "started_at",
    "latency_seconds", "status", "number", "answer", "finish_reason",
    "prompt_tokens", "completion_tokens", "reasoning_tokens", "cost_usd",
    "response_id", "trace_id", "api_request_count", "tool_calls_requested",
    "tool_calls_executed", "tool_calls_successful", "reported_cost_usd", "cost_reported_steps", "error",
]


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def redact(value, secret):
    """Redact the configured credential even if a provider echoes it in an error."""
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, dict):
        return {key: redact(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secret) for item in value]
    return value


def classify(answer, finish_reason):
    if finish_reason in {"length", "max_tokens"}:
        return "truncated", None
    if finish_reason not in {"stop", "end_turn"}:
        return "incomplete", None
    if not answer.strip():
        return "empty", None
    # Count every numeric expression, not just those inside the desired range.
    numbers = re.findall(r"(?<!\w)[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?(?!\w)", answer)
    if len(numbers) != 1:
        return "ambiguous" if numbers else "unparsed", None
    if not re.fullmatch(r"[0-9]+", numbers[0]):
        return "unparsed", None
    number = int(numbers[0])
    return ("valid", number) if 1 <= number <= 10 else ("out_of_range", None)


def response_fields(raw):
    if raw.get("error"):
        return {"status": "error", "number": None, "answer": "", "error": json.dumps(raw["error"])}
    choices = raw.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    answer = message.get("content") or ""
    if not isinstance(answer, str):
        answer = ""  # The unmodified payload is still retained in JSONL.
    finish_reason = choice.get("finish_reason")
    status, number = classify(answer, finish_reason)
    usage = raw.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    return {
        "returned_model": raw.get("model"), "provider": raw.get("provider"),
        "answer": answer, "finish_reason": finish_reason, "status": status,
        "number": number, "response_id": raw.get("id"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": details.get("reasoning_tokens"),
        "cost_usd": usage.get("cost"),
    }


def append_event(handle, event, secret):
    handle.write(json.dumps(redact(event, secret), ensure_ascii=False) + "\n")
    handle.flush()
    os.fsync(handle.fileno())


def aggregate_usage(row):
    steps = [response_fields(raw) for raw in row["api_steps"]]
    costs = [step["cost_usd"] for step in steps if step.get("cost_usd") is not None]
    row["reported_cost_usd"] = sum(costs) if costs else None
    row["cost_reported_steps"] = len(costs)
    row["cost_usd"] = sum(costs) if costs and len(costs) == row["api_request_count"] else None
    for key in ["prompt_tokens", "completion_tokens", "reasoning_tokens"]:
        values = [step[key] for step in steps if step.get(key) is not None]
        row[key] = sum(values) if values else None


def complete_trial(row, args, client, tool_runner, events, secret):
    messages = [{"role": "user", "content": args.prompt}]
    row.update(api_steps=[], tool_calls=[], api_request_count=0, tool_calls_requested=0,
               tool_calls_executed=0, tool_calls_successful=0)
    max_rounds = getattr(args, "max_tool_rounds", 2) if tool_runner else 0
    for step in range(max_rounds + 1):
        request = {
            "model": row["model"], "messages": list(messages), "max_tokens": args.max_tokens,
            "extra_body": {
                "reasoning": {"effort": args.reasoning}, "usage": {"include": True},
                "provider": {"require_parameters": True},
                "trace": {"trace_id": row["trace_id"], "trace_name": "llm-number-bias",
                          "run_id": row["run_id"], "sample": row["sample"], "requested_model": row["model"], "step": step},
            },
        }
        if tool_runner:
            request.update(tools=[PYTHON_TOOL], tool_choice="auto")
        row["api_request_count"] += 1
        response = client.chat.completions.create(**request)
        raw = response.model_dump(mode="json")
        row["api_steps"].append(raw)
        row.update(response_fields(raw), response=raw)
        aggregate_usage(row)
        append_event(events, {"event": "api_response", "run_id": row["run_id"], "sample": row["sample"],
                              "model": row["model"], "trace_id": row["trace_id"], "step": step, "response": raw}, secret)
        choice = (raw.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        calls = message.get("tool_calls") or []
        if not calls or row["status"] in {"error", "truncated"}:
            return
        row["tool_calls_requested"] += len(calls)
        if not tool_runner or step == max_rounds or row["tool_calls_requested"] > 4:
            row.update(status="tool_limit", number=None, error="Tool loop limit reached; final answer not received.")
            return
        # Preserve reasoning_details and other assistant fields for reasoning models.
        messages.append(response.choices[0].message.model_dump(exclude_none=True, mode="json"))
        for call in calls:
            function = call.get("function") or {}
            started = time.monotonic()
            name, arguments = function.get("name"), function.get("arguments")
            try:
                result = tool_runner.execute(name, arguments)
            except (OSError, RuntimeError) as exc:
                result = {"stdout": "", "stderr": str(exc), "exit_code": 1}
            execution = {"step": step, "id": call.get("id"), "name": name, "arguments": arguments,
                         "result": result, "latency_seconds": round(time.monotonic() - started, 3)}
            row["tool_calls"].append(execution)
            row["tool_calls_executed"] += name == "run_python" and not result.get("stderr", "").startswith("Invalid tool arguments:")
            row["tool_calls_successful"] += name == "run_python" and result.get("exit_code") == 0
            append_event(events, {"event": "tool_result", "run_id": row["run_id"], "sample": row["sample"],
                                  "model": row["model"], "trace_id": row["trace_id"], **execution}, secret)
            print(f"  tool {name} · exit {result.get('exit_code')} · stdout {result.get('stdout', '').strip()[:80]!r}", flush=True)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)})


def summarize(rows, config):
    models = []
    for model in config["models"]:
        selected = [row for row in rows if row["model"] == model]
        valid = [row for row in selected if row["status"] == "valid"]
        counts = Counter(row["number"] for row in valid)
        costs = [row.get("reported_cost_usd", row.get("cost_usd")) for row in selected]
        known_costs = [cost for cost in costs if cost is not None]
        models.append({
            "model": model, "planned": config["samples"], "recorded": len(selected),
            "valid": len(valid), "non_valid": len(selected) - len(valid),
            "statuses": dict(Counter(row["status"] for row in selected)),
            "counts": {str(n): counts[n] for n in range(1, 11)},
            "percentages": {str(n): 100 * counts[n] / len(valid) if valid else 0 for n in range(1, 11)},
            "reported_cost_usd": sum(known_costs) if known_costs else None,
            "cost_reported_for": sum(row.get("cost_usd") is not None for row in selected),
            "api_requests": sum(row.get("api_request_count", 1) for row in selected),
            "trials_with_tool_calls": sum(bool(row.get("tool_calls_requested")) for row in selected),
            "tool_calls_requested": sum(row.get("tool_calls_requested", 0) for row in selected),
            "tool_calls_executed": sum(row.get("tool_calls_executed", 0) for row in selected),
            "tool_calls_successful": sum(row.get("tool_calls_successful", 0) for row in selected),
            "prompt_tokens": sum(row.get("prompt_tokens") or 0 for row in selected),
            "completion_tokens": sum(row.get("completion_tokens") or 0 for row in selected),
            "reasoning_tokens": sum(row.get("reasoning_tokens") or 0 for row in selected),
        })
    costs = [row.get("reported_cost_usd", row.get("cost_usd")) for row in rows]
    known_costs = [cost for cost in costs if cost is not None]
    return {
        "run_id": config["run_id"], "state": config["state"], "models": models,
        "recorded": len(rows), "planned": config["samples"] * len(config["models"]),
        "reported_cost_usd": sum(known_costs) if known_costs else None,
        "cost_reported_for": sum(row.get("cost_usd") is not None for row in rows),
        "api_requests": sum(model["api_requests"] for model in models),
        "trials_with_tool_calls": sum(model["trials_with_tool_calls"] for model in models),
        "tool_calls_executed": sum(model["tool_calls_executed"] for model in models),
        "percentage_denominator": "valid answers per model",
    }


def render_chart(summary, config, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import PercentFormatter
    import textwrap

    models = summary["models"]
    columns = min(2, len(models))
    nrows = math.ceil(len(models) / columns)
    colors = ["#2563eb", "#d97706", "#7c3aed", "#059669", "#dc2626"]
    with plt.rc_context({"font.family": "DejaVu Sans", "font.size": 10, "axes.spines.top": False, "axes.spines.right": False}):
        fig, axes = plt.subplots(nrows, columns, figsize=(12, 3.15 * nrows + 1.6), squeeze=False)
        fig.patch.set_facecolor("#f8fafc")
        for i, (ax, model) in enumerate(zip(axes.flat, models)):
            percentages = list(model["percentages"].values())
            bars = ax.bar(range(1, 11), percentages, color=colors[i % len(colors)], width=0.72, zorder=3)
            ax.axhline(10, color="#64748b", linestyle="--", linewidth=1, label="Uniform: 10%")
            for bar, pct in zip(bars, percentages):
                if pct:
                    ax.text(bar.get_x() + bar.get_width() / 2, pct + 1.5, f"{pct:.0f}%", ha="center", fontsize=9)
            ax.set_ylim(0, 112)
            ax.set_yticks([0, 25, 50, 75, 100])
            ax.yaxis.set_major_formatter(PercentFormatter(100))
            ax.set_xticks(range(1, 11))
            ax.set_axisbelow(True)
            ax.grid(axis="y", color="#e2e8f0")
            ax.set_title(model["model"], loc="left", fontsize=12, fontweight="bold", pad=26)
            tool_note = f" · Python: {model['trials_with_tool_calls']}/{model['recorded']}" if config.get("python_tool") else ""
            ax.text(0, 1.02, f"{model['valid']} valid / {model['recorded']} recorded / {model['planned']} planned · {model['non_valid']} non-valid{tool_note}", transform=ax.transAxes, color="#475569", fontsize=8 if tool_note else 9)
            ax.set_xlabel("Chosen number")
            ax.set_ylabel("Share of valid answers")
            ax.legend(loc="upper right", fontsize=8, frameon=False)
            if not model["valid"]:
                ax.text(0.5, 0.5, "No valid answers", ha="center", transform=ax.transAxes, color="#64748b")
        for ax in list(axes.flat)[len(models):]:
            ax.set_visible(False)
        title = "Offline preview — synthetic test data" if config.get("data_source") == "synthetic" else "Which number does each LLM pick?"
        height = fig.get_figheight()
        fig.text(0.08, 1 - 0.18 / height, title, ha="left", va="top", fontsize=20, fontweight="bold", color="#0f172a")
        subtitle = textwrap.fill(textwrap.shorten(f"Prompt: {config['prompt']!r}", width=200), width=110)
        fig.text(0.08, 1 - 0.65 / height, subtitle, va="top", fontsize=10, color="#475569")
        cost = summary["reported_cost_usd"]
        cost_text = f"Reported cost: ${cost:.4f}" if cost is not None else "Cost not reported"
        tool_mode = " · Python tool: auto" if config.get("python_tool") else ""
        fig.text(0.08, 0.025, f"{config['run_id']} · {config['state']} · reasoning: {config['reasoning']} · max tokens: {config['max_tokens']}{tool_mode}\n{cost_text} ({summary['cost_reported_for']}/{summary['recorded']} records) · Small samples are exploratory, not proof of randomness.", fontsize=9, color="#475569")
        fig.tight_layout(rect=(0.02, 0.65 / height, 0.99, 1 - 1.1 / height))
        fig.savefig(path, dpi=160, facecolor=fig.get_facecolor())
        plt.close(fig)


def export_results(directory, config, rows):
    with (directory / "responses.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    summary = summarize(rows, config)
    write_json(directory / "summary.json", summary)
    render_chart(summary, config, directory / "distribution.png")
    return summary


def run_experiment(args, client, secret, tool_runner=None):
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    directory = args.output / run_id
    directory.mkdir(parents=True, exist_ok=False)
    config = {
        "run_id": run_id, "started_at": utc_now(), "state": "running",
        "models": args.models, "samples": args.samples, "prompt": args.prompt,
        "reasoning": args.reasoning, "max_tokens": args.max_tokens,
        "temperature": None, "temperature_sent": False, "base_url": BASE_URL,
        "timeout_seconds": args.timeout, "automatic_retries": 0,
        "method": "independent single-user-message requests, round-robin order",
    }
    if tool_runner:
        config.update(python_tool=True, tool_choice="auto", tool_schema=PYTHON_TOOL,
                      python_image=tool_runner.image, python_image_id=tool_runner.image_id,
                      max_tool_rounds=args.max_tool_rounds, max_tool_calls=4,
                      method="independent trials with optional Python tool loop, round-robin order")
    write_json(directory / "config.json", config)
    rows = []
    interrupted = False
    fatal = False
    total = len(args.models) * args.samples
    print(f"Run {run_id}: {total} trials; output: {directory}", flush=True)
    try:
        with (directory / "responses.jsonl").open("a", encoding="utf-8") as log, (directory / "events.jsonl").open("a", encoding="utf-8") as events:
            for sample in range(1, args.samples + 1):
                for model in args.models:
                    trace_id = uuid4().hex
                    row = {"run_id": run_id, "sample": sample, "model": model, "started_at": utc_now(), "trace_id": trace_id}
                    started = time.monotonic()
                    try:
                        complete_trial(row, args, client, tool_runner, events, secret)
                    except KeyboardInterrupt:
                        row.update(status="interrupted", number=None, answer="", error="Interrupted in flight; provider may still bill this request. Cost unknown.")
                        aggregate_usage(row)
                        interrupted = True
                    except APIError as exc:
                        status_code = getattr(exc, "status_code", None)
                        row.update(status="error", number=None, answer="", error=str(exc), http_status=status_code)
                        aggregate_usage(row)
                        fatal = status_code in {401, 402, 403}
                    row["latency_seconds"] = round(time.monotonic() - started, 3)
                    row = redact(row, secret)
                    log.write(json.dumps(row, ensure_ascii=False) + "\n")
                    log.flush()
                    os.fsync(log.fileno())
                    rows.append(row)
                    preview = " ".join(row.get("answer", "").split())[:100]
                    print(f"[{len(rows)}/{total}] {model} · {row['status']} · {row['latency_seconds']}s · {preview or row.get('error', '')}", flush=True)
                    if interrupted or fatal:
                        break
                if interrupted or fatal:
                    break
    except KeyboardInterrupt:
        interrupted = True
    finally:
        config["state"] = "interrupted" if interrupted else "failed" if fatal or len(rows) != total else "completed"
        config["finished_at"] = utc_now()
        config["recorded"] = len(rows)
        write_json(directory / "config.json", config)
        summary = export_results(directory, config, rows)
        cost = summary["reported_cost_usd"]
        print(f"Saved logs, CSV, summary and PNG to {directory}", flush=True)
        print(f"Reported cost: {'unknown' if cost is None else f'${cost:.6f}'} ({summary['cost_reported_for']}/{len(rows)} records); missing costs are not zero.", flush=True)
    if interrupted:
        return 130
    return 1 if any(row["status"] != "valid" for row in rows) else 0


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def positive_float(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return number


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Make paid OpenRouter requests and save a chart")
    run.add_argument("--samples", type=positive_int, default=10, help="Requests per model (default: 10)")
    run.add_argument("--models", nargs="+", default=DEFAULT_MODELS, help="Exact OpenRouter model IDs")
    run.add_argument("--prompt", default=PROMPT)
    run.add_argument("--reasoning", choices=["none", "minimal", "low", "medium", "high", "xhigh", "max"], default="low")
    run.add_argument("--max-tokens", type=positive_int, default=1024)
    run.add_argument("--timeout", type=positive_float, default=120, help="Request timeout in seconds")
    run.add_argument("--output", type=Path, default=Path("results"), help="Parent directory for new runs")
    run.add_argument("--python-tool", action="store_true", help="Offer an optional general Python tool, executed in Docker")
    run.add_argument("--python-image", default=DEFAULT_IMAGE, help="Pre-pulled Python Docker image")
    run.add_argument("--max-tool-rounds", type=positive_int, default=2, help="Maximum tool rounds per trial (default: 2)")
    plot = commands.add_parser("plot", help="Rebuild CSV, summary and PNG offline (no API calls)")
    plot.add_argument("directory", type=Path, help="Existing run directory")
    return cli


def main():
    cli = parser()
    args = cli.parse_args()
    if args.command == "plot":
        config = json.loads((args.directory / "config.json").read_text(encoding="utf-8"))
        rows = [json.loads(line) for line in (args.directory / "responses.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        export_results(args.directory, config, rows)
        print(f"Rebuilt CSV, summary and PNG in {args.directory}; no API calls.")
        return 0
    if not args.prompt.strip():
        cli.error("--prompt must not be empty")
    if len(args.models) != len(set(args.models)):
        cli.error("--models must not contain duplicate IDs")
    load_dotenv(Path(__file__).resolve().parent / ".env")
    secret = (os.getenv("OPENROUTER_API_KEY") or os.getenv("OPEN_ROUTER_API_KEY") or "").strip()
    if not secret:
        cli.error("Set OPENROUTER_API_KEY in .env or your environment. Copy .env.example to .env first.")
    try:
        tool_runner = DockerPython(args.python_image) if args.python_tool else None
    except RuntimeError as exc:
        cli.error(str(exc))
    with OpenAI(api_key=secret, base_url=BASE_URL, max_retries=0, timeout=args.timeout) as client:
        return run_experiment(args, client, secret, tool_runner)


if __name__ == "__main__":
    sys.exit(main())
