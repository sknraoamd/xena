# xena_packet_sweep.py — Automatic Packet Size Sweep

Runs a single bidirectional UDP traffic test across all standard RFC-2544 frame sizes in one go — no need to run the script multiple times.

For each frame size the script sends traffic at 100% line rate for the specified duration, collects per-second statistics, then moves on to the next size. At the end, a consolidated summary table is printed covering all frame sizes side by side.

---

## Requirements

- Python 3.11 or 3.12 (**required** — `xoa-driver` is not compatible with Python 3.13 or 3.14)
- `xoa-driver` Python package (`pip install xoa-driver`)
- A running Xena chassis accessible on the network

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
python xena_packet_sweep.py <duration> <port1> <port2>
```

| Argument | Description |
|---|---|
| `duration` | How long traffic runs for each frame size, in seconds |
| `port1` | First port in `module/port` format, e.g. `0/0` |
| `port2` | Second port in `module/port` format, e.g. `0/1` |

**Example:**
```bash
python xena_packet_sweep.py 30 0/0 0/1
```
> Runs 30 seconds of traffic per frame size on ports 0/0 and 0/1.
> Total runtime: ~30 × 7 = ~3.5 minutes.

---

## Frame Sizes

The script automatically iterates over all 7 standard RFC-2544 Ethernet frame sizes (bytes, including FCS):

```
64, 128, 256, 512, 1024, 1280, 1518
```

---

## What It Measures

For each frame size and each direction (Port 1 → Port 2 and Port 2 → Port 1):

| Metric | Description |
|---|---|
| Avg TX fps | Average transmit rate in frames per second |
| Avg RX fps | Average receive rate in frames per second |
| Total TX pkts | Total packets transmitted over the trial |
| Total RX pkts | Total packets received over the trial |
| Lat min / avg / max | Minimum, average, and maximum latency in nanoseconds |
| Jit avg | Average jitter in nanoseconds |
| Lost pkts | Total lost packets (TX − RX) |

---

## Output

### Console — Summary Table

At the end of the run a consolidated table is printed showing results for all frame sizes at a glance:

```
==========================================================================================================
  PACKET SWEEP — CONSOLIDATED SUMMARY
==========================================================================================================
  Size  Direction    Avg TX fps    Avg RX fps    Total TX      Total RX  Lat min ns  Lat avg ns  Lat max ns  Jit avg ns   Lost pkts
----------------------------------------------------------------------------------------------------------
    64  P1→P2          14880952      14880952    446428560    446428560         312         318         334          12           0
        P2→P1          14880952      14880952    446428560    446428560         308         315         330          10           0
----------------------------------------------------------------------------------------------------------
   128  P1→P2           8127000       8127000    243810000    243810000         298         305         320           9           0
   ...
==========================================================================================================
```

### Log File

Per-second statistics for every frame size are written to `packet_sweep.log` in the working directory. Each line is prefixed with the current frame size for easy filtering, e.g.:

```
2025-03-29 10:01:05  [64B] s=  1  P1 TX=14880952pps RX=14880952pps  lat_avg=318ns  loss=0  |  P2 ...
2025-03-29 10:01:06  [64B] s=  2  P1 TX=14880952pps RX=14880952pps  lat_avg=316ns  loss=0  |  P2 ...
...
2025-03-29 10:02:15  [128B] s=  1  P1 TX=8127000pps ...
```

---

## Notes

- Traffic runs at **100% line rate** for all frame sizes. There is no binary search or loss threshold — this is a straightforward sweep, not a throughput search.
- The chassis connection and port reservation happen **once** at the start. Streams are reconfigured between sizes without dropping the connection.
- A 2-second settling pause is inserted between frame sizes to allow the port to stabilise before the next trial begins.
- If the script is interrupted with `Ctrl+C`, streams and ports are cleaned up before exiting.
