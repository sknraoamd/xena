**RFC-2544**

v2 - P_TRAFFIC: Failed error
v3 - Add Unidirectional Support
v4 - At the end we get KeyError: 'max_burst_no_loss_sec'.

**PKT-Sweep**

V2 - Logging error
V3 - Report explicitly the number of packets (not just FPS)
V4 - Result summary has null values in the final 6 columns (Fixed)

**Xena-Two-Ports**

V2 - Summarize the results - Improve final reporting


**RFC2544:**

V4:
This happens when Back-to-Back was skipped (due to indeterminate throughput) — the result dict is `{"skipped": True}` and doesn't have `max_burst_no_loss_sec`. The summary printer doesn't guard against that. Easy fix:Fixed. The summary table now uses `.get()` with `None` fallbacks for every field that could be missing, and prints `N/A` instead of crashing. It also adds a `Note` column that shows `INDETERMINATE` for any frame size where throughput couldn't be determined, so the summary is still readable end-to-end even when some sizes fail.

V3:
Yes, absolutely. The simplest approach is to only transmit from Port 1 → Port 2 (stream 1 only), disable stream 2, and measure RX only on Port 2. Let me update the script:Good, I have everything I need. The changes needed are:

1. Add `--unidirectional` to the CLI parser
2. Thread a `unidirectional` flag through all the helpers and test functions so stream 2 is disabled and only Port 1 TX / Port 2 RX is measuredNow update the helpers to be unidirectional-aware:Now update the stream setup to disable stream2 in unidirectional mode, and the four RFC-2544 test functions to pass the flag through:Now update the four test functions and `run_rfc2544` to pass `unidirectional` through:Good — that one was already updated by replacement 1. Now update `run_rfc2544` to pass `UNIDIRECTIONAL` to all the calls:Now update the header comment and `_parse_args` usage string to document the new flag:To run in unidirectional mode just add the flag:

```bash
python xena_rfc2544.py 60 64 0/0 0/1 --rfc2544 --unidirectional
python xena_rfc2544.py 60 64 0/0 0/1 --rfc2544 --unidirectional --loss-threshold 0.1
```

Here's a summary of what changes in unidirectional mode:

| What | Bidirectional | Unidirectional |
|---|---|---|
| Stream 1 (P1→P2) | Enabled, transmits | Enabled, transmits |
| Stream 2 (P2→P1) | Enabled, transmits | Created but **disabled** |
| `worst_loss` in binary search | `max(loss_fwd, loss_rev)` | `loss_fwd` only |
| Latency measurement | Both port RX | Port 2 RX only (tid=0) |
| Frame Loss Rate | Both directions | P1→P2 only |
| Back-to-Back pass/fail | Both directions zero loss | P1→P2 zero loss only |
| Log banner | `Bidirectional` | `Unidirectional (P1->P2)` |


V2:
Looking at the log, there are two problems:

**Problem 1 — Throughput converged at 0%**
The binary search kept finding loss at every rate and converged to 0% line rate. This means the DUT is dropping packets even at very low rates for 64B frames — likely the port hasn't fully come up, or the stream rate fraction of `0` causes the `_traffic_on` to fail with `P_TRAFFIC: FAILED`.

**Problem 2 — Crash on `_traffic_on` at 0% rate**
When throughput is 0%, `rate_fraction = int(0.0 / 100.0 * 1_000_000) = 0`, and setting a stream to rate 0 then starting traffic is what triggers the `XmpFailedError`. The port refuses to start traffic with a zero-rate stream.

Both problems need fixing. Let me read the current script first:Now I have the full picture. I'll fix three things:

1. **Guard against `throughput_pct = 0`** — skip Latency/FLR/B2B and log a clear warning instead of crashing
2. **Add a minimum rate floor** in the binary search (1% line rate) so the search never reaches 0
3. **Add a minimum rate guard** in `rfc2544_latency` as a safety netNow add the guard in `rfc2544_latency` to skip gracefully if throughput is 0:Now add the same guard in `run_rfc2544` to skip FLR and B2B too when throughput is indeterminate:Also fix the Unicode characters in the summary logging that would cause the same cp1252 error you saw earlier:All clean.Three fixes applied:

**1. Binary search floor at 1% line rate**
The search now starts at `low_pct = 1.0` instead of `0.0`, so `rate_fraction` can never reach zero. This directly prevents the `P_TRAFFIC: FAILED` crash.

**2. Indeterminate throughput detection**
If the search converges without ever finding a passing rate, `best_pct` stays at `0.0` and the result is flagged `"indeterminate": True`. A clear warning is logged explaining the likely cause and suggesting `--loss-threshold`.

**3. Graceful skip for Latency, FLR, and Back-to-Back**
When throughput is indeterminate, all three downstream tests are skipped with a warning rather than crashing. The JSON results still record them as `{"skipped": True}` so the output file remains complete.

Regarding the high loss you saw — 64B frames at even 1% line rate showing 89% loss suggests the DUT itself (your NIC or switch under test) is genuinely dropping packets, possibly due to buffer limits, flow control not being set up, or the MAC addresses in the headers not matching what the DUT expects. Worth checking those independently.


** Packet Sweep **

V2:
The Windows console (cp1252 encoding) can't display the `─` Unicode box-drawing character. Fix is to replace those with plain `-` characters in the script.All clean — no non-ASCII characters remain.Three substitutions were made throughout the file:

- `─` (box-drawing) → `-`
- `—` (em dash) → `-`
- `→` (arrow) → `->`

These appeared in log strings, comment separators, and the summary table direction labels (`P1->P2`, `P2->P1`). The script should now run without any encoding errors on Windows.
