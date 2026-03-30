################################################################
#
#              RFC-2544 BENCHMARKING TESTS
#
# Extends xena_two_port.py with RFC-2544 test suite:
#   1. Throughput       - Binary search for max loss-free rate
#   2. Latency          - Measure latency at throughput rate
#   3. Frame Loss Rate  - Loss % swept across load levels
#   4. Back-to-Back     - Max burst length without frame loss
#
# Usage:
#   python xena_rfc2544.py <duration> <frame_size> <port1> <port2> [OPTIONS]
#
# Positional args:
#   duration      Trial duration in seconds (e.g. 60)
#   frame_size    Frame size in bytes for plain traffic mode (ignored in RFC-2544 mode)
#   port1         TX port, e.g. "0/0"
#   port2         RX port, e.g. "0/1"
#
# Options:
#   --rfc2544                   Run RFC-2544 suite instead of plain traffic test.
#                               Iterates over standard frame sizes:
#                               64, 128, 256, 512, 1024, 1280, 1518 bytes.
#   --loss-threshold <float>    Acceptable frame loss % for Throughput binary search.
#                               Default: 0.0 (zero loss). Examples: 0.1, 0.01, 1.0
#   --unidirectional            Only transmit Port1->Port2. Port2 receives only.
#                               Useful when reverse traffic causes interference
#                               or when testing a one-way path.
#
# Examples:
#   python xena_rfc2544.py 60 64 0/0 0/1 --rfc2544
#   python xena_rfc2544.py 60 64 0/0 0/1 --rfc2544 --loss-threshold 0.1
#   python xena_rfc2544.py 60 64 0/0 0/1 --rfc2544 --unidirectional
#   python xena_rfc2544.py 60 64 0/0 0/1 --rfc2544 --unidirectional --loss-threshold 0.1
#   python xena_rfc2544.py 10 64 0/0 0/1
#
################################################################

import asyncio
import sys
import os
import json
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

from xoa_driver import testers, modules, ports, utils, enums
from xoa_driver.enums import *
from xoa_driver.hlfuncs import mgmt, headers
from xoa_driver.misc import Hex

# -----------------------
# CLI ARG PARSING
# -----------------------

def _parse_args():
    """Parse positional and optional CLI arguments."""
    args = sys.argv[1:]

    # Positional args
    if len(args) < 4:
        print("Usage: xena_rfc2544.py <duration> <frame_size> <port1> <port2> [--rfc2544] [--unidirectional] [--loss-threshold <float>]")
        sys.exit(1)

    duration   = int(args[0])
    frame_size = int(args[1])
    port1      = args[2]
    port2      = args[3]

    # Optional flags
    run_rfc2544    = "--rfc2544" in args
    unidirectional = "--unidirectional" in args
    loss_threshold = 0.0
    if "--loss-threshold" in args:
        idx = args.index("--loss-threshold")
        try:
            loss_threshold = float(args[idx + 1])
        except (IndexError, ValueError):
            print("ERROR: --loss-threshold requires a numeric argument, e.g. --loss-threshold 0.1")
            sys.exit(1)
        if not (0.0 <= loss_threshold <= 100.0):
            print("ERROR: --loss-threshold must be between 0.0 and 100.0")
            sys.exit(1)

    return duration, frame_size, port1, port2, run_rfc2544, loss_threshold, unidirectional


TRAFFIC_DURATION, FRAME_SIZE, PORT1, PORT2, RUN_RFC2544, LOSS_THRESHOLD, UNIDIRECTIONAL = _parse_args()

# -----------------------
# GLOBAL PARAMS
# -----------------------
CHASSIS_IP = "xena-11464930.amd.com"
USERNAME   = "maniskum"

# -----------------------
# RFC-2544 CONFIGURATION
# -----------------------

# Standard RFC-2544 frame sizes (bytes, including Ethernet FCS)
RFC2544_FRAME_SIZES = [64, 128, 256, 512, 1024, 1280, 1518]

@dataclass
class RFC2544Config:
    # Throughput binary search parameters
    throughput_resolution_pct: float = 0.1      # stop when high-low < this %
    throughput_max_iterations: int   = 20        # max binary search rounds
    throughput_loss_threshold: float = 0.0       # acceptable frame loss %  (0 = zero loss)

    # Trial duration (seconds) for each RFC-2544 measurement point
    trial_duration: int = 60                     # RFC-2544 recommends 60 s

    # Frame Loss Rate sweep: list of load levels (% of line rate, 0-100)
    flr_load_points: list = field(default_factory=lambda: [10, 20, 30, 40, 50, 60, 70, 80, 90, 100])

    # Back-to-back: burst duration step (seconds) and max burst duration
    b2b_step_sec: float = 0.1
    b2b_max_sec:  float = 2.0

    # Latency: number of iterations to average
    latency_iterations: int = 20

