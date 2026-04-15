# DualEmaCrossoverBot Autonomous Phase Runner Prompt

Use the prompt below in a separate Codex chat if you want that agent to keep monitoring, analyzing, and advancing the DualEmaCrossoverBot optimization workflow with minimal supervision.

---

You are working inside the repo `/Users/fra/Documents/ctrader-opti-server`.

Your job is to act as an autonomous optimization supervisor for `DualEmaCrossoverBot`. Repeatedly inspect the current phase status, analyze completed results, update the next phase configs when needed, submit the next jobs to the VPS, and continue until either:

1. a clearly robust winning configuration is found and validated, or
2. the evidence shows the strategy does not have a usable edge under this workflow.

You should operate with high autonomy. Do not stop after a single analysis step. Continue through the full phase loop unless you hit a real blocker or a decision with non-obvious risk.

## Repo Context

- Strategy source: `/Users/fra/Documents/ctrader-opti-server/cbot_strategies/DualEmaCrossoverBot/DualEmaCrossoverBot.c`
- Exported defaults: `/Users/fra/Documents/ctrader-opti-server/cbot_strategies/DualEmaCrossoverBot/DualEmaCrossoverBot, EURUSD t100 - Default Parameters.cbotset`
- Exported optimizer snapshot: `/Users/fra/Documents/ctrader-opti-server/cbot_strategies/DualEmaCrossoverBot/DualEmaCrossoverBot, EURUSD t100 - Default Optimisation Parameters.optset`
- Phase workflow doc: `/Users/fra/Documents/ctrader-opti-server/docs/dualemacrossoverbot-phased-optimization.md`
- Phase templates:
  - `/Users/fra/Documents/ctrader-opti-server/examples/dualemacrossoverbot/phase1_benchmark.yaml`
  - `/Users/fra/Documents/ctrader-opti-server/examples/dualemacrossoverbot/phase2_baseline_insample.yaml`
  - `/Users/fra/Documents/ctrader-opti-server/examples/dualemacrossoverbot/phase2_baseline_oos.yaml`
  - `/Users/fra/Documents/ctrader-opti-server/examples/dualemacrossoverbot/phase3_signal_genetic.yaml`
  - `/Users/fra/Documents/ctrader-opti-server/examples/dualemacrossoverbot/phase4_exit_genetic.yaml`
  - `/Users/fra/Documents/ctrader-opti-server/examples/dualemacrossoverbot/phase5_filters_genetic.yaml`
- cAlgo project:
  - `/Users/fra/cAlgo/Sources/Robots/DualEmaCrossoverBot/DualEmaCrossoverBot/DualEmaCrossoverBot.csproj`
- Compiled algo:
  - `/Users/fra/cAlgo/Sources/Robots/DualEmaCrossoverBot.algo`

## Important Operational Rules

- Use the repo’s documented phase order in `/docs/dualemacrossoverbot-phased-optimization.md`.
- Treat the VPS benchmark as `Phase 0` only. It validates infrastructure, not strategy edge.
- Keep `MAX_PARALLEL_JOBS=1` during heavy optimization on this VPS.
- Use day-first dates for cTrader configs, for example `31/12/2025`, not `12/31/2025`.
- Rank by robustness:
  - `profit_factor` descending
  - `max_drawdown_pct` ascending
  - `average_trade` descending
- Do not optimize everything at once.
- Do not trust a single best pass without checking cluster behavior and out-of-sample follow-through.
- Do not optimize `RiskPercent` or `MaxPositionSizePercent` early. The exported optimizer history already pushed those to non-deployable territory.
- For boolean A/B checks, use explicit `true` / `false` fixed-param configs in separate one-pass jobs. Do not rely on numeric `0` / `1` optimization ranges for booleans.

## Current Known State

### Strategy import

- strategy files are present in `/Users/fra/Documents/ctrader-opti-server/cbot_strategies/DualEmaCrossoverBot`
- repo source and cAlgo project source matched at inspection time
- exported baseline chart is `EURUSD` on `t100`
- tick-chart phases should use `data_mode: ticks`
- exported baseline parameters include:
  - `FastEmaPeriod = 35`
  - `SlowEmaPeriod = 144`
  - `UseHtfFilter = false`
  - `HtfTimeFrame = h1`
  - `HtfEmaPeriod = 50`
  - `UseCompounding = true`
  - `RiskPercent = 2.0`
  - `MaxPositionSizePercent = 300.0`
  - `TpRMultiple = 2.5`
  - `TrailingActivationPips = 0.3`
  - `TrailingStopStandardPips = 2.7`
  - `TrailingStopHighVolPips = 3.8`
  - `SessionStartHour = 8`
  - `SessionEndHour = 16`
  - `RequireFreshCross = true`

### Important strategy observations

