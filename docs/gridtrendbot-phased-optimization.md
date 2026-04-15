# GridTrendBot Phased Optimization

This workflow assumes:

- `EURUSD`
- `m5`
- exported GridTrendBot defaults as the baseline
- ranking by robustness, not raw net profit
- `MAX_PARALLEL_JOBS=1` on the current `6 vCPU / 6 GB RAM` VPS during heavy optimization

## Current Status

As of `04/04/2026`, the live workflow state is:

- Phase 0 benchmark complete. The resynced benchmark runs favored `parallel_workers: 7`:
  - `w5` job `24a37562-29e1-402a-a682-2b75aac906ba` finished in `254.34s`
  - `w6` job `45c33cd2-fc13-408c-a7fd-c09d14cab724` finished in `245.98s`
  - `w7` job `d0bdd7f6-c99d-4658-8514-8cb9d4b6dedd` finished in `223.32s`
- Phase 1 baseline complete:
  - in-sample job `092b4009-800c-45c1-aa78-224280859ac8` ended at `profit_factor 0.64`, `average_trade -0.24`, `total_trades 1239`
  - out-of-sample job `4c98d683-11ea-41ca-b17a-8617560b819d` ended at `profit_factor 0.75`, `average_trade -0.17`, `total_trades 158`
- Phase 2 reduced structure calibration is complete:
  - run 1: `ecf3f50a-37b6-4e5a-875a-ce333475e53f`
  - run 2: `77eae6b4-f37a-4e83-b13d-42ec7bbfacce`
  - run 3: `4adb6d9f-ff44-472c-bbc1-6abdab32abd7`
  - confirmation in-sample job: `14fe74af-a96c-41d4-ba54-de197b574d61`
  - confirmation OOS job: `a0d952a0-7948-46a0-9044-201cd395d7f0`
  - best in-sample confirmation candidate was `GridSizePips 36`, `EntryMode 1`, `TrailingMode 0`, `profit_factor 0.90`, `max_drawdown_pct 0.9921`, `average_trade -0.17`, `total_trades 258`
  - best OOS confirmation candidate was `GridSizePips 34`, `EntryMode 0`, `TrailingMode 0`, `profit_factor 0.97`, `max_drawdown_pct 0.3445`, `average_trade -0.06`, `total_trades 39`
  - decision: Phase 2 improved the structure materially but did not produce a promotable edge because the confirmation set remained below `profit_factor 1.0` with negative `average_trade` in both windows
- Phase 3 market filters is complete as a provisional branch from the most OOS-stable Phase 2 structure:
  - filter genetic job: `b795166c-4dc8-4b97-9f3b-95c6b816e923`
  - anchor config: `GridSizePips 34`, `EntryMode 0`, `TrailingMode 0`
  - best filter-tuning candidate was `AdxThresholdLow 28`, `AdxThresholdHigh 40`, `SessionStartHour 7`, `SessionEndHour 22`, with `profit_factor 0.84`, `max_drawdown_pct 1.0129`, `average_trade -0.26`, `total_trades 272`
  - explicit boolean A/B reruns superseded the earlier numeric toggle checks:
    - `EnableAdxFilter`: `f58c7a7f-8323-4301-af8e-fec41f899c2d` (`true`) beat `3c4683c6-797b-49c9-96d9-51cf01da0d3d` (`false`) with `profit_factor 0.84` vs `0.69`, `average_trade -0.26` vs `-0.56`, `max_drawdown_pct 1.0129` vs `2.7524`
    - `EnableSessionFilter`: `23b1a58e-f5f5-4d08-876f-0284cb526c47` (`true`) beat `ce0cef8d-7868-4a69-bf07-1bbb94525f4e` (`false`) with `profit_factor 0.84` vs `0.81`, `average_trade -0.26` vs `-0.31`, `max_drawdown_pct 1.0129` vs `1.2327`
    - `EnableAtrFilter`: `af590c86-2d3e-4ba1-bec4-e119e964ad70` (`false`) beat `fbbacec2-7674-4bce-ab88-20779043f803` (`true`) on trade quality with `profit_factor 0.84` vs `0.81` and `average_trade -0.26` vs `-0.31`, although ATR-on reduced `max_drawdown_pct` from `1.0129` to `0.7634`
  - decision: the corrected Phase 3 winner is `EnableAdxFilter=true`, `EnableSessionFilter=true`, `EnableAtrFilter=false`, but it still failed to beat the broader Phase 2 branch and remains non-promotable, so Phase 4 should stay closed