# -----------------------
# PACKET HEADER TEMPLATES  (same as original)
# -----------------------
DESTINATION_MAC  = "AAAAAAAAAAAA"
SOURCE_MAC       = "BBBBBBBBBBBB"
ETHERNET_TYPE    = "0800"
IP               = "45000032000000007F11A655C0A80A0AC0A80A0B"
UDP              = "10C310C400220000"

DESTINATION_MAC2 = "BBBBBBBBBBBB"
SOURCE_MAC2      = "AAAAAAAAAAAA"
ETHERNET_TYPE2   = "0800"
IP2              = "4500002E000000007F11A659C0A80A0BC0A80A0A"
UDP2             = "10C410C3001A0000"

# -----------------------
# HELPERS
# -----------------------

def _loss_pct(tx: int, rx: int) -> float:
    """Return frame loss as a percentage (0-100)."""
    if tx == 0:
        return 0.0
    return max(0.0, (tx - rx) / tx * 100.0)


async def _clear_stats(port1: ports.GenericL23Port, port2: ports.GenericL23Port) -> None:
    await asyncio.gather(
        port1.statistics.tx.clear.set(),
        port1.statistics.rx.clear.set(),
        port2.statistics.tx.clear.set(),
        port2.statistics.rx.clear.set(),
    )


async def _get_frame_counts(
        port1: ports.GenericL23Port,
        port2: ports.GenericL23Port,
        unidirectional: bool = False,
) -> tuple[int, int, int, int]:
    """Return (p1_tx, p2_rx, p2_tx, p1_rx) total packet counts since last clear.
    In unidirectional mode p2_tx and p1_rx are returned as 0.
    """
    if unidirectional:
        p1_tx_r, p2_rx_r = await asyncio.gather(
            port1.statistics.tx.total.get(),
            port2.statistics.rx.total.get(),
        )
        return (
            p1_tx_r.packet_count_since_cleared,
            p2_rx_r.packet_count_since_cleared,
            0,
            0,
        )
    else:
        p1_tx_r, p2_rx_r, p2_tx_r, p1_rx_r = await asyncio.gather(
            port1.statistics.tx.total.get(),
            port2.statistics.rx.total.get(),
            port2.statistics.tx.total.get(),
            port1.statistics.rx.total.get(),
        )
        return (
            p1_tx_r.packet_count_since_cleared,
            p2_rx_r.packet_count_since_cleared,
            p2_tx_r.packet_count_since_cleared,
            p1_rx_r.packet_count_since_cleared,
        )


async def _set_rate(stream1, stream2, rate_fraction: int, unidirectional: bool = False) -> None:
    """Set both streams to the same rate fraction (1 000 000 = 100 %).
    In unidirectional mode only stream1 is updated.
    """
    if unidirectional:
        await stream1.rate.fraction.set(rate_fraction)
    else:
        await asyncio.gather(
            stream1.rate.fraction.set(rate_fraction),
            stream2.rate.fraction.set(rate_fraction),
        )


async def _traffic_on(port1: ports.GenericL23Port, port2: ports.GenericL23Port) -> None:
    await asyncio.gather(
        port1.traffic.state.set_start(),
        port2.traffic.state.set_start(),
    )


async def _traffic_off(port1: ports.GenericL23Port, port2: ports.GenericL23Port) -> None:
    await asyncio.gather(
        port1.traffic.state.set_stop(),
        port2.traffic.state.set_stop(),
    )


async def _get_latency(
        port1: ports.GenericL23Port,
        port2: ports.GenericL23Port,
        unidirectional: bool = False,
) -> dict:
    """Return latency stats. In unidirectional mode only Port2 RX (tid=0) is read."""
    if unidirectional:
        p2_lat = await port2.statistics.rx.access_tpld(0).latency.get()
        return {
            "p1_rx_min_ns": None,
            "p1_rx_max_ns": None,
            "p1_rx_avg_ns": None,
            "p2_rx_min_ns": p2_lat.min_val,
            "p2_rx_max_ns": p2_lat.max_val,
            "p2_rx_avg_ns": p2_lat.avg_val,
        }
    else:
        p1_lat, p2_lat = await asyncio.gather(
            port1.statistics.rx.access_tpld(1).latency.get(),
            port2.statistics.rx.access_tpld(0).latency.get(),
        )
        return {
            "p1_rx_min_ns": p1_lat.min_val,
            "p1_rx_max_ns": p1_lat.max_val,
            "p1_rx_avg_ns": p1_lat.avg_val,
            "p2_rx_min_ns": p2_lat.min_val,
            "p2_rx_max_ns": p2_lat.max_val,
            "p2_rx_avg_ns": p2_lat.avg_val,
        }


# -----------------------
# RFC-2544 TEST FUNCTIONS
# -----------------------

