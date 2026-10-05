#!/usr/bin/env python3
import cv2
import csv
import time
import struct
import hashlib
import threading

from collections import deque
from pathlib import Path


# ============================================================
# Constants
# ============================================================

COMMON_PRIORITY = 65535
DEFAULT_SP_PRIORITY = 1000

GATE_RETRY = 0.1

HEADER_FMT = "<IIHHHH"
HEADER_SIZE = struct.calcsize(HEADER_FMT)

OBJECT_CONFIG = 1
OBJECT_SUBPIC = 2
OBJECT_INIT = 3

INIT_FMT = "<HfI"

IDR_TYPES = {7, 8}

csv_lock = threading.Lock()

assert HEADER_SIZE == 16


# ============================================================
# General Helpers
# ============================================================

def sha256(path):
    h = hashlib.sha256()

    with open(path, "rb") as f:
        while b := f.read(1024 * 1024):
            h.update(b)

    return h.hexdigest()


# ============================================================
# VVC Random Access Helpers
# ============================================================

def get_vvc_nal_types(data):
    types = []
    n = len(data)
    i = 0

    while i + 4 <= n:
        if data[i:i + 3] == b"\x00\x00\x01":
            header = i + 3

        elif i + 5 <= n and data[i:i + 4] == b"\x00\x00\x00\x01":
            header = i + 4

        else:
            i += 1
            continue

        if header + 1 < n:
            nal_type = (data[header + 1] >> 3) & 0x1F
            types.append(nal_type)

        i = header + 2

    return types


def is_idr_frame(split_files, sid, fid):
    data = split_files["frames"][sid][fid]["data"]
    nal_types = get_vvc_nal_types(data)

    return any(t in IDR_TYPES for t in nal_types)


def find_idr_frames(split_files, sid):
    return [fid for fid in range(len(split_files["frames"][sid])) if is_idr_frame(split_files, sid, fid)]


def find_next_idr(split_files, sid, start_fid):
    for fid in range(start_fid, len(split_files["frames"][sid])):
        if is_idr_frame(split_files, sid, fid):
            return fid

    return None


# ============================================================
# Wire Objects
# ============================================================

def make_wire_data(frame_id, subpic_id, object_type, payload):
    return struct.pack(HEADER_FMT, frame_id, len(payload), object_type, subpic_id, 0, 1) + payload


def build_init_object(num_subpics, fps, frame_count, common_stream):
    payload = struct.pack(INIT_FMT, int(num_subpics), float(fps), int(frame_count))
    wire = make_wire_data(0, 0xFFFF, OBJECT_INIT, payload)

    return {
        "frame_id": 0,
        "subpic_id": 0xFFFF,
        "kind": "init",
        "object_type": OBJECT_INIT,
        "stream": common_stream,
        "stream_id": common_stream.stream_id,
        "priority": COMMON_PRIORITY,
        "data": payload,
        "size": len(payload),
        "wire_data": wire,
        "wire_size": len(wire),
        "offset": 0
    }


def make_object(fid, sid, kind, stream, priority, data):
    object_type = OBJECT_CONFIG if kind == "config" else OBJECT_SUBPIC
    wire = make_wire_data(fid, sid, object_type, data)

    return {
        "frame_id": fid,
        "subpic_id": sid,
        "kind": kind,
        "object_type": object_type,
        "stream": stream,
        "stream_id": stream.stream_id,
        "priority": priority,
        "data": data,
        "size": len(data),
        "wire_data": wire,
        "wire_size": len(wire),
        "offset": 0
    }


def build_config_objects(split_files, common_stream):
    objs = []

    for sid, data in sorted(split_files["configs"].items()):
        if data:
            objs.append(make_object(0, sid, "config", common_stream, COMMON_PRIORITY, data))

    return objs


def build_frame_objects(fid, split_files, sp_streams, priorities, active_subpics):
    objs = []

    for sid in sorted(active_subpics):
        stream = sp_streams.get(sid)

        if stream is None or not stream.is_active:
            continue

        frame = split_files["frames"][sid][fid]

        if int(frame["frame_id"]) != fid:
            raise RuntimeError(f"Frame ID mismatch: SP{sid}, expected={fid}, got={frame['frame_id']}")

        data = frame["data"]

        if data:
            objs.append(make_object(fid, sid, "subpic", stream, priorities[sid], data))

    return objs