## Phase 0: Hardware Benchmark

Use [phase1_benchmark.yaml](../examples/gridtrendbot/phase1_benchmark.yaml) as the template and run it three times with:

- `parallel_workers: 5`
- `parallel_workers: 6`
- `parallel_workers: 7`

Keep `MAX_PARALLEL_JOBS=1` on the VPS while benchmarking. Record:

- total runtime
- failed-pass count
- peak active `ctrader-console` containers
- CPU / RAM snapshots

Choose the highest stable worker count that is at least 8% faster than the next lower setting. The current live winner is `parallel_workers: 7`, so the heavy optimization templates now use that value.

This phase measures infrastructure, not strategy edge. The recent VPS benchmark showed the worker model is healthy, but it did not prove the strategy already has a profitable plateau.

## Phase 1: Baseline

Run the baseline unchanged with:

- [phase2_baseline_insample.yaml](../examples/gridtrendbot/phase2_baseline_insample.yaml)
- [phase2_baseline_oos.yaml](../examples/gridtrendbot/phase2_baseline_oos.yaml)

Stop if:

- in-sample `total_trades < 100`
- out-of-sample `total_trades < 20`

Use this phase to answer one question first: does the exported default bot show any real edge before we start tuning? If both windows remain weak, proceed to a reduced structure phase before touching more filters.

## Phase 2: Reduced Structure Calibration

Start with [phase3_core_genetic.yaml](../examples/gridtrendbot/phase3_core_genetic.yaml).

Only tune:

- `GridSizePips`
- `EntryMode`
- `TrailingMode`

Keep `LevelsAboveBelow` fixed at the exported default unless a later run proves it materially changes results. In the current benchmark it behaved as an inert parameter.

Run three independent genetic jobs. Narrow the ranges to the overlap seen in at least two runs, then run a confirmation grid inside that overlap.

Promote a structure winner only if it improves in-sample ranking and remains acceptable out-of-sample. If the narrowed confirmation grid still fails that test but the OOS result is near breakeven, it is reasonable to open a provisional filter branch from the most OOS-stable structure rather than declaring a full promotion.

## Phase 3: Market Filters

Before Phase 3, copy the frozen winner from Phase 2 into the `fixed_params` block of [phase4_filters_genetic.yaml](../examples/gridtrendbot/phase4_filters_genetic.yaml).

Then tune:

- `AdxThresholdLow`
- `AdxThresholdHigh`
- `SessionStartHour`
- `SessionEndHour`

Maintain the rule `AdxThresholdHigh > AdxThresholdLow`. The bot auto-corrects invalid values on startup, so invalid pairs should be avoided in optimization inputs.

Run ATR as a gated filter, not as an automatic optimization target:

- first do an `EnableAtrFilter` off vs on A/B check using the exported ATR defaults
- only if ATR-on wins, tune `AtrPeriod` and `AtrMaPeriod`

After the filter tuning round, run A/B checks on:

- `EnableAtrFilter`
- `EnableAdxFilter`
- `EnableSessionFilter`

Run boolean A/B checks as separate single-pass configs with explicit `true` / `false` values in `fixed_params`. Do not encode boolean toggles as numeric optimization ranges such as `0` / `1`.

Promote the filter winner only if it improves out-of-sample trade quality, not just in-sample ranking.

## Phase 4: Execution And Profit Extraction

Only open this phase after a structure or filter winner has shown an out-of-sample edge. The current GridTrendBot workflow has not met that bar, so Phase 4 should remain closed unless the search space or validation design changes materially.

Tune small execution controls that affect trade handling more than raw signal creation:

- `CooldownBars`
- `MaxSpreadPips`

Test mid-line logic later and in isolation:

- `EnableMidLines`
- `EnableMidLinePartialTP`
- `PartialTPPercent`
- `MidLineMomentumGate`

## Phase 5: Robustness Validation

Run the final candidate through:

- out-of-sample validation
- multi-period validation
- trending / ranging / high-volatility regime checks

Only after a candidate survives Phase 5 should you open later branches such as:

- grid branch
- ATR volatility branch
- MA bias branch
- risk / deployment profile

Keep `fixed_params` as the frozen baseline for every later phase so each job only changes the intended subset.
