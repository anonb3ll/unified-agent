"""Task-space (``ts``) chat worker — Discord/Telegram ingestion boundary.

Package layout mirrors :mod:`unified_agent.bug_worker`. The worker itself lives
in :mod:`unified_agent.ts_worker.worker`; the HTTP transport adapters live in
:mod:`unified_agent.ts_worker.transport`. Nothing here imports a Discord or
Telegram SDK — both surfaces are plain REST over ``httpx``, and tests run
against an in-process stub transport so no live connection is ever opened.
"""