# ============================================================
# Ready Queue
# ============================================================

class ReadyQueue:

    def __init__(self):
        self.q = deque()
        self.lock = threading.Lock()

    def push_many(self, x):
        with self.lock:
            self.q.extend(x)

    def pop(self):
        with self.lock:
            return self.q.popleft() if self.q else None

    def empty(self):
        with self.lock:
            return not self.q

    def __len__(self):
        with self.lock:
            return len(self.q)

    def mark_last_for_close(self, sid):
        with self.lock:
            for obj in reversed(self.q):
                if obj.get("kind") == "subpic" and obj.get("subpic_id") == sid:
                    obj["close_after_send"] = True
                    return True

        return False


# ============================================================
# Network
# ============================================================

def get_network_state(conn):
    s = conn._client.get_network_stats()

    return {
        "rtt_us": int(s.rtt_us),
        "cwnd_bytes": int(s.cwnd_bytes),
        "bytes_in_flight": int(s.bytes_in_flight),
        "bytes_sent": int(s.bytes_sent),
        "stream_bytes_sent": int(s.send_total_stream_bytes),
        "estimated_bandwidth_bps": int(s.estimated_bandwidth_bps),
        "posted_bytes": int(s.posted_bytes),
        "ideal_bytes": int(s.ideal_bytes)
    }


def get_send_budget(state):
    return max(0, state["cwnd_bytes"] - state["bytes_in_flight"])


# ============================================================
# Logging
# ============================================================

def log_sender(writer, counters, obj, video_id, trace_name, trace_idx, start_time, state):
    with csv_lock:
        counters["sender"] += 1

        writer.writerow([
            counters["sender"],
            video_id,
            trace_name,
            trace_idx,
            f"{start_time:.6f}",
            f"{time.time() - start_time:.6f}",
            obj["stream_id"],
            obj["kind"],
            obj["subpic_id"],
            obj["frame_id"],
            obj.get("source_time_ns", 0),
            obj.get("submit_complete_ns", 0),
            obj["size"],
            obj["wire_size"],
            obj["priority"],
            state["rtt_us"],
            state["cwnd_bytes"],
            state["bytes_in_flight"],
            state["estimated_bandwidth_bps"]
        ])


def stats_thread_fn(conn, writer, interval, stop, video_id, trace_name, trace_idx, start_time, counters):
    while not stop.is_set():
        try:
            state = get_network_state(conn)

            with csv_lock:
                counters["network"] += 1

                writer.writerow([
                    counters["network"],
                    video_id,
                    trace_name,
                    trace_idx,
                    f"{start_time:.3f}",
                    f"{time.time() - start_time:.3f}",
                    state["rtt_us"],
                    state["cwnd_bytes"],
                    state["bytes_sent"],
                    state["stream_bytes_sent"],
                    state["estimated_bandwidth_bps"],
                    state["bytes_in_flight"],
                    state["posted_bytes"],
                    state["ideal_bytes"]
                ])

        except Exception as e:
            print(f"[Stats] {e}")

        stop.wait(interval)


# ============================================================
# Sender Gate
# ============================================================

def sender_gate_thread(conn, queue, event, finished, writer, video_id, trace_name, trace_idx, start_time, counters):
    active = None

    while not finished.is_set() or not queue.empty() or active is not None:

        if active is None:
            active = queue.pop()

            if active is None:
                event.wait(GATE_RETRY)
                event.clear()
                continue

        try:
            state = get_network_state(conn)
            budget = get_send_budget(state)

        except Exception as e:
            print(f"[GATE] stats error: {e}")
            time.sleep(GATE_RETRY)
            continue

        if budget <= 0:
            time.sleep(GATE_RETRY)
            continue

        offset = active["offset"]
        remaining = active["wire_size"] - offset
        send_n = min(remaining, budget)

        if send_n <= 0:
            active = None
            continue

        chunk = active["wire_data"][offset:offset + send_n]

        if not active["stream"].send_chunk(chunk):
            print(f"[GATE] FAIL frame={active['frame_id']} kind={active['kind']} sp={active['subpic_id']} offset={offset} n={send_n}")
            time.sleep(GATE_RETRY)
            continue

        active["offset"] += send_n
        remaining = active["wire_size"] - active["offset"]

        print(
            f"[GATE] SEND frame={active['frame_id']:3d} "
            f"kind={active['kind']:6s} "
            f"sp={active['subpic_id']:2d} "
            f"chunk={send_n:6d}B "
            f"offset={active['offset']:6d}/{active['wire_size']:6d} "
            f"remain={remaining:6d}B "
            f"cwnd={state['cwnd_bytes']:6d} "
            f"bif={state['bytes_in_flight']:6d} "
            f"budget={budget:6d}"
        )

        if remaining == 0:
            active["submit_complete_ns"] = time.time_ns()

            log_sender(writer, counters, active, video_id, trace_name, trace_idx, start_time, state)

            print(
                f"[GATE] COMPLETE frame={active['frame_id']:3d} "
                f"kind={active['kind']} "
                f"sp={active['subpic_id']} "
                f"payload={active['size']}B"
            )

            if active.get("close_after_send", False):
                sid = active["subpic_id"]
                stream = active["stream"]

                print(f"[LIFECYCLE] SP{sid} queue drained -> CLOSE stream={stream.stream_id}")

                stream.close()

            active = None

        time.sleep(0.001)


