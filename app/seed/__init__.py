# app/seed/__init__.py
"""Seed data for the RIC POC: master data, inventory, Aruba events and prompts.

Data lives in YAML files under seed/data. Pure generators turn those files into
rows; thin writers upsert the rows inside a session they are given.
"""
