################################################################
#
#                   UDP TRAFFIC & STATISTICS
#
# 1. Connect to a Xena
# 2. Reserve port
# 3. Create a stream on the ports
# 4. Start traffic
# 5. Collect statistics
#
################################################################

import asyncio
import sys
import os
from xoa_driver import testers
from xoa_driver import modules
from xoa_driver import ports
from xoa_driver import utils, enums
from xoa_driver.enums import *
from xoa_driver.hlfuncs import mgmt, headers
from xoa_driver.misc import Hex
import logging

#---------------------------
# GLOBAL PARAMS
#---------------------------

CHASSIS_IP = "xena-11464930.amd.com"
USERNAME = "maniskum"
PORT1 = sys.argv[3] #[0/0]
PORT2 = sys.argv[4] #[0/1]
TEST_TRAFFIC_DURATION = sys.argv[1] # seconds
TRAFFIC_DURATION = int(TEST_TRAFFIC_DURATION)
# TRAFFIC_DURATION = 180
FRAME_SIZE = sys.argv[2]
#---------------------------
# statistics_background_task
#---------------------------
async def statistics_background_task(
        port1: ports.GenericL23Port,
        port2: ports.GenericL23Port,
        duration: int,
        stop_event: asyncio.Event
    ) -> None:

    # collect statistics
    logging.info(f"Collecting statistics..")

    count = 0
    resultIterationPort1 =dict()
    runIterationCounterPort1 = 0
    resultIterationPort2 =dict()
    runIterationCounterPort2 = 0

    # calculate the TX effective port speed from nominal speed
    resp = await port1.speed.current.get()
    tx_port_nominal_speed_Mbps = resp.port_speed
    resp = await port1.speed.reduction.get()
    tx_port_speed_reduction_ppm = resp.ppm
    tx_port_effective_speed = tx_port_nominal_speed_Mbps*(1 - tx_port_speed_reduction_ppm/1_000_000)*1_000_000
    logging.info(f"{port1}")
    logging.info(f"TX port effective speed: {tx_port_effective_speed/1_000_000_000} Gbps")

    # calculate the RX effective port speed from nominal speed and reduction ppm
    resp = await port2.speed.current.get()
    rx_port_nominal_speed_Mbps = resp.port_speed
    resp = await port2.speed.reduction.get()
    rx_port_speed_reduction_ppm = resp.ppm
    rx_port_effective_speed = rx_port_nominal_speed_Mbps*(1 - rx_port_speed_reduction_ppm/1_000_000)*1_000_000
    logging.info(f"{port2}")
    # logging.info(f"TX port effective speed: {tx_port_effective_speed/1_000_000_000} Gbps")
    logging.info(f"RX port effective speed: {rx_port_effective_speed/1_000_000_000} Gbps")


    while not stop_event.is_set():
        (p1_tx, p1_rx, p2_tx, p2_rx) = await asyncio.gather(
            port1.statistics.tx.obtain_from_stream(0).get(),
            port1.statistics.rx.access_tpld(1).traffic.get(),
            port2.statistics.tx.obtain_from_stream(0).get(),
            port2.statistics.rx.access_tpld(0).traffic.get(),
        )

        p1rx_latency, p1rx_jitter, p1rx_error, p2rx_latency, p2rx_jitter, p2rx_error = await utils.apply(

            # tpld level latency stats
            port1.statistics.rx.access_tpld(1).latency.get(),

            # tpld level jitter stats
            port1.statistics.rx.access_tpld(1).jitter.get(),

            # tpld level error stats
            port1.statistics.rx.access_tpld(1).errors.get(),

            # tpld level latency stats
            port2.statistics.rx.access_tpld(0).latency.get(),

            # tpld level jitter stats
            port2.statistics.rx.access_tpld(0).jitter.get(),

            # tpld level error stats
            port2.statistics.rx.access_tpld(0).errors.get()


        )

        p1_tx_resp, p1_rx_resp = await asyncio.gather(
                port1.statistics.tx.total.get(),
                port1.statistics.rx.total.get()
                )

        tx_fps = p1_tx_resp.packet_count_last_sec
        tx_mpps = tx_fps/1000000

        rx_fps = p1_rx_resp.packet_count_last_sec
        rx_mpps = rx_fps/1000000
        runIterationCounterPort1 = runIterationCounterPort1 + 1
        runIterationCounterPort2 = runIterationCounterPort2 + 1
        
        logging.info(f"#"*count)
        logging.info(f"Port 1")

        logging.info(f"TX frames per second   : {tx_fps} pps")
        logging.info(f"RX frames per second   : {rx_fps} pps")
        logging.info(f"TxValue Frames per Second(Mpps): {tx_mpps} MPPS")
        logging.info(f"RxValue Frames per Second(Mpps): {rx_mpps} MPPS")


        logging.info(f"  TX(tid=0).Byte_Count: {p1_tx.byte_count_since_cleared}")
        logging.info(f"  TX(tid=0).Packet_Count: {p1_tx.packet_count_since_cleared}")
        logging.info(f"  RX(tid=1).Byte_Count: {p1_rx.byte_count_since_cleared}")
        logging.info(f"  RX(tid=1).Packet_Count: {p1_rx.packet_count_since_cleared}")

        logging.info(f" RX min latency: {p1rx_latency.min_val}")
        logging.info(f" RX max latency: {p1rx_latency.max_val}")
        logging.info(f" RX avg latency: {p1rx_latency.avg_val}")
        logging.info(f" RX min jitter: {p1rx_jitter.min_val}")
        logging.info(f" RX max jitter: {p1rx_jitter.max_val}")
        logging.info(f" RX avg jitter: {p1rx_jitter.avg_val}")
        logging.info(f" RX Lost Packets: {p1rx_error.non_incre_seq_event_count}")
        logging.info(f" RX Misordered: {p1rx_error.swapped_seq_misorder_event_count}")
        logging.info(f" RX Payload Errors: {p1rx_error.non_incre_payload_packet_count}")

        resultIterationPort1[runIterationCounterPort1]= {
                "packet Size": FRAME_SIZE,
                "TX frames per second(pps)": tx_fps,
                "RX frames per second(pps)": rx_fps,
                "TX(tid=0)_Byte_Count": p1_tx.byte_count_since_cleared,
                "TX(tid=0)_Packet_Count": p1_tx.packet_count_since_cleared,
                "RX(tid=1)_Byte_Count": p1_rx.byte_count_since_cleared,
                "RX(tid=1)_Packet_Count": p1_rx.packet_count_since_cleared,
                "RxValue Frames per Second(Mpps)" : rx_mpps,
                "TxValue Frames per Second(Mpps)"   : tx_mpps,
                "RX min latency": p1rx_latency.min_val,
                "RX max latency": p1rx_latency.max_val,
                "RX avg latency": p1rx_latency.avg_val,
                "RX min jitter": p1rx_jitter.min_val,
                "RX max jitter": p1rx_jitter.max_val,
                "RX avg jitter": p1rx_jitter.avg_val,
                "RX Lost Packets": p1rx_error.non_incre_seq_event_count,    
                "RX Misordered":  p1rx_error.swapped_seq_misorder_event_count,
                "RX Payload Errors": p1rx_error.non_incre_payload_packet_count
            }


        p2_tx_resp, p2_rx_resp = await asyncio.gather(
                port2.statistics.tx.total.get(),
                port2.statistics.rx.total.get()
                )

        p2_tx_fps = p2_tx_resp.packet_count_last_sec
        p2_tx_mpps = p2_tx_fps/1000000

        p2_rx_fps = p2_rx_resp.packet_count_last_sec
        p2_rx_mpps = p2_rx_fps/1000000
        logging.info(f"#"*count)
        logging.info(f"Port 2")

        logging.info(f"TX frames per second   : {p2_tx_fps} pps")
        logging.info(f"RX frames per second   : {p2_rx_fps} pps")
        logging.info(f"TxValue Frames per Second(Mpps): {p2_tx_mpps} MPPS")
        logging.info(f"RxValue Frames per Second(Mpps): {p2_rx_mpps} MPPS")


        logging.info(f"  TX(tid=1).Byte_Count: {p2_tx.byte_count_since_cleared}")
        logging.info(f"  TX(tid=1).Packet_Count: {p2_tx.packet_count_since_cleared}")
        logging.info(f"  RX(tid=0).Byte_Count: {p2_rx.byte_count_since_cleared}")
        logging.info(f"  RX(tid=0).Packet_Count: {p2_rx.packet_count_since_cleared}")

        logging.info(f" RX min latency: {p2rx_latency.min_val}")
        logging.info(f" RX max latency: {p2rx_latency.max_val}")
        logging.info(f" RX avg latency: {p2rx_latency.avg_val}")
        logging.info(f" RX min jitter: {p2rx_jitter.min_val}")
        logging.info(f" RX max jitter: {p2rx_jitter.max_val}")
        logging.info(f" RX avg jitter: {p2rx_jitter.avg_val}")
        logging.info(f" RX Lost Packets: {p2rx_error.non_incre_seq_event_count}")
        logging.info(f" RX Misordered: {p2rx_error.swapped_seq_misorder_event_count}")
        logging.info(f" RX Payload Errors: {p2rx_error.non_incre_payload_packet_count}")

        resultIterationPort2[runIterationCounterPort2]= {
                "packet Size": FRAME_SIZE,
                "TX frames per second(pps)": p2_tx_fps,
                "RX frames per second(pps)": p2_rx_fps,
                "TX(tid=0)_Byte_Count": p2_tx.byte_count_since_cleared,
                "TX(tid=0)_Packet_Count": p2_tx.packet_count_since_cleared,
                "RX(tid=1)_Byte_Count": p2_rx.byte_count_since_cleared,
                "RX(tid=1)_Packet_Count": p2_rx.packet_count_since_cleared,
                "RxValue Frames per Second(Mpps)" : p2_rx_mpps,
                "TxValue Frames per Second(Mpps)"   : p2_tx_mpps,
                "RX min latency": p2rx_latency.min_val,
                "RX max latency": p2rx_latency.max_val,
                "RX avg latency": p2rx_latency.avg_val,
                "RX min jitter": p2rx_jitter.min_val,
                "RX max jitter": p2rx_jitter.max_val,
                "RX avg jitter": p2rx_jitter.avg_val,
                "RX Lost Packets": p2rx_error.non_incre_seq_event_count,    
                "RX Misordered":  p2rx_error.swapped_seq_misorder_event_count,
                "RX Payload Errors": p2rx_error.non_incre_payload_packet_count
            }

        count+=1
        await asyncio.sleep(1.0)

        if count >= duration:
            stop_event.set()

    print(f"Final Result Data Port1: {str(resultIterationPort1)} Result Data END")
    print(f"Final Result Data Port2: {str(resultIterationPort2)} Result Data END")

