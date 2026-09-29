#!/usr/bin/env python3
import sys, csv, time, queue, ctypes, hashlib, argparse, threading, struct
from pathlib import Path
from collections import defaultdict

from playback_buffer import PlaybackBuffer
from vvc_decoder import VVCDecoder

import struct

OBJECT_CONFIG = 1
OBJECT_SUBPIC = 2
OBJECT_INIT = 3

INIT_FMT = "<HfI"
INIT_SIZE = struct.calcsize(INIT_FMT)

# ============================================================
# Configuration
# ============================================================

ROOT = Path(__file__).resolve().parent
LIB_DIR = ROOT / "lib"
DEFAULT_OUTPUT = ROOT / "output_res" / "vvc_receiver_online"

DEFAULT_FPS = 25.0
PLAYBACK_DELAY_MS = 500

ctypes.CDLL(str(LIB_DIR / "libmsquic.so.2"), mode=ctypes.RTLD_GLOBAL)
sys.path.append(str(LIB_DIR))
import quic_media


# ============================================================
# Helpers
# ============================================================

def sha256(path, chunk_size=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def human_bytes(n):
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"



# ============================================================
# Receiver
# ============================================================

class VVCReceiver:
    def __init__(self, output_dir, fps, playback_delay_ms):
        self.output_dir = Path(output_dir)
        self.stream_dir = self.output_dir / "streams"
        self.object_dir = self.output_dir / "objects"
        self.decode_dir = self.output_dir / "decoded_subpics"

        for p in [self.output_dir, self.stream_dir,
                  self.object_dir, self.decode_dir]:
            p.mkdir(parents=True, exist_ok=True)

        self.start_ns = None
        self.packet_count = 0
        self.object_count = 0
        self.total_bytes = 0

        self.media_bytes = 0
        self.config_bytes = 0

        self.num_subpics = None
        self.stream_fps = None
        self.frame_count = None
        self.session_initialized = False

        # Stall statistics
        self.stall_start_ns = None
        self.stall_frame_id = None
        self.stall_expected_subpics = None

        self.stall_count = 0
        self.total_stall_ns = 0
        self.max_stall_ns = 0

        self.stream_packets = defaultdict(int)
        self.stream_bytes = defaultdict(int)
        self.files = {}
        self.file_lock = threading.Lock()

        # ----------------------------------------------------
        # Subpicture / stream lifecycle
        # ----------------------------------------------------

        self.known_subpics = set()
        self.active_subpics = set()

        # self.known_subpics = set(range(4))
        # self.active_subpics = set(range(4))

        # QUIC stream_id -> SP
        self.stream_to_subpic = {}

        # SP -> current QUIC stream_id
        self.subpic_to_stream = {}

        # QUIC streams that received PEER_SEND_SHUTDOWN
        self.closing_streams = set()
        self.closed_streams = set()

        self.subpic_lock = threading.Lock()
        self.lifecycle_lock = threading.Lock()

        # Config assembly
        self.config_parts = defaultdict(dict)

        self.stall_csv = open(
            self.output_dir / "stall_events.csv",
            "w", newline="", buffering=1
        )

        self.stall_writer = csv.writer(self.stall_csv)
        self.stall_writer.writerow([
            "stall_id",
            "frame_id",
            "start_ns",
            "end_ns",
            "duration_ms",
            "expected_subpics",
            "buffer_occupancy",
        ])

        # Playback
        self.buffer = PlaybackBuffer(
            fps=fps
        )

        self.decoder = VVCDecoder(
            output_dir=self.decode_dir
        )

        # ----------------------------------------------------
        # Decoded frame saver
        # ----------------------------------------------------

        self.frame_save_queue = queue.Queue()

        self.frame_writer_thread = threading.Thread(
            target=self._frame_writer,
            daemon=True
        )

        self.frame_writer_thread.start()

        # ----------------------------------------------------
        # Object log
        # ----------------------------------------------------

        self.object_csv = open(
            self.output_dir / "received_objects.csv",
            "w", newline="", buffering=1
        )

        self.object_writer = csv.writer(self.object_csv)
        self.object_writer.writerow([
            "object_seq", "receive_complete_ns", "elapsed_ms",
            "stream_id", "frame_id", "subpic_id", "object_type",
            "payload_len", "fragment_idx", "fragment_count", "file"
        ])

        # ----------------------------------------------------
        # Playback metrics
        # ----------------------------------------------------

        self.metrics_csv = open(
            self.output_dir / "playback_metrics.csv",
            "w", newline="", buffering=1
        )

        self.metrics_writer = csv.writer(self.metrics_csv)
        self.metrics_writer.writerow([
            "frame_id", "subpic_id", "stream_id",
            "receive_time_ns", "playback_time_ns", "decode_start_ns", "decode_end_ns", "playback_deadline_ns",
            "buffer_wait_ms", "decode_ms", "payload_bytes",
            "buffer_occupancy", "received", "feed_success",
            "decoded_now", "decoded_total",
            "late", "missing", "error"
        ])

        # ----------------------------------------------------
        # Raw receiver log
        # ----------------------------------------------------

        self.log_queue = queue.Queue()

        self.csv_file = open(
            self.output_dir / "receiver_log.csv",
            "w", newline="", buffering=1
        )

        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow([
            "packet_seq", "receive_time_ns", "elapsed_ms",
            "stream_id", "callback_bytes",
            "stream_packet_seq", "stream_total_bytes"
        ])

        self.running = True

        self.writer_thread = threading.Thread(
            target=self._log_writer,
            daemon=True
        )
        self.writer_thread.start()

        self.playback_thread = threading.Thread(
            target=self._playback_loop,
            daemon=True
        )
        self.playback_thread.start()

    # ========================================================
    # Subpicture lifecycle
    # ========================================================

    def _register_subpic(self, subpic_id):
        spid = int(subpic_id)

        with self.subpic_lock:
            if spid not in self.known_subpics:
                self.known_subpics.add(spid)
                print(
                    f"[Receiver] Discovered SP{spid} | "
                    f"known={sorted(self.known_subpics)}"
                )

    def _activate_media_stream(self, stream_id, subpic_id):
        sid = int(stream_id)
        spid = int(subpic_id)

        with self.lifecycle_lock:
            old_spid = self.stream_to_subpic.get(sid)

            if old_spid is not None and old_spid != spid:
                print(
                    f"[WARN] stream={sid} changed "
                    f"SP{old_spid} -> SP{spid}"
                )

            self.stream_to_subpic[sid] = spid
            self.subpic_to_stream[spid] = sid

            self.closing_streams.discard(sid)
            self.closed_streams.discard(sid)

            with self.subpic_lock:
                if spid not in self.active_subpics:
                    self.active_subpics.add(spid)

                    print(
                        f"[Lifecycle] SP{spid} ACTIVE | "
                        f"stream={sid} | "
                        f"active={sorted(self.active_subpics)}"
                    )

    def _get_known_subpics(self):
        with self.subpic_lock:
            return sorted(self.known_subpics)

    def _get_active_subpics(self):
        with self.subpic_lock:
            return sorted(self.active_subpics)

    # ========================================================
    # Stream close callback
    # ========================================================

    def on_stream_close(self, stream_id):
        """
        Called by QUIC PEER_SEND_SHUTDOWN.

        Important:
        only MARK the stream closing here.

        Do NOT flush PyAV from this callback because complete
        AUs may still be waiting inside PlaybackBuffer.
        """

        sid = int(stream_id)

        with self.lifecycle_lock:
            if sid in self.closed_streams:
                return

            self.closing_streams.add(sid)
            spid = self.stream_to_subpic.get(sid)

        print(
            f"[Lifecycle] QUIC stream={sid} INPUT CLOSED | "
            f"sp={spid} | waiting for PlaybackBuffer drain"
        )

    def _try_drain_closed_streams(self):
        """
        Called ONLY by playback thread.

        Once a closed QUIC stream has no complete AU remaining
        in PlaybackBuffer:

            drop incomplete remainder
            -> drain PyAV DPB
            -> deactivate SP
            -> retire QUIC stream lifecycle
        """

        with self.lifecycle_lock:
            closing = list(self.closing_streams)

        for sid in closing:

            pending = self.buffer.pending_stream_count(sid)

            if pending > 0:
                continue

            with self.lifecycle_lock:

                if sid not in self.closing_streams:
                    continue

                spid = self.stream_to_subpic.get(sid)

                # If the same SP has already moved to a NEW stream,
                # this old stream must not deactivate/flush the new
                # active lifecycle.
                current_sid = (
                    self.subpic_to_stream.get(spid)
                    if spid is not None
                    else None
                )

                dropped = self.buffer.drop_incomplete_stream(sid)

                flushed = []

                if spid is not None and current_sid == sid:

                    flushed = self.decoder.flush_subpic(spid)

                    if flushed:
                        self._queue_decoded_frames(flushed)

                    with self.subpic_lock:
                        self.active_subpics.discard(spid)

                    self.subpic_to_stream.pop(spid, None)

                self.closing_streams.discard(sid)
                self.closed_streams.add(sid)
                self.stream_to_subpic.pop(sid, None)

            print(
                f"[Lifecycle] stream={sid} DRAINED | "
                f"sp={spid} | "
                f"dropped_incomplete={dropped} | "
                f"flush_out={len(flushed)} | "
                f"active={self._get_active_subpics()}"
            )

    # ========================================================
    # Logging
    # ========================================================

    def _log_writer(self):
        while True:
            row = self.log_queue.get()

            if row is None:
                self.log_queue.task_done()
                break

            self.csv_writer.writerow(row)
            self.log_queue.task_done()

    def _get_stream_file(self, stream_id):
        with self.file_lock:
            if stream_id not in self.files:
                path = self.stream_dir / f"stream_{stream_id}.vvc"
                self.files[stream_id] = open(path, "ab", buffering=0)

                print(
                    f"[Receiver] New stream {stream_id} -> {path}"
                )

            return self.files[stream_id]

    def _queue_decoded_frames(self, decoded_items):
        """
        Queue decoded PyAV VideoFrames for lossless PNG saving.

        decoded_items:
            [
                {
                    "frame_id": ...,
                    "subpic_id": ...,
                    "frame": av.VideoFrame
                },
                ...
            ]
        """

        for item in decoded_items:
            self.frame_save_queue.put((
                int(item["frame_id"]),
                int(item["subpic_id"]),
                item["frame"]
            ))


    def _frame_writer(self):

        while True:

            item = self.frame_save_queue.get()

            if item is None:
                self.frame_save_queue.task_done()
                break

            frame_id, spid, frame = item

            try:

                sp_dir = (
                    self.decode_dir /
                    f"sp_{spid}"
                )

                sp_dir.mkdir(
                    parents=True,
                    exist_ok=True
                )

                path = (
                    sp_dir /
                    f"frame_{frame_id:06d}.png"
                )

                # Lossless PNG.
                # Low compression keeps online CPU overhead small.
                frame.to_image().save(
                    path,
                    compress_level=1
                )

            except Exception as e:

                print(
                    f"[FrameSave] SP{spid} "
                    f"frame={frame_id} error: "
                    f"{type(e).__name__}: {e}"
                )

            finally:
                self.frame_save_queue.task_done()

    # ========================================================
    # Config
    # ========================================================

    def _push_config(
        self,
        subpic_id,
        fragment_idx,
        fragment_count,
        payload
    ):
        parts = self.config_parts[subpic_id]
        parts[fragment_idx] = bytes(payload)

        if len(parts) != fragment_count:
            return

        if not all(i in parts for i in range(fragment_count)):
            return

        config = b"".join(
            parts[i]
            for i in range(fragment_count)
        )

        self.decoder.set_config(
            subpic_id,
            config
        )

        del self.config_parts[subpic_id]

    # ========================================================
    # QUIC receive callback
    # ========================================================


    def on_receive(
        self,
        stream_id,
        frame_id,
        subpic_id,
        object_type,
        fragment_idx,
        fragment_count,
        data
    ):
        recv_ns = time.time_ns()

        if self.start_ns is None:
            self.start_ns = recv_ns

        stream_id = int(stream_id)
        frame_id = int(frame_id)
        subpic_id = int(subpic_id)
        object_type = int(object_type)
        fragment_idx = int(fragment_idx)
        fragment_count = int(fragment_count)

        payload = bytes(data)
        size = len(payload)

        # ----------------------------------------------------
        # Statistics
        # ----------------------------------------------------

        self.packet_count += 1
        self.object_count += 1
        self.total_bytes += size

        if object_type == OBJECT_CONFIG:
            self.config_bytes += size

        elif object_type == OBJECT_SUBPIC:
            self.media_bytes += size

        self.stream_packets[stream_id] += 1
        self.stream_bytes[stream_id] += size

        # ----------------------------------------------------
        # Object type
        # ----------------------------------------------------

        kind = {
            OBJECT_CONFIG: "config",
            OBJECT_SUBPIC: "subpic",
            OBJECT_INIT: "init",
        }.get(
            object_type,
            f"type{object_type}"
        )

        # ----------------------------------------------------
        # Save raw stream
        # ----------------------------------------------------

        self._get_stream_file(stream_id).write(payload)

        # ----------------------------------------------------
        # Save object
        # ----------------------------------------------------

        object_seq = self.object_count

        stream_obj_dir = (
            self.object_dir /
            f"stream_{stream_id}"
        )

        stream_obj_dir.mkdir(
            parents=True,
            exist_ok=True
        )

        obj_path = stream_obj_dir / (
            f"obj_{object_seq:06d}_"
            f"{kind}_sp{subpic_id}_"
            f"frame_{frame_id:06d}_"
            f"frag_{fragment_idx:04d}.vvc"
        )

        obj_path.write_bytes(payload)

        elapsed_ms = (
            recv_ns - self.start_ns
        ) / 1e6

        self.object_writer.writerow([
            object_seq,
            recv_ns,
            f"{elapsed_ms:.3f}",
            stream_id,
            frame_id,
            subpic_id,
            object_type,
            size,
            fragment_idx,
            fragment_count,
            str(obj_path)
        ])

        self.log_queue.put([
            self.packet_count,
            recv_ns,
            f"{elapsed_ms:.3f}",
            stream_id,
            size,
            self.stream_packets[stream_id],
            self.stream_bytes[stream_id]
        ])

        # ====================================================
        # INIT
        # ====================================================

        if object_type == OBJECT_INIT:

            if len(payload) != INIT_SIZE:
                print(
                    f"[INIT] ERROR invalid payload size "
                    f"{len(payload)} != {INIT_SIZE}"
                )
                return

            num_subpics, fps, frame_count = struct.unpack(
                INIT_FMT,
                payload
            )

            self.num_subpics = int(num_subpics)
            self.stream_fps = float(fps)
            self.frame_count = int(frame_count)

            if self.num_subpics <= 0:
                print(
                    f"[INIT] ERROR invalid "
                    f"num_subpics={self.num_subpics}"
                )
                return

            if self.stream_fps <= 0:
                print(
                    f"[INIT] ERROR invalid "
                    f"fps={self.stream_fps}"
                )
                return

            # Sender tells receiver exactly which SPs exist.
            with self.subpic_lock:

                self.known_subpics = set(
                    range(self.num_subpics)
                )

                # Initial interval:
                # all SPs participate in playback.
                self.active_subpics = set(
                    range(self.num_subpics)
                )

            # Use sender-provided FPS for playback timing.
            self.buffer.set_fps(
                self.stream_fps
            )

            self.session_initialized = True

            print(
                f"[INIT] "
                f"SPs={self.num_subpics} | "
                f"fps={self.stream_fps:.3f} | "
                f"frames={self.frame_count} | "
                f"known={sorted(self.known_subpics)} | "
                f"active={sorted(self.active_subpics)}"
            )

        # ====================================================
        # CONFIG: VPS / SPS / PPS
        # ====================================================

        elif object_type == OBJECT_CONFIG:

            # Only real SP IDs are registered.
            self._register_subpic(
                subpic_id
            )

            self._push_config(
                subpic_id,
                fragment_idx,
                fragment_count,
                payload
            )

        # ====================================================
        # MEDIA: PH + VCL
        # ====================================================

        elif object_type == OBJECT_SUBPIC:

            # Only real SP IDs are registered.
            self._register_subpic(
                subpic_id
            )

            # Associate this QUIC stream with this SP.
            self._activate_media_stream(
                stream_id,
                subpic_id
            )

            accepted = self.buffer.push(
                frame_id=frame_id,
                subpic_id=subpic_id,
                stream_id=stream_id,
                fragment_idx=fragment_idx,
                fragment_count=fragment_count,
                payload=payload,
                receive_ns=recv_ns
            )

            if not accepted:
                print(
                    f"[BUFFER] DROP "
                    f"frame={frame_id} "
                    f"sp={subpic_id} "
                    f"stream={stream_id}"
                )

        # ====================================================
        # Unknown object
        # ====================================================

        else:

            print(
                f"[RX] WARNING unknown object_type="
                f"{object_type} "
                f"stream={stream_id} "
                f"frame={frame_id} "
                f"sp={subpic_id}"
            )

        # ----------------------------------------------------
        # Debug
        # ----------------------------------------------------

        if (
            self.packet_count <= 20
            or self.packet_count % 1000 == 0
        ):
            print(
                f"[RX] seq={self.packet_count} "
                f"stream={stream_id} "
                f"frame={frame_id} "
                f"sp={subpic_id} "
                f"type={object_type}({kind}) "
                f"frag={fragment_idx}/{fragment_count} "
                f"bytes={size} "
                f"elapsed={elapsed_ms:.2f} ms"
            )

    # ========================================================
    # Playback
    # ========================================================

    def _playback_loop(self):
        while self.running:

            # ----------------------------------------------------
            # Retire closed streams whose remaining complete AUs
            # have already been consumed.
            # ----------------------------------------------------
            self._try_drain_closed_streams()

            expected_subpics = set(
                self._get_active_subpics()
            )

            if not expected_subpics:
                time.sleep(0.001)
                continue

            now_ns = time.time_ns()

            item = self.buffer.pop_next_ready(
                expected_subpics=expected_subpics,
                now_ns=now_ns
            )

            # ====================================================
            # No frame available for playback
            # ====================================================
            if item is None:

                # Before playback starts:
                # simply buffer until frame 0 has all active SPs.
                # This is startup buffering, NOT stall.
                if not self.buffer.started:
                    time.sleep(0.001)
                    continue

                # ------------------------------------------------
                # Playback has started.
                #
                # Only call it STALL when:
                #
                #   playback time has arrived
                #       AND
                #   current frame is still incomplete.
                # ------------------------------------------------
                if self.buffer.playback_due(
                    now_ns=now_ns
                ):

                    next_fid = self.buffer.next_frame

                    if not self.buffer.frame_ready(
                        next_fid,
                        expected_subpics
                    ):

                        if self.stall_start_ns is None:

                            # Stall begins at the scheduled playback
                            # time, not at polling time.
                            self.stall_start_ns = (
                                self.buffer.next_play_ns
                            )

                            self.stall_frame_id = next_fid

                            self.stall_expected_subpics = sorted(
                                expected_subpics
                            )

                            missing = (
                                self.buffer.missing_subpics(
                                    next_fid,
                                    expected_subpics
                                )
                            )

                            print(
                                f"[STALL] START "
                                f"frame={next_fid} "
                                f"expected="
                                f"{self.stall_expected_subpics} "
                                f"missing={missing} "
                                f"buffer="
                                f"{self.buffer.occupancy()}"
                            )

                time.sleep(0.001)
                continue

            # ====================================================
            # Frame ready
            # ====================================================

            frame_id, subpics = item

            playback_time_ns = time.time_ns()

            # ====================================================
            # End stall
            # ====================================================

            if self.stall_start_ns is not None:

                stall_end_ns = playback_time_ns

                stall_ns = (
                    stall_end_ns
                    - self.stall_start_ns
                )

                self.stall_count += 1

                self.total_stall_ns += (
                    stall_ns
                )

                self.max_stall_ns = max(
                    self.max_stall_ns,
                    stall_ns
                )

                self.stall_writer.writerow([
                    self.stall_count,
                    self.stall_frame_id,
                    self.stall_start_ns,
                    stall_end_ns,
                    f"{stall_ns / 1e6:.3f}",
                    ",".join(
                        map(
                            str,
                            self.stall_expected_subpics
                            or []
                        )
                    ),
                    self.buffer.occupancy(),
                ])

                print(
                    f"[STALL] END "
                    f"frame={self.stall_frame_id} "
                    f"duration="
                    f"{stall_ns / 1e6:.3f} ms"
                )

                self.stall_start_ns = None
                self.stall_frame_id = None
                self.stall_expected_subpics = None

            # ====================================================
            # Playback
            # ====================================================

            occupancy = (
                self.buffer.occupancy()
            )

            print(
                f"[PLAYBACK] "
                f"frame={frame_id} "
                f"received={sorted(subpics)} "
                f"expected="
                f"{sorted(expected_subpics)} "
                f"buffer={occupancy}"
            )

            # ====================================================
            # Feed all active SPs to decoder
            # ====================================================

            for spid in sorted(
                expected_subpics
            ):

                obj = subpics.get(spid)

                # ------------------------------------------------
                # This should NOT normally happen anymore.
                #
                # PlaybackBuffer only releases a frame when every
                # active SP is complete.
                # ------------------------------------------------
                if obj is None:

                    print(
                        f"[ERROR] "
                        f"frame={frame_id} "
                        f"SP{spid} missing after "
                        f"frame_ready=True"
                    )

                    continue

                # ------------------------------------------------
                # Decode
                # ------------------------------------------------

                decode_start_ns = (
                    time.time_ns()
                )

                decode = (
                    self.decoder.append_frame(
                        frame_id,
                        spid,
                        obj["data"]
                    )
                )

                decode_end_ns = (
                    time.time_ns()
                )

                # ------------------------------------------------
                # Save decoder outputs asynchronously
                # ------------------------------------------------

                if decode["frames"]:
                    self._queue_decoded_frames(
                        decode["frames"]
                    )

                # ------------------------------------------------
                # Time spent waiting inside PlaybackBuffer
                # ------------------------------------------------

                buffer_wait_ms = (
                    playback_time_ns
                    - obj["receive_ns"]
                ) / 1e6

                # ------------------------------------------------
                # Metrics
                #
                # Keep the existing CSV column count for now.
                #
                # deadline_ns -> ""
                # late        -> 0
                # missing     -> 0
                #
                # We can clean the CSV schema later.
                # ------------------------------------------------

                self.metrics_writer.writerow([
                    frame_id,
                    spid,
                    obj["stream_id"],

                    obj["receive_ns"],
                    playback_time_ns,
                    decode_start_ns,
                    decode_end_ns,

                    "",  # old deadline_ns

                    f"{buffer_wait_ms:.3f}",
                    f"{decode['decode_ms']:.3f}",

                    obj["bytes"],
                    occupancy,

                    1,  # received
                    int(decode["success"]),
                    decode["decoded_now"],
                    decode["decoded_total"],

                    0,  # old late
                    0,  # missing

                    decode["error"],
                ])

                print(
                    f"  SP{spid}: "
                    f"{'FEED-OK' if decode['success'] else 'FAIL'} "
                    f"out={decode['decoded_now']} "
                    f"total={decode['decoded_total']} "
                    f"wait={buffer_wait_ms:.3f} ms "
                    f"decode={decode['decode_ms']:.3f} ms"
                )

            # ----------------------------------------------------
            # pop_frame() happened before append_frame().
            #
            # Therefore stream drain must be checked only AFTER
            # all AUs from this frame have reached PyAV.
            # ----------------------------------------------------

            self._try_drain_closed_streams()

    # ========================================================
    # Close
    # ========================================================

    def close(self):
        self.running = False

        self.playback_thread.join(
            timeout=5
        )

        # Program-level final fallback.
        # Lifecycle-closed codecs were already removed by
        # flush_subpic(), so flush_all() only drains remaining
        # active decoders.
        print("\n[Decoder] Final flush...")

        final_flushed = self.decoder.flush_all()

        for spid, decoded_items in final_flushed.items():
        
            if decoded_items:
                self._queue_decoded_frames(
                    decoded_items
                )

        # Wait until all decoded frames are saved.
        self.frame_save_queue.join()

        self.frame_save_queue.put(None)

        self.frame_writer_thread.join(timeout=10)

        self.log_queue.join()
        self.log_queue.put(None)

        self.writer_thread.join(
            timeout=2
        )

        for f in self.files.values():
            f.flush()
            f.close()

        self.csv_file.close()
        self.object_csv.close()
        self.metrics_csv.close()
        self.stall_csv.close()

        self._write_summary()

    # ========================================================
    # Summary
    # ========================================================

    def _write_summary(self):
        summary = (
            self.output_dir /
            "receiver_summary.csv"
        )

        with open(summary, "w", newline="") as f:
            w = csv.writer(f)

            w.writerow([
                "stream_id",
                "callbacks",
                "bytes",
                "size",
                "sha256",
                "file"
            ])

            for sid in sorted(self.stream_bytes):

                path = (
                    self.stream_dir /
                    f"stream_{sid}.vvc"
                )

                w.writerow([
                    sid,
                    self.stream_packets[sid],
                    self.stream_bytes[sid],
                    human_bytes(
                        self.stream_bytes[sid]
                    ),
                    sha256(path),
                    str(path)
                ])

        print("\n" + "=" * 70)
        print("RECEIVER SUMMARY")
        print("=" * 70)

        print(
            f"Callbacks       : {self.packet_count}"
        )

        print(
            f"Media bytes     : {self.media_bytes:,}"
        )

        print(
            f"Config bytes    : {self.config_bytes:,}"
        )

        print(
            f"Stall count     : {self.stall_count}"
        )

        print(
            f"Total stall     : "
            f"{self.total_stall_ns / 1e6:.3f} ms"
        )

        print(
            f"Max stall       : "
            f"{self.max_stall_ns / 1e6:.3f} ms"
        )

        print(
            f"Total bytes     : {self.total_bytes:,}"
        )

        print(
            f"Streams         : {len(self.stream_bytes)}"
        )

        print(
            f"Known subpics   : {self._get_known_subpics()}"
        )

        print(
            f"Active subpics  : {self._get_active_subpics()}"
        )

        print(
            f"Closed streams  : {sorted(self.closed_streams)}"
        )

        print(
            f"Decoded counts  : {self.decoder.decoded_count}"
        )

        print(
            f"Output          : {self.output_dir}"
        )

        print("=" * 70)


# ============================================================
# Main
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description="Online VVC-over-QUIC Receiver"
    )

    parser.add_argument(
        "--host",
        default="0.0.0.0"
    )

    parser.add_argument(
        "--port",
        type=int,
        default=15433
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT
    )

    parser.add_argument(
        "--fps",
        type=float,
        default=DEFAULT_FPS
    )

    parser.add_argument(
        "--playback-delay-ms",
        type=float,
        default=PLAYBACK_DELAY_MS
    )

    args = parser.parse_args()

    cert = (
        ROOT /
        ".." /
        "certs" /
        "server.crt"
    ).resolve()

    key = (
        ROOT /
        ".." /
        "certs" /
        "server.key"
    ).resolve()

    if not cert.exists() or not key.exists():
        raise FileNotFoundError(
            f"Certificate not found:\n"
            f"  cert={cert}\n"
            f"  key ={key}"
        )

    receiver = VVCReceiver(
        args.output,
        fps=args.fps,
        playback_delay_ms=args.playback_delay_ms
    )

    server = quic_media.QuicMediaServer()
    server.configure_mtu(1200, 1500)

    # Register callbacks BEFORE listening.
    server.set_recv_callback(
        receiver.on_receive
    )

    server.set_stream_close_callback(
        receiver.on_stream_close
    )

    print("=" * 70)
    print("Online VVC-over-QUIC Receiver")
    print("=" * 70)

    print(f"Host           : {args.host}")
    print(f"Port           : {args.port}")
    print(f"FPS            : {args.fps}")
    print(
        f"Playback delay : "
        f"{args.playback_delay_ms} ms"
    )
    print(f"Output         : {args.output}")

    print("=" * 70)

    if not server.start_server(
        args.host,
        args.port,
        str(cert),
        str(key)
    ):
        server.set_recv_callback(None)
        server.set_stream_close_callback(None)
        receiver.close()

        raise RuntimeError(
            "Failed to start QUIC server."
        )

    try:

        print(
            "[Server] Running. "
            "Press Ctrl+C after sender finishes."
        )

        while True:
            time.sleep(1)

    except KeyboardInterrupt:

        print(
            "\n[Server] Stopping..."
        )

    finally:

        # Remove Python callbacks first so MsQuic cannot call
        # into receiver while it is being destroyed.
        server.set_recv_callback(None)
        server.set_stream_close_callback(None)

        server.stop_server()

        receiver.close()

        print(
            "[Server] Done."
        )


if __name__ == "__main__":
    main()