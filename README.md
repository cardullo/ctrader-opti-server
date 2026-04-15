# cTrader Optimization Server 🚀

A high-performance cBot optimization orchestration system. It wraps the standalone [cTrader CLI Docker image](https://github.com/spotware/ctrader-cli) to run parameter-sweep backtests as massive parallel jobs, overcoming the single-threaded limitations of the standard CLI.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Docker](https://img.shields.io/badge/Docker-Enabled-blue)](https://www.docker.com/)

---

### Why this project?
cTrader's official CLI is powerful but only supports individual backtests. To perform a parameter sweep (optimization), users traditionally have to manually script multiple runs or use the Desktop GUI—which is tied to a single machine.

This project enables:
- **True Parallelization**: Run hundreds of optimization passes in parallel across multiple CPU cores.
- **Remote Orchestration**: Submit a job from your Mac/local machine and let a powerful Linux VPS handle the heavy lifting.
- **Smart Strategies**: Includes built-in support for **Grid**, **Random**, and **Genetic** search algorithms.
- **Remote Export Jobs**: Queue chunked historical-data export passes that write `bars.csv` and `manifest.json` artifacts for later import elsewhere.
- **Robust Ranking**: Support ordered ranking rules plus hard constraints such as minimum trade count and drawdown caps.
- **REST API**: Fully headless management of backtest jobs.

---

```mermaid
graph LR
    A[opti CLI] -- REST --> B[FastAPI Server]
    B -- Docker Socket --> C[Pass #1]
    B -- Docker Socket --> D[Pass #2]
    B -- Docker Socket --> E[Pass #N]
    style B fill:#f9f,stroke:#333,stroke-width:2px
```

## Quick Start
Detailed installation guides for all platforms can be found in [docs/setup.md](docs/setup.md).


## Prerequisites

| Component | Requirement |
|-----------|-------------|
| **VPS (Server)** | Linux with Docker 20+ installed |
| **Client (Mac)** | Python 3.11+ |
| **cTrader** | Valid cTrader ID and account with API access |

## Server Setup

### 1. Clone & configure

```bash
git clone <this-repo> && cd ctrader-opti-server
cp .env.example .env
```

Edit `.env` with your cTrader credentials:

```env
API_KEY=your-strong-secret-key
CTID=your_ctid@example.com
PWD_FILE_PATH=/data/pwd
CTRADER_ACCOUNT=12345678
HOST_DATA_DIR=/home/opti/ctrader-opti-server/data
HOST_PWD_FILE_PATH=/home/opti/ctrader-opti-server/data/pwd
HOST_FSB_REPO_ROOT=/home/opti/ctrade-backtest-engine
FSB_REPO_ROOT=/opt/fsb
FSB_PYTHON_BIN=/opt/fsb/.venv/bin/python
FSB_DATA_DSN=postgresql://postgres:postgres@localhost:55432/market
```

`PWD_FILE_PATH` is the path inside the server container. When the server runs
via Docker Compose and launches sibling `ctrader-console` containers through
the Docker socket, `HOST_DATA_DIR` and `HOST_PWD_FILE_PATH` must point to the
real host-side paths so those sibling containers can mount the `.algo`,
results, and password file correctly.

For remote `fsb_search` jobs, the server also needs access to a checked-out
`ctrade-backtest-engine` repo plus a ready Python environment. The compose
file mounts `HOST_FSB_REPO_ROOT` into the container at `FSB_REPO_ROOT`, and
the worker launches the hidden fsb runner through `FSB_PYTHON_BIN`.

`FSB_DATA_DSN` is mandatory for `fsb_search` jobs. If it is missing, `/health`
will report `"fsb_ready": false` and `POST /jobs` for `job_type=fsb_search`
will fail immediately with a clear error instead of silently falling back.

### 2. Create the password file

```bash
mkdir -p data
echo -n "your_ctrader_password" > data/pwd
chmod 600 data/pwd
```

> ⚠️ The password file is **never** transmitted over the API — it stays on the
> server and is bind-mounted into ctrader-cli containers.

### 3. Start the server

```bash
docker compose up -d --build
```

The API is now live at `http://your-vps-ip:8000`.
Check health:

```bash
curl http://localhost:8000/health
```

For MacBook-to-VPS DB sync from `fsb db *`, the Postgres port must be reachable
from the Mac. You can either open port `55432` on the VPS firewall or keep it
private and use an SSH tunnel such as:

```bash
ssh -L 55432:localhost:55432 opti@your-vps-ip
```

## Client Setup

### 1. Install dependencies

```bash
cd client
pip install -r requirements.txt
```

### 2. Configure

Create `~/.opti/config.yaml`:

```yaml
server_url: http://your-vps-ip:8000
api_key: your-strong-secret-key
```

Or set environment variables:

```bash
export OPTI_SERVER_URL=http://your-vps-ip:8000
export OPTI_API_KEY=your-strong-secret-key
```

## Usage — Full Workflow

### Step 1: Create a job config

Save as `job.yaml` (see [examples/job.yaml](examples/job.yaml)):

```yaml
name: EMA_v2.2_EURUSD_grid
symbol: EURUSD
period: H1
start: "01/01/2023"
end: "01/01/2025"
data_mode: m1
balance: 10000
commission: 15
spread: 1
strategy: grid          # grid | random | genetic
max_passes: 500
parallel_workers: 4
fitness: net_profit      # Backward-compatible fallback when ranking is omitted
fixed_params:
  EnableSessionFilter: true
  MaxSpreadPips: 3.0
ranking:
  - metric: profit_factor
    direction: desc
  - metric: max_drawdown_pct
    direction: asc
  - metric: average_trade
    direction: desc
constraints:
  - metric: total_trades
    operator: gte
    value: 100
  - metric: max_drawdown_pct
    operator: lte
    value: 25
params:
  FastPeriod:
    min: 5
    max: 30
    step: 5
  SlowPeriod:
    min: 20
    max: 100
    step: 10
  StopLossPips:
    min: 20
    max: 60
    step: 10
  TakeProfitPips:
    min: 30
    max: 90
    step: 10
```

### Step 2: Submit

```bash
python -m client.opti submit --algo MyBot.algo --config job.yaml
```

Output:
```
✓ Job submitted
  Job ID:       a1b2c3d4-...
  Total passes: 270
```

### Step 3: Watch progress

```bash
python -m client.opti watch a1b2c3d4-...
```

Live-updating display with progress bar and top 10 results.

### Step 4: View results

```bash
python -m client.opti results a1b2c3d4-... --top 20
```

Rich table with all parameters and performance metrics, ordered by the job's
ranking profile. Add `--sort-by METRIC` to override the default ordering for
manual inspection.

### Step 5: Get the best parameters

```bash
python -m client.opti best a1b2c3d4-...
```

Shows the winning parameter set in a copy-paste ready format.

### Cancel a job

```bash
python -m client.opti cancel a1b2c3d4-...
```

## Usage — Remote Export Jobs

For data-export jobs, submit an export-aware config instead of an optimization grid. Each chunk becomes one queued pass on the VPS, and successful passes write artifacts under `data/results/<pass-id>/`.

Example config: [examples/export-job.yaml](examples/export-job.yaml)

```yaml
name: EURUSD_tick_export
strategy: export
parallel_workers: 2
chunks:
  - symbol: EURUSD
    period: t1
    start_utc: "2025-01-01T00:00:00Z"
    end_utc: "2025-02-01T00:00:00Z"
    data_mode: ticks
    broker_code: icmarkets
```

Submit it the same way:

```bash
python -m client.opti submit --algo CandleExportBot.algo --config export-job.yaml
```

Then inspect progress with:

```bash
python -m client.opti status
python -m client.opti status <job-id>
python -m client.opti results <job-id> --top 20
```

For export jobs, `results` shows recent completed chunks with row counts and artifact directories instead of optimization metrics.

### VPS-first candle flow

If the VPS `market-db` is your primary candle store, keep the heavy import work
on the VPS too:

1. Push your local candle store to the VPS only when you need a bootstrap or refresh.
2. Run export jobs on the VPS as usual.
3. Import completed export artifacts directly into the VPS `market-db`.
4. Delete imported artifacts immediately to keep disk usage under control.

The `ctrader-backtest` sync helper now supports this mode through:

```bash
./scripts/sync_remote_export_jobs.sh \
  --target vps \
  --job-id YOUR-EXPORT-JOB-ID \
  --delete-remote
```

This mode keeps the old local-import flow available with `--target local`, so a
future VPS-to-local rebuild workflow is still supported.

## CLI Commands Reference

| Command | Description |
|---------|-------------|
| `opti submit --algo FILE --config YAML` | Upload algo + start optimization |
| `opti status [JOB_ID]` | Show one or all jobs |
| `opti watch JOB_ID` | Live progress polling |
| `opti results JOB_ID [--top N] [--sort-by METRIC]` | Top N passes |
| `opti best JOB_ID` | Single best pass + params |
| `opti cancel JOB_ID` | Cancel and clean up |
| `opti cleanup [--status S] [--before D]` | Bulk delete jobs by status or date |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/jobs` | Create optimization job (multipart upload) |
| `POST` | `/jobs` | Create `fsb_search` job (JSON payload) |
| `GET` | `/jobs` | List all jobs |
| `DELETE` | `/jobs` | Bulk cancel / delete jobs by status and/or date |
| `GET` | `/jobs/{id}` | Job details + top 20 passes |
| `GET` | `/jobs/{id}/passes` | Paginated passes with filters |
| `POST` | `/jobs/{id}/import-completed-exports` | Import completed export artifacts into the VPS market DB |
| `GET` | `/jobs/{id}/result-bundle` | Download compressed fsb result bundle |
| `GET` | `/jobs/{id}/best` | Best pass + cbotset params |
| `DELETE` | `/jobs/{id}` | Cancel job |
| `GET` | `/health` | Server health check |

All endpoints (except `/health`) require the `X-API-Key` header.

## Optimization Strategies

### Grid
Cartesian product of all parameter ranges. Exhaustive but can be very large.
Capped at `max_passes`.

### Random
Uniform random sampling from each parameter range, snapped to step increments.
Good for large search spaces where grid is infeasible.

### Genetic
Simple evolutionary algorithm:
1. Start with 20 random individuals
2. Run backtests for the generation
3. Select the top 50% by the job's ranking profile (with constraints applied)
4. Crossover (uniform) + mutation (±1 step, 20% rate)
5. Repeat until `max_passes` exhausted

## Ranking and Constraints

- `fitness` remains supported for legacy configs and maps to a single descending
  ranking rule.
- `ranking` accepts an ordered list of `{metric, direction}` rules.
- `constraints` accepts hard filters in the form
  `{metric, operator, value}` where `operator` is one of `gt`, `gte`, `lt`,
  `lte`, or `eq`.
- `average_trade` is available for ranking and display, and falls back to
  `net_profit / total_trades` when cTrader does not emit it directly.

`fixed_params` lets you pin a full exported `.cbotset` baseline while only
optimizing the subset declared under `params`. This is especially useful for
phased workflows where each later phase freezes the winning values from the
earlier phase.

For a ready-to-run phased example based on GridTrendBot, see
[docs/gridtrendbot-phased-optimization.md](docs/gridtrendbot-phased-optimization.md)
and the templates under [examples/gridtrendbot](examples/gridtrendbot).

## Architecture

- **Server** runs as a single FastAPI process with a background asyncio worker
- **Worker** polls the SQLite DB every 2s for queued passes
- Each backtest runs in an isolated Docker container (sibling, not nested)
- Docker socket (`/var/run/docker.sock`) is mounted for container management
- All containers run with `--rm` for automatic cleanup
- Graceful restart recovery: any passes stuck in `running` state are re-queued

## License

MIT
