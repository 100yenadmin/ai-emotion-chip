"""bench-probe: a harness-only Hermes plugin that appends one JSONL row per API request and response.

The runner sets BENCH_PROBE_OUT (the item's probe file) and BENCH_PROBE_ITEM (the item dir, normalised out of
the prompt hash). Never shipped with Hermes Emotion Chip.
"""
import json
import os

try:
    from .probe import request_row, response_row
except ImportError:  # flat import in tests
    from probe import request_row, response_row


def _append(row):
    path = os.environ.get("BENCH_PROBE_OUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def pre_api_request(**kwargs):
    _append(request_row(kwargs, os.environ.get("BENCH_PROBE_ITEM")))


def post_api_request(**kwargs):
    _append(response_row(kwargs))


def register(ctx):
    ctx.register_hook("pre_api_request", pre_api_request)
    ctx.register_hook("post_api_request", post_api_request)