async def rfc2544_throughput(
        port1: ports.GenericL23Port,
        port2: ports.GenericL23Port,
        stream1, stream2,
        frame_size: int,
        cfg: RFC2544Config,
        unidirectional: bool = False,
) -> dict:
    """
    RFC-2544 Throughput - binary search for the highest load (% of line rate)
    at which frame loss <= cfg.throughput_loss_threshold %.

    The search floor is 1% line rate. If loss is still above threshold at 1%,
    the result is reported as 0% (indeterminate) and subsequent tests are skipped.

    Returns a dict with the result for this frame size.
    """
    MIN_RATE_PCT = 1.0   # never test below 1% - avoids zero-rate stream errors

    logging.info(f"[RFC-2544 Throughput] frame_size={frame_size}B  target_loss<={cfg.throughput_loss_threshold}%")

    low_pct   = MIN_RATE_PCT
    high_pct  = 100.0
    best_pct  = 0.0
    best_loss = 100.0
    iterations = []

    for iteration in range(cfg.throughput_max_iterations):
        mid_pct = (low_pct + high_pct) / 2.0
        mid_pct = max(mid_pct, MIN_RATE_PCT)          # safety floor
        rate_fraction = int(mid_pct / 100.0 * 1_000_000)

        await _set_rate(stream1, stream2, rate_fraction, unidirectional)
        await _clear_stats(port1, port2)
        await _traffic_on(port1, port2)
        await asyncio.sleep(cfg.trial_duration)
        await _traffic_off(port1, port2)
        await asyncio.sleep(1)

        p1_tx, p2_rx, p2_tx, p1_rx = await _get_frame_counts(port1, port2, unidirectional)
        loss_fwd = _loss_pct(p1_tx, p2_rx)
        loss_rev = _loss_pct(p2_tx, p1_rx) if not unidirectional else 0.0
        worst_loss = loss_fwd if unidirectional else max(loss_fwd, loss_rev)

        logging.info(
            f"  iter={iteration+1:2d}  rate={mid_pct:.2f}%  "
            f"loss_fwd={loss_fwd:.4f}%  loss_rev={loss_rev:.4f}%"
        )
        iterations.append({
            "iteration": iteration + 1,
            "rate_pct": mid_pct,
            "p1_tx": p1_tx, "p2_rx": p2_rx,
            "p2_tx": p2_tx, "p1_rx": p1_rx,
            "loss_fwd_pct": loss_fwd,
            "loss_rev_pct": loss_rev,
        })

        if worst_loss <= cfg.throughput_loss_threshold:
            best_pct  = mid_pct
            best_loss = worst_loss
            low_pct   = mid_pct
        else:
            high_pct = mid_pct

        if (high_pct - low_pct) < cfg.throughput_resolution_pct:
            logging.info(f"  Converged at {best_pct:.3f}% line rate.")
            break

    if best_pct == 0.0:
        logging.warning(
            f"[RFC-2544 Throughput] WARNING: Could not find a passing rate above "
            f"{MIN_RATE_PCT}% for {frame_size}B frames with loss threshold "
            f"{cfg.throughput_loss_threshold}%. "
            f"The DUT may be dropping packets at all tested rates. "
            f"Consider raising --loss-threshold or checking the DUT."
        )

    result = {
        "test": "throughput",
        "frame_size_bytes": frame_size,
        "throughput_pct": round(best_pct, 4),
        "worst_loss_pct": round(best_loss, 6),
        "indeterminate": best_pct == 0.0,
        "iterations": iterations,
    }
    logging.info(f"[RFC-2544 Throughput] RESULT: {best_pct:.3f}% @ {frame_size}B")
    return result


async def rfc2544_latency(
        port1: ports.GenericL23Port,
        port2: ports.GenericL23Port,
        stream1, stream2,
        frame_size: int,
        throughput_pct: float,
        cfg: RFC2544Config,
        unidirectional: bool = False,
) -> dict:
    """
    RFC-2544 Latency - measure latency at the previously determined
    throughput rate. Averages over cfg.latency_iterations samples.
    Skipped if throughput_pct is 0 (indeterminate throughput result).
    """
    if throughput_pct == 0.0:
        logging.warning(
            f"[RFC-2544 Latency] Skipping {frame_size}B - "
            f"throughput was indeterminate (0%). No valid rate to test at."
        )
        return {
            "test": "latency",
            "frame_size_bytes": frame_size,
            "load_pct": 0.0,
            "skipped": True,
            "p1_rx": {"min_ns": None, "max_ns": None, "avg_ns": None},
            "p2_rx": {"min_ns": None, "max_ns": None, "avg_ns": None},
            "samples": [],
        }

    logging.info(
        f"[RFC-2544 Latency] frame_size={frame_size}B  "
        f"load={throughput_pct:.3f}%  iterations={cfg.latency_iterations}"
    )

    rate_fraction = int(throughput_pct / 100.0 * 1_000_000)
    await _set_rate(stream1, stream2, rate_fraction)

    samples = []
    for i in range(cfg.latency_iterations):
        await _clear_stats(port1, port2)
        await _traffic_on(port1, port2)
        await asyncio.sleep(cfg.trial_duration)
        lat = await _get_latency(port1, port2, unidirectional)
        await _traffic_off(port1, port2)
        await asyncio.sleep(1)
        samples.append(lat)
        logging.info(
            f"  sample={i+1:2d}  "
            f"p1_rx_avg={lat['p1_rx_avg_ns']}ns  p2_rx_avg={lat['p2_rx_avg_ns']}ns"
        )

    def _avg(key): return sum(s[key] for s in samples) / len(samples)
    def _min(key): return min(s[key] for s in samples)
    def _max(key): return max(s[key] for s in samples)

    result = {
        "test": "latency",
        "frame_size_bytes": frame_size,
        "load_pct": throughput_pct,
        "p1_rx": {
            "min_ns": _min("p1_rx_min_ns"),
            "max_ns": _max("p1_rx_max_ns"),
            "avg_ns": _avg("p1_rx_avg_ns"),
        },
        "p2_rx": {
            "min_ns": _min("p2_rx_min_ns"),
            "max_ns": _max("p2_rx_max_ns"),
            "avg_ns": _avg("p2_rx_avg_ns"),
        },
        "samples": samples,
    }
    logging.info(
        f"[RFC-2544 Latency] RESULT  p1_rx_avg={result['p1_rx']['avg_ns']:.0f}ns  "
        f"p2_rx_avg={result['p2_rx']['avg_ns']:.0f}ns"
    )
    return result


