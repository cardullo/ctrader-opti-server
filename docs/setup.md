# Setup Guide

This guide covers the installation and configuration of the cTrader Optimization Server on both your local machine (Client) and a remote VPS (Server).

---

## 1. Server Setup (Linux VPS)

The server is designed to run on a Linux VPS with Docker. We recommend **Ubuntu 22.04 LTS** with at least 4 CPU cores and 8GB RAM for heavy optimizations.

### Prerequisites
- Docker 20.10+
- Docker Compose v2+
- Ports 8000 (API) and 22 (SSH) open in firewall

### Installation
1. **Clone the repository**:
   ```bash
   git clone https://github.com/cardullo/ctrader-opti-server.git
   cd ctrader-opti-server
   ```

2. **Configure Environment**:
   Copy `.env.example` to `.env` and fill in your details:
   ```bash
   cp .env.example .env
   ```
   **Key Variables**:
   - `API_KEY`: A strong secret for authenticating CLI requests.
   - `CTID`: Your cTrader ID (email).
   - `CTRADER_ACCOUNT`: The account number for backtests.
   - `HOST_DATA_DIR`: Absolute path to the `data/` folder on the VPS host (e.g., `/home/opti/ctrader-opti-server/data`).
   - `HOST_PWD_FILE_PATH`: Absolute path to your cTrader password file on the host.
   - `HOST_FSB_REPO_ROOT`: Host path to the checked-out `ctrade-backtest-engine` repo used for `fsb_search`.
   - `FSB_REPO_ROOT`: In-container mount point for that repo, usually `/opt/fsb`.
   - `FSB_PYTHON_BIN`: Python interpreter inside the mounted fsb virtualenv.
   - `FSB_DATA_DSN`: Market DB DSN the VPS fsb worker must use. If this is missing, `/health` reports `fsb_ready=false` and `fsb_search` job creation is rejected.

3. **Secure cTrader Password**:
   Create a password file on the VPS. This file is **never** sent over the network; it is mounted directly into the `ctrader-cli` containers.
   ```bash
   mkdir -p data
   echo -n "YOUR_PASSWORD" > data/pwd
   chmod 600 data/pwd
   ```

4. **Launch the Server**:
   ```bash
   docker compose up -d --build
   ```

5. **Optional SSH tunnel for MacBook DB sync**:
   If you do not want to expose Postgres publicly, tunnel it instead:
   ```bash
   ssh -L 55432:localhost:55432 opti@your-vps-ip
   ```

6. **VPS-first export imports**:
   The VPS can now import completed export artifacts straight into its own
   `market-db`. This is the recommended steady-state flow once the VPS DB has
   been bootstrapped:
   ```bash
   ./scripts/sync_remote_export_jobs.sh \
     --target vps \
     --job-id YOUR-EXPORT-JOB-ID \
     --delete-remote
   ```
   Successful imports delete the corresponding artifact folders immediately,
   while malformed artifacts are quarantined under `data/quarantine/`.

---

## 2. Client Setup

The client CLI (`opti`) can run on Windows, Mac, or Linux. It requires **Python 3.11+**.

### Windows
1. Install [Python 3.11+](https://www.python.org/downloads/windows/).
2. Open PowerShell and clone the repo.
3. Create a virtual environment:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r client/requirements.txt
   ```

### macOS / Linux
1. Clone the repo and navigate to the directory.
2. Create a virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r client/requirements.txt
   ```

### Configuration
Create a configuration file at `~/.opti/config.yaml` (Linux/Mac) or `%USERPROFILE%\.opti\config.yaml` (Windows):
```yaml
server_url: http://your-vps-ip:8000
api_key: your-strong-secret-key
```

---

## 3. Workflow & Usage

1. **Submit a Job**:
   ```bash
   python -m client.opti submit --algo MyBot.algo --config job.yaml
   ```
2. **Watch Progress**:
   ```bash
   python -m client.opti watch <job-id>
   ```
3. **Analyze Results**:
   ```bash
   python -m client.opti results <job-id> --sort-by net_profit
   ```

---

## 4. Troubleshooting

### "Unable to determine destination" (Path Mapping)
When the server runs in Docker but spawns sibling containers, it passes paths to the Docker daemon. If you see this error, ensure `HOST_DATA_DIR` in your `.env` is an **absolute path on the host machine**, not a container path.

### "Permission Denied" (Docker Socket)
The server needs access to `/var/run/docker.sock`. On some systems, you may need to add the `opti` user to the `docker` group:
```bash
sudo usermod -aG docker opti
```

### Authentication Failed
Ensure your `CTID` and `data/pwd` are correct. The `ctrader-cli` is very sensitive to these credentials. Check the server logs for detailed error messages:
```bash
docker compose logs -f server
```
