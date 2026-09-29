import sys, csv, time, ctypes, argparse, threading, struct, hashlib
from pathlib import Path
from collections import deque

# ============================================================
# Config
# ============================================================

ROOT = Path(__file__).resolve().parent
LIB_DIR = ROOT / "lib"
ctypes.CDLL(str(LIB_DIR / "libmsquic.so.2"), mode=ctypes.RTLD_GLOBAL)
sys.path.append(str(LIB_DIR))

from quic_connection import QuicConnection
import vvc_splitter

CONTROL_INTERVAL_S = 1.0
INITIAL_WARMUP_S = 1.0
COMMON_PRIORITY = 65535
# SP_PRIORITIES = [60000, 1000, 100, 10]
SP_PRIORITIES = [1000, 1000, 1000, 1000]
GATE_RETRY = 0.1
DEFAULT_FPS = 30.0
HEADER_FMT = "<IIHHHH"
HEADER_SIZE = struct.calcsize(HEADER_FMT)
PH_TYPE = 19
csv_lock = threading.Lock()

TEST_SP = 0
CLOSE_FRAME = 20
REOPEN_FRAME = 30

assert HEADER_SIZE == 16

# ============================================================
# Helpers
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

IDR_TYPES = {7, 8}   # IDR_W_RADL, IDR_N_LP


def get_vvc_nal_types(data):
    """
    Extract VVC nal_unit_type values from Annex-B payload.

    VVC 2-byte NAL header:
        nal_unit_type = (header_byte_1 >> 3) & 0x1F
    """
    types = []
    n = len(data)
    i = 0

    while i + 4 <= n:

        # 00 00 01
        if data[i:i + 3] == b"\x00\x00\x01":
            header = i + 3

        # 00 00 00 01
        elif (
            i + 5 <= n
            and data[i:i + 4] == b"\x00\x00\x00\x01"
        ):
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
    frame = split_files["frames"][sid][fid]

    nal_types = get_vvc_nal_types(
        frame["data"]
    )

    return any(
        t in IDR_TYPES
        for t in nal_types
    )


def find_idr_frames(split_files, sid):
    return [
        fid
        for fid in range(len(split_files["frames"][sid]))
        if is_idr_frame(split_files, sid, fid)
    ]


def find_next_idr(split_files, sid, start_fid):
    for fid in range(
        start_fid,
        len(split_files["frames"][sid])
    ):
        if is_idr_frame(
            split_files,
            sid,
            fid
        ):
            return fid

    return None

OBJECT_CONFIG = 1
OBJECT_SUBPIC = 2
OBJECT_INIT = 3

INIT_FMT = "<HfI"
# uint16: num_subpics
# float : fps
# uint32: frame_count

def build_init_object(
    num_subpics,
    fps,
    frame_count,
    common_stream
):
    payload = struct.pack(
        INIT_FMT,
        int(num_subpics),
        float(fps),
        int(frame_count)
    )

    wire = make_wire_data(
        0,
        0xFFFF,          # not a real SP
        OBJECT_INIT,
        payload
    )

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

def make_wire_data(frame_id, subpic_id, object_type, payload):
    return struct.pack(
        HEADER_FMT,
        frame_id,
        len(payload),
        object_type,
        subpic_id,
        0,      # fragment_idx
        1       # fragment_count
    ) + payload


class ReadyQueue:
    def __init__(self):
        self.q, self.lock = deque(), threading.Lock()

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
                if (
                    obj.get("kind") == "subpic"
                    and obj.get("subpic_id") == sid
                ):
                    obj["close_after_send"] = True
                    return True
    
        return False


# ============================================================
# Objects
# ============================================================

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
    """VPS/SPS/PPS: send once through the common stream."""
    objs = []
    for sid, data in sorted(split_files["configs"].items()):
        if data:
            objs.append(
                make_object(
                    0, sid, "config",
                    common_stream, COMMON_PRIORITY, data
                )
            )
    return objs


