# app/llm/__init__.py
"""The LLM layer: send a prompt to a model and get back JSON we can trust.

    types.py          LlmRequest, LlmResult and the error types
    validation.py     turn the model's text into a JSON object that matches the schema
    providers.py      OpenAI-compatible provider (NVIDIA NIM, Groq, Azure AI Foundry)
    fake.py           scripted provider for tests and key-less demos
    fallback.py       primary -> secondary provider when the primary is unavailable
    factory.py        build the configured provider from settings (.env)
    prompts.py        load the active prompt version and fill its template
    runs.py           record every call in ai_workflow_runs

Callers (the Phase 9 worker) depend only on the LlmProvider interface; which
provider and model answer is decided by settings.
"""