async def rfc2544_frame_loss_rate(
        port1: ports.GenericL23Port,
        port2: ports.GenericL23Port,
        stream1, stream2,
        frame_size: int,
        cfg: RFC2544Config,
        unidirectional: bool = False,
) -> dict:
    """
    RFC-2544 Frame Loss Rate - measure frame loss at each load level
    defined in cfg.flr_load_points.
    """
    logging.info(
        f"[RFC-2544 Frame Loss Rate] frame_size={frame_size}B  "
        f"load_points={cfg.flr_load_points}"
    )

    data_points = []
    for load_pct in cfg.flr_load_points:
        rate_fraction = int(load_pct / 100.0 * 1_000_000)
        await _set_rate(stream1, stream2, rate_fraction, unidirectional)
        await _clear_stats(port1, port2)
        await _traffic_on(port1, port2)
        await asyncio.sleep(cfg.trial_duration)
        await _traffic_off(port1, port2)
        await asyncio.sleep(1)

        p1_tx, p2_rx, p2_tx, p1_rx = await _get_frame_counts(port1, port2, unidirectional)
        loss_fwd = _loss_pct(p1_tx, p2_rx)
        loss_rev = _loss_pct(p2_tx, p1_rx) if not unidirectional else 0.0

        logging.info(
            f"  load={load_pct:3d}%  "
            f"loss_fwd={loss_fwd:.4f}%  loss_rev={loss_rev:.4f}%"
        )
        data_points.append({
            "load_pct": load_pct,
            "p1_tx": p1_tx, "p2_rx": p2_rx,
            "p2_tx": p2_tx, "p1_rx": p1_rx,
            "loss_fwd_pct": round(loss_fwd, 6),
            "loss_rev_pct": round(loss_rev, 6),
        })

    result = {
        "test": "frame_loss_rate",
        "frame_size_bytes": frame_size,
        "data_points": data_points,
    }
    logging.info(f"[RFC-2544 Frame Loss Rate] RESULT: {len(data_points)} points collected")
    return result


async def rfc2544_back_to_back(
        port1: ports.GenericL23Port,
        port2: ports.GenericL23Port,
        stream1, stream2,
        frame_size: int,
        cfg: RFC2544Config,
        unidirectional: bool = False,
) -> dict:
    """
    RFC-2544 Back-to-Back - find the longest burst (in seconds) that the DUT
    can absorb at 100% line rate without dropping frames.
    Uses binary search between 0 and cfg.b2b_max_sec.
    """
    logging.info(
        f"[RFC-2544 Back-to-Back] frame_size={frame_size}B  "
        f"max_burst={cfg.b2b_max_sec}s  step={cfg.b2b_step_sec}s"
    )

    await _set_rate(stream1, stream2, 1_000_000, unidirectional)  # 100% line rate

    low_sec  = 0.0
    high_sec = cfg.b2b_max_sec
    best_sec = 0.0
    trials   = []

    # Coarse step sweep first, then binary-search refinement
    step = cfg.b2b_step_sec
    burst_sec = step
    while burst_sec <= cfg.b2b_max_sec:
        await _clear_stats(port1, port2)
        await _traffic_on(port1, port2)
        await asyncio.sleep(burst_sec)
        await _traffic_off(port1, port2)
        await asyncio.sleep(1)

        p1_tx, p2_rx, p2_tx, p1_rx = await _get_frame_counts(port1, port2, unidirectional)
        loss_fwd = _loss_pct(p1_tx, p2_rx)
        loss_rev = _loss_pct(p2_tx, p1_rx) if not unidirectional else 0.0
        no_loss = (loss_fwd == 0.0) if unidirectional else (loss_fwd == 0.0 and loss_rev == 0.0)

        logging.info(
            f"  burst={burst_sec:.2f}s  "
            f"loss_fwd={loss_fwd:.4f}%  loss_rev={loss_rev:.4f}%  {'OK' if no_loss else 'LOSS'}"
        )
        trials.append({
            "burst_sec": burst_sec,
            "p1_tx": p1_tx, "p2_rx": p2_rx,
            "p2_tx": p2_tx, "p1_rx": p1_rx,
            "loss_fwd_pct": round(loss_fwd, 6),
            "loss_rev_pct": round(loss_rev, 6),
            "passed": no_loss,
        })

        if no_loss:
            best_sec = burst_sec
        else:
            break   # first failure - stop coarse sweep

        burst_sec += step

    result = {
        "test": "back_to_back",
        "frame_size_bytes": frame_size,
        "max_burst_no_loss_sec": best_sec,
        "trials": trials,
    }
    logging.info(f"[RFC-2544 Back-to-Back] RESULT: max burst = {best_sec:.2f}s @ {frame_size}B")
    return result