def build_frame_objects(fid, split_files, sp_streams, priorities, active_subpics):
    objs = []

    for sid in sorted(active_subpics):

        stream = sp_streams.get(sid)

        if stream is None or not stream.is_active:
            continue

        frame = split_files["frames"][sid][fid]

        if int(frame["frame_id"]) != fid:
            raise RuntimeError(
                f"Frame ID mismatch: SP{sid}, "
                f"expected={fid}, "
                f"got={frame['frame_id']}"
            )

        data = frame["data"]

        if data:
            objs.append(
                make_object(fid, sid, "subpic", stream, priorities[sid], data)
            )

    return objs


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


def get_send_budget(s):
    return max(0, s["cwnd_bytes"] - s["bytes_in_flight"])


# ============================================================
# Logs
# ============================================================

def log_sender(w, counters, obj, video_id, trace_name,
               trace_idx, start_time, state):

    with csv_lock:
        counters["sender"] += 1

        w.writerow([
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
            state["estimated_bandwidth_bps"],
        ])


def stats_thread_fn(conn, w, interval, stop, video_id,
                    trace_name, trace_idx, start_time, counters):

    while not stop.is_set():
        try:
            s = get_network_state(conn)

            with csv_lock:
                counters["network"] += 1

                w.writerow([
                    counters["network"], video_id, trace_name,
                    trace_idx, f"{start_time:.3f}",
                    f"{time.time() - start_time:.3f}",
                    s["rtt_us"], s["cwnd_bytes"],
                    s["bytes_sent"], s["stream_bytes_sent"],
                    s["estimated_bandwidth_bps"],
                    s["bytes_in_flight"],
                    s["posted_bytes"], s["ideal_bytes"]
                ])

        except Exception as e:
            print(f"[Stats] {e}")

        stop.wait(interval)


# ============================================================
# Python Gate
# ============================================================

def sender_gate_thread(conn, queue, event, finished, writer,
                       video_id, trace_name, trace_idx,
                       start_time, counters):

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

        # Gate decision is object/frame level,
        # actual submission is byte/chunk level.
        send_n = min(remaining, budget)

        if send_n <= 0:
            active = None
            continue

        chunk = active["wire_data"][offset:offset + send_n]

        if not active["stream"].send_chunk(chunk):
            print(
                f"[GATE] FAIL frame={active['frame_id']} "
                f"kind={active['kind']} sp={active['subpic_id']} "
                f"offset={offset} n={send_n}"
            )
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

            log_sender(
                writer, counters, active,
                video_id, trace_name, trace_idx,
                start_time, state
            )

            print(
                f"[GATE] COMPLETE frame={active['frame_id']:3d} "
                f"kind={active['kind']} "
                f"sp={active['subpic_id']} "
                f"payload={active['size']}B"
            )

            # --------------------------------------------
            # Deferred lifecycle close
            # --------------------------------------------
            if active.get("close_after_send", False):
                sid = active["subpic_id"]
                stream = active["stream"]

                print(
                    f"[LIFECYCLE] SP{sid} queue drained -> "
                    f"CLOSE stream={stream.stream_id}"
                )

                stream.close()

            active = None

        time.sleep(0.001)


# ============================================================
# Streams
# ============================================================

def create_streams(conn, num_subpics):
    if num_subpics > len(SP_PRIORITIES):
        raise RuntimeError(
            f"{num_subpics} SPs but only "
            f"{len(SP_PRIORITIES)} priorities"
        )

    common = conn.open_stream(
        priority=COMMON_PRIORITY
    )

    priorities = SP_PRIORITIES[:num_subpics]

    sp_streams = {
        sid: conn.open_stream(
            priority=priorities[sid]
        )
        for sid in range(num_subpics)
    }

    print(
        f"[Live] COMMON -> stream={common.stream_id}, "
        f"priority={COMMON_PRIORITY}"
    )

    for sid, s in sp_streams.items():
        print(
            f"[Live] SP{sid} -> stream={s.stream_id}, "
            f"priority={priorities[sid]}"
        )

    return common, sp_streams, priorities

def apply_priorities(
    sp_streams,
    priorities,
    new_priorities,
    active_subpics
):
    for sid in sorted(active_subpics):

        if sid not in new_priorities:
            continue

        new_p = int(new_priorities[sid])

        if new_p == priorities[sid]:
            continue

        stream = sp_streams.get(sid)

        if stream is None or not stream.is_active:
            continue

        if not stream.set_priority(new_p):
            raise RuntimeError(
                f"Failed to update "
                f"SP{sid} priority={new_p}"
            )

        priorities[sid] = new_p

