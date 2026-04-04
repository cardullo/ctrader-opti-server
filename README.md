# cTrader Optimization Server

A remote cBot optimization orchestration system that wraps the official
[cTrader CLI Docker image](https://github.com/spotware/ctrader-cli) to run
parameter‑sweep backtests as parallel Docker jobs, with **grid**, **random**,
and **genetic** optimization strategies.

```
┌──────────────┐          ┌──────────────────────────────────┐
│  opti CLI    │  REST    │  FastAPI Server (VPS)            │
│  (your Mac)  │ ──────▶  │  ┌──────┐  ┌──────┐  ┌──────┐  │
│              │          │  │ pass │  │ pass │  │ pass │  │
│  submit      │          │  │  #1  │  │  #2  │  │  #3  │  │
│  watch       │          │  └──────┘  └──────┘  └──────┘  │
│  results     │          │      ▲ Docker containers ▲      │
│  best        │          │      └── ctrader-cli ──────┘    │
└──────────────┘          └──────────────────────────────────┘
```

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
```

`PWD_FILE_PATH` is the path inside the server container. When the server runs
via Docker Compose and launches sibling `ctrader-console` containers through
the Docker socket, `HOST_DATA_DIR` and `HOST_PWD_FILE_PATH` must point to the
real host-side paths so those sibling containers can mount the `.algo`,
results, and password file correctly.

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
fitness: net_profit      # net_profit | sharpe_ratio | profit_factor | win_rate
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
python -m client.opti results a1b2c3d4-... --top 20 --sort-by net_profit
```

Rich table with all parameters and performance metrics.

### Step 5: Get the best parameters

```bash
python -m client.opti best a1b2c3d4-...
```

Shows the winning parameter set in a copy-paste ready format.

### Cancel a job

```bash
python -m client.opti cancel a1b2c3d4-...
```

## CLI Commands Reference

| Command | Description |
|---------|-------------|
| `opti submit --algo FILE --config YAML` | Upload algo + start optimization |
| `opti status [JOB_ID]` | Show one or all jobs |
| `opti watch JOB_ID` | Live progress polling |
| `opti results JOB_ID [--top N] [--sort-by METRIC]` | Top N passes |
| `opti best JOB_ID` | Single best pass + params |
| `opti cancel JOB_ID` | Cancel and clean up |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/jobs` | Create optimization job (multipart upload) |
| `GET` | `/jobs` | List all jobs |
| `GET` | `/jobs/{id}` | Job details + top 20 passes |
| `GET` | `/jobs/{id}/passes` | Paginated passes with filters |
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
3. Select top 50% by fitness metric
4. Crossover (uniform) + mutation (±1 step, 20% rate)
5. Repeat until `max_passes` exhausted

## Architecture

- **Server** runs as a single FastAPI process with a background asyncio worker
- **Worker** polls the SQLite DB every 2s for queued passes
- Each backtest runs in an isolated Docker container (sibling, not nested)
- Docker socket (`/var/run/docker.sock`) is mounted for container management
- All containers run with `--rm` for automatic cleanup
- Graceful restart recovery: any passes stuck in `running` state are re-queued

## License

MIT