# -----------------------
# STREAM SETUP  (shared by both modes)
# -----------------------

async def _create_and_configure_streams(port_obj_1, port_obj_2, frame_size: int, unidirectional: bool = False):
    """Create streams on both ports and return (stream1, stream2).
    In unidirectional mode stream2 is created but disabled so the port
    can still be started without transmitting reverse traffic.
    """
    logging.info(f"Creating streams for frame_size={frame_size}B  unidirectional={unidirectional}")
    my_stream1 = await port_obj_1.streams.create()
    my_stream2 = await port_obj_2.streams.create()

    header_data1 = f"{DESTINATION_MAC}{SOURCE_MAC}{ETHERNET_TYPE}{IP}{UDP}"
    header_data2 = f"{DESTINATION_MAC2}{SOURCE_MAC2}{ETHERNET_TYPE2}{IP2}{UDP2}"

    await asyncio.gather(
        my_stream1.tpld_id.set(0),
        my_stream1.packet.length.set(length_type=LengthType.FIXED, min_val=frame_size, max_val=frame_size),
        my_stream1.packet.header.protocol.set(segments=[
            ProtocolOption.ETHERNET, ProtocolOption.IP, ProtocolOption.UDP]),
        my_stream1.enable.set_on(),
        my_stream1.rate.fraction.set(1_000_000),
        my_stream1.packet.header.data.set(hex_data=Hex(header_data1)),

        my_stream2.tpld_id.set(1),
        my_stream2.packet.length.set(length_type=LengthType.FIXED, min_val=frame_size, max_val=frame_size),
        my_stream2.packet.header.protocol.set(segments=[
            ProtocolOption.ETHERNET, ProtocolOption.IP, ProtocolOption.UDP]),
        # Disable stream2 in unidirectional mode so port2 only receives
        my_stream2.enable.set_off() if unidirectional else my_stream2.enable.set_on(),
        my_stream2.rate.fraction.set(1_000_000),
        my_stream2.packet.header.data.set(hex_data=Hex(header_data2)),
    )

    # Header modifiers (source port incrementing)
    await my_stream1.packet.header.modifiers.configure(1)
    mod1 = my_stream1.packet.header.modifiers.obtain(0)
    await mod1.specification.set(position=36, mask=Hex("FFFF0000"), action=enums.ModifierAction.INC, repetition=1)
    await mod1.range.set(min_val=0, step=1, max_val=65535)

    if not unidirectional:
        await my_stream2.packet.header.modifiers.configure(1)
        mod2 = my_stream2.packet.header.modifiers.obtain(0)
        await mod2.specification.set(position=36, mask=Hex("FFFF0000"), action=enums.ModifierAction.INC, repetition=1)
        await mod2.range.set(min_val=0, step=1, max_val=65535)

    return my_stream1, my_stream2


async def _teardown_streams(stream1, stream2):
    await stream1.packet.header.modifiers.configure(0)
    await stream2.packet.header.modifiers.configure(0)
    await stream1.delete()
    await stream2.delete()


# -----------------------
# ORIGINAL STATISTICS HELPERS  (kept intact)
# -----------------------

