# DualEmaCrossoverBot Phased Optimization

This workflow assumes:

- `EURUSD`
- `t100`
- `ticks` backtesting data mode
- exported DualEmaCrossoverBot defaults as the baseline
- ranking by robustness, not raw net profit
- `MAX_PARALLEL_JOBS=1` on the current `6 vCPU / 6 GB RAM` VPS during heavy optimization
- risk controls stay frozen during research unless a later phase clearly proves they are the bottleneck

## Current Status

As of `05/04/2026`, the live workflow state is:

- Strategy files are present in the repo at `/cbot_strategies/DualEmaCrossoverBot`
- the repo source matches the cAlgo project source exactly
- compiled algo was rebuilt successfully and is available at `/Users/fra/cAlgo/Sources/Robots/DualEmaCrossoverBot.algo`
- the first in-sample baseline canary `aa173d1e-3d1e-4e6a-ad68-06985ee79c75` failed immediately under `data_mode: m1` with `Invalid settings`
- all DualEma phase templates were corrected to `data_mode: ticks`
- Phase 0 benchmark is complete:
  - `w3` job `ee0414fe-3e7d-4a78-81c3-6eff82b5b809` finished in `623.05s`
  - `w4` job `3e8e7be6-19a9-403e-9096-347acf52089f` finished in `1055.98s`
  - `w5` job `31497ab3-7107-4f41-9455-5a58bf551e80` finished in `1375.14s`
  - decision: `parallel_workers: 3` is the clear winner for `EURUSD t100`, and heavier worker counts are materially slower on this VPS
- Phase 1 baseline is complete:
  - in-sample job `96414b90-362f-4137-9d7c-fdac5980921d` ended at `profit_factor 0.89`, `net_profit -2658.77`, `average_trade -10.76`, `max_drawdown_pct 51.0892`, `total_trades 247`
  - out-of-sample job `b425ce36-9267-4daf-9c6e-813f523fa686` ended at `profit_factor 0.87`, `net_profit -392.24`, `average_trade -10.90`, `max_drawdown_pct 12.5512`, `total_trades 36`
  - decision: trade count is sufficient to continue, but the exported default configuration is clearly not deployable and needs signal recalibration
- Phase 2 signal core genetic runs are complete:
  - run 1: `55ed8ff8-a566-4aa8-adaf-ebfdebe5ad6e` best `FastEmaPeriod 35`, `SlowEmaPeriod 130`, `profit_factor 0.89`, `average_trade -10.24`, `max_drawdown_pct 49.7095`, `total_trades 253`
  - run 2: `7d0795ef-c9e4-413f-8b2e-93afa412c2b6` best repeated the same `35 / 130` leader, with a nearby secondary cluster at `40 / 90`
  - run 3: `e21b83d5-8d8b-4238-88b7-66b23c45d335` best `FastEmaPeriod 60`, `SlowEmaPeriod 90`, `profit_factor 0.90`, `average_trade -9.68`, `max_drawdown_pct 39.1846`, `total_trades 230`
  - decision: Phase 2 improved the signal branch only marginally versus baseline and still failed the live constraints in every pass, but the runs did converge enough to justify one narrowed confirmation grid before deciding whether to open the exit phase
- Phase 2 confirmation is complete:
  - in-sample confirmation job `14d3ce87-71c4-4e4f-b6e3-54e2fb32c1f7` best `FastEmaPeriod 45`, `SlowEmaPeriod 110`, `profit_factor 0.92`, `average_trade -8.46`, `max_drawdown_pct 48.8693`, `total_trades 253`
  - out-of-sample confirmation job `4af55676-5956-477b-9e27-6aca7621098d` best `FastEmaPeriod 40`, `SlowEmaPeriod 110`, `profit_factor 1.82`, `average_trade 63.16`, `max_drawdown_pct 8.6186`, `total_trades 33`
  - decision: the confirmation split is contradictory because the OOS branch is strong while the IS branch still fails the drawdown gate, but the overlap around `SlowEmaPeriod 110` is strong enough to justify a provisional exit phase rather than stopping here
- Phase 3 exit management is complete:
  - provisional `40 / 110` job `1bd03c85-22ca-4b0f-ace2-a0ed797ea982` best `TpRMultiple 3.0`, `TrailingActivationPips 0.6`, `TrailingStopStandardPips 1.5`, `TrailingStopHighVolPips 2.25`, `profit_factor 0.82`, `average_trade -14.44`, `max_drawdown_pct 40.8352`, `total_trades 235`
  - provisional `45 / 110` job `679e14d2-811c-45f5-96c1-124dd81a2793` best `TpRMultiple 2.5`, `TrailingActivationPips 0.4`, `TrailingStopStandardPips 2.0`, `TrailingStopHighVolPips 5.5`, `profit_factor 0.92`, `average_trade -8.40`, `max_drawdown_pct 42.3852`, `total_trades 253`
  - both exit jobs finished with `best_pass_summary = null`, and every inspected top pass was `ranking_eligible: false`
  - decision: exit tuning did not rescue either anchor and instead weakened the stronger OOS branch, so Phase 4 filters and Phase 5 robustness should remain closed for this strategy branch