- this is a tick-chart EMA crossover bot, so benchmark results from `m5` strategies should not be assumed to transfer
- the code comments document a previous optimizer drift into `RiskPercent 16-18` and `MaxPositionSizePercent 800+`, which should be treated as overfit/non-deployable unless proven otherwise
- the exported `.optset` only had `TpRMultiple` and `TrailingActivationPips` selected for optimization, so the broader search plan must be built explicitly

### Workflow status

- the first in-sample baseline canary `aa173d1e-3d1e-4e6a-ad68-06985ee79c75` failed under `data_mode: m1` with `Invalid settings`
- all DualEma templates were then corrected to `data_mode: ticks`
- Phase 0 benchmark is complete:
  - `ee0414fe-3e7d-4a78-81c3-6eff82b5b809` for `parallel_workers: 3` finished in `623.05s`
  - `3e8e7be6-19a9-403e-9096-347acf52089f` for `parallel_workers: 4` finished in `1055.98s`
  - `31497ab3-7107-4f41-9455-5a58bf551e80` for `parallel_workers: 5` finished in `1375.14s`
  - use `parallel_workers: 3` for heavy DualEma optimization templates unless newer benchmark evidence overturns it
- corrected baseline in-sample job `96414b90-362f-4137-9d7c-fdac5980921d` is `done` with:
  - `profit_factor = 0.89`
  - `net_profit = -2658.77`
  - `average_trade = -10.76`
  - `max_drawdown_pct = 51.0892`
  - `total_trades = 247`
- corrected baseline out-of-sample job `b425ce36-9267-4daf-9c6e-813f523fa686` is `done` with:
  - `profit_factor = 0.87`
  - `net_profit = -392.24`
  - `average_trade = -10.90`
  - `max_drawdown_pct = 12.5512`
  - `total_trades = 36`
- interpretation so far:
  - trade count is sufficient to continue
  - the exported default configuration does not show edge yet
  - the next logical step is the signal core phase, not exits or filters
- Phase 2 signal runs are complete:
  - `55ed8ff8-a566-4aa8-adaf-ebfdebe5ad6e` best `35 / 130` with `profit_factor 0.89`, `average_trade -10.24`, `max_drawdown_pct 49.7095`
  - `7d0795ef-c9e4-413f-8b2e-93afa412c2b6` repeated that same `35 / 130` leader and showed a nearby `40 / 90` cluster
  - `e21b83d5-8d8b-4238-88b7-66b23c45d335` best `60 / 90` with `profit_factor 0.90`, `average_trade -9.68`, `max_drawdown_pct 39.1846`
- no Phase 2 pass satisfied the current ranking constraints, but the three runs converged enough to justify a narrowed confirmation grid
- Phase 2 confirmation is complete:
  - in-sample `14d3ce87-71c4-4e4f-b6e3-54e2fb32c1f7` best `45 / 110` with `profit_factor 0.92`, `average_trade -8.46`, `max_drawdown_pct 48.8693`, `total_trades 253`
  - out-of-sample `4af55676-5956-477b-9e27-6aca7621098d` best `40 / 110` with `profit_factor 1.82`, `average_trade 63.16`, `max_drawdown_pct 8.6186`, `total_trades 33`
- the split is contradictory, but the repeated `SlowEmaPeriod = 110` band is strong enough to justify a provisional exit branch rather than ending the strategy immediately
- Phase 3 exit management is complete:
  - provisional `40 / 110` job `1bd03c85-22ca-4b0f-ace2-a0ed797ea982` best `TpRMultiple 3.0`, `TrailingActivationPips 0.6`, `TrailingStopStandardPips 1.5`, `TrailingStopHighVolPips 2.25`, `profit_factor 0.82`, `average_trade -14.44`, `max_drawdown_pct 40.8352`, `total_trades 235`
  - provisional `45 / 110` job `679e14d2-811c-45f5-96c1-124dd81a2793` best `TpRMultiple 2.5`, `TrailingActivationPips 0.4`, `TrailingStopStandardPips 2.0`, `TrailingStopHighVolPips 5.5`, `profit_factor 0.92`, `average_trade -8.40`, `max_drawdown_pct 42.3852`, `total_trades 253`
  - both exit jobs returned `best_pass_summary = null`
  - every inspected top pass was `ranking_eligible = false`
  - the exit phase weakened the provisional branch rather than confirming it
- VPS queue is currently idle:
  - `queued_jobs = 0`
  - `queued_passes = 0`
- current conclusion:
  - do not open Phase 4 filters or Phase 5 robustness from this branch
  - treat `DualEmaCrossoverBot` as non-viable under the current `EURUSD t100` workflow unless the user explicitly asks for a redesigned search or alternate validation branch
- no future live job IDs should be invented; inspect the VPS first before reporting progress

## Your Mission

Repeatedly do this loop:

1. Inspect the latest phase state.
2. Determine whether the current phase is:
   - not started
   - running
   - completed successfully
   - failed
   - completed but not good enough to promote
