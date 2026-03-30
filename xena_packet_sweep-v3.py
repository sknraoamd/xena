################################################################
#
#             UDP TRAFFIC & STATISTICS - PACKET SIZE SWEEP
#
# Extends xena_two_port.py to automatically iterate over all
# standard RFC-2544 frame sizes in a single run:
#   64, 128, 256, 512, 1024, 1280, 1518 bytes
#
# For each frame size the script:
#   1. Reconfigures both streams to the new frame size
#   2. Runs traffic for <duration> seconds at 100% line rate
#   3. Collects per-second statistics (throughput, latency, jitter, loss)
#   4. Prints a per-size summary
#
# At the end, a consolidated summary table is printed covering
# all frame sizes in one view.
#
# Usage:
#   python xena_packet_sweep.py <duration> <port1> <port2>
#
# Example:
#   python xena_packet_sweep.py 30 0/0 0/1
#
# Requirements:
#   Python 3.11 or 3.12  (xoa-driver is not compatible with 3.13+)
#   pip install xoa-driver
#
################################################################

import asyncio
import sys
import logging
from xoa_driver import testers, modules, ports, utils, enums
from xoa_driver.enums import *
from xoa_driver.hlfuncs import mgmt
from xoa_driver.misc import Hex

# ----------------------------------------------
# GLOBAL PARAMS
# ----------------------------------------------

CHASSIS_IP       = "xena-11464930.amd.com"
USERNAME         = "maniskum"

if len(sys.argv) < 4:
    print("Usage: xena_packet_sweep.py <duration> <port1> <port2>")
    print("Example: xena_packet_sweep.py 30 0/0 0/1")
    sys.exit(1)

TRAFFIC_DURATION = int(sys.argv[1])
PORT1            = sys.argv[2]
PORT2            = sys.argv[3]

# Standard RFC-2544 frame sizes (bytes, Ethernet frame including FCS)
FRAME_SIZES = [64, 128, 256, 512, 1024, 1280, 1518]

# ----------------------------------------------
# PACKET HEADERS  (same as original script)
# ----------------------------------------------

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

# ----------------------------------------------
# PER-SIZE STATISTICS COLLECTOR
# ----------------------------------------------

