# app/processing/__init__.py
"""Network event processing: raw Aruba events -> network_events -> current status.

aruba.py      turns one raw Aruba payload into a flat, typed ExtractedEvent
matching.py   finds the network element an event belongs to
status.py     decides which events change an element's current status
processor.py  processes one raw event (dedupe, store, extract, match, insert, apply)
runner.py     a whole run: lock, run record, rebuild/incremental, batches
"""
