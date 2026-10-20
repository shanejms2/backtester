# Developer guide

Python 3.12. Format and checks:

```bash
ruff check src tests
ruff format src tests
mypy
pytest
```

Tests live in `tests/`. They load JSON fixtures. Do not point a test at a live vendor or at a real `.env` key. Chart tests pass `open_browser=False` or `--no-open`.

Style already in the tree: absolute imports, Google-style docstrings on public functions, pydantic models with `extra="forbid"` on Spec and strategy params, `BacktesterError` for failures a user should see. Match the surrounding file.

## Trace a strategy run

1. `cli._cmd_strat_run` → `_strat_execute`.
2. `strategy.spec.load_spec` validates the YAML.
3. `strategy.engine.run_spec` loads plugins, builds one Panel, and loops Runs.
4. `strategy.panel.load_panel` reads Coverage through `BarReader` and aligns the clock.
5. `Strategy.targets` returns `ts_utc`, `instrument_id`, `weight`.
6. `strategy.broker.execute` shifts the weight by one bar and fills at the next open.
7. `compute_metrics` scores equity from `ready_bars`, then the engine realigns every Run to the slowest ready bar.

A fill bug belongs in `broker.py` and `tests/test_broker.py`. A signal bug belongs in the plugin. An indicator bug belongs in `ta/` and `tests/test_ta.py`.

## Add a strategy

1. Add `src/backtester/strategy/plugins/<name>.py`.
2. Subclass `Strategy`. Set `name` and `param_model` (pydantic, `extra="forbid"`).
3. Implement `ready_bars` (at least the longest indicator window) and `targets`. Use `backtester.ta` for indicators. Return null to hold and `0` to exit. Do not emit fills, fees, or equity.
4. Decorate the class with `@register("<name>")`.
5. Import the class from `strategy/plugins/__init__.py` so `strat list` sees it.
6. Add a focused test that builds a small panel and checks weights. If the broker is involved, assert that truncating the last bar does not change earlier fills.
7. Add the name to the expected list in `tests/test_strategy.py` and `tests/test_cli.py` if those tests assert the full registry.

A plugin that should not ship as a built-in can stay out of that package. Put its path in the Spec `plugins:` list (`a.module` or `path/to/file.py`).

Cross-section math stays in the plugin. The engine must not grow an `if strategy == "..."` branch.

## Add an indicator

1. Add a function in the matching `ta/` module (`trend`, `momentum`, `volatility`, `volume`, `structure`).
2. Rolling expressions go through `ta/_core.py` so they are `.over("instrument_id")`.
3. At bar `t`, use history through `t` only. Warm-up is null unless the function documents a seed (EMA).
4. Export it from `ta/__init__.py` and append the name to `CATALOG` in catalog order.
5. Extend `tests/test_ta.py`: hand values, null warm-up, no leak across `instrument_id`, and truncating the last bar leaves the previous value unchanged.

Do not return weights from `ta/`.

## Add a vendor adapter

1. Implement `SourceAdapter` in `adapters/`. Map the vendor payload to `VendorBar` or `PairSnapshot`. Leave timezone policy to `normalize`.
2. Register the source in `ingest/engine.py` `default_adapter`. A CCXT venue id already falls through to `CCXTVenueAdapter`.
3. Record a fixture under `tests/fixtures/` and drive the adapter with that JSON in `tests/test_adapters.py`.
4. If the vendor cannot serve a native timeframe (Yahoo and Alpha Vantage have no 4h), raise `NativeTimeframeError` rather than silently resampling into the store.

`_RANGE_VENDORS` in the ingest engine is the set of sources that accept one dated window. Session-day holes are collapsed only for that set, so a multi-year daily Yahoo request stays one download.

## Change the store

`ClickHouseStore` schema lives in `infra/clickhouse/init/01_schema.sql` and is mirrored in `ClickHouseStore._SCHEMA_SQL` for bootstrap. A new column needs:

- the `CREATE TABLE` / `ALTER` path (new databases and existing installs),
- a compatible read path for rows written before the column existed (`adj_close` is the pattern: null means `close`),
- a test in `tests/test_store.py` that round-trips the column (needs ClickHouse; skips if unavailable).

Do not hold work open across a vendor HTTP call. Fetch a page, normalize, then `write_batch`. Coverage updates after a successful write.

Bar writes are `INSERT` into `ReplacingMergeTree`. Coverage updates stay delete-and-insert for that `(instrument, timeframe, source)` triple.

## Change a metric or the stdout table

`Metrics` in `strategy/types.py` is the struct. `compute_metrics` fills it. `format_metrics_table` chooses the columns a human sees. Adding a column changes the table contract; update `tests/test_strategy.py` and `tests/test_sip.py` in the same change. SIP math is `strategy/sip.py`. Do not recompute SIP inside a plugin.

## Debugging

| Symptom | Look at |
| --- | --- |
| `error: ...` and exit 2 | The exception type in `exceptions.py` and the `raise` site |
| Second ingest still hits the network | `store.coverage` and whether the first call was truncated |
| Weekend bars missing for equities | Expected. `calendar/session.py` |
| Weekend bars missing for crypto | Unexpected. Coverage or the adapter page |
| Strategy and buy-and-hold start on different days | Compare realigns to the latest `ready_bars` |
| Fill on the signal bar | Broker must shift weight by one. See `tests/test_broker.py` |
| IST / EDT rejected | By design. Pass an IANA name |
| `strat compare` HTML | `$TMPDIR/strat-compare-<name>.html`. Tests use `--no-open` |
| PRISM internals | `diagnostics: true` writes `prism-diagnostics.parquet` and `.csv` under `$TMPDIR` |

`pytest tests/test_broker.py tests/test_clock.py -q` is the fast check for fill and decision-clock changes (no ClickHouse required for most of those). `pytest tests/test_ingest.py tests/test_store.py -q` covers Coverage and ClickHouse when a server is up.
