# NotSoBlindBooking

A web app that reverse-engineers the [Eurowings Blind Booking](https://blindbooking.eurowings.com) site to reveal which destinations are actually available for a given airport, date range, and passenger count — before you commit to buying.

Results stream live to your browser as the search runs. Available destinations show in green, uncertain ones in yellow, and unavailable ones are hidden.

## How it works

The Eurowings blind booking page shows a price only when **≥ 2 selected destinations are simultaneously available**. The app exploits this as a binary oracle.

For each travel theme it runs an adaptive **constraint-satisfaction search**:

1. Query all cities at once — if no price, nothing is available, done immediately.
2. Test triples of cities in a greedy coverage order, recording each result as a constraint.
3. Every few queries, run a backtracking CSP solver. If exactly one consistent assignment remains, emit the full result.
4. When the oracle is noisy (contradictory results), fall back to direct-evidence voting and flag anything conflicting as *uncertain*.

## Project structure

```
.
├── app/
│   ├── algorithm.py        — CSP-based destination classifier
│   ├── browser.py          — Async Playwright automation (Eurowings page flow)
│   ├── server.py           — FastAPI app, SSE streaming, search orchestration
│   └── templates/
│       └── index.html      — Single-page UI (vanilla JS + Server-Sent Events)
├── scripts/
│   ├── setup.ps1           — Windows development setup
│   ├── setup.sh            — Linux / macOS development setup
│   └── deploy.sh           — Linux production setup (systemd service)
├── tests/
│   └── test_algorithm.py
├── pyproject.toml
└── requirements.txt
```

---

## Prerequisites

| Requirement | Windows | Linux / macOS |
|---|---|---|
| **Python 3.11+** | [python.org/downloads](https://www.python.org/downloads/) — tick **"Add Python to PATH"** | Usually pre-installed; `sudo apt-get install python3 python3-venv` if missing |
| **Git** | [git-scm.com](https://git-scm.com/download/win) | `sudo apt-get install git` |

The setup scripts handle everything else (virtual environment, pip packages, Chromium).

---

## Setup — Windows (development)

```powershell
git clone git@github.com:KarlBuckler/NotSoBlindBooking.git
cd NotSoBlindBooking
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

The script checks your Python version, creates `.venv`, installs all packages, and downloads the Chromium browser used by Playwright.

Start the server:

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn app.server:app --reload
```

Open **http://localhost:8000**. Chromium runs headless by default. To make the browser window visible for debugging:

```powershell
$env:HEADLESS = "false"
uvicorn app.server:app --reload
```

---

## Setup — Linux / macOS (development)

```bash
git clone git@github.com:KarlBuckler/NotSoBlindBooking.git
cd NotSoBlindBooking
bash scripts/setup.sh
```

The script checks Python, creates `.venv`, installs all packages, downloads Chromium, and (on Linux) installs the required system libraries via `playwright install-deps`.

Start the server:

```bash
source .venv/bin/activate
uvicorn app.server:app --reload
```

Open **http://localhost:8000**. Chromium runs headless on Linux.

---

## Setup — Linux production (systemd service)

Clone the repo directly to the install location and run the deploy script as root:

```bash
git clone git@github.com:KarlBuckler/NotSoBlindBooking.git /opt/NotSoBlindBooking
cd /opt/NotSoBlindBooking
sudo bash scripts/deploy.sh
```

The deploy script:
1. Installs `python3`, `python3-venv`, `git` via apt
2. Creates `.venv` and installs all packages
3. Downloads Chromium and its system libraries
4. Writes and enables a **systemd service** (`blindbooking`) that starts on boot and restarts on failure

Useful commands after deploy:

```bash
systemctl status blindbooking       # check it's running
journalctl -u blindbooking -f       # follow live logs
systemctl restart blindbooking      # restart after a code update
```

To update the app:

```bash
cd /opt/NotSoBlindBooking
git pull
/opt/NotSoBlindBooking/.venv/bin/pip install -r requirements.txt
systemctl restart blindbooking
```

---

## Configuration

| Setting | Location | Default |
|---|---|---|
| Port | `scripts/deploy.sh` → `PORT` | `8000` |
| Headless mode | `HEADLESS` env var | `true` (set to `false` to show the browser window) |
| Slow-mo delay | `app/browser.py` → `SLOW_MO` | `0` ms (increase if bot detection triggers) |
| Airport list | `app/server.py` → `AIRPORTS` | 11 airports (hardcoded) |

---

## Supported airports

| IATA | City |
|---|---|
| BER | Berlin |
| DUS | Düsseldorf |
| GRZ | Graz |
| HAM | Hamburg |
| HAJ | Hannover |
| CGN | Köln-Bonn |
| PMI | Palma de Mallorca |
| PRG | Prag |
| SZG | Salzburg |
| ARN | Stockholm |
| STR | Stuttgart |

---

## Result states

| State | Display | Meaning |
|---|---|---|
| **available** | ✅ green chip | Consistent evidence — destination has flights |
| **uncertain** | ❓ yellow chip | Oracle gave contradictory results (noise) |
| **unavailable** | hidden | No flights in this date range / theme |

---

## Running tests

```bash
# Activate the venv first
source .venv/bin/activate        # Linux/macOS
.\.venv\Scripts\Activate.ps1     # Windows

pytest
```

The tests use a simulated oracle and verify correctness across zero, one, two, and many available cities.

---

## Troubleshooting

**`playwright install chromium` fails / no browser window**  
Make sure you are running the command inside the activated venv (`.venv\Scripts\Activate.ps1` on Windows, `source .venv/bin/activate` on Linux).

**403 or bot detection**  
`playwright-stealth` handles most fingerprinting. If detection increases, raise `SLOW_MO` in `app/browser.py` (try `500`–`1000` ms).

**No themes found for airport**  
That airport may not be enrolled in blind booking for your selected dates. Try different dates or a different airport.

**Service won't start on Linux**  
```bash
journalctl -u blindbooking -n 50
```
Most common cause: Playwright browser not installed. Re-run `sudo bash scripts/deploy.sh` from the repo root, or manually:
```bash
/opt/NotSoBlindBooking/.venv/bin/playwright install chromium
/opt/NotSoBlindBooking/.venv/bin/playwright install-deps chromium
systemctl restart blindbooking
```
