# Flow engine — acceptance suite

Executable acceptance criteria for `docs/spec/gyrfalcon/15-flow.md`, written
before the engine exists.

## How it works

Each module declares the symbols it needs:

```python
pytestmark = requires("gyrfalcon.flow.states:State", section="§1.3, §2.1")
```

Missing symbols skip the module with a reason naming both the symbol and the spec
section. The suite is therefore green today and **turns itself on** as each phase
lands — no editing tests to enable them.

```bash
uv run --with pytest python -m pytest tests/flow/ -q        # all
uv run --with pytest python -m pytest tests/flow/ -q -rs    # with skip reasons
uv run --with pytest python -m pytest tests/flow/test_orchestration.py -q
```

`_spec.py` holds `requires()` / `sym()`; `conftest.py` holds fixtures
(`recorder`, `flaky`, `clock`). `sym()` resolves `module:attr` at call time so
collection never hard-fails on absent modules.

## Coverage by build phase

Phases are from §13.5. Ship them in order; each row's tests should go green as
that phase completes.

| Phase | Spec | Module | Cases |
|---|---|---|---|
| 1 — state model | §1.3, §2.1–2.3 | `test_state_model.py` | 51 |
| 2–3 — engine, retries, timeouts, hooks | §1.1–1.2, §3.1–3.5 | `test_templates_and_engine.py` | 18 |
| 4–5 — orchestration boundary | §4.1–4.4 | `test_orchestration.py` | 19 |
| 6–7 — futures, map, cache, transactions | §5, §6 | `test_results_cache_transactions.py` | 20 |
| 8–9 — pause/suspend, agent policy | §7, §8, §13.3–13.4 | `test_pause_and_agent.py` | 24 |

## What each module pins down

**`test_state_model.py`** — the closed-enum/open-name split that everything else
rests on. Nine types, explicit terminal set, and all 20 documented state names
mapped to their types. The distinctions §2.3 calls load-bearing get their own
tests: `Failed` vs `Crashed`, `Paused` vs `Suspended` (discriminated by
`pause_reschedule`), `Cancelling` vs `Cancelled`. Also asserts agentic vocabulary
(`Deliberating`, `AwaitingTool`) needs no enum change, and that `StateDetails`
accepts new fields without a `State` migration.

**`test_templates_and_engine.py`** — templates carry no run state (asserted under
16 concurrent runs), `with_options()` copies rather than mutates, and hooks
register both as decorator kwargs *and* decorator methods (the §13.2 papercut).
For the engine: the `NotSet` sentinel distinguishing "returned None" from "hasn't
returned"; retry as a state transition rather than recursion; the delay list as a
backoff schedule whose last value repeats; delayed retry as a `SCHEDULED` state
with a future `scheduled_time` rather than `sleep()`; `retry_condition_fn` veto;
`TimedOut` distinguishable from a user `TimeoutError`; and a raising hook being
logged and swallowed without corrupting run state.

**`test_orchestration.py`** — the four-way protocol: ACCEPT adopts server
identity, REJECT substitutes the server's state, REJECT-with-`PAUSED` raises
`Pause`, ABORT raises and does *not* re-propose, WAIT sleeps and re-proposes.
Rules gate on `FROM_STATES`/`TO_STATES`, expose the four verbs, nest
before-outside-in / after-inside-out, and `cleanup` releases a slot when an inner
rule aborts. Policies are ordered data, with `CacheRetrieval` before
`SecureTaskConcurrencySlots` — the ordering that encodes a product decision.

**`test_results_cache_transactions.py`** — futures identified by run id and
rehydratable from it; `unmapped`/`allow_failure` at the argument position;
`MappingMissingIterable`; implicit edges from passing a future vs explicit
`wait_for`; blocked (`Pending(name="NotReady")`) distinct from broken. Cache
policies compose with `+`, `DEFAULT` includes source hash so editing a function
invalidates its cache, a raising policy degrades to no-caching, and a cache policy
forces `persist_result`. Transactions: nested lookup falls through to parent, a
later failure fires the earlier step's `on_rollback`, and `RolledBack` is
`COMPLETED`-typed.

**`test_pause_and_agent.py`** — context round-trips across a process boundary and
is marked `detached`; `get_run_context()` raises outside a run. Pause gates:
`RunInput` renders as a schema, `pause_key` makes a re-entered pause idempotent,
`pause_timeout` is on the state, resume carries typed input back. Then the §13.3
boundary — an agent step is a flow run, one model call is one task, output
crosses as a validated schema or fails as an ordinary retryable task — and all six
§13.4 rules, each asserted to be an ordinary `BaseOrchestrationRule` subclass,
which is the spec's own proof that they need zero engine changes.

## Deliberate gaps

Not covered here, and why:

- **§9 deployments/workers/runners** — needs process and infrastructure fixtures;
  belongs in an integration suite, not this unit-level one.
- **§10 events/automations, §11 API surface** — no contract to test until the
  endpoints exist. Add an API suite at phase 10 covering filter-by-POST,
  `/history` bucketing, and `set_state` as an action endpoint.
- **§12/§14 UI** — component and e2e tests belong with the frontend.
- **Crash recovery under real process death** — `Crashed` is asserted as a state,
  but genuinely killing a worker mid-run needs the integration harness.

## When a test and the spec disagree

The spec wins, and the test gets updated with a comment naming the section. These
tests encode a reading of the document; where that reading is wrong, fix it here
rather than bending the implementation to match.
