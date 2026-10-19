# app/services/__init__.py
"""Business services: the only code that writes business tables.

Services receive a session and an ActorContext, raise app.core.errors
exceptions (never HTTP errors), and are called by routes, the worker and the seed.
"""