def deactivate_subpic(sid, active_subpics, closing_subpics):
    if sid not in active_subpics:
        return

    active_subpics.discard(sid)
    closing_subpics.add(sid)

    print(f"[LIFECYCLE] SP{sid} -> CLOSING")

def activate_subpic(
    conn,
    sid,
    sp_streams,
    priorities,
    active_subpics
):
    if sid in active_subpics:
        return

    stream = conn.open_stream(
        priority=priorities[sid]
    )

    sp_streams[sid] = stream
    active_subpics.add(sid)

    print(
        f"[LIFECYCLE] SP{sid} OPEN "
        f"new_stream={stream.stream_id} "
        f"priority={priorities[sid]}"
    )

# ============================================================
# Live Streaming
# ============================================================

def live_streaming(conn, split_files, common_stream, sp_streams,
                   priorities, writer, video_id, trace_name,
                   trace_idx, start_time, counters):
    active_subpics = set(sp_streams.keys())
    closing_subpics = set()
    fps = float(split_files["bitstream_info"].get("fps") or DEFAULT_FPS)
    if fps <= 0:
        raise RuntimeError(f"Invalid FPS={fps}")

    nframes = int(split_files["frame_count"])
    interval = 1.0 / fps

    idr_frames = find_idr_frames(split_files, TEST_SP)

    print(
        f"[IDR] SP{TEST_SP} "
        f"frames={idr_frames}"
    )

    next_reopen_idr = find_next_idr(split_files, TEST_SP, REOPEN_FRAME)

    if next_reopen_idr is None:
        print(
            f"[IDR] WARNING: "
            f"SP{TEST_SP} has no IDR "
            f"at/after frame {REOPEN_FRAME}; "
            f"reopen disabled"
        )
    else:
        print(
            f"[IDR] SP{TEST_SP} "
            f"reopen request={REOPEN_FRAME} "
            f"actual_reopen={next_reopen_idr}"
        )

    print(
        f"[IDR] reopen request={REOPEN_FRAME} "
        f"-> next_idr={next_reopen_idr}"
    )

    queue = ReadyQueue()
    event = threading.Event()
    finished = threading.Event()

    t = threading.Thread(
        target=sender_gate_thread,
        args=(conn, queue, event, finished, writer,
              video_id, trace_name, trace_idx, start_time, counters),
        daemon=True
    )
    t.start()

    # --------------------------------------------------------
    # Initialization: VPS/SPS/PPS -> common stream, once
    # --------------------------------------------------------
    init_obj = build_init_object(
        num_subpics=len(sp_streams),
        fps=fps,
        frame_count=nframes,
        common_stream=common_stream
    )
    
    config_objs = build_config_objects(
        split_files,
        common_stream
    )
    
    queue.push_many([init_obj])
    queue.push_many(config_objs)
    event.set()
    
    print(
        f"[INIT] num_subpics={len(sp_streams)} "
        f"fps={fps:.3f} "
        f"frames={nframes}"
    )
    
    print(
        f"[CONFIG] objects={len(config_objs)} "
        f"bytes={sum(x['size'] for x in config_objs):,} "
        f"stream={common_stream.stream_id}"
    )

    # Wait until initialization objects leave the Python queue.
    # Gate still controls actual submission.
    while not queue.empty():
        time.sleep(0.001)

    # --------------------------------------------------------
    # Live source: PH + VCL -> corresponding SP stream
    # --------------------------------------------------------
    t0 = time.monotonic()

    print(
        f"[Live] START fps={fps:.3f}, frames={nframes}, "
        f"SPs={len(sp_streams)}"
    )

    control_frames = max(
        1,
        int(round(fps * CONTROL_INTERVAL_S))
    )

    for fid in range(nframes):

        # --------------------------------------------------------
        # Live frame release timing
        # --------------------------------------------------------
        wait = t0 + fid * interval - time.monotonic()

        if wait > 0:
            time.sleep(wait)

        # --------------------------------------------------------
        # Scheduling update every 1 second
        #
        # frame 0 ~ control_frames-1:
        #     all SPs active + equal priority
        #
        # frame control_frames:
        #     first scheduling decision
        # --------------------------------------------------------
        if fid > 0 and fid % control_frames == 0:

            state = get_network_state(conn)

            control_idx = fid // control_frames

            print(
                f"[SCHED] control={control_idx} "
                f"frame={fid} "
                f"active={sorted(active_subpics)} "
                f"priority={priorities} "
                f"bw={state['estimated_bandwidth_bps']} "
                f"rtt={state['rtt_us']}us "
                f"cwnd={state['cwnd_bytes']}B "
                f"bif={state['bytes_in_flight']}B"
            )

            # ----------------------------------------------------
            # TODO: later add scheduler here
            #
            # new_priorities = ...
            # new_active_subpics = ...
            #
            # apply_priorities(...)
            # lifecycle open / close(...)
            # ----------------------------------------------------

        # --------------------------------------------------------
        # Lifecycle CLOSE / REOPEN test
        # --------------------------------------------------------

        if fid == CLOSE_FRAME and TEST_SP in active_subpics:
            print(
                f"[TEST-CLOSE] frame={fid} "
                f"SP{TEST_SP} stream={sp_streams[TEST_SP].stream_id}"
            )

            # Stop producing new objects immediately,
            # but DO NOT close the QUIC stream yet.
            deactivate_subpic(TEST_SP, active_subpics, closing_subpics)
            
            found = queue.mark_last_for_close(TEST_SP)

            if not found:
                print(
                    f"[LIFECYCLE] SP{TEST_SP} no pending object -> "
                    f"close immediately"
                )
                sp_streams[TEST_SP].close()
                closing_subpics.discard(TEST_SP)


        # --------------------------------------------------------
        # Lifecycle REOPEN
        #
        # REOPEN_FRAME = scheduler/request time.
        # Actual reopen occurs at the first IDR >= REOPEN_FRAME.
        # --------------------------------------------------------

        if (
            next_reopen_idr is not None
            and fid == next_reopen_idr
            and TEST_SP not in active_subpics
        ):

            old_stream_id = (sp_streams[TEST_SP].stream_id)
            activate_subpic(conn, TEST_SP, sp_streams, priorities, active_subpics)
            closing_subpics.discard(TEST_SP)

            print(
                f"[TEST-REOPEN] "
                f"request_frame={REOPEN_FRAME} "
                f"actual_frame={fid} "
                f"SP{TEST_SP} "
                f"old_stream={old_stream_id} "
                f"new_stream="
                f"{sp_streams[TEST_SP].stream_id} "
                f"IDR=True"
            )

        # --------------------------------------------------------
        # Current frame enters sender pipeline
        # --------------------------------------------------------
        source_time_ns = time.time_ns()

        objs = build_frame_objects(
            fid,
            split_files,
            sp_streams,
            priorities,
            active_subpics
        )

        for obj in objs:
            obj["source_time_ns"] = source_time_ns

        queue.push_many(objs)
        event.set()

        print(
            f"[SOURCE] frame={fid:3d} "
            f"objects={len(objs)} "
            f"bytes={sum(x['size'] for x in objs):,} "
            f"active={sorted(active_subpics)} "
            f"queue={len(queue)}"
        )

    finished.set()
    event.set()
    t.join()

    print("[Live] Source finished and queue drained")


