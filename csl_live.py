"""Opt-in, bounded synthetic live continuations. Never accepts private session inputs."""

from decimal import Decimal, InvalidOperation
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import uuid

import csl_canonical as canonical
from csl_live_fixtures import asset_digests, media_case, question, special_cases, text_case


def redact(value, env):
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS", "OPENAI_API_KEY", "CODEX_API_KEY"):
        if env.get(key):
            value = value.replace(env[key], "[REDACTED]")
    value = re.sub(r"sk-[A-Za-z0-9_-]+|Bearer\s+\S+|eyJ[A-Za-z0-9_.-]{30,}", "[REDACTED]", value)
    return value


def answer_json(answer):
    answer = answer.strip()
    if answer.startswith("```"):
        answer = re.sub(r"^```(?:json)?\s*|\s*```$", "", answer)
    value = canonical.strict_json(answer)
    if not isinstance(value, dict):
        raise ValueError("Live answer must be a JSON object")
    return value


def checks(answer, expected):
    value = answer_json(answer)
    missing = []
    for key, expected_value in expected.items():
        actual = value.get(key)
        if key == "invoice_amount":
            try:
                correct = Decimal(str(actual)) == Decimal(str(expected_value))
            except Exception:
                correct = False
        elif isinstance(expected_value, bool):
            correct = actual is expected_value
        else:
            correct = isinstance(actual, str) and actual.casefold() == str(expected_value).casefold()
        if not correct:
            missing.append(key)
    return missing