#---------------------------
# final_statistic_fetcher
#---------------------------
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

    p1rx_lat, p1rx_jit, p1rx_err, p2rx_lat, p2rx_jit, p2rx_err = await utils.apply(
        port1.statistics.rx.access_tpld(1).latency.get(),
        port1.statistics.rx.access_tpld(1).jitter.get(),
        port1.statistics.rx.access_tpld(1).errors.get(),
        port2.statistics.rx.access_tpld(0).latency.get(),
        port2.statistics.rx.access_tpld(0).jitter.get(),
        port2.statistics.rx.access_tpld(0).errors.get(),
    )

    SEP = "=" * 60

    # Helper: safe loss %
    def loss_pct(tx, rx):
        return (tx - rx) / tx * 100.0 if tx > 0 else 0.0

    p1_tx_pkts = p1_tx.packet_count_since_cleared
    p2_rx_pkts = p2_rx.packet_count_since_cleared
    p2_tx_pkts = p2_tx.packet_count_since_cleared
    p1_rx_pkts = p1_rx.packet_count_since_cleared

    fwd_lost = p1_tx_pkts - p2_rx_pkts
    rev_lost = p2_tx_pkts - p1_rx_pkts

    logging.info(SEP)
    logging.info("FINAL RESULTS")
    logging.info(SEP)

    logging.info("Direction        : Port 1 -> Port 2  (tid=0)")
    logging.info(f"  Frame Size     : {FRAME_SIZE} bytes")
    logging.info(f"  TX Packets     : {p1_tx_pkts:,}")
    logging.info(f"  RX Packets     : {p2_rx_pkts:,}")
    logging.info(f"  TX Bytes       : {p1_tx.byte_count_since_cleared:,}")
    logging.info(f"  RX Bytes       : {p2_rx.byte_count_since_cleared:,}")
    logging.info(f"  Lost Packets   : {fwd_lost:,}")
    logging.info(f"  Frame Loss     : {loss_pct(p1_tx_pkts, p2_rx_pkts):.4f}%")
    logging.info(f"  Lat min / avg / max : {p2rx_lat.min_val} / {p2rx_lat.avg_val} / {p2rx_lat.max_val} ns")
    logging.info(f"  Jit min / avg / max : {p2rx_jit.min_val} / {p2rx_jit.avg_val} / {p2rx_jit.max_val} ns")
    logging.info(f"  Misordered     : {p2rx_err.swapped_seq_misorder_event_count:,}")
    logging.info(f"  Payload Errors : {p2rx_err.non_incre_payload_packet_count:,}")

    logging.info("-" * 60)

    logging.info("Direction        : Port 2 -> Port 1  (tid=1)")
    logging.info(f"  Frame Size     : {FRAME_SIZE} bytes")
    logging.info(f"  TX Packets     : {p2_tx_pkts:,}")
    logging.info(f"  RX Packets     : {p1_rx_pkts:,}")
    logging.info(f"  TX Bytes       : {p2_tx.byte_count_since_cleared:,}")
    logging.info(f"  RX Bytes       : {p1_rx.byte_count_since_cleared:,}")
    logging.info(f"  Lost Packets   : {rev_lost:,}")
    logging.info(f"  Frame Loss     : {loss_pct(p2_tx_pkts, p1_rx_pkts):.4f}%")
    logging.info(f"  Lat min / avg / max : {p1rx_lat.min_val} / {p1rx_lat.avg_val} / {p1rx_lat.max_val} ns")
    logging.info(f"  Jit min / avg / max : {p1rx_jit.min_val} / {p1rx_jit.avg_val} / {p1rx_jit.max_val} ns")
    logging.info(f"  Misordered     : {p1rx_err.swapped_seq_misorder_event_count:,}")
    logging.info(f"  Payload Errors : {p1rx_err.non_incre_payload_packet_count:,}")

    logging.info(SEP)


