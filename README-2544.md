# xena_rfc2544.py — Xena Traffic Generator Test Script

Runs UDP traffic tests and RFC-2544 benchmarking tests on a Xena chassis using two ports.

---

## Requirements

- Python 3.11 or 3.12 (**required** — see note below)
- `xoa-driver` Python package (`pip install xoa-driver`)
- A running Xena chassis accessible on the network

> **⚠️ Python Version Compatibility**
> `xoa-driver` is not compatible with Python 3.13 or 3.14. Running the script on those versions will cause a `XmpBadParameterError` during the chassis handshake. Use Python 3.11 or 3.12.
>
> To check your available Python versions on Windows:
> ```bash
> py -0
> ```
> To run with a specific version:
> ```bash
> py -3.12 xena_rfc2544.py 60 0 0/0 0/1 --rfc2544
> ```
> To install `xoa-driver` for a specific version:
> ```bash
> py -3.12 -m pip install xoa-driver
> ```

---

## Configuration

Before running, open the script and update these two lines near the top:

```python
CHASSIS_IP = "your-chassis-hostname-or-ip"
USERNAME   = "your-username"
```

---

## Usage

```
python xena_rfc2544.py <duration> <frame_size> <port1> <port2> [OPTIONS]
```

| Argument | Description |
|---|---|
| `duration` | How long each traffic trial runs, in seconds |
| `frame_size` | Frame size in bytes — only used in plain traffic mode (see below) |
| `port1` | First port in `module/port` format, e.g. `0/0` |
| `port2` | Second port in `module/port` format, e.g. `0/1` |

| Option | Description |
|---|---|
| `--rfc2544` | Run the RFC-2544 test suite (see below) |
| `--loss-threshold <float>` | Acceptable frame loss % for Throughput test. Default: `0.0` |

---

## Modes

### Plain Traffic Mode

Sends bidirectional UDP traffic at 100% line rate for the specified duration and logs per-second statistics (throughput, latency, jitter, frame loss).

```bash
python xena_rfc2544.py 30 512 0/0 0/1
```
> Runs a 30-second traffic test with 512-byte frames on ports 0/0 and 0/1.

Output is written to `test.log`.

---

### RFC-2544 Mode

Runs the full RFC-2544 benchmarking suite across all standard Ethernet frame sizes: **64, 128, 256, 512, 1024, 1280, 1518 bytes**.

The `frame_size` argument is ignored in this mode.

```bash
python xena_rfc2544.py 60 0 0/0 0/1 --rfc2544
```
> Runs the RFC-2544 suite with 60-second trials on ports 0/0 and 0/1.

#### Four tests are run for each frame size:

1. **Throughput** — Binary search to find the highest traffic rate (% of line rate) at which frame loss stays at or below the loss threshold.
2. **Latency** — Measures min/max/average latency at the throughput rate found in step 1.
3. **Frame Loss Rate** — Sweeps traffic load from 10% to 100% in 10% steps and records frame loss at each level.
4. **Back-to-Back** — Sends 100% line rate bursts of increasing duration to find the longest burst the device can handle without dropping frames.

#### Controlling the loss threshold:

```bash
# Default: zero frame loss required
python xena_rfc2544.py 60 0 0/0 0/1 --rfc2544

# Allow up to 0.1% frame loss
python xena_rfc2544.py 60 0 0/0 0/1 --rfc2544 --loss-threshold 0.1
```

Output is written to `rfc2544.log` and full results are saved to `rfc2544_results.json`.

---

## Output Files

| File | Mode | Description |
|---|---|---|
| `test.log` | Plain traffic | Per-second statistics for both ports |
| `rfc2544.log` | RFC-2544 | Detailed log of every test iteration |
| `rfc2544_results.json` | RFC-2544 | Full results for all frame sizes in JSON format |

A summary table is also printed to the console at the end of an RFC-2544 run:

```
Size    Throughput    AvgLat P1→P2      MaxB2B
 (B)      (% line)            (ns)       (sec)
--------------------------------------------------
  64        94.531           312           0.40
 128        97.812           298           0.80
 256        98.906           285           1.20
...
```
