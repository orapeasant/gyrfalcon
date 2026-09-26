"""`gyrfalcon sessions` — inspect PostgreSQL session storage."""

from __future__ import annotations

from typing import Optional

from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("sessions_cmd")


def run_sessions_cli(action: str = "status", session_id: Optional[str] = None) -> None:
    from gyrfalcon.gyrfalcon_constants import get_run_mode
    from gyrfalcon.sessions import get_session_store

    store = get_session_store()
    print("\nSession storage")
    print(f"  run mode       : {get_run_mode()}")
    print(f"  backend        : {store.backend}")
    print(f"  schema version : {store.schema_version}")
    print(f"  full-text index: "
          f"{'yes' if store.dialect.supports_fulltext else 'no (LIKE fallback)'}")

    if session_id:
        rows = store.get_usage(session_id)
        print(f"\n  {len(rows)} recorded calls for {session_id}")
        total = 0.0
        for row in rows:
            total += row["cost_usd"] or 0.0
            print(f"    #{row['seq']:<3} {row['model'] or '?':24s} "
                  f"in={row['uncached_input_tokens']:<7} "
                  f"read={row['cache_read_tokens']:<7} "
                  f"write={row['cache_write_tokens']:<6} "
                  f"out={row['output_tokens']:<6} ${row['cost_usd']:.6f}")
        if rows:
            print(f"    {'total':29s} ${total:.6f}")
    else:
        by_model = store.usage_by_model()
        if by_model:
            print("\n  Spend by model (last 30 days)")
            for row in by_model:
                print(f"    {row['model'] or '?':28s} {row['calls']:>5} calls  "
                      f"${row['cost_usd'] or 0:.6f}")
        else:
            print("\n  No per-call usage recorded yet.")
        print("\n  Pass --session <id> for one session's call history.")
        print()