# ============================================================
# Streams
# ============================================================

def create_streams(conn, num_subpics, default_priority=DEFAULT_SP_PRIORITY):
    common = conn.open_stream(priority=COMMON_PRIORITY)

    priorities = [int(default_priority)] * num_subpics

    sp_streams = {
        sid: conn.open_stream(priority=priorities[sid])
        for sid in range(num_subpics)
    }

    print(f"[Live] COMMON -> stream={common.stream_id}, priority={COMMON_PRIORITY}")

    for sid, stream in sp_streams.items():
        print(f"[Live] SP{sid} -> stream={stream.stream_id}, priority={priorities[sid]}")

    return common, sp_streams, priorities


def apply_priorities(sp_streams, priorities, new_priorities, active_subpics):
    for sid in sorted(active_subpics):

        if sid not in new_priorities:
            continue

        new_priority = int(new_priorities[sid])

        if new_priority == priorities[sid]:
            continue

        stream = sp_streams.get(sid)

        if stream is None or not stream.is_active:
            continue

        if not stream.set_priority(new_priority):
            raise RuntimeError(f"Failed to update SP{sid} priority={new_priority}")

        priorities[sid] = new_priority


def deactivate_subpic(sid, active_subpics, closing_subpics):
    if sid not in active_subpics:
        return

    active_subpics.discard(sid)
    closing_subpics.add(sid)

    print(f"[LIFECYCLE] SP{sid} -> CLOSING")


def activate_subpic(conn, sid, sp_streams, priorities, active_subpics):
    if sid in active_subpics:
        return

    stream = conn.open_stream(priority=priorities[sid])

    sp_streams[sid] = stream
    active_subpics.add(sid)

    print(f"[LIFECYCLE] SP{sid} OPEN new_stream={stream.stream_id} priority={priorities[sid]}")


# ============================================================
# YUV420 Reader
# ============================================================

class YUVReader:

    def __init__(self, path, width, height):
        import cv2
        import numpy as np

        self.cv2 = cv2
        self.np = np

        self.path = Path(path)

        self.w = int(width)
        self.h = int(height)

        self.frame_bytes = self.w * self.h * 3 // 2

        self.f = open(self.path, "rb")

    def read(self, fid):
        self.f.seek(int(fid) * self.frame_bytes)

        buf = self.f.read(self.frame_bytes)

        if len(buf) != self.frame_bytes:
            return None

        yuv = self.np.frombuffer(buf, dtype=self.np.uint8).reshape(self.h * 3 // 2, self.w)

        return self.cv2.cvtColor(yuv, self.cv2.COLOR_YUV2BGR_I420)

    def close(self):
        if self.f and not self.f.closed:
            self.f.close()

class FrameReader:

    def __init__(self, frame_dir, start_index=1):
        self.frame_dir = Path(frame_dir)
        self.start_index = int(start_index)

        if not self.frame_dir.exists():
            raise RuntimeError(f"Frame directory not found: {self.frame_dir}")

    def read(self, fid):
        frame_id = fid + self.start_index
        path = self.frame_dir / f"{frame_id:06d}.png"

        frame = cv2.imread(str(path))

        if frame is None:
            raise RuntimeError(f"Failed to read frame: {path}")

        return frame