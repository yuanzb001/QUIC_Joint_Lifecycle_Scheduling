import os
import sys
import argparse
import time
import csv
import threading

# Ensure we can load the local module in python/lib/ directory
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), 'lib')))

from quic_connection import QuicConnection
import vvc_splitter

csv_lock = threading.Lock()

def stats_timer_thread(conn, network_csv, interval, stop_event, video_id, trace_name, trace_start_index, start_time, seq_counters):
    """Periodically fetches network stats in timer mode."""
    while not stop_event.is_set():
        stats = conn._client.get_network_stats()
        with csv_lock:
            seq_counters['network'] += 1
            gop_index = seq_counters.get('gop', 0)
            match_time = time.time() - start_time
            network_csv.writerow([
                seq_counters['network'],
                video_id,
                gop_index,
                trace_name,
                trace_start_index,
                f"{start_time:.3f}",
                f"{match_time:.3f}",
                stats.rtt_us, 
                stats.cwnd_bytes, 
                stats.bytes_sent,
                stats.estimated_bandwidth_bps,
                stats.bytes_in_flight,
                stats.posted_bytes,
                stats.ideal_bytes
            ])
        stop_event.wait(interval)

def send_file_thread(conn, filepath, priority, sender_csv, network_csv, stats_mode, video_id, trace_name, trace_start_index, start_time, seq_counters):
    """
    Reads a file and sends it over a single QUIC stream in chunks.
    """
    stream = conn.open_stream()
    stream_id = stream.stream_id
    file_type = os.path.basename(filepath)
    
    print(f"[Client] 🚀 Sending {file_type} via Stream {stream_id} (Priority: {priority})")
    
    # Use vvc_splitter to parse NALUs
    try:
        nalus = vvc_splitter.parse_annexb(filepath)
    except RuntimeError as e:
        print(f"[Client] ⚠️ Warning: {e}. Skipping {file_type}.")
        stream.close()
        return
    
    frame_id = 0
    for nalu in nalus:
        chunk = nalu["raw"]
        nal_type = nalu["type"]
        
        # 7: IDR_W_RADL, 8: IDR_N_LP, 9: CRA_NUT
        is_iframe = (nal_type in [7, 8, 9])
        
        if file_type == "subpic_0.vvc" and is_iframe:
            with csv_lock:
                seq_counters['gop'] += 1

        if stats_mode == "iframe" and file_type == "subpic_0.vvc" and is_iframe:
            stats = conn._client.get_network_stats()
            with csv_lock:
                seq_counters['network'] += 1
                gop_index = seq_counters.get('gop', 0)
                match_time = time.time() - start_time
                network_csv.writerow([
                    seq_counters['network'],
                    video_id,
                    gop_index,
                    trace_name,
                    trace_start_index,
                    f"{start_time:.3f}",
                    f"{match_time:.3f}",
                    stats.rtt_us, 
                    stats.cwnd_bytes, 
                    stats.bytes_sent,
                    stats.estimated_bandwidth_bps,
                    stats.bytes_in_flight,
                    stats.posted_bytes,
                    stats.ideal_bytes
                ])
                
        stream.send_data(frame_id=frame_id, data=chunk, priority=priority)
        
        with csv_lock:
            seq_counters['sender'] += 1
            gop_index = seq_counters.get('gop', 0)
            match_time_sender = time.time() - start_time
            sender_csv.writerow([
                seq_counters['sender'],
                video_id,
                gop_index,
                trace_name,
                trace_start_index,
                f"{start_time:.3f}",
                f"{match_time_sender:.3f}",
                stream_id,
                file_type,
                frame_id,
                len(chunk)
            ])
        
        frame_id += 1
        time.sleep(0.005) # Simulate pacing
            
    # Send FIN flag or just close stream gracefully
    stream.close()
    print(f"[Client] ✅ Finished sending {file_type} (Stream {stream_id}). Sent {frame_id} NALUs.")

def process_video(conn, filepath, sender_csv, network_csv, stats_mode, stats_interval, video_id, trace_name, trace_start_index, start_time, seq_counters):
    print(f"\n=== Processing Video: {filepath} ===")
    output_dir = "split_temp_dir"
    split_files = vvc_splitter.split(filepath, output_dir, num_subpics=4)
    print("Split completed successfully!\n")

    stop_event = threading.Event()
    stats_thread = None
    if stats_mode == "timer":
        stats_thread = threading.Thread(target=stats_timer_thread, args=(conn, network_csv, stats_interval, stop_event, video_id, trace_name, trace_start_index, start_time, seq_counters))
        stats_thread.start()

    threads = []
    
    # common.vvc
    t_common = threading.Thread(target=send_file_thread, args=(conn, split_files['common'], 100, sender_csv, network_csv, stats_mode, video_id, trace_name, trace_start_index, start_time, seq_counters))
    threads.append(t_common)
    
    priorities = [80, 60, 40, 20]
    for i in range(4):
        t = threading.Thread(target=send_file_thread, args=(conn, split_files['subpics'][i], priorities[i], sender_csv, network_csv, stats_mode, video_id, trace_name, trace_start_index, start_time, seq_counters))
        threads.append(t)
    
    for t in threads:
        t.start()
        
    for t in threads:
        t.join()

    if stats_mode == "timer":
        stop_event.set()
        stats_thread.join()

