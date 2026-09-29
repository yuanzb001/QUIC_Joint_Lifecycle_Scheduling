import time
import threading
import datetime
import quic_media

class QuicStream:
    def __init__(self, client, stream_id, priority=0):
        self._client = client
        self.stream_id = stream_id
        self.is_active = True
        self.priority = int(priority)
        self.frames_sent = 0
        self.bytes_sent = 0

    def send_data(self, frame_id, subpic_id=0, obj_type=1, data=b"", priority=0, is_fin=False, delay_send=False):
        if not self.is_active:
            return False
        
        obj_id = self._client.enqueue_object(
            self.stream_id, frame_id, subpic_id, obj_type, data, priority, is_fin, delay_send
        )
        # Check if obj_id is not the failure value (uint64_t -1)
        success = (obj_id != 18446744073709551615 and obj_id != -1)
        if success:
            self.frames_sent += 1
            self.bytes_sent += len(data)
        return success

    def send_chunk(self, data, is_fin=False, delay_send=False):
        if not self.is_active or not data:
            return False
    
        success = self._client.send_raw(
            self.stream_id,
            data,
            is_fin,
            delay_send
        )
    
        if success:
            self.bytes_sent += len(data)
    
        return success
    
    def set_priority(self, priority):
        """Dynamically update QUIC stream priority."""
        if not self.is_active:
            return False

        priority = int(priority)

        if priority < 0 or priority > 65535:
            raise ValueError(
                f"priority must be in [0, 65535], got {priority}"
            )

        success = self._client.set_stream_priority(
            self.stream_id,
            priority
        )

        if success:
            self.priority = priority

            print(
                f"[Stream {self.stream_id}] "
                f"priority -> {priority}"
            )

        return bool(success)

    def close(self):
        """Graceful close"""
        if self.is_active:
            self.is_active = False
            return self._client.close_stream(self.stream_id)
        return False

    def drop(self):
        """Abort stream immediately"""
        if self.is_active:
            self.is_active = False
            return self._client.drop_stream(self.stream_id)
        return False

class QuicConnection:
    def __init__(self, host, port, scheduling_scheme=1):
        self._client = quic_media.QuicMediaClient()
        # 0 = FIFO, 1 = ROUND_ROBIN
        self._client.set_stream_scheduling_scheme(scheduling_scheme)
        
        self.host = host
        self.port = port
        self.streams = []
        
        # Generator state
        self._generator_running = False
        self._generator_thread = None
        self._worker_threads = []

        self.connect()

    def connect(self):
        print(f"[Connection] Connecting to {self.host}:{self.port}...")
        success = self._client.connect(self.host, self.port)
        if not success:
            raise ConnectionError(f"Failed to connect to {self.host}:{self.port}. Server might be offline.")

    def disconnect(self, force=False):
        print(f"[Connection] Disconnecting (force={force})...")
        self.stop_stream_generator()
        
        # Wait for all background workers to finish enqueueing BEFORE disconnecting
        for t in self._worker_threads:
            t.join()
        self._worker_threads.clear()

        self._client.disconnect(force=force)

    def open_stream(self, priority=0):
        stream_id = self._client.open_stream()
    
        if stream_id is None or int(stream_id) < 0:
            raise RuntimeError("Failed to open QUIC stream")
    
        stream = QuicStream(
            self._client,
            stream_id,
            priority=priority
        )
    
        # Apply initial priority immediately after stream creation.
        if not stream.set_priority(priority):
            stream.close()
            raise RuntimeError(
                f"Failed to set priority={priority} "
                f"for stream={stream_id}"
            )
    
        self.streams.append(stream)
    
        print(
            f"[Connection] Opened stream={stream.stream_id} "
            f"priority={priority}"
        )
    
        return stream

    def _auto_send_task(self, stream, count, delay, payload_size):
        payload = b"A" * payload_size
        for i in range(count):
            if not stream.is_active:
                break
            
            now_str = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
            print(f"[Send] Time: {now_str} | Stream ID: {stream.stream_id} | Enqueue Frame {i}")
            
            success = stream.send_data(frame_id=i, data=payload)
            if not success:
                break
                
            time.sleep(delay)
            
        print(f"[Stream {stream.stream_id}] Finished queuing {stream.frames_sent} frames to MsQuic.")

    def start_stream_generator(self, interval=2.0, frames_per_stream=100, delay=0.033, payload_size=1024*1024):
        if self._generator_running:
            return
            
        print(f"[Connection] Starting Stream Generator (1 stream every {interval}s)")
        self._generator_running = True
        self._generator_thread = threading.Thread(
            target=self._generator_task,
            args=(interval, frames_per_stream, delay, payload_size)
        )
        self._generator_thread.start()

    def _generator_task(self, interval, frames_per_stream, delay, payload_size):
        while self._generator_running:
            stream = self.open_stream()
            print(f"\n[Generator] Opened New Stream: {stream.stream_id}")
            
            t = threading.Thread(
                target=self._auto_send_task,
                args=(stream, frames_per_stream, delay, payload_size)
            )
            t.start()
            self._worker_threads.append(t)
            
            # Wait for interval or stop event
            wait_time = 0
            while wait_time < interval and self._generator_running:
                time.sleep(0.1)
                wait_time += 0.1

    def stop_stream_generator(self):
        if self._generator_running:
            print("[Connection] Stopping Stream Generator...")
            self._generator_running = False
            if self._generator_thread:
                self._generator_thread.join()
                self._generator_thread = None