3. If a phase is running:
   - report the relevant job IDs
   - report elapsed progress
   - estimate remaining time only if the estimate is grounded in observed pass timing
   - wait and check again later
4. If a phase finished:
   - analyze the results deeply
   - identify the best-ranked settings
   - inspect top clusters, not just one winner
   - decide whether the phase passed its promotion criteria
5. If the phase passed:
   - update the next phase config files with the frozen winner where appropriate
   - submit the next jobs
6. If the phase failed:
   - decide whether to:
     - refine the current phase
     - branch to the next appropriate phase
     - stop the overall pipeline as strategy-not-promising
7. Keep cycling until the workflow is complete or until the evidence is strong enough to declare the strategy non-viable.

If the workflow state already shows a completed exit phase with no ranking-eligible passes and no queued jobs, stop the loop, update the docs, and report that the strategy branch is finished without a usable edge.

## Phase-By-Phase Decision Rules

### Phase 0: Tick Benchmark

Use `/examples/dualemacrossoverbot/phase1_benchmark.yaml`.

Run it with:

- `parallel_workers: 3`
- `parallel_workers: 4`
- `parallel_workers: 5`

Choose the highest stable worker count that is at least 8% faster than the next lower setting.

### Phase 1: Baseline

Use:

- `/examples/dualemacrossoverbot/phase2_baseline_insample.yaml`
- `/examples/dualemacrossoverbot/phase2_baseline_oos.yaml`

Promote beyond baseline only if:

- in-sample `total_trades >= 100`
- out-of-sample `total_trades >= 20`

If both windows remain weak, proceed to signal calibration rather than exit or filter tuning.

### Phase 2: Signal Core

Use `/examples/dualemacrossoverbot/phase3_signal_genetic.yaml`.

Tune only:

- `FastEmaPeriod`
- `SlowEmaPeriod`

Expected process:

- run 3 independent genetic jobs
- compare the top clusters across runs
- shrink the ranges to the overlap present in at least 2 of 3 runs
- create and run a confirmation grid
- validate the top candidates out-of-sample

Promote only if a candidate improves in-sample ranking and remains acceptable out-of-sample.

### Phase 3: Exit Management

Use `/examples/dualemacrossoverbot/phase4_exit_genetic.yaml`.

Tune:

- `TpRMultiple`
- `TrailingActivationPips`
- `TrailingStopStandardPips`
- `TrailingStopHighVolPips`

Keep risk sizing frozen.

### Phase 4: Filters And Session Gating

Before opening a full filter tuning branch, run explicit boolean A/B checks on:

- `UseHtfFilter`
- `RequireFreshCross`

Only if `UseHtfFilter=true` beats `false` should you open `/examples/dualemacrossoverbot/phase5_filters_genetic.yaml`.

Then tune:

- `HtfEmaPeriod`
- `SessionStartHour`
- `SessionEndHour`

Keep `HtfTimeFrame = h1` unless there is a compelling reason to change it.

### Phase 5: Robustness Validation

A candidate is only truly promotable if it survives:

- out-of-sample validation
- multi-period checks
- spread sensitivity
- different volatility regimes

## Practical Commands

Use these from repo root:

```bash
.venv/bin/python -m client.opti status
.venv/bin/python -m client.opti status <JOB_ID>
.venv/bin/python -m client.opti watch <JOB_ID>
.venv/bin/python -m client.opti results <JOB_ID> --top 20
.venv/bin/python -m client.opti best <JOB_ID>
```

To submit:

```bash
.venv/bin/python -m client.opti submit --algo "/Users/fra/cAlgo/Sources/Robots/DualEmaCrossoverBot.algo" --config <CONFIG_PATH>
```

## Required Working Style

- Before substantial work, state what phase you are checking and what you are about to do.
- If a job is still running, do not invent results.
- If a phase template needs adjustment, edit the YAMLs directly in the repo.
- Keep the docs aligned with reality. If you change the phase order or promotion logic, update `/docs/dualemacrossoverbot-phased-optimization.md`.
- If a submission fails, diagnose the exact cause, fix it, and resubmit.
- Do not stop at “the job submitted.” Stay with it until you know whether it succeeded, failed, or is clearly still running.
- When comparing candidates, focus on:
  - `profit_factor`
  - `net_profit`
  - `average_trade`
  - `max_drawdown_pct`
  - `total_trades`
  - consistency across neighboring parameter values

## Stop Conditions

Stop the pipeline and say the strategy currently looks non-viable if repeated phases show one or more of these patterns:

- no candidate reaches `profit_factor >= 1.05` out-of-sample
- top candidates collapse badly out-of-sample
- trade quality improves only when risk parameters are pushed beyond sane deployment levels
- improvements only exist as isolated spikes with no nearby cluster support
- parameter tuning repeatedly improves cosmetics but not edge
