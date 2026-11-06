"""Offline gateway benchmark. No real accounts, network or upstream quota used.

python scripts/benchmark_gateway.py --reference 70ba58b --rows 15000
Reference functions are AST-extracted from git, never imported as an old server.
"""
import argparse
import ast
import copy
import io
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def measure(fn, rounds=9):
    values = []
    for _ in range(rounds):
        start = time.perf_counter_ns()
        fn()
        values.append((time.perf_counter_ns() - start) / 1e6)
    return round(statistics.median(values), 4)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", default="70ba58b")
    parser.add_argument("--rows", type=int, default=15000)
    parser.add_argument("--output")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="qd-benchmark-") as directory:
        os.environ["ACCOUNTS_DIR"] = directory
        os.environ["QD_PROXY_USAGE_DIR"] = directory
        import qoder_proxy as P
        reference = subprocess.check_output(["git", "show", args.reference + ":qoder_proxy.py"], cwd=ROOT).decode("utf-8")
        functions = {"has_fingerprint", "sanitize_text", "sanitize_content", "sanitize_tool_calls",
                     "sanitize_messages", "flatten_messages", "build_qoder_body", "open_upstream",
                     "_sse_recover_dsml", "_sse_recover_tool_tags"}
        nodes = [n for n in ast.parse(reference).body if isinstance(n, ast.FunctionDef) and n.name in functions]
        namespace = dict(vars(P))
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "<reference functions>", "exec"), namespace)
        account = P.qoder_accounts.Account({"uid": "benchmark", "realm": "cn", "accessToken": "FAKE"})
        account.enabled = True
        account.expires_at = 0
        text = "汉字 abcdef123456 " * 16000
        payload = {"model": "Qwen3.8-Flash", "messages": [
            {"role": "system", "content": "synthetic system"},
            {"role": "user", "content": "synthetic task"}] + [
                {"role": "user" if i % 2 == 0 else "assistant", "content": text[:20000]}
                for i in range(40)]}
        # Warm local catalog and caches equally, never call upstream.
        P.build_qoder_body(copy.deepcopy(payload), account, "qfmodel", "cn")
        results = []
        def compare(name, old, new):
            old_ms = measure(old)
            new_ms = measure(new)
            results.append({"name": name, "before_ms": old_ms, "after_ms": new_ms,
                            "speedup": round(old_ms / new_ms, 2) if new_ms else None})
        compare("sanitize_clean_240k", lambda: namespace["sanitize_text"](text), lambda: P.sanitize_text(text))
        compare("build_40_message_history", lambda: namespace["build_qoder_body"](payload, account, "qfmodel", "cn"),
                lambda: P.build_qoder_body(payload, account, "qfmodel", "cn"))
        frame = ("data: " + json.dumps({"id": "benchmark", "model": "fake", "choices": [
            {"index": 0, "delta": {"content": "普通文本 chunk no protocol"}, "finish_reason": None}],
            "extension": {"preserve": "value"}}) + "\n\n").encode()
        frames = [frame] * 1000
        compare("dsml_1000_text_frames", lambda: list(namespace["_sse_recover_dsml"](iter(frames), {"tool"})),
                lambda: list(P._sse_recover_dsml(iter(frames), {"tool"})))
        compare("tool_tag_1000_text_frames", lambda: list(namespace["_sse_recover_tool_tags"](iter(frames), {"tool"})),
                lambda: list(P._sse_recover_tool_tags(iter(frames), {"tool"})))
        source = Path(directory) / "usage.jsonl"
        now = time.time()
        with source.open("w", encoding="utf-8") as out:
            for i in range(max(1, args.rows)):
                out.write(json.dumps({"at": now - i, "model": "Qwen3.8-Flash", "account": "fake-%d" % (i % 4),
                    "realm": "cn", "prompt_tokens": 1000, "cached_tokens": 900, "completion_tokens": 100,
                    "total_tokens": 1100, "credit": 1.2}) + "\n")
        P.USAGE_LOG = str(source)
        P.POOL = None
        for kind, compute in (("stats", P.compute_usage_stats), ("analytics", P.compute_usage_analytics)):
            cold = measure(lambda: compute(range="all"), rounds=5)
            with mock.patch.object(P.time, "time", return_value=now):
                initial = P.cached_usage_report(kind, range="all")
                assert initial == compute(range="all"), "cached report changes semantics"
            warm = measure(lambda: P.cached_usage_report(kind, range="all"))
            results.append({"name": kind + "_unchanged_log_poll", "before_ms": cold,
                            "after_ms": warm, "speedup": round(cold / warm, 2)})
        report = {"reference": args.reference, "python": platform.python_version(), "platform": platform.platform(),
                  "rows": args.rows, "method": "local synthetic data; median; dashboard hits only for unchanged log; no upstream latency claim",
                  "results": results}
        encoded = json.dumps(report, ensure_ascii=False, indent=2)
        print(encoded)
        if args.output:
            Path(args.output).write_text(encoded + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
