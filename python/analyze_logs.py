import csv
from collections import Counter
import os

try:
    log_dir = "logs"
    sender = list(csv.DictReader(open(os.path.join(log_dir, 'sender_log.csv'))))
    receiver = list(csv.DictReader(open(os.path.join(log_dir, 'receiver_log.csv'))))
    network = list(csv.DictReader(open(os.path.join(log_dir, 'network_log.csv'))))

    print("📊 QUIC Media Transmission Report 📊")
    print("=" * 40)
    print(f"Total NALUs Sent:      {len(sender)}")
    print(f"Total NALUs Received:  {len(receiver)}")
    print(f"GOP Boundaries Logged: {len(network)}")
    print(f"Packet Loss Rate:      {100.0 * (1.0 - len(receiver) / len(sender)) if len(sender) > 0 else 0:.2f}%")
    print("=" * 40)

    print("\n--- 1. Subpicture Transmission Stats ---")
    counts = Counter(r['file_type'] for r in sender)
    for k, v in sorted(counts.items()):
        print(f" - {k:<15}: {v} frames sent")

    print("\n--- 2. Network Statistics (GOP Boundaries) ---")
    if len(network) > 0:
        rtts = [int(r['rtt_us']) for r in network]
        cwnds = [int(r['cwnd_bytes']) for r in network]
        bytes_s = [int(r['total_bytes_sent']) for r in network]
        bws = [int(r['estimated_bandwidth_bps']) for r in network if 'estimated_bandwidth_bps' in r]
        in_flight = [int(r['bytes_in_flight']) for r in network if 'bytes_in_flight' in r]
        posted = [int(r['posted_bytes']) for r in network if 'posted_bytes' in r]
        ideal = [int(r['ideal_bytes']) for r in network if 'ideal_bytes' in r]

        print(f"RTT (us)        : Min = {min(rtts):<6} | Max = {max(rtts):<6} | Avg = {sum(rtts)/len(rtts):.0f}")
        print(f"CWND (bytes)    : Min = {min(cwnds):<6} | Max = {max(cwnds):<6} | Avg = {sum(cwnds)/len(cwnds):.0f}")
        if bws:
            print(f"Est. Bandwidth  : Min = {min(bws)/1e6:<6.2f} | Max = {max(bws)/1e6:<6.2f} | Avg = {sum(bws)/len(bws)/1e6:.2f} Mbps")
        if in_flight:
            print(f"Bytes In-Flight : Min = {min(in_flight):<6} | Max = {max(in_flight):<6} | Avg = {sum(in_flight)/len(in_flight):.0f}")
        if posted:
            print(f"Posted Bytes    : Min = {min(posted):<6} | Max = {max(posted):<6} | Avg = {sum(posted)/len(posted):.0f}")
        if ideal:
            print(f"Ideal Bytes     : Min = {min(ideal):<6} | Max = {max(ideal):<6} | Avg = {sum(ideal)/len(ideal):.0f}")
        print(f"Total Bytes Sent: {max(bytes_s):,}")
    else:
        print("No network stats recorded.")

    print("\n--- 3. Performance Summary ---")
    if len(sender) > 0:
        min_t = min(int(r['send_time_ns']) for r in sender)
        max_t = max(int(r['send_time_ns']) for r in sender)
        duration = (max_t - min_t) / 1e9
        total_bytes = sum(int(r['bytes_sent']) for r in sender)
        if duration > 0:
            print(f"Total Duration    : {duration:.2f} seconds")
            print(f"Total Data Transferred: {total_bytes / (1024*1024):.2f} MB")
            print(f"Average Throughput: {(total_bytes * 8) / (duration * 1e6):.2f} Mbps")
            print(f"Simulated FPS     : {len(sender) / duration:.2f} NALUs/sec")

except Exception as e:
    print(f"Analysis failed: {e}")
