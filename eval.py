"""eval-harness — an evaluation harness for LLM outputs.

Loads test suites from YAML, runs each prompt through one or more models,
scores the outputs against checks (rules + LLM-as-judge), and prints a
scorecard. Supports side-by-side model comparison and baseline regression
diffs.

Usage:
    python eval.py evals/example.yaml --model claude
    python eval.py evals/support/safety.yaml --compare claude,qwen
    python eval.py evals/support/safety.yaml --model claude --save results/run1.json
    python eval.py evals/support/safety.yaml --model claude --baseline results/run1.json
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

import yaml

VERSION = "0.5.0"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def strip_fences(text: str) -> str:
    """Models often wrap JSON in ```json ... ``` fences. Strip them."""
    text = text.strip()
    match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    return match.group(1) if match else text


# ---------------------------------------------------------------------------
# 1. Model providers — given a prompt, return the model's text output.
# ---------------------------------------------------------------------------
def run_model(prompt: str, model: str = "mock", system: str | None = None) -> str:
    if model == "mock":
        return prompt  # echo — a stand-in that needs no API key

    if model == "claude":
        import anthropic  # lazy import: only loaded when actually calling Claude
        client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
        kwargs = dict(
            model="claude-haiku-4-5-20251001",  # fast + cheap — ideal for evals
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        if system:
            kwargs["system"] = system
        return client.messages.create(**kwargs).content[0].text

    if model == "qwen":
        import urllib.request  # lazy — local Ollama over HTTP
        payload = {"model": "qwen2.5:3b", "prompt": prompt, "stream": False}
        if system:
            payload["system"] = system
        req = urllib.request.Request(
            "http://localhost:11434/api/generate",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())["response"]

    raise ValueError(f"Unknown model: {model!r}")


# ---------------------------------------------------------------------------
# 2. Checks — each takes (output, arg, ctx) and returns (passed, detail).
#    ctx carries suite-level context (e.g. which model acts as judge).
# ---------------------------------------------------------------------------
def check_contains(output, needles, ctx):
    missing = [n for n in needles if n.lower() not in output.lower()]
    return (not missing, f"missing {missing}" if missing else f"contains {needles}")


def check_not_contains(output, needles, ctx):
    found = [n for n in needles if n.lower() in output.lower()]
    return (not found, f"must-not-contain present: {found}" if found else "clean")


def check_regex(output, pattern, ctx):
    ok = re.search(pattern, output, re.IGNORECASE) is not None
    return ok, f"regex /{pattern}/ {'matched' if ok else 'not found'}"


def check_is_json(output, expected, ctx):
    try:
        json.loads(strip_fences(output))
        is_json = True
    except (json.JSONDecodeError, ValueError):
        is_json = False
    return is_json == expected, f"is_json={is_json} (wanted {expected})"


def check_json_schema(output, schema, ctx):
    import jsonschema
    try:
        data = json.loads(strip_fences(output))
    except (json.JSONDecodeError, ValueError):
        return False, "output is not valid JSON"
    try:
        jsonschema.validate(data, schema)
        return True, "schema valid"
    except jsonschema.ValidationError as e:
        return False, f"schema violation: {e.message}"


def check_numeric_bounds(output, spec, ctx):
    """Find numbers with a regex capture group; assert all within [min, max].
    spec: {pattern: str, min: num, max: num}"""
    values = [float(v) for v in re.findall(spec["pattern"], output)]
    if not values:
        return False, f"no matches for /{spec['pattern']}/"
    lo, hi = spec.get("min", float("-inf")), spec.get("max", float("inf"))
    bad = [v for v in values if not (lo <= v <= hi)]
    return (not bad, f"out of bounds {bad} (allowed {lo}–{hi})" if bad
            else f"{len(values)} value(s) within {lo}–{hi}")


def check_word_count(output, spec, ctx):
    n = len(output.split())
    lo, hi = spec.get("min", 0), spec.get("max", float("inf"))
    return lo <= n <= hi, f"{n} words (allowed {lo}–{hi})"


def check_llm_judge(output, spec, ctx):
    """A second model grades the output 1–5 against a rubric.
    spec: {rubric: str, threshold: int (default 4)}"""
    judge_prompt = (
        "You are a strict evaluator of AI assistant responses.\n"
        f"RUBRIC (what a good response looks like):\n{spec['rubric']}\n\n"
        f"RESPONSE TO EVALUATE:\n{output}\n\n"
        'Reply with ONLY JSON: {"score": <integer 1-5>, "reason": "<one sentence>"}'
    )
    try:
        raw = run_model(judge_prompt, model=ctx.get("judge_model", "claude"))
        verdict = json.loads(strip_fences(raw))
        score, reason = int(verdict["score"]), verdict.get("reason", "")
    except Exception as e:  # no API key, judge offline, unparseable verdict…
        return False, f"judge error: {e}"
    threshold = spec.get("threshold", 4)
    return score >= threshold, f"judge {score}/5 (need ≥{threshold}) — {reason}"


CHECKS = {
    "contains": check_contains,
    "not_contains": check_not_contains,
    "regex": check_regex,
    "is_json": check_is_json,
    "json_schema": check_json_schema,
    "numeric_bounds": check_numeric_bounds,
    "word_count": check_word_count,
    "llm_judge": check_llm_judge,
}


# ---------------------------------------------------------------------------
# 3. Suite loading — supports two formats:
#    old: a bare list of tests
#    new: {config: {system, judge_model}, tests: [...]}
# ---------------------------------------------------------------------------
def load_suite(path: str):
    with open(path) as f:
        data = yaml.safe_load(f)
    if isinstance(data, list):
        return {}, data
    config = data.get("config", {}) or {}
    # `system_file` (relative to the suite) loads the system prompt from disk,
    # so every suite tests ONE shared prompt — the single source of truth.
    if config.get("system_file"):
        config["system"] = (Path(path).parent / config["system_file"]).resolve().read_text()
    return config, data["tests"]


# ---------------------------------------------------------------------------
# 4. Run one test through one model, apply every check.
# ---------------------------------------------------------------------------
def score_test(test, model, config):
    ctx = {"judge_model": config.get("judge_model", "claude")}
    output = run_model(test["prompt"], model=model, system=config.get("system"))
    results, all_passed = [], True
    for check_name, arg in test.get("checks", {}).items():
        if check_name not in CHECKS:
            results.append(f"?? unknown check '{check_name}'")
            all_passed = False
            continue
        passed, detail = CHECKS[check_name](output, arg, ctx)
        results.append(f"{'✓' if passed else '✗'} {check_name}: {detail}")
        all_passed = all_passed and passed
    return all_passed, results, output


def run_suite(tests, model, config, verbose=True):
    """Run every test against one model. Returns {test_name: bool}."""
    outcomes = {}
    for test in tests:
        ok, results, _ = score_test(test, model, config)
        outcomes[test["name"]] = ok
        if verbose:
            print(f"[{'PASS' if ok else 'FAIL'}] {test['name']}")
            for r in results:
                print(f"    {r}")
    return outcomes


# ---------------------------------------------------------------------------
# 5. CLI — single model, side-by-side compare, save/baseline regression diff.
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a suite of LLM evals from a YAML file and print a scorecard."
    )
    parser.add_argument("tests", help="Path to a YAML suite")
    parser.add_argument("--model", default="mock", help="Model provider (default: mock)")
    parser.add_argument("--compare", help="Comma-separated models to run side-by-side, e.g. claude,qwen")
    parser.add_argument("--judge-model", help="Override the model used for llm_judge checks")
    parser.add_argument("--save", help="Write results to a JSON file")
    parser.add_argument("--baseline", help="Compare against a previous --save file and report regressions")
    parser.add_argument("--report", help="Write a markdown scorecard to this file")
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero if any test fails (use as a CI gate)")
    parser.add_argument("--dry-run", action="store_true", help="List tests without calling any model")
    args = parser.parse_args()

    config, tests = load_suite(args.tests)
    if args.judge_model:
        config["judge_model"] = args.judge_model

    if args.dry_run:
        print(f"suite: {args.tests} · {len(tests)} tests")
        for t in tests:
            print(f"  - {t['name']}  (checks: {', '.join(t.get('checks', {}))})")
        return 0

    models = args.compare.split(",") if args.compare else [args.model]
    print(f"eval-harness v{VERSION}  ·  suite={Path(args.tests).name}  ·  "
          f"models={','.join(models)}  ·  {len(tests)} tests\n")

    all_outcomes = {}
    for model in models:
        if len(models) > 1:
            print(f"——— {model} ———")
        all_outcomes[model] = run_suite(tests, model, config)
        passed = sum(all_outcomes[model].values())
        pct = round(100 * passed / len(tests)) if tests else 0
        print(f"\nScore [{model}]: {passed}/{len(tests)} ({pct}%)\n")

    # Side-by-side table when comparing.
    if len(models) > 1:
        name_w = max(len(t["name"]) for t in tests) + 2
        print("=" * (name_w + 10 * len(models)))
        print("TEST".ljust(name_w) + "".join(m.ljust(10) for m in models))
        for t in tests:
            row = t["name"].ljust(name_w)
            row += "".join(("✓" if all_outcomes[m][t["name"]] else "✗").ljust(10) for m in models)
            print(row)
        print("-" * (name_w + 10 * len(models)))
        totals = "TOTAL".ljust(name_w)
        totals += "".join(f"{sum(all_outcomes[m].values())}/{len(tests)}".ljust(10) for m in models)
        print(totals)

    # Save results for future regression diffs.
    if args.save:
        Path(args.save).parent.mkdir(parents=True, exist_ok=True)
        with open(args.save, "w") as f:
            json.dump({"suite": args.tests, "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "outcomes": all_outcomes}, f, indent=2)
        print(f"\nsaved → {args.save}")

    # Markdown scorecard.
    if args.report:
        lines = [
            "# eval-harness scorecard", "",
            f"- **suite:** `{args.tests}`",
            f"- **models:** {', '.join(models)}",
            f"- **generated:** {time.strftime('%Y-%m-%d %H:%M:%S')}", "",
            "| test | " + " | ".join(models) + " |",
            "|---|" + "---|" * len(models),
        ]
        for t in tests:
            row = [("✅" if all_outcomes[m][t["name"]] else "❌") for m in models]
            lines.append(f"| {t['name']} | " + " | ".join(row) + " |")
        lines.append("")
        for m in models:
            passed = sum(all_outcomes[m].values())
            lines.append(f"**{m}: {passed}/{len(tests)} "
                         f"({round(100 * passed / len(tests)) if tests else 0}%)**  ")
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text("\n".join(lines) + "\n")
        print(f"report → {args.report}")

    # Regression check against a previous run.
    if args.baseline:
        with open(args.baseline) as f:
            base = json.load(f)["outcomes"]
        print(f"\n— regression check vs {args.baseline} —")
        regressions = fixes = 0
        for model in models:
            for name, ok in all_outcomes[model].items():
                prev = base.get(model, {}).get(name)
                if prev is True and ok is False:
                    print(f"  ⚠️  REGRESSION [{model}] {name}: was PASS, now FAIL")
                    regressions += 1
                elif prev is False and ok is True:
                    print(f"  🎉 fixed [{model}] {name}: was FAIL, now PASS")
                    fixes += 1
        if not regressions and not fixes:
            print("  no changes vs baseline")
        if regressions:
            return 1  # non-zero exit → usable as a CI gate

    # Strict mode: any failing test fails the run (prompt-change CI gate).
    if args.strict and any(not ok for m in models for ok in all_outcomes[m].values()):
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