# ============================================================
# Video
# ============================================================

def process_video(conn, filepath, sender_writer, network_writer,
                  stats_interval, video_id, trace_name,
                  trace_idx, start_time, counters):

    print(f"\n{'=' * 70}")
    print(f"Processing: {filepath}")
    print("=" * 70)

    split_files = vvc_splitter.split(
        filepath,
        Path("split_temp_dir") / video_id
    )

    info = split_files["bitstream_info"]
    num_sp = int(split_files["num_subpics"])

    print(
        f"[Client] VVC={info['width']}x{info['height']} "
        f"FPS={info.get('fps')} "
        f"frames={info['frame_count']} "
        f"SPs={num_sp}"
    )

    for sid, path in enumerate(split_files["subpics"]):
        p = Path(path)

        if not p.exists() or p.stat().st_size == 0:
            raise RuntimeError(f"Invalid SP{sid}: {p}")

        print(
            f"[Client] SP{sid}: {p.stat().st_size:,}B "
            f"SHA256={sha256(p)[:16]}..."
        )

    common, sp_streams, priorities = create_streams(conn, num_sp)

    stop = threading.Event()

    nt = threading.Thread(
        target=stats_thread_fn,
        args=(
            conn, network_writer, stats_interval, stop,
            video_id, trace_name, trace_idx,
            start_time, counters
        ),
        daemon=True
    )

    nt.start()

    try:
        live_streaming(
            conn, split_files,
            common, sp_streams, priorities,
            sender_writer, video_id,
            trace_name, trace_idx,
            start_time, counters
        )

    finally:
        stop.set()
        nt.join()

        print("[Live] Closing streams")

        common.close()

        for sid, s in sp_streams.items():
            print(f"[Live] Close SP{sid} stream={s.stream_id}")
            s.close()