def main():
    import glob
    parser = argparse.ArgumentParser(description="Semantic-aware QUIC Media Client")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Target server host address")
    parser.add_argument("--port", type=int, default=4433, help="Target server port")
    parser.add_argument("--file", type=str, default="", help="Single VVC file to send")
    default_input_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../streaming_pre/vvc_subpic_outputs"))
    parser.add_argument("--input_dir", type=str, default=default_input_dir, help="Directory of VVC files to send")
    parser.add_argument("--stats_timer", type=float, nargs='?', const=1.0, default=None, help="Enable timer mode for stats. Optionally specify interval in seconds (default 1.0). If omitted, uses iframe mode.")
    parser.add_argument("--trace_name", type=str, default="unknown_trace", help="Name of the trace file for logging purposes")
    parser.add_argument("--single_conn", action="store_true", help="Use a single QUIC connection for all videos")
    parser.add_argument("--flooding", nargs="?", const=30, type=int, default=False, help="Send continuous dummy data. Optionally specify duration in seconds (default 30).")
    parser.add_argument("--loop", type=int, default=1, help="Number of times to loop the video sending (0 for infinite)")
    args = parser.parse_args()

    stats_mode = "timer" if args.stats_timer is not None else "iframe"
    stats_interval = args.stats_timer if args.stats_timer is not None else 0.0

    files_to_process = []
    if args.file and os.path.exists(args.file):
        files_to_process.append(args.file)
    elif os.path.isdir(args.input_dir):
        # find all .vvc files, exclude temp files
        all_vvc = glob.glob(os.path.join(args.input_dir, "*.vvc"))
        files_to_process = [f for f in all_vvc if "common" not in os.path.basename(f) and "subpic_" not in os.path.basename(f)]
        files_to_process.sort() # Ensure GOPs are processed in deterministic order
    
    if not files_to_process:
        print("Error: No VVC files found to process.")
        return

    print(f"=== Found {len(files_to_process)} videos to send ===")

    ns3_start_time = time.time()
    trace_start_index = 0
    try:
        with open("/tmp/ns3_sync.txt", "r") as f:
            lines = f.readlines()
            if len(lines) >= 1:
                ns3_start_time = float(lines[0].strip())
            if len(lines) >= 2:
                trace_start_index = int(lines[1].strip())
        print(f"[Sync] Successfully read ns-3 start time: {ns3_start_time}, trace start index: {trace_start_index}")
    except Exception as e:
        print(f"[Sync] Warning: Could not read /tmp/ns3_sync.txt. Using current time. ({e})")
        
    start_time = ns3_start_time

    sender_log_file = open("sender_log.csv", "w", newline="")
    sender_csv = csv.writer(sender_log_file)
    sender_csv.writerow(["seq_num", "video_id", "gop_index", "trace_name", "trace_start_index", "start_time", "match_time", "stream_id", "file_type", "frame_id", "bytes_sent"])
    
    network_log_file = open("network_log.csv", "w", newline="")
    network_csv = csv.writer(network_log_file)
    network_csv.writerow(["seq_num", "video_id", "gop_index", "trace_name", "trace_start_index", "start_time", "match_time", "rtt_us", "cwnd_bytes", "total_bytes_sent", "estimated_bandwidth_bps", "bytes_in_flight", "posted_bytes", "ideal_bytes"])

    seq_counters = {'network': 0, 'sender': 0, 'gop': 0}

    if args.flooding is not False:
        duration = args.flooding
        print(f"\n=== Starting Flooding Test for {duration} seconds ===")
        conn = QuicConnection(host=args.host, port=args.port, scheduling_scheme=1)
        conn._client.set_ack_callback(lambda e: None)
        conn._client.configure_mtu(1200, 1500)
        
        stats_stop_event = threading.Event()
        stats_thread = None
        if stats_mode == "timer":
            stats_thread = threading.Thread(target=stats_timer_thread, args=(conn, network_csv, stats_interval, stats_stop_event, "flooding", args.trace_name, trace_start_index, start_time, seq_counters))
            stats_thread.start()
            
        stream = conn.open_stream()
        dummy_data = b'A' * (128 * 1024) # 128KB chunks
        
        print(f"[Flooding] Started sending data on Stream {stream.stream_id}...")
        test_start = time.time()
        try:
            while time.time() - test_start < duration:
                stats = conn._client.get_network_stats()
                # Limit outstanding un-transmitted data to ~4MB to prevent OOM
                if stream.bytes_sent - stats.bytes_sent > 4 * 1024 * 1024:
                    time.sleep(0.02)
                    continue
                    
                stream.send_data(frame_id=0, data=dummy_data)
                # Small sleep to prevent CPU hogging
                time.sleep(0.001)
        except KeyboardInterrupt:
            print("\n[Flooding] Interrupted by user.")
            
        stats_stop_event.set()
        if stats_thread:
            stats_thread.join()
            
        stream.drop()
        conn.disconnect(force=True)
        sender_log_file.close()
        network_log_file.close()
        print("=== Flooding Test Completed ===")
        return

    if args.single_conn:
        print(f"\n=== Starting Single QUIC Connection for all videos ===")
        conn = QuicConnection(host=args.host, port=args.port, scheduling_scheme=1)
        conn._client.set_ack_callback(lambda e: None)
        conn._client.configure_mtu(1200, 1500)

    loop_count = 0
    while True:
        loop_count += 1
        if args.loop > 0 and loop_count > args.loop:
            break
        print(f"\n=== Starting Video Loop Round {loop_count} ===")
        for filepath in files_to_process:
            video_id = os.path.splitext(os.path.basename(filepath))[0]
            
            if args.single_conn:
                print(f"\n=== Processing {video_id} over existing connection ===")
            else:
                print(f"\n=== Starting QUIC Connection for {video_id} ===")
                conn = QuicConnection(host=args.host, port=args.port, scheduling_scheme=1)
                conn._client.set_ack_callback(lambda e: None)
                conn._client.configure_mtu(1200, 1500)
            
            process_video(conn, filepath, sender_csv, network_csv, stats_mode, stats_interval, video_id, args.trace_name, trace_start_index, start_time, seq_counters)
            
            time.sleep(2) # Wait for ACKs
            
            if not args.single_conn:
                conn.disconnect(force=True)
                time.sleep(1) # Grace period between videos

    if args.single_conn:
        conn.disconnect(force=True)

    sender_log_file.close()
    network_log_file.close()
    print("\n=== All transmissions completed successfully! ===")

if __name__ == "__main__":
    main()
