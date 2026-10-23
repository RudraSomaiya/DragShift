"""Conversion engine: input kinds, the Op registry, and the worker-pool job runner (PRD §9.4).

Everything importable from the UI process stays lean (CONTRIBUTING.md rule 2). Engine libraries are
imported inside worker functions only.
"""