async def statistics_background_task(
        port1: ports.GenericL23Port,
        port2: ports.GenericL23Port,
        duration: int,
        stop_event: asyncio.Event
) -> None:
    logging.info("Collecting statistics..")

    count = 0
    resultIterationPort1 = dict()
    runIterationCounterPort1 = 0
    resultIterationPort2 = dict()
    runIterationCounterPort2 = 0

    resp = await port1.speed.current.get()
    tx_port_nominal_speed_Mbps = resp.port_speed
    resp = await port1.speed.reduction.get()
    tx_port_speed_reduction_ppm = resp.ppm
    tx_port_effective_speed = tx_port_nominal_speed_Mbps * (1 - tx_port_speed_reduction_ppm / 1_000_000) * 1_000_000
    logging.info(f"TX port effective speed: {tx_port_effective_speed/1_000_000_000} Gbps")

    resp = await port2.speed.current.get()
    rx_port_nominal_speed_Mbps = resp.port_speed
    resp = await port2.speed.reduction.get()
    rx_port_speed_reduction_ppm = resp.ppm
    rx_port_effective_speed = rx_port_nominal_speed_Mbps * (1 - rx_port_speed_reduction_ppm / 1_000_000) * 1_000_000
    logging.info(f"RX port effective speed: {rx_port_effective_speed/1_000_000_000} Gbps")

    while not stop_event.is_set():
        (p1_tx, p1_rx, p2_tx, p2_rx) = await asyncio.gather(
            port1.statistics.tx.obtain_from_stream(0).get(),
            port1.statistics.rx.access_tpld(1).traffic.get(),
            port2.statistics.tx.obtain_from_stream(0).get(),
            port2.statistics.rx.access_tpld(0).traffic.get(),
        )
        p1rx_latency, p1rx_jitter, p1rx_error, p2rx_latency, p2rx_jitter, p2rx_error = await utils.apply(
            port1.statistics.rx.access_tpld(1).latency.get(),
            port1.statistics.rx.access_tpld(1).jitter.get(),
            port1.statistics.rx.access_tpld(1).errors.get(),
            port2.statistics.rx.access_tpld(0).latency.get(),
            port2.statistics.rx.access_tpld(0).jitter.get(),
            port2.statistics.rx.access_tpld(0).errors.get(),
        )
        p1_tx_resp, p1_rx_resp = await asyncio.gather(
            port1.statistics.tx.total.get(),
            port1.statistics.rx.total.get()
        )
        tx_fps  = p1_tx_resp.packet_count_last_sec
        tx_mpps = tx_fps / 1_000_000
        rx_fps  = p1_rx_resp.packet_count_last_sec
        rx_mpps = rx_fps / 1_000_000

        runIterationCounterPort1 += 1
        runIterationCounterPort2 += 1

        logging.info(f"Port 1 | TX={tx_fps}pps  RX={rx_fps}pps  "
                     f"lat_avg={p1rx_latency.avg_val}ns  loss={p1rx_error.non_incre_seq_event_count}")
        resultIterationPort1[runIterationCounterPort1] = {
            "packet_size": FRAME_SIZE,
            "TX_fps": tx_fps, "RX_fps": rx_fps,
            "TX_Mpps": tx_mpps, "RX_Mpps": rx_mpps,
            "TX_bytes": p1_tx.byte_count_since_cleared,
            "TX_pkts": p1_tx.packet_count_since_cleared,
            "RX_bytes": p1_rx.byte_count_since_cleared,
            "RX_pkts": p1_rx.packet_count_since_cleared,
            "lat_min_ns": p1rx_latency.min_val,
            "lat_max_ns": p1rx_latency.max_val,
            "lat_avg_ns": p1rx_latency.avg_val,
            "jit_min_ns": p1rx_jitter.min_val,
            "jit_max_ns": p1rx_jitter.max_val,
            "jit_avg_ns": p1rx_jitter.avg_val,
            "lost_pkts": p1rx_error.non_incre_seq_event_count,
            "misordered": p1rx_error.swapped_seq_misorder_event_count,
            "payload_err": p1rx_error.non_incre_payload_packet_count,
        }

        p2_tx_resp, p2_rx_resp = await asyncio.gather(
            port2.statistics.tx.total.get(),
            port2.statistics.rx.total.get()
        )
        p2_tx_fps  = p2_tx_resp.packet_count_last_sec
        p2_tx_mpps = p2_tx_fps / 1_000_000
        p2_rx_fps  = p2_rx_resp.packet_count_last_sec
        p2_rx_mpps = p2_rx_fps / 1_000_000

        logging.info(f"Port 2 | TX={p2_tx_fps}pps  RX={p2_rx_fps}pps  "
                     f"lat_avg={p2rx_latency.avg_val}ns  loss={p2rx_error.non_incre_seq_event_count}")
        resultIterationPort2[runIterationCounterPort2] = {
            "packet_size": FRAME_SIZE,
            "TX_fps": p2_tx_fps, "RX_fps": p2_rx_fps,
            "TX_Mpps": p2_tx_mpps, "RX_Mpps": p2_rx_mpps,
            "TX_bytes": p2_tx.byte_count_since_cleared,
            "TX_pkts": p2_tx.packet_count_since_cleared,
            "RX_bytes": p2_rx.byte_count_since_cleared,
            "RX_pkts": p2_rx.packet_count_since_cleared,
            "lat_min_ns": p2rx_latency.min_val,
            "lat_max_ns": p2rx_latency.max_val,
            "lat_avg_ns": p2rx_latency.avg_val,
            "jit_min_ns": p2rx_jitter.min_val,
            "jit_max_ns": p2rx_jitter.max_val,
            "jit_avg_ns": p2rx_jitter.avg_val,
            "lost_pkts": p2rx_error.non_incre_seq_event_count,
            "misordered": p2rx_error.swapped_seq_misorder_event_count,
            "payload_err": p2rx_error.non_incre_payload_packet_count,
        }

        count += 1
        await asyncio.sleep(1.0)
        if count >= duration:
            stop_event.set()

    print(f"Final Result Data Port1: {str(resultIterationPort1)} Result Data END")
    print(f"Final Result Data Port2: {str(resultIterationPort2)} Result Data END")