async def collect_statistics(
        port1: ports.GenericL23Port,
        port2: ports.GenericL23Port,
        frame_size: int,
        duration: int,
        stop_event: asyncio.Event,
        size_results: dict,
) -> None:
    """
    Mirrors the original statistics_background_task but:
      - accepts frame_size so per-second records carry the correct size
      - stores the averaged final result into size_results[frame_size]
        so the sweep loop can build the summary table
    """
    logging.info(f"[{frame_size}B] Collecting statistics for {duration}s ...")

    count = 0
    per_second_p1 = []
    per_second_p2 = []

    # Log effective port speeds once per size
    resp = await port1.speed.current.get()
    nominal_spd = resp.port_speed
    resp = await port1.speed.reduction.get()
    eff_spd = nominal_spd * (1 - resp.ppm / 1_000_000) * 1_000_000
    logging.info(f"[{frame_size}B] TX port effective speed: {eff_spd/1_000_000_000:.2f} Gbps")

    while not stop_event.is_set():
        # -- stream-level TX/RX counts --
        (p1_tx, p1_rx, p2_tx, p2_rx) = await asyncio.gather(
            port1.statistics.tx.obtain_from_stream(0).get(),
            port1.statistics.rx.access_tpld(1).traffic.get(),
            port2.statistics.tx.obtain_from_stream(0).get(),
            port2.statistics.rx.access_tpld(0).traffic.get(),
        )

        # -- TPLD-level latency / jitter / error stats --
        p1rx_lat, p1rx_jit, p1rx_err, p2rx_lat, p2rx_jit, p2rx_err = await utils.apply(
            port1.statistics.rx.access_tpld(1).latency.get(),
            port1.statistics.rx.access_tpld(1).jitter.get(),
            port1.statistics.rx.access_tpld(1).errors.get(),
            port2.statistics.rx.access_tpld(0).latency.get(),
            port2.statistics.rx.access_tpld(0).jitter.get(),
            port2.statistics.rx.access_tpld(0).errors.get(),
        )

        # -- port-level PPS --
        p1_tx_tot, p1_rx_tot = await asyncio.gather(
            port1.statistics.tx.total.get(),
            port1.statistics.rx.total.get(),
        )
        p2_tx_tot, p2_rx_tot = await asyncio.gather(
            port2.statistics.tx.total.get(),
            port2.statistics.rx.total.get(),
        )

        # -- per-second log (same fields as original) --
        tx_fps  = p1_tx_tot.packet_count_last_sec
        rx_fps  = p1_rx_tot.packet_count_last_sec
        p2_tx_fps = p2_tx_tot.packet_count_last_sec
        p2_rx_fps = p2_rx_tot.packet_count_last_sec

        logging.info(f"[{frame_size}B] s={count+1:3d}  "
                     f"P1 TX={tx_fps}pps RX={rx_fps}pps  "
                     f"lat_avg={p1rx_lat.avg_val}ns  loss={p1rx_err.non_incre_seq_event_count}  |  "
                     f"P2 TX={p2_tx_fps}pps RX={p2_rx_fps}pps  "
                     f"lat_avg={p2rx_lat.avg_val}ns  loss={p2rx_err.non_incre_seq_event_count}")

        per_second_p1.append({
            "frame_size":   frame_size,
            "TX_fps":       tx_fps,
            "RX_fps":       rx_fps,
            "TX_bytes":     p1_tx.byte_count_since_cleared,
            "TX_pkts":      p1_tx.packet_count_since_cleared,
            "RX_bytes":     p1_rx.byte_count_since_cleared,
            "RX_pkts":      p1_rx.packet_count_since_cleared,
            "lat_min_ns":   p1rx_lat.min_val,
            "lat_max_ns":   p1rx_lat.max_val,
            "lat_avg_ns":   p1rx_lat.avg_val,
            "jit_min_ns":   p1rx_jit.min_val,
            "jit_max_ns":   p1rx_jit.max_val,
            "jit_avg_ns":   p1rx_jit.avg_val,
            "lost_pkts":    p1rx_err.non_incre_seq_event_count,
            "misordered":   p1rx_err.swapped_seq_misorder_event_count,
            "payload_err":  p1rx_err.non_incre_payload_packet_count,
        })

        per_second_p2.append({
            "frame_size":   frame_size,
            "TX_fps":       p2_tx_fps,
            "RX_fps":       p2_rx_fps,
            "TX_bytes":     p2_tx.byte_count_since_cleared,
            "TX_pkts":      p2_tx.packet_count_since_cleared,
            "RX_bytes":     p2_rx.byte_count_since_cleared,
            "RX_pkts":      p2_rx.packet_count_since_cleared,
            "lat_min_ns":   p2rx_lat.min_val,
            "lat_max_ns":   p2rx_lat.max_val,
            "lat_avg_ns":   p2rx_lat.avg_val,
            "jit_min_ns":   p2rx_jit.min_val,
            "jit_max_ns":   p2rx_jit.max_val,
            "jit_avg_ns":   p2rx_jit.avg_val,
            "lost_pkts":    p2rx_err.non_incre_seq_event_count,
            "misordered":   p2rx_err.swapped_seq_misorder_event_count,
            "payload_err":  p2rx_err.non_incre_payload_packet_count,
        })

        count += 1
        await asyncio.sleep(1.0)
        if count >= duration:
            stop_event.set()

    # -- Average over all seconds and store for summary --
    def avg(records, key):
        vals = [r[key] for r in records if r[key] is not None]
        return sum(vals) / len(vals) if vals else 0

    def last(records, key):
        return records[-1][key] if records else 0

    size_results[frame_size] = {
        "p1": {
            "avg_TX_fps":    avg(per_second_p1, "TX_fps"),
            "avg_RX_fps":    avg(per_second_p1, "RX_fps"),
            "total_TX_pkts": last(per_second_p1, "TX_pkts"),
            "total_RX_pkts": last(per_second_p1, "RX_pkts"),
            "lat_min_ns":    min(r["lat_min_ns"] for r in per_second_p1),
            "lat_max_ns":    max(r["lat_max_ns"] for r in per_second_p1),
            "lat_avg_ns":    avg(per_second_p1, "lat_avg_ns"),
            "jit_avg_ns":    avg(per_second_p1, "jit_avg_ns"),
            "total_lost":    last(per_second_p1, "lost_pkts"),
        },
        "p2": {
            "avg_TX_fps":    avg(per_second_p2, "TX_fps"),
            "avg_RX_fps":    avg(per_second_p2, "RX_fps"),
            "total_TX_pkts": last(per_second_p2, "TX_pkts"),
            "total_RX_pkts": last(per_second_p2, "RX_pkts"),
            "lat_min_ns":    min(r["lat_min_ns"] for r in per_second_p2),
            "lat_max_ns":    max(r["lat_max_ns"] for r in per_second_p2),
            "lat_avg_ns":    avg(per_second_p2, "lat_avg_ns"),
            "jit_avg_ns":    avg(per_second_p2, "jit_avg_ns"),
            "total_lost":    last(per_second_p2, "lost_pkts"),
        },
    }

    logging.info(f"[{frame_size}B] Done.")

# ----------------------------------------------
# FINAL FRAME-LOSS SNAPSHOT  (same as original)
# ----------------------------------------------