# ============================================================
# Main
# ============================================================

def main():
    p = argparse.ArgumentParser(
        description="VVC Subpicture QUIC Sender"
    )

    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--file", default="")
    p.add_argument(
        "--input_dir",
        default=str(
            ROOT.parent /
            "data/vvc_bitstream/HEVC_CTC_classB/VVC_2x2_50frames/BasketballDrive"
        )
    )
    p.add_argument("--stats_interval", type=float, default=0.1)
    p.add_argument("--trace_name", default="unknown_trace")
    p.add_argument("--loop", type=int, default=1)

    args = p.parse_args()

    files = (
        [args.file]
        if args.file
        else sorted(
            str(f)
            for f in Path(args.input_dir).rglob("*.vvc")
            if "common" not in f.name
            and "subpic_" not in f.name
        )
    )

    if not files:
        raise RuntimeError("No VVC files found")

    start_time, trace_idx = time.time(), 0

    try:
        lines = Path("/tmp/ns3_sync.txt").read_text().splitlines()
        if lines:
            start_time = float(lines[0])
        if len(lines) > 1:
            trace_idx = int(lines[1])
        print(f"[Sync] start={start_time}, trace={trace_idx}")
    except Exception as e:
        print(f"[Sync] Warning: {e}")

    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    sf = open(log_dir / "sender_log.csv", "w", newline="")
    nf = open(log_dir / "network_log.csv", "w", newline="")
    sw, nw = csv.writer(sf), csv.writer(nf)

    sw.writerow([
        "seq_num", "video_id", "trace_name",
        "trace_start_index", "start_time", "match_time",
        "stream_id", "kind", "subpic_id", "frame_id",
        "source_time_ns", "submit_complete_ns",
        "payload_bytes", "wire_bytes", "priority",
        "rtt_us", "cwnd_bytes", "bytes_in_flight", "estimated_bandwidth_bps",
    ])

    nw.writerow([
        "seq_num", "video_id", "trace_name",
        "trace_start_index", "start_time", "match_time",
        "rtt_us", "cwnd_bytes",
        "total_bytes_sent", "stream_bytes_sent",
        "estimated_bandwidth_bps", "bytes_in_flight",
        "posted_bytes", "ideal_bytes"
    ])

    counters = {"network": 0, "sender": 0}

    conn = QuicConnection(
        host=args.host,
        port=args.port,
        scheduling_scheme=0
    )

    conn._client.configure_mtu(1200, 1500)

    try:
        for loop in range(args.loop):
            print(f"\n=== Loop {loop + 1}/{args.loop} ===")

            for filepath in files:
                process_video(
                    conn, filepath, sw, nw,
                    args.stats_interval,
                    Path(filepath).stem,
                    args.trace_name,
                    trace_idx,
                    start_time,
                    counters
                )
                time.sleep(60)

    finally:
        conn.disconnect(force=True)
        sf.close()
        nf.close()

    print("\n=== Streaming completed ===")


if __name__ == "__main__":
    main()