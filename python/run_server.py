import os
import sys
import argparse
import time
import threading
import csv

# Ensure we can load the local module in python/lib/ directory
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), 'lib')))

import quic_media

file_lock = threading.Lock()
csv_file = None
csv_writer = None
packet_counter = 0

def main():
    parser = argparse.ArgumentParser(description="QUIC Media Server")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host address to bind to")
    parser.add_argument("--port", type=int, default=4433, help="Port to bind to")
    args = parser.parse_args()

    print(f"=== Starting QUIC Server on {args.host}:{args.port} ===")
    server = quic_media.QuicMediaServer()
    
    server.configure_mtu(1200, 1500)
    
    # We need a cert for msquic server. Assumes they exist in ../certs/
    cert_file = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'certs', 'server.crt'))
    key_file = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'certs', 'server.key'))
    
    if not os.path.exists(cert_file) or not os.path.exists(key_file):
        print(f"Error: Certificates not found at {cert_file} or {key_file}.")
        print("Please ensure you have generated server.crt and server.key in the certs directory.")
        return

    success = server.start_server(args.host, args.port, cert_file, key_file)
    if not success:
        print("Failed to start server.")
        return
        
    # Setup CSV Logging for Receiver Timestamps
    global csv_file, csv_writer, packet_counter
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    csv_file = open(os.path.join(log_dir, "receiver_log.csv"), "w", newline="")
    csv_writer = csv.writer(csv_file)
    csv_writer.writerow(["receive_time_ns", "stream_id", "packet_seq", "bytes_received"])
    
    import queue
    log_queue = queue.Queue()
    
    def log_writer_thread():
        while True:
            item = log_queue.get()
            if item is None:
                break
            csv_writer.writerow(item)
            
    writer_thread = threading.Thread(target=log_writer_thread, daemon=True)
    writer_thread.start()

    def on_receive(stream_id, data):
        global packet_counter
        recv_time = time.time_ns()
        
        # No lock needed for simple counter in Python due to GIL, 
        # and queue.put is thread-safe. This avoids blocking the MsQuic thread!
        packet_counter += 1
        log_queue.put([recv_time, stream_id, packet_counter, len(data)])
        
        if packet_counter % 10000 == 0:
            print(f"[Server] Received {packet_counter} packets so far...")
        
    server.set_recv_callback(on_receive)
    
    try:
        print("Server is running. Press Ctrl+C to stop.")
        # Instead of blocking in C++ with server.run(), we sleep in Python.
        # This allows Python to catch KeyboardInterrupt (Ctrl+C) instantly.
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        server.set_recv_callback(None)
        server.stop_server()
        # Close CSV file
        log_queue.put(None)
        writer_thread.join(timeout=2.0)
        if csv_file:
            csv_file.close()
        print("Server stopped. Logs saved to receiver_log.csv.")
    

if __name__ == "__main__":
    main()