async def final_statistic_fetcher(
        port1: ports.GenericL23Port,
        port2: ports.GenericL23Port,
        frame_size: int,
) -> None:
    (p1_tx, p1_rx, p2_tx, p2_rx) = await asyncio.gather(
        port1.statistics.tx.obtain_from_stream(0).get(),
        port1.statistics.rx.access_tpld(1).traffic.get(),
        port2.statistics.tx.obtain_from_stream(0).get(),
        port2.statistics.rx.access_tpld(0).traffic.get(),
    )
    p1_lost = p1_tx.packet_count_since_cleared - p2_rx.packet_count_since_cleared
    p2_lost = p2_tx.packet_count_since_cleared - p1_rx.packet_count_since_cleared
    logging.info(f"[{frame_size}B] Frame Loss - P1->P2: {p1_lost}  P2->P1: {p2_lost}")

# ----------------------------------------------
# STREAM HELPERS
# ----------------------------------------------

async def _configure_streams(stream1, stream2, frame_size: int) -> None:
    """Update both streams to a new frame size (streams already exist)."""
    header_data1 = f"{DESTINATION_MAC}{SOURCE_MAC}{ETHERNET_TYPE}{IP}{UDP}"
    header_data2 = f"{DESTINATION_MAC2}{SOURCE_MAC2}{ETHERNET_TYPE2}{IP2}{UDP2}"

    await asyncio.gather(
        stream1.packet.length.set(length_type=LengthType.FIXED, min_val=frame_size, max_val=frame_size),
        stream1.packet.header.data.set(hex_data=Hex(header_data1)),
        stream2.packet.length.set(length_type=LengthType.FIXED, min_val=frame_size, max_val=frame_size),
        stream2.packet.header.data.set(hex_data=Hex(header_data2)),
    )

# ----------------------------------------------
# SUMMARY TABLE PRINTER
# ----------------------------------------------

def print_summary(size_results: dict) -> None:
    SEP = "=" * 130
    HDR = (f"{'Size':>6}  {'Direction':<10}  "
           f"{'Avg TX fps':>12}  {'Avg RX fps':>12}  "
           f"{'Total TX pkts':>15}  {'Total RX pkts':>15}  "
           f"{'Lost pkts':>10}  {'Loss %':>8}  "
           f"{'Lat min ns':>12}  {'Lat avg ns':>12}  {'Lat max ns':>12}  "
           f"{'Jit avg ns':>12}")

    print()
    print(SEP)
    print("  PACKET SWEEP - CONSOLIDATED SUMMARY")
    print(SEP)
    print(HDR)
    print("-" * 130)

    for fs in FRAME_SIZES:
        if fs not in size_results:
            continue
        for direction, key in [("P1->P2", "p1"), ("P2->P1", "p2")]:
            r = size_results[fs][key]
            size_col = str(fs) if direction == "P1->P2" else ""
            tx  = r["total_TX_pkts"]
            rx  = r["total_RX_pkts"]
            lost = r["total_lost"]
            loss_pct = (lost / tx * 100.0) if tx > 0 else 0.0
            print(
                f"{size_col:>6}  {direction:<10}  "
                f"{r['avg_TX_fps']:>12.0f}  {r['avg_RX_fps']:>12.0f}  "
                f"{tx:>15,}  {rx:>15,}  "
                f"{lost:>10,}  {loss_pct:>8.4f}  "
                f"{r['lat_min_ns']:>12}  {r['lat_avg_ns']:>12.0f}  {r['lat_max_ns']:>12}  "
                f"{r['jit_avg_ns']:>12.0f}"
            )
        print("-" * 130)

    print(SEP)
    print()

# ----------------------------------------------
# MAIN SWEEP LOOP
# ----------------------------------------------