- VPS queue is idle:
  - server health shows `queued_jobs = 0` and `queued_passes = 0`
  - current conclusion: `DualEmaCrossoverBot` does not show a usable edge under the present `EURUSD t100` workflow

## Phase 0: Tick Benchmark

Use [phase1_benchmark.yaml](../examples/dualemacrossoverbot/phase1_benchmark.yaml) as the template and run it three times with:

- `parallel_workers: 3`
- `parallel_workers: 4`
- `parallel_workers: 5`

Do not assume the GridTrendBot `m5` benchmark winner applies here. `t100` work is much heavier, so this strategy needs its own worker check.

Record:

- total runtime
- failed-pass count
- peak active `ctrader-console` containers
- CPU / RAM snapshots

Choose the highest stable worker count that is at least 8% faster than the next lower setting. The current live winner is `parallel_workers: 3`, so the heavy DualEma templates now use that value.

This phase measures infrastructure, not edge.

## Phase 1: Baseline

Run the exported baseline unchanged with:

- [phase2_baseline_insample.yaml](../examples/dualemacrossoverbot/phase2_baseline_insample.yaml)
- [phase2_baseline_oos.yaml](../examples/dualemacrossoverbot/phase2_baseline_oos.yaml)

Stop if:

- in-sample `total_trades < 100`
- out-of-sample `total_trades < 20`

Use this phase to answer one question first: does the exported bot show any durable edge before tuning?

## Phase 2: Signal Core

Start with [phase3_signal_genetic.yaml](../examples/dualemacrossoverbot/phase3_signal_genetic.yaml).

Only tune:

- `FastEmaPeriod`
- `SlowEmaPeriod`

Keep these frozen in this phase:

- `UseHtfFilter`
- `HtfEmaPeriod`
- `TpRMultiple`
- trailing-stop parameters
- all risk and position-size parameters

Keep the fast EMA materially below the slow EMA. The current template uses separated ranges to avoid wasting search budget on nonsensical inversions.

Run three independent genetic jobs. Narrow the ranges to the overlap seen in at least two runs, then run confirmation in-sample and out-of-sample before promoting a signal winner.

## Phase 3: Exit Management

Before Phase 3, copy the frozen winner from Phase 2 into the `fixed_params` block of [phase4_exit_genetic.yaml](../examples/dualemacrossoverbot/phase4_exit_genetic.yaml).

Then tune:

- `TpRMultiple`
- `TrailingActivationPips`
- `TrailingStopStandardPips`
- `TrailingStopHighVolPips`

Keep these fixed unless repeated evidence shows they matter:

- `HighVolStartHour`
- `HighVolEndHour`
- `MinStopLossPips`
- `MaxStopLossPips`

Do not optimize `RiskPercent` or `MaxPositionSizePercent` in this phase. The prior optimizer history already showed that unconstrained risk tuning can create cosmetically strong but non-deployable results.

## Phase 4: Filters And Session Gating

Before opening a full filter optimization, run explicit boolean A/B checks as separate single-pass jobs for:

- `UseHtfFilter`
- `RequireFreshCross`

Do not encode boolean toggles as numeric `0` / `1` optimization ranges.

Only if `UseHtfFilter=true` wins should you open [phase5_filters_genetic.yaml](../examples/dualemacrossoverbot/phase5_filters_genetic.yaml) and tune:

- `HtfEmaPeriod`
- `SessionStartHour`
- `SessionEndHour`

Keep `HtfTimeFrame` fixed at the exported `h1` unless later evidence strongly suggests otherwise.

The current branch did not reach this phase. Do not open it unless a redesigned signal or exit branch first produces a promotable candidate.

## Phase 5: Robustness Validation

Run the final candidate through:

- out-of-sample validation
- multi-period validation
- spread sensitivity checks
- low-volatility and high-volatility regime checks

Only after a candidate survives Phase 5 should you consider opening risk-profile or deployment branches.

## Stop Conditions

Stop the pipeline and say the strategy currently looks non-viable if repeated phases show one or more of these patterns:

- no candidate reaches `profit_factor >= 1.05` out-of-sample
- trade counts collapse when a candidate is moved out-of-sample
- improvements depend on risk escalation rather than better trade quality
- improvements appear only as isolated spikes without nearby support
- exit tuning changes drawdown cosmetics but not `profit_factor` or `average_trade`
- a provisional OOS winner collapses when reintroduced in-sample during exit tuning