async def final_statistic_fetcher(
        port1: ports.GenericL23Port,
        port2: ports.GenericL23Port
) -> None:
    (p1_tx, p1_rx, p2_tx, p2_rx) = await asyncio.gather(
        port1.statistics.tx.obtain_from_stream(0).get(),
        port1.statistics.rx.access_tpld(1).traffic.get(),
        port2.statistics.tx.obtain_from_stream(0).get(),
        port2.statistics.rx.access_tpld(0).traffic.get(),
    )
    logging.info("Frame Loss (TX-RX)")
    logging.info(f"  Port1->Port2 (tid=0): {p1_tx.packet_count_since_cleared - p2_rx.packet_count_since_cleared}")
    logging.info(f"  Port2->Port1 (tid=1): {p2_tx.packet_count_since_cleared - p1_rx.packet_count_since_cleared}")


# -----------------------
# CHASSIS / PORT SETUP  (shared entry point)
# -----------------------

async def _connect_and_reserve(chassis: str, username: str, port1_str: str, port2_str: str):
    logging.info(f"Connecting to chassis: {chassis}  user={username}")
    tester_obj = await testers.L23Tester(
        host=chassis, username=username, password="amd1234!", port=22606, enable_logging=False
    )

    _mid1, _pid1 = int(port1_str.split("/")[0]), int(port1_str.split("/")[1])
    _mid2, _pid2 = int(port2_str.split("/")[0]), int(port2_str.split("/")[1])

    module_obj_1 = tester_obj.modules.obtain(_mid1)
    module_obj_2 = tester_obj.modules.obtain(_mid2)

    if isinstance(module_obj_1, modules.ModuleChimera) or isinstance(module_obj_2, modules.ModuleChimera):
        raise RuntimeError("Chimera modules not supported.")

    port_obj_1 = module_obj_1.ports.obtain(_pid1)
    port_obj_2 = module_obj_2.ports.obtain(_pid2)

    logging.info(f"Reserving ports {_mid1}/{_pid1} and {_mid2}/{_pid2}")
    await mgmt.free_module(module=module_obj_1, should_free_ports=False)
    await mgmt.reserve_port(port=port_obj_1, force=True)
    await mgmt.reset_port(port_obj_1)
    await mgmt.free_module(module=module_obj_2, should_free_ports=False)
    await mgmt.reserve_port(port=port_obj_2, force=True)
    await mgmt.reset_port(port_obj_2)

    await asyncio.sleep(5)
    return tester_obj, port_obj_1, port_obj_2


# -----------------------
# RFC-2544 MAIN RUNNER
# -----------------------

