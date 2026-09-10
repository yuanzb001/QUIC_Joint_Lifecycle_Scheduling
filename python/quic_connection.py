import time
import threading
import datetime
import quic_media

class QuicStream:
    def __init__(self, client, stream_id):
        self._client = client
        self.stream_id = stream_id
        self.is_active = True
        self.frames_sent = 0
        self.bytes_sent = 0

    def send_data(self, frame_id, subpic_id=0, obj_type=1, data=b"", priority=0, is_fin=False):
        if not self.is_active:
            return False
        
        obj_id = self._client.enqueue_object(
            self.stream_id, frame_id, subpic_id, obj_type, data, priority, is_fin
        )
        # Check if obj_id is not the failure value (uint64_t -1)
        success = (obj_id != 18446744073709551615 and obj_id != -1)
        if success:
            self.frames_sent += 1
            self.bytes_sent += len(data)
        return success

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

    def open_stream(self):
        stream_id = self._client.open_stream()
        stream = QuicStream(self._client, stream_id)
        self.streams.append(stream)
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
