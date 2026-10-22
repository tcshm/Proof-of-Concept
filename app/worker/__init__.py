# app/worker/__init__.py
"""The change worker: turns pending incident journal entries into store alerts,
actions and notifications with the two LLM workflows.

    rules.py           pure decisions: which workflow (or skip), answer checks,
                       template alert, due times, retry delays
    queue.py           claim / release / finish journal entries
    context.py         the context sent to the model for one entry
    runner.py          the worker loop: pacing, calls, applying results, retries,
                       action expiry sweep, drain

Run it with: uv run python -m scripts.worker  (--drain to empty the queue and stop)
"""