#------------------------------------
# Build Packet and Generate Traffic
#------------------------------------
async def gen_traffic(
        chassis: str,
        username: str,
        port1_str: str,
        port2_str: str,
        duration: int,
        stop_event: asyncio.Event
    ):
    # configure basic logger
    logging.basicConfig(
        format="%(asctime)s  %(message)s",
        level=logging.DEBUG,
        handlers=[
            logging.FileHandler(filename="test.log", mode="a"),
            logging.StreamHandler()]
        )

    # create tester instance and establish connection
    logging.info(f"===================================")
    logging.info(f"{'Connect to chassis:':<20}{chassis}")
    logging.info(f"{'Username:':<20}{username}")
    tester_obj = await testers.L23Tester(host=chassis, username=username, password="amd1234!", port=22606, enable_logging=False)

    # access the port objects
    _mid1 = int(port1_str.split("/")[0])
    _pid1 = int(port1_str.split("/")[1])
    _mid2 = int(port2_str.split("/")[0])
    _pid2 = int(port2_str.split("/")[1])
    module_obj_1 = tester_obj.modules.obtain(_mid1)
    module_obj_2 = tester_obj.modules.obtain(_mid2)

    # commands which used in this example are not supported by Chimera Module
    if isinstance(module_obj_1, modules.ModuleChimera):
        return None
    if isinstance(module_obj_2, modules.ModuleChimera):
        return None

    port_obj_1 = module_obj_1.ports.obtain(_pid1)
    port_obj_2 = module_obj_2.ports.obtain(_pid2)

    logging.info(f"Reserve and reset port {_mid1}/{_pid1}")
    logging.info(f"Reserve and reset port {_mid2}/{_pid2}")

    await mgmt.free_module(module=module_obj_1, should_free_ports=False)
    await mgmt.reserve_port(port=port_obj_1, force=True)
    await mgmt.reset_port(port_obj_1)
    await mgmt.free_module(module=module_obj_2, should_free_ports=False)
    await mgmt.reserve_port(port=port_obj_2, force=True)
    await mgmt.reset_port(port_obj_2)

    await asyncio.sleep(5)

    # Create one stream on the port
    logging.info(f"Creating a stream on port {_mid1}/{_pid1}")
    my_stream1 = await port_obj_1.streams.create()
    # Create one stream on the port
    logging.info(f"Creating a stream on port {_mid2}/{_pid2}")
    my_stream2 = await port_obj_2.streams.create()

    logging.info(f"Configuring streams..")

    DESTINATION_MAC =   "AAAAAAAAAAAA"
    SOURCE_MAC =        "BBBBBBBBBBBB"
    ETHERNET_TYPE =     "0800"
    VLAN =              "000B0800"
    IP =                "45000032000000007F11A655C0A80A0AC0A80A0B"
    UDP=                "10C310C400220000"

    DESTINATION_MAC2 =   "BBBBBBBBBBBB"
    SOURCE_MAC2 =        "AAAAAAAAAAAA"
    ETHERNET_TYPE2 =     "0800"
    VLAN2 =              "000C0800"
    IP2 =                "4500002E000000007F11A659C0A80A0BC0A80A0A"
    UDP2 =               "10C410C3001A0000"


    header_data1 = f'{DESTINATION_MAC}{SOURCE_MAC}{ETHERNET_TYPE}{IP}{UDP}'
    header_data2 = f'{DESTINATION_MAC2}{SOURCE_MAC2}{ETHERNET_TYPE2}{IP2}{UDP2}'

    await asyncio.gather(
        # Create the TPLD index of stream
        my_stream1.tpld_id.set(0),
        # Configure the packet size
        my_stream1.packet.length.set(length_type=LengthType.FIXED, min_val=FRAME_SIZE, max_val=FRAME_SIZE),
        my_stream1.packet.header.protocol.set(segments=[
                ProtocolOption.ETHERNET,
                ProtocolOption.IP,
                ProtocolOption.UDP,
                ]),
        # Enable streams
        my_stream1.enable.set_on(),
        # Configure the stream rate
        my_stream1.rate.fraction.set(1000000),

        # my_stream.packet.limit.set(packet_count=10000),
        my_stream1.packet.header.data.set(hex_data=Hex(header_data1)),

        my_stream2.tpld_id.set(1),
        # Configure the packet size
        my_stream2.packet.length.set(length_type=LengthType.INCREMENTING, min_val=FRAME_SIZE, max_val=FRAME_SIZE),
        my_stream2.packet.header.protocol.set(segments=[
            ProtocolOption.ETHERNET,
            ProtocolOption.IP,
            ProtocolOption.UDP
            ]),
        # Enable streams
        my_stream2.enable.set_on(),
        # Configure the stream rate
        my_stream2.rate.fraction.set(1000000),
        my_stream2.packet.header.data.set(hex_data=Hex(header_data2)),
    )

    #Multiple Streams
    await my_stream1.packet.header.modifiers.configure(1)

    my_modifier_s1 = my_stream1.packet.header.modifiers.obtain(0)
    await my_modifier_s1.specification.set(position=36, mask=Hex("FFFF0000"), action=enums.ModifierAction.INC, repetition=1)
    await my_modifier_s1.range.set(min_val=0, step=1, max_val=65535)

    await my_stream2.packet.header.modifiers.configure(1)

    my_modifier_s2 = my_stream2.packet.header.modifiers.obtain(0)
    await my_modifier_s2.specification.set(position=36, mask=Hex("FFFF0000"), action=enums.ModifierAction.INC, repetition=1)
    await my_modifier_s2.range.set(min_val=0, step=1, max_val=65535)

    # clear port statistics
    logging.info(f"Clearing statistics")
    await asyncio.gather(
        port_obj_1.statistics.tx.clear.set(),
        port_obj_1.statistics.rx.clear.set(),
        port_obj_2.statistics.tx.clear.set(),
        port_obj_2.statistics.rx.clear.set()
    )

    # start traffic on the ports
    logging.info(f"Starting traffic")
    await asyncio.gather(
        port_obj_1.traffic.state.set_start(),
        port_obj_2.traffic.state.set_start()
    )

    # let traffic runs for 10 seconds
    logging.info(f"Wait for {duration} seconds...")

    # spawn a Task to wait until 'event' is set.
    asyncio.create_task(statistics_background_task(port_obj_1, port_obj_2, duration, stop_event))
    await stop_event.wait()

    # stop traffic on the Tx port
    logging.info(f"Stopping traffic..")
    await asyncio.gather(
        port_obj_1.traffic.state.set_stop(),
        port_obj_2.traffic.state.set_stop()
    )
    await asyncio.sleep(2)

    # final statistics
    await final_statistic_fetcher(port_obj_1, port_obj_2)

    #Stream Cleanup
    await my_stream1.packet.header.modifiers.configure(0)
    await my_stream2.packet.header.modifiers.configure(0)


    # free ports
    logging.info(f"Free ports")
    await mgmt.free_port(port_obj_1)
    await mgmt.free_port(port_obj_2)

    # done
    logging.info(f"Test done")

async def main():
    stop_event = asyncio.Event()
    try:
        await gen_traffic(
            chassis=CHASSIS_IP,
            username=USERNAME,
            port1_str=PORT1,
            port2_str=PORT2,
            duration=TRAFFIC_DURATION,
            stop_event=stop_event
            )
    except KeyboardInterrupt:
        stop_event.set()


if __name__ == "__main__":
    asyncio.run(main())