# app/queries/__init__.py
"""Read-side SQL for the API. One module per screen; every function takes the
store id and filters by it. A route needs one to three queries in total, never
one query per row. Functions return plain rows; routes map them to responses.
"""
