"""Concurrency load test for the local inference stack.

Measures what single-request timing cannot: how time-to-first-token, throughput
and queue depth behave as concurrent requests rise past `--max-num-seqs`.

Run against LiteLLM rather than vLLM directly, so the numbers include the
gateway hop the application actually pays.

    uv run python scripts/load_test.py
    uv run python scripts/load_test.py --model order-tracking-model --levels 1,4,8

Uses the master key by default: the application key is deliberately capped at 8
parallel requests, which would throttle the test rather than the engine. Pass
--key to measure the application's own limits instead.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time

import httpx


PROMPT = (
    "A customer asks about the status of order ORD-031. Decide which support "
    "specialist should handle it and explain your choice in one sentence."
)


async def one_request(client: httpx.AsyncClient, url: str, key: str,
                      model: str, max_tokens: int) -> dict:
    """Stream one completion, timing first token and total."""
    started = time.perf_counter()
    first_token: float | None = None
    tokens = 0
    try:
        async with client.stream(
            "POST",
            f"{url}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": PROMPT}],
                "max_tokens": max_tokens,
                "temperature": 0,
                "stream": True,
            },
        ) as response:
            if response.status_code != 200:
                body = (await response.aread())[:120].decode("utf-8", "replace")
                return {"ok": False, "status": response.status_code, "error": body}
            async for line in response.aiter_lines():
                if not line.startswith("data:") or line.endswith("[DONE]"):
                    continue
                try:
                    chunk = json.loads(line[5:])
                except ValueError:
                    continue
                delta = (chunk.get("choices") or [{}])[0].get("delta", {}).get("content")
                if delta:
                    tokens += 1
                    if first_token is None:
                        first_token = time.perf_counter() - started
    except Exception as error:  # noqa: BLE001 - a failed request is a result
        return {"ok": False, "status": 0, "error": type(error).__name__}

    total = time.perf_counter() - started
    return {"ok": True, "ttft": first_token or total, "total": total, "tokens": tokens}


async def gauge(client: httpx.AsyncClient, engine: str, metric: str) -> float:
    """Read one gauge straight from an engine, for queue depth and cache use.

    `engine` is a base URL. Cluster DNS is not resolvable from a host running
    this script, so in practice that means a port-forward:
        kubectl port-forward -n agentops svc/vllm-qwen-agent 8001:8000
    """
    url = f"{engine.rstrip('/')}/metrics"
    try:
        text = (await client.get(url)).text
    except Exception:
        return -1.0
    for line in text.splitlines():
        if line.startswith(f"{metric}{{") or line.startswith(f"{metric} "):
            try:
                return float(line.rsplit(" ", 1)[1])
            except ValueError:
                return -1.0
    return -1.0


async def run_level(url: str, key: str, model: str, level: int,
                    max_tokens: int, engine: str | None) -> dict:
    """Fire `level` requests at once and summarise the batch."""
    limits = httpx.Limits(max_connections=level + 4, max_keepalive_connections=level + 4)
    async with httpx.AsyncClient(timeout=300, limits=limits) as client:
        started = time.perf_counter()
        stop = asyncio.Event()
        sampler = asyncio.create_task(sample_engine(engine, stop)) if engine else None
        results = await asyncio.gather(
            *(one_request(client, url, key, model, max_tokens) for _ in range(level))
        )
        wall = time.perf_counter() - started
        peaks = {}
        if sampler:
            # Stop by flag rather than cancellation: cancelling mid-request
            # leaves the HTTP client unwinding and the await never returns.
            stop.set()
            peaks = await sampler

    ok = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]
    if not ok:
        return {"level": level, "ok": 0, "failed": len(failed),
                "errors": {r.get("status"): r.get("error", "")[:60] for r in failed}}

    ttfts = sorted(r["ttft"] for r in ok)
    totals = sorted(r["total"] for r in ok)
    tokens = sum(r["tokens"] for r in ok)
    return {
        "level": level,
        "ok": len(ok),
        "failed": len(failed),
        "ttft_p50": statistics.median(ttfts),
        "ttft_p95": ttfts[int(len(ttfts) * 0.95) - 1] if len(ttfts) > 1 else ttfts[0],
        "total_p50": statistics.median(totals),
        "wall": wall,
        "tokens": tokens,
        "throughput": tokens / wall if wall else 0,
        "peak_waiting": peaks.get("waiting", -1),
        "peak_cache": peaks.get("cache", -1),
        "errors": {r.get("status") for r in failed} if failed else None,
    }


async def sample_engine(engine: str, stop: asyncio.Event) -> dict:
    """Poll the engine while a batch runs, keeping the peaks."""
    peaks = {"waiting": 0.0, "cache": 0.0}
    async with httpx.AsyncClient(timeout=5) as client:
        while not stop.is_set():
            waiting = await gauge(client, engine, "vllm:num_requests_waiting")
            cache = await gauge(client, engine, "vllm:kv_cache_usage_perc")
            peaks["waiting"] = max(peaks["waiting"], waiting)
            peaks["cache"] = max(peaks["cache"], cache)
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass
    return peaks


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.getenv("LLM_BASE_URL", "http://litellm.agentops.local/v1"))
    parser.add_argument("--key", default=os.getenv("LOAD_TEST_KEY", os.getenv("LLM_API_KEY", "")))
    parser.add_argument("--model", default="supervisor-model")
    parser.add_argument("--levels", default="1,2,4,8,16")
    parser.add_argument("--max-tokens", type=int, default=64)
    parser.add_argument("--engine", default=None,
                        help="base URL of a vLLM engine to poll, e.g. http://127.0.0.1:8001")
    args = parser.parse_args()

    if not args.key:
        raise SystemExit("no API key: pass --key or set LLM_API_KEY")

    levels = [int(x) for x in args.levels.split(",")]
    print(f"model={args.model}  url={args.url}  max_tokens={args.max_tokens}", flush=True)
    print()
    print("  conc   ok/fail   TTFT p50   TTFT p95   total p50   tok/s   peak-wait  peak-KV", flush=True)
    print("  " + "-" * 76, flush=True)

    for level in levels:
        row = await run_level(args.url, args.key, args.model, level,
                              args.max_tokens, args.engine)
        if not row.get("ok"):
            print(f"  {level:>4}   {row['ok']}/{row['failed']}      all failed: {row.get('errors')}", flush=True)
            continue
        print(
            f"  {row['level']:>4}   {row['ok']}/{row['failed']}"
            f"      {row['ttft_p50']:>7.2f}s   {row['ttft_p95']:>7.2f}s"
            f"   {row['total_p50']:>8.2f}s"
            f"   {row['throughput']:>6.1f}"
            f"   {row['peak_waiting']:>8.0f}"
            f"   {row['peak_cache']:>6.1%}",
            flush=True,
        )
        # let the engine drain so the next level starts from idle
        await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())