async def run_rfc2544(
        chassis: str,
        username: str,
        port1_str: str,
        port2_str: str,
        cfg: Optional[RFC2544Config] = None,
        unidirectional: bool = False,
):
    """Run the full RFC-2544 test suite across all standard frame sizes."""
    if cfg is None:
        cfg = RFC2544Config()
        cfg.trial_duration          = TRAFFIC_DURATION   # honour CLI duration arg
        cfg.throughput_loss_threshold = LOSS_THRESHOLD   # honour --loss-threshold arg

    logging.basicConfig(
        format="%(asctime)s  %(message)s",
        level=logging.DEBUG,
        handlers=[
            logging.FileHandler(filename="rfc2544.log", mode="a"),
            logging.StreamHandler(),
        ]
    )

    logging.info("=" * 60)
    logging.info("RFC-2544 TEST SUITE STARTED")
    logging.info(f"Frame sizes : {RFC2544_FRAME_SIZES}")
    logging.info(f"Trial dur   : {cfg.trial_duration}s")
    logging.info(f"Loss thresh : {cfg.throughput_loss_threshold}%")
    logging.info(f"Direction   : {'Unidirectional (P1->P2)' if unidirectional else 'Bidirectional'}")
    logging.info("=" * 60)

    tester_obj, port_obj_1, port_obj_2 = await _connect_and_reserve(
        chassis, username, port1_str, port2_str
    )

    all_results = []

    try:
        for frame_size in RFC2544_FRAME_SIZES:
            logging.info(f"\n{'='*60}")
            logging.info(f"Frame size: {frame_size} bytes")
            logging.info(f"{'='*60}")

            stream1, stream2 = await _create_and_configure_streams(
                port_obj_1, port_obj_2, frame_size, unidirectional
            )
            await _clear_stats(port_obj_1, port_obj_2)

            frame_results = {"frame_size_bytes": frame_size}

            # 1. Throughput
            tp_result = await rfc2544_throughput(
                port_obj_1, port_obj_2, stream1, stream2, frame_size, cfg, unidirectional
            )
            frame_results["throughput"] = tp_result
            throughput_pct = tp_result["throughput_pct"]

            # 2. Latency (at throughput rate) - skipped automatically if throughput=0
            lat_result = await rfc2544_latency(
                port_obj_1, port_obj_2, stream1, stream2, frame_size, throughput_pct, cfg, unidirectional
            )
            frame_results["latency"] = lat_result

            if tp_result.get("indeterminate"):
                logging.warning(
                    f"[RFC-2544] Skipping Frame Loss Rate and Back-to-Back for "
                    f"{frame_size}B - throughput was indeterminate."
                )
                frame_results["frame_loss_rate"] = {"skipped": True}
                frame_results["back_to_back"]    = {"skipped": True}
            else:
                # 3. Frame Loss Rate
                flr_result = await rfc2544_frame_loss_rate(
                    port_obj_1, port_obj_2, stream1, stream2, frame_size, cfg, unidirectional
                )
                frame_results["frame_loss_rate"] = flr_result

                # 4. Back-to-Back
                b2b_result = await rfc2544_back_to_back(
                    port_obj_1, port_obj_2, stream1, stream2, frame_size, cfg, unidirectional
                )
                frame_results["back_to_back"] = b2b_result

            all_results.append(frame_results)
            await _teardown_streams(stream1, stream2)

    finally:
        logging.info("Freeing ports")
        await mgmt.free_port(port_obj_1)
        await mgmt.free_port(port_obj_2)

    # - Print / save summary ---------
    logging.info("\n" + "=" * 60)
    logging.info("RFC-2544 SUMMARY")
    logging.info("=" * 60)
    logging.info(f"{'Size':>6}  {'Throughput':>12}  {'AvgLat P1->P2':>14}  {'MaxB2B':>10}")
    logging.info(f"{'(B)':>6}  {'(% line)':>12}  {'(ns)':>14}  {'(sec)':>10}")
    logging.info("-" * 50)
    for r in all_results:
        sz   = r["frame_size_bytes"]
        tp   = r["throughput"]["throughput_pct"]
        lat  = r["latency"]["p2_rx"]["avg_ns"]
        b2b  = r["back_to_back"]["max_burst_no_loss_sec"]
        logging.info(f"{sz:>6}  {tp:>12.3f}  {lat:>14.0f}  {b2b:>10.2f}")

    # Save full results to JSON
    results_file = "rfc2544_results.json"
    with open(results_file, "w") as f:
        json.dump(all_results, f, indent=2)
    logging.info(f"\nFull results saved to: {results_file}")
    logging.info("RFC-2544 TEST SUITE COMPLETE")

    return all_results


# -----------------------
# ORIGINAL PLAIN TRAFFIC MODE  (kept intact)
# -----------------------

async def gen_traffic(
        chassis: str,
        username: str,
        port1_str: str,
        port2_str: str,
        duration: int,
        stop_event: asyncio.Event
):
    logging.basicConfig(
        format="%(asctime)s  %(message)s",
        level=logging.DEBUG,
        handlers=[
            logging.FileHandler(filename="test.log", mode="a"),
            logging.StreamHandler(),
        ]
    )

    tester_obj, port_obj_1, port_obj_2 = await _connect_and_reserve(
        chassis, username, port1_str, port2_str
    )

    stream1, stream2 = await _create_and_configure_streams(port_obj_1, port_obj_2, FRAME_SIZE)

    logging.info("Clearing statistics")
    await _clear_stats(port_obj_1, port_obj_2)

    logging.info("Starting traffic")
    await _traffic_on(port_obj_1, port_obj_2)
    logging.info(f"Wait for {duration} seconds...")

    asyncio.create_task(statistics_background_task(port_obj_1, port_obj_2, duration, stop_event))
    await stop_event.wait()

    logging.info("Stopping traffic..")
    await _traffic_off(port_obj_1, port_obj_2)
    await asyncio.sleep(2)

    await final_statistic_fetcher(port_obj_1, port_obj_2)
    await _teardown_streams(stream1, stream2)

    logging.info("Free ports")
    await mgmt.free_port(port_obj_1)
    await mgmt.free_port(port_obj_2)
    logging.info("Test done")


# -----------------------
# ENTRY POINT
# -----------------------

async def main():
    if RUN_RFC2544:
        await run_rfc2544(
            chassis=CHASSIS_IP,
            username=USERNAME,
            port1_str=PORT1,
            port2_str=PORT2,
            unidirectional=UNIDIRECTIONAL,
        )
    else:
        stop_event = asyncio.Event()
        try:
            await gen_traffic(
                chassis=CHASSIS_IP,
                username=USERNAME,
                port1_str=PORT1,
                port2_str=PORT2,
                duration=TRAFFIC_DURATION,
                stop_event=stop_event,
            )
        except KeyboardInterrupt:
            stop_event.set()


if __name__ == "__main__":
    asyncio.run(main())