async def run_sweep(
        chassis: str,
        username: str,
        port1_str: str,
        port2_str: str,
        duration: int,
) -> None:
    logging.basicConfig(
        format="%(asctime)s  %(message)s",
        level=logging.DEBUG,
        handlers=[
            logging.FileHandler(filename="packet_sweep.log", mode="a"),
            logging.StreamHandler(),
        ]
    )

    logging.info("=" * 60)
    logging.info("PACKET SIZE SWEEP STARTED")
    logging.info(f"Frame sizes  : {FRAME_SIZES}")
    logging.info(f"Duration/size: {duration}s")
    logging.info(f"Total runtime: ~{duration * len(FRAME_SIZES)}s")
    logging.info("=" * 60)

    # -- Connect once --
    logging.info(f"Connecting to chassis: {chassis}  user={username}")
    tester_obj = await testers.L23Tester(
        host=chassis, username=username, password="amd1234!", port=22606, enable_logging=False
    )

    _mid1, _pid1 = int(port1_str.split("/")[0]), int(port1_str.split("/")[1])
    _mid2, _pid2 = int(port2_str.split("/")[0]), int(port2_str.split("/")[1])

    module_obj_1 = tester_obj.modules.obtain(_mid1)
    module_obj_2 = tester_obj.modules.obtain(_mid2)

    if isinstance(module_obj_1, modules.ModuleChimera) or isinstance(module_obj_2, modules.ModuleChimera):
        raise RuntimeError("Chimera modules are not supported.")

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

    # -- Create streams once, reuse across all sizes --
    logging.info("Creating streams")
    stream1 = await port_obj_1.streams.create()
    stream2 = await port_obj_2.streams.create()

    header_data1 = f"{DESTINATION_MAC}{SOURCE_MAC}{ETHERNET_TYPE}{IP}{UDP}"
    header_data2 = f"{DESTINATION_MAC2}{SOURCE_MAC2}{ETHERNET_TYPE2}{IP2}{UDP2}"

    await asyncio.gather(
        stream1.tpld_id.set(0),
        stream1.packet.header.protocol.set(segments=[
            ProtocolOption.ETHERNET, ProtocolOption.IP, ProtocolOption.UDP]),
        stream1.enable.set_on(),
        stream1.rate.fraction.set(1_000_000),
        stream1.packet.header.data.set(hex_data=Hex(header_data1)),

        stream2.tpld_id.set(1),
        stream2.packet.header.protocol.set(segments=[
            ProtocolOption.ETHERNET, ProtocolOption.IP, ProtocolOption.UDP]),
        stream2.enable.set_on(),
        stream2.rate.fraction.set(1_000_000),
        stream2.packet.header.data.set(hex_data=Hex(header_data2)),
    )

    # Header modifiers (source port incrementing - same as original)
    await stream1.packet.header.modifiers.configure(1)
    mod1 = stream1.packet.header.modifiers.obtain(0)
    await mod1.specification.set(position=36, mask=Hex("FFFF0000"), action=enums.ModifierAction.INC, repetition=1)
    await mod1.range.set(min_val=0, step=1, max_val=65535)

    await stream2.packet.header.modifiers.configure(1)
    mod2 = stream2.packet.header.modifiers.obtain(0)
    await mod2.specification.set(position=36, mask=Hex("FFFF0000"), action=enums.ModifierAction.INC, repetition=1)
    await mod2.range.set(min_val=0, step=1, max_val=65535)

    size_results = {}

    try:
        # -- Iterate over each frame size --
        for frame_size in FRAME_SIZES:
            logging.info(f"\n{'-'*60}")
            logging.info(f"Frame size: {frame_size} bytes")
            logging.info(f"{'-'*60}")

            # Reconfigure frame size on both streams
            await _configure_streams(stream1, stream2, frame_size)

            # Clear stats before each run
            await asyncio.gather(
                port_obj_1.statistics.tx.clear.set(),
                port_obj_1.statistics.rx.clear.set(),
                port_obj_2.statistics.tx.clear.set(),
                port_obj_2.statistics.rx.clear.set(),
            )

            # Start traffic
            logging.info(f"[{frame_size}B] Starting traffic")
            await asyncio.gather(
                port_obj_1.traffic.state.set_start(),
                port_obj_2.traffic.state.set_start(),
            )

            # Run statistics collection for <duration> seconds
            stop_event = asyncio.Event()
            await collect_statistics(
                port_obj_1, port_obj_2,
                frame_size, duration,
                stop_event, size_results,
            )

            # Stop traffic
            logging.info(f"[{frame_size}B] Stopping traffic")
            await asyncio.gather(
                port_obj_1.traffic.state.set_stop(),
                port_obj_2.traffic.state.set_stop(),
            )
            await asyncio.sleep(2)

            # Final frame-loss snapshot
            await final_statistic_fetcher(port_obj_1, port_obj_2, frame_size)

            # Brief pause between sizes to let the port settle
            await asyncio.sleep(2)

    finally:
        # Cleanup streams and free ports regardless of errors
        await stream1.packet.header.modifiers.configure(0)
        await stream2.packet.header.modifiers.configure(0)
        await stream1.delete()
        await stream2.delete()

        logging.info("Freeing ports")
        await mgmt.free_port(port_obj_1)
        await mgmt.free_port(port_obj_2)

    # -- Print consolidated summary --
    print_summary(size_results)
    logging.info("PACKET SIZE SWEEP COMPLETE")


# ----------------------------------------------
# ENTRY POINT
# ----------------------------------------------

async def main():
    try:
        await run_sweep(
            chassis=CHASSIS_IP,
            username=USERNAME,
            port1_str=PORT1,
            port2_str=PORT2,
            duration=TRAFFIC_DURATION,
        )
    except KeyboardInterrupt:
        logging.info("Interrupted by user.")

if __name__ == "__main__":
    asyncio.run(main())
