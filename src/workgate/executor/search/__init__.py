"""Executor-owned Search implementation package.

Public control tools are routed to composed executor services.  Domain code lives
in :mod:`workgate.executor.search.service`, :mod:`.composition`, and :mod:`.core`;
there is intentionally no ambient/sessionless compatibility facade here.
"""