def invoke(provider, sid, home, project, env, model, budget):
    child = {"PATH": env["PATH"], "HOME": str(project.parent), "LANG": "C.UTF-8",
             "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1", "DISABLE_AUTOUPDATER": "1",
             "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    if provider == "claude":
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_CUSTOM_HEADERS"):
            if env.get(key):
                child[key] = env[key]
        child["CLAUDE_CONFIG_DIR"] = str(home)
        command = ["claude", "--bare", "--strict-mcp-config", "--setting-sources", "", "--tools", "",
                   "--permission-mode", "dontAsk", "--effort", "low", "--max-turns", "1",
                   "--max-budget-usd", str(budget), "--model", model, "--resume", sid,
                   "--output-format", "json", "--print"]
    else:
        child["CODEX_HOME"] = str(home)
        command = ["codex", "exec", "--ignore-user-config", "--ignore-rules", "-s", "read-only",
            "-c", 'model_reasoning_effort="low"', "-c", 'approval_policy="never"',
            "-c", "project_doc_max_bytes=0", "-c", 'web_search="disabled"',
            "--model", model]
        for feature in ("shell_tool", "multi_agent", "apps", "plugins", "hooks", "browser_use", "computer_use", "view_image", "image_generation", "skill_search", "sleep_tool", "unbounded_connection_retries"):
            command += ["--disable", feature]
        command += ["resume", "--all", "--skip-git-repo-check", "--json", sid, "-"]
    return command, child


def live_call(provider, sid, home, project, env, model, budget, prompt, timeout=90):
    command, child = invoke(provider, sid, home, project, env, model, budget)
    # Each subprocess gets only its own credential family and an isolated HOME.
    if provider == "claude":
        command += [prompt]
    process = subprocess.Popen(command, env=child, cwd=project, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    timed_out = False
    def stop():
        nonlocal timed_out
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    timer = threading.Timer(timeout, stop)
    timer.start()
    tool_calls, answers, usage, errors = 0, [], {}, []
    try:
        if provider == "claude":
            stdout, stderr = process.communicate(timeout=timeout + 5)
            try:
                result = canonical.strict_json(stdout)
            except ValueError:
                result = {}
                errors.append("invalid-provider-json")
            if not isinstance(result, dict):
                result = {}
                errors.append("invalid-provider-json")
            answer = result.get("result", "")
            cost = result.get("total_cost_usd")
            usage = result.get("usage", {})
            if result.get("is_error"):
                errors.append("provider-reported-error")
            # Tools are disabled; verify appended transcript separately too.
        else:
            process.stdin.write(prompt)
            process.stdin.close()
            errors_buffer = deque(maxlen=200)
            def drain():
                for line in process.stderr:
                    errors_buffer.append(line)
            # Drain into bounded memory; never persist raw credential-bearing logs.
            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                item = event.get("item", {})
                if item.get("type") in ("command_execution", "mcp_tool_call", "web_search", "file_change"):
                    tool_calls += event.get("type") in ("item.started", "item.completed")
                    stop()
                if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                    answers.append(item.get("text", ""))
                if event.get("type") == "turn.completed":
                    usage = event.get("usage", {})
                if event.get("type") in ("error", "turn.failed"):
                    errors.append(event.get("type"))
            process.wait(timeout=5)
            reader.join(timeout=5)
            stderr = "".join(errors_buffer)
            answer, cost = "\n".join(answers), None
    finally:
        timer.cancel()
        if process.poll() is None:
            stop()
            process.wait(timeout=5)
        for stream in (process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()
    return {"answer": redact(answer, env), "exit_code": process.returncode, "usage": usage,
            "reported_cost_usd": cost, "tool_calls": tool_calls, "timed_out": timed_out,
            "provider_failure": bool(process.returncode or errors or timed_out),
            "stderr_excerpt": redact(stderr, env)[-1000:] if process.returncode else ""}


def write_report(path, report, env):
    serialized = redact(json.dumps(report, ensure_ascii=False, indent=2), env)
    temporary = path.with_suffix(".new")
    with temporary.open("w", encoding="utf-8") as output:
        os.chmod(temporary, 0o600)
        output.write(serialized)
    os.replace(temporary, path)


def run(args, csl):
    if not args.allow_paid:
        raise ValueError("Live verification requires --allow-paid, explicit budgets and isolated authentication")
    targets = [args.targets] if args.targets != "both" else ["codex", "claude"]
    env = dict(os.environ)
    codex_home = args.codex_home.expanduser().resolve() if args.codex_home else None
    if "codex" in targets:
        if codex_home is None or codex_home == (Path.home() / ".codex").resolve():
            raise ValueError("Provide --codex-home with a separately authenticated test home, never ~/.codex")
        status = subprocess.run(["codex", "login", "status"], env={"PATH": env["PATH"], "CODEX_HOME": str(codex_home)},
                                capture_output=True, text=True, timeout=20)
        if status.returncode or "ChatGPT" not in status.stdout + status.stderr:
            raise ValueError("Isolated Codex home must be logged in with ChatGPT; no API-key fallback is allowed")
    if "claude" in targets and not env.get("ANTHROPIC_API_KEY"):
        raise ValueError("Provide ANTHROPIC_API_KEY in memory for bounded Claude --bare verification")
    for target in targets:
        if not shutil.which(target):
            raise ValueError(f"{target} is not installed")
    budget, total = Decimal(str(args.claude_budget_usd)), Decimal(str(args.total_budget_usd))
    if not budget.is_finite() or not total.is_finite() or not Decimal("0") < budget <= Decimal("0.10") or not budget <= total <= Decimal("2"):
        raise ValueError("Use per-call Claude budget <= 0.10 USD and total reservation <= 2 USD")
    root = args.out.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    project = root / "project"
    project.mkdir()
    claude_home = root / "claude"
    homes = {"codex": codex_home, "claude": claude_home}
    run_env = {**env, "CODEX_HOME": str(codex_home or root / "codex"), "CLAUDE_CONFIG_DIR": str(claude_home)}
    nonce = uuid.uuid4().hex[:6].upper()
    report = {"schema_version": 1, "nonce": nonce, "results": [], "calls_attempted": 0,
              "claude_reserved_usd": "0", "claude_reported_usd": "0", "codex_cost_basis": "ChatGPT plan usage, not API dollars",
              "live_continuation": "in-progress", "cli_versions": {}, "budget_note": "CLI-reported costs, not an invoice; first-request spend can exceed a CLI threshold",
              "configuration": {"targets": targets, "cases": args.cases, "max_calls": args.max_calls,
                                "codex_model": args.codex_model, "claude_model": args.claude_model,
                                "claude_per_call_usd": str(budget), "total_reservation_usd": str(total)},
              "adapter_sha256": {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                  for name in ("csl_canonical.py", "csl_content.py", "csl_media.py", "csl_media_worker.py", "csl_conversion.py", "csl_live.py", "csl_live_fixtures.py")}}
    report_path = root / "report.json"
    write_report(report_path, report, env)
    try:
        specs = [text_case(nonce)]
        if args.cases in ("all", "media"):
            specs.append(media_case(nonce))
        if args.cases == "media":
            specs = specs[1:]
    except Exception as exc:
        report.update(live_continuation="incomplete", completed=0, passed=0, error=redact(str(exc), env))
        write_report(report_path, report, env)
        raise
    for target in targets:
        report["cli_versions"][target] = subprocess.run([target, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    csl_path = Path(__file__).with_name("codex_sessions.py")
    media_paths = []
    consecutive_failures = {target: 0 for target in targets}

    def convert(source, target, repair=False):
        command = [sys.executable, str(csl_path), "convert", str(source), "--to", target, "--cwd", str(project)]
        if repair:
            command.append("--repair-interrupted")
        result = subprocess.run(command, env=run_env, capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise ValueError("Synthetic conversion failed: " + redact(result.stderr, env)[-500:])
        sid = re.search(r"imported: ([0-9a-f-]+)", result.stdout).group(1)
        path = next((homes[target] / ("sessions" if target == "codex" else "projects")).rglob(f"*{sid}.jsonl"))
        return sid, path

    def attempt(name, source, target, spec, prompt=None, expected=None, baseline=False):
        if report["calls_attempted"] >= args.max_calls:
            raise ValueError("Live call budget exhausted; no additional calls launched")
        if target == "claude" and max(Decimal(report["claude_reserved_usd"]), Decimal(report["claude_reported_usd"])) + budget > total:
            raise ValueError("Claude reservation budget exhausted; no additional calls launched")
        sid, path = convert(source, target, spec.get("repair", False))
        before = canonical.normalize(target, path)
        original_assets = asset_digests(before)
        calls_before = sum(block["type"] == "tool_call" for event in before["events"] for block in event["blocks"])
        source_hash = hashlib.sha256(Path(source).read_bytes()).hexdigest()
        report["calls_attempted"] += 1
        if target == "claude":
            report["claude_reserved_usd"] = str(Decimal(report["claude_reserved_usd"]) + budget)
        write_report(report_path, report, env)
        result = live_call(target, sid, homes[target], project, env,
                           args.codex_model if target == "codex" else args.claude_model, budget, prompt or spec["question"])
        try:
            after = canonical.normalize(target, path)
            retained = asset_digests(after) == original_assets
            calls_after = sum(block["type"] == "tool_call" for event in after["events"] for block in event["blocks"])
            result["tool_calls"] += max(0, calls_after - calls_before)
            result["retained_assets"] = retained
        except (ValueError, KeyError, TypeError) as exc:
            result["retained_assets"] = False
            result["retention_error"] = str(exc)
        result["source_unchanged"] = source_hash == hashlib.sha256(Path(source).read_bytes()).hexdigest()
        try:
            result["missing_fields"] = [] if baseline and result["answer"].strip() == "RECORDED" else checks(result["answer"], expected or spec["expected"])
        except ValueError:
            result["missing_fields"] = ["invalid-json-answer"]
        result["forbidden_present"] = any(marker in result["answer"] for marker in spec.get("forbidden", []))
        result["passed"] = not (result["provider_failure"] or result["tool_calls"] or result["missing_fields"] or result["forbidden_present"] or not result["retained_assets"] or not result["source_unchanged"])
        result.update(name=name, target=target, session_id=sid)
        if result.get("reported_cost_usd") is not None:
            report["claude_reported_usd"] = str(Decimal(report["claude_reported_usd"]) + Decimal(str(result["reported_cost_usd"])))
        report["results"].append(result)
        write_report(report_path, report, env)
        print(json.dumps({"case": name, "passed": result["passed"], "provider_failure": result["provider_failure"],
                          "missing_fields": result["missing_fields"], "asset_retention": result["retained_assets"]}), flush=True)
        consecutive_failures[target] = consecutive_failures[target] + 1 if result["provider_failure"] else 0
        if consecutive_failures[target] >= 2:
            raise ValueError(f"{target} failed twice consecutively; remaining calls were not launched")
        return path

    try:
        for spec in specs:
            for source_provider in ("codex", "claude"):
                seed = root / f"seed-{spec['name']}-{source_provider}.jsonl"
                writer = canonical.restore_codex if source_provider == "codex" else canonical.restore_claude
                writer(spec["data"], seed, project)
                if spec["name"] == "text" and source_provider in targets:
                    seed = attempt(f"origin-{source_provider}", seed, source_provider, spec,
                        prompt="Do not use tools or restate any values. Reply exactly RECORDED to acknowledge the installed historical record.", baseline=True)
                for target in targets:
                    continuation = "ROUND-" + uuid.uuid4().hex[:6].upper()
                    prompt = spec["question"]
                    expected = dict(spec["expected"])
                    if spec["name"] == "media":
                        prompt += f" Retain continuation tag {continuation} for later; include it as continuation_tag in the JSON."
                        expected["continuation_tag"] = continuation
                    path = attempt(f"{spec['name']}-{source_provider}-to-{target}", seed, target, spec, prompt, expected)
                    if spec["name"] == "media":
                        media_paths.append((source_provider, target, path, spec, expected))
        if args.cases != "media":
            for spec in special_cases(nonce):
                seed = root / f"seed-{spec['name']}.jsonl"
                sid, stamp = str(uuid.uuid4()), canonical.now()
                records = spec["records"]
                if spec["source"] == "codex":
                    records = [{"type": "session_meta", "payload": {"id": sid, "timestamp": stamp, "cwd": str(project),
                        "originator": "csl_live_synthetic", "cli_version": "0.166.0", "source": "cli", "model_provider": "openai"}}] + records
                else:
                    records = [{**record, "sessionId": sid, "cwd": str(project), "version": "2.1.308", "isSidechain": False} for record in records]
                canonical.write_jsonl(seed, [{"timestamp": stamp, **record} for record in records])
                spec["question"] = question(spec["expected"])
                for target in targets:
                    attempt(f"{spec['name']}-to-{target}", seed, target, spec)
        for source_provider, previous, path, spec, expected in media_paths:
            target = previous if source_provider == previous else source_provider
            if target not in targets:
                continue
            prompt = spec["question"] + " Also return the previously retained continuation_tag."
            attempt(f"roundtrip-{source_provider}-{previous}-{target}", path, target, spec, prompt, expected)
        report["live_continuation"] = "passed" if all(result["passed"] for result in report["results"]) else "failed"
    except Exception as exc:
        report["live_continuation"] = "incomplete"
        report["error"] = redact(str(exc), env)
    finally:
        report["passed"] = sum(result["passed"] for result in report["results"])
        report["completed"] = len(report["results"])
        write_report(report_path, report, env)
    print(json.dumps({key: report[key] for key in ("live_continuation", "calls_attempted", "completed", "passed", "claude_reported_usd", "claude_reserved_usd")}), flush=True)
    if report["live_continuation"] != "passed":
        raise ValueError("Live suite did not pass completely; inspect the private report (no automatic retries)")
    return report


def add_commands(subparsers, csl):
    parser = subparsers.add_parser("verify-live", help="Opt-in paid synthetic continuation battery in isolated homes; never tests private session inputs.")
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--codex-home", type=Path, help="Separately authenticated ChatGPT test home; normal ~/.codex is rejected.")
    parser.add_argument("--targets", choices=("codex", "claude", "both"), default="both")
    parser.add_argument("--cases", choices=("text", "media", "all"), default="text")
    parser.add_argument("--max-calls", type=int, choices=range(1, 25), default=24)
    def money(value):
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("Budget must be a decimal amount") from exc
    parser.add_argument("--claude-budget-usd", type=money, default=Decimal("0.05"))
    parser.add_argument("--total-budget-usd", type=money, default=Decimal("0.60"))
    parser.add_argument("--codex-model", default="gpt-6.1-sol")
    parser.add_argument("--claude-model", default="claude-haiku-5-5")
    parser.add_argument("--out", type=Path, required=True, help="New private run directory; never overwritten.")
    def execute(args):
        try:
            return run(args, csl)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            print(f"csl verify-live: {redact(str(exc), os.environ)}", file=sys.stderr)
            raise SystemExit(2) from None
    parser.set_defaults(func=execute)
