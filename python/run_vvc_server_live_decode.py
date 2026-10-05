#!/usr/bin/env python3

import argparse
import csv
import ctypes
import queue
import struct
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

from playback_buffer import PlaybackBuffer
from vvc_decoder import VVCDecoder

OBJECT_CONFIG = 1
OBJECT_SUBPIC = 2
OBJECT_INIT = 3
INIT_FMT = "<HfI"
INIT_SIZE = struct.calcsize(INIT_FMT)

ROOT = Path(__file__).resolve().parent
LIB_DIR = ROOT / "lib"
DEFAULT_OUTPUT = ROOT / "output_res" / "vvc_receiver" /"BasketballDrive_1920x1080_50"/ "5Mbps"
DEFAULT_FPS = 25.0
PLAYBACK_DELAY_MS = 500.0

ctypes.CDLL(str(LIB_DIR / "libmsquic.so.2"), mode=ctypes.RTLD_GLOBAL)
sys.path.append(str(LIB_DIR))
import quic_media


class VVCReceiver:
    def __init__(self, output_dir, fps, playback_delay_ms):
        self.output_dir = Path(output_dir)
        self.stream_dir = self.output_dir / "streams"
        self.decode_dir = self.output_dir / "decoded_subpics"

        for p in [self.output_dir, self.stream_dir, self.decode_dir]:
            p.mkdir(parents=True, exist_ok=True)

        self.start_ns = None
        self.packet_count = 0
        self.total_bytes = 0
        self.media_bytes = 0
        self.config_bytes = 0

        self.num_subpics = None
        self.stream_fps = None
        self.frame_count = None
        self.session_initialized = False

        self.stream_packets = defaultdict(int)
        self.stream_bytes = defaultdict(int)
        self.files = {}
        self.file_lock = threading.Lock()

        self.known_subpics = set()
        self.active_subpics = set()
        self.stream_to_subpic = {}
        self.subpic_to_stream = {}
        self.closing_streams = set()
        self.closed_streams = set()
        self.subpic_lock = threading.Lock()
        self.lifecycle_lock = threading.Lock()

        self.config_parts = defaultdict(dict)

        self.buffer = PlaybackBuffer(
            fps=fps,
            playback_delay_ms=playback_delay_ms
        )
        self.decoder = VVCDecoder(output_dir=self.decode_dir)

        self.rx_records = {}
        self.timing_lock = threading.Lock()

        self.timing_csv = open(
            self.output_dir / "stream_timing.csv",
            "w",
            newline="",
            buffering=1
        )
        self.timing_writer = csv.writer(self.timing_csv)
        self.timing_writer.writerow([
            "frame_id",
            "subpic_id",
            "stream_id",
            "receive_first_ns",
            "receive_complete_ns",
            "frame_ready_ns",
            "scheduled_play_ns",
            "actual_play_ns",
            "decode_start_ns",
            "decode_end_ns",
            "payload_bytes",
            "fragment_count",
            "expected_subpics",
            "raw_buffer_frames_after_pop",
            "playable_frames_after_pop",
            "playable_buffer_ms_after_pop",
            "received_complete",
            "played",
            "feed_success",
            "decoded_now",
            "decoded_total",
            "error",
        ])

        self.frame_save_queue = queue.Queue()
        self.frame_writer_thread = threading.Thread(
            target=self._frame_writer,
            daemon=True
        )
        self.frame_writer_thread.start()

        self.running = True
        self.playback_thread = threading.Thread(
            target=self._playback_loop,
            daemon=True
        )
        self.playback_thread.start()

    # ========================================================
    # Lifecycle
    # ========================================================

    def _register_subpic(self, subpic_id):
        spid = int(subpic_id)
        with self.subpic_lock:
            if spid not in self.known_subpics:
                self.known_subpics.add(spid)
                print(f"[Receiver] Discovered SP{spid} | known={sorted(self.known_subpics)}")

    def _activate_media_stream(self, stream_id, subpic_id):
        sid = int(stream_id)
        spid = int(subpic_id)

        with self.lifecycle_lock:
            old_spid = self.stream_to_subpic.get(sid)
            if old_spid is not None and old_spid != spid:
                print(f"[WARN] stream={sid} changed SP{old_spid} -> SP{spid}")

            self.stream_to_subpic[sid] = spid
            self.subpic_to_stream[spid] = sid
            self.closing_streams.discard(sid)
            self.closed_streams.discard(sid)

            with self.subpic_lock:
                if spid not in self.active_subpics:
                    self.active_subpics.add(spid)
                    print(
                        f"[Lifecycle] SP{spid} ACTIVE | "
                        f"stream={sid} | active={sorted(self.active_subpics)}"
                    )

    def _get_known_subpics(self):
        with self.subpic_lock:
            return sorted(self.known_subpics)

    def _get_active_subpics(self):
        with self.subpic_lock:
            return sorted(self.active_subpics)

    def on_stream_close(self, stream_id):
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
        with self.lifecycle_lock:
            closing = list(self.closing_streams)

        for sid in closing:
            if self.buffer.pending_stream_count(sid) > 0:
                continue

            with self.lifecycle_lock:
                if sid not in self.closing_streams:
                    continue

                spid = self.stream_to_subpic.get(sid)
                current_sid = self.subpic_to_stream.get(spid) if spid is not None else None
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
                f"[Lifecycle] stream={sid} DRAINED | sp={spid} | "
                f"dropped_incomplete={dropped} | flush_out={len(flushed)} | "
                f"active={self._get_active_subpics()}"
            )

    # ========================================================
    # Timing records
    # ========================================================

    def _update_rx_record(
        self,
        frame_id,
        subpic_id,
        stream_id,
        fragment_idx,
        fragment_count,
        recv_ns,
        size
    ):
        key = (int(frame_id), int(subpic_id), int(stream_id))

        with self.timing_lock:
            rec = self.rx_records.setdefault(key, {
                "frame_id": int(frame_id),
                "subpic_id": int(subpic_id),
                "stream_id": int(stream_id),
                "fragment_count": int(fragment_count),
                "fragments": {},
            })

            rec["fragment_count"] = int(fragment_count)
            rec["fragments"][int(fragment_idx)] = (int(recv_ns), int(size))

    def _take_rx_record(self, frame_id, subpic_id, stream_id):
        key = (int(frame_id), int(subpic_id), int(stream_id))
        with self.timing_lock:
            return self.rx_records.pop(key, None)

    @staticmethod
    def _rx_summary(rec):
        if not rec or not rec["fragments"]:
            return "", "", "", "", 0

        fragments = rec["fragments"]
        times = [x[0] for x in fragments.values()]
        payload_bytes = sum(x[1] for x in fragments.values())
        fragment_count = int(rec["fragment_count"])
        complete = (
            len(fragments) == fragment_count
            and all(i in fragments for i in range(fragment_count))
        )
        complete_ns = max(times) if complete else ""

        return min(times), complete_ns, payload_bytes, fragment_count, int(complete)

    def _write_timing_row(
        self,
        frame_id,
        spid,
        stream_id,
        rec,
        frame_ready_ns="",
        scheduled_play_ns="",
        actual_play_ns="",
        decode_start_ns="",
        decode_end_ns="",
        expected_subpics=None,
        raw_buffer_frames="",
        playable_frames="",
        playable_buffer_ms="",
        played=0,
        feed_success="",
        decoded_now="",
        decoded_total="",
        error=""
    ):
        first_ns, complete_ns, payload_bytes, fragment_count, received_complete = self._rx_summary(rec)

        self.timing_writer.writerow([
            frame_id,
            spid,
            stream_id,
            first_ns,
            complete_ns,
            frame_ready_ns,
            scheduled_play_ns,
            actual_play_ns,
            decode_start_ns,
            decode_end_ns,
            payload_bytes,
            fragment_count,
            ";".join(map(str, sorted(expected_subpics or []))),
            raw_buffer_frames,
            playable_frames,
            f"{playable_buffer_ms:.3f}" if playable_buffer_ms != "" else "",
            received_complete,
            int(played),
            feed_success,
            decoded_now,
            decoded_total,
            error,
        ])

    def _flush_unplayed_records(self):
        with self.timing_lock:
            items = list(self.rx_records.items())
            self.rx_records.clear()

        for (_, _, _), rec in sorted(items):
            self._write_timing_row(
                rec["frame_id"],
                rec["subpic_id"],
                rec["stream_id"],
                rec,
                played=0
            )

    # ========================================================
    # Raw stream + decoded frame output
    # ========================================================

    def _get_stream_file(self, stream_id):
        with self.file_lock:
            if stream_id not in self.files:
                path = self.stream_dir / f"stream_{stream_id}.vvc"
                self.files[stream_id] = open(path, "wb", buffering=0)
                print(f"[Receiver] New stream {stream_id} -> {path}")
            return self.files[stream_id]

    def _queue_decoded_frames(self, decoded_items):
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
                sp_dir = self.decode_dir / f"sp_{spid}"
                sp_dir.mkdir(parents=True, exist_ok=True)
                path = sp_dir / f"frame_{frame_id:06d}.png"
                frame.to_image().save(path, compress_level=1)
            except Exception as e:
                print(
                    f"[FrameSave] SP{spid} frame={frame_id} error: "
                    f"{type(e).__name__}: {e}"
                )
            finally:
                self.frame_save_queue.task_done()

    # ========================================================
    # Config
    # ========================================================

    def _push_config(self, subpic_id, fragment_idx, fragment_count, payload):
        parts = self.config_parts[int(subpic_id)]
        parts[int(fragment_idx)] = bytes(payload)

        if len(parts) != int(fragment_count):
            return

        if not all(i in parts for i in range(int(fragment_count))):
            return

        config = b"".join(parts[i] for i in range(int(fragment_count)))
        self.decoder.set_config(int(subpic_id), config)
        del self.config_parts[int(subpic_id)]

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

        self.packet_count += 1
        self.total_bytes += size
        self.stream_packets[stream_id] += 1
        self.stream_bytes[stream_id] += size

        if object_type == OBJECT_CONFIG:
            self.config_bytes += size
        elif object_type == OBJECT_SUBPIC:
            self.media_bytes += size

        self._get_stream_file(stream_id).write(payload)

        if object_type == OBJECT_INIT:
            if len(payload) != INIT_SIZE:
                print(f"[INIT] ERROR payload size {len(payload)} != {INIT_SIZE}")
                return

            num_subpics, fps, frame_count = struct.unpack(INIT_FMT, payload)
            self.num_subpics = int(num_subpics)
            self.stream_fps = float(fps)
            self.frame_count = int(frame_count)

            if self.num_subpics <= 0 or self.stream_fps <= 0:
                raise RuntimeError(
                    f"Invalid INIT: SPs={self.num_subpics}, FPS={self.stream_fps}"
                )

            with self.subpic_lock:
                self.known_subpics = set(range(self.num_subpics))
                self.active_subpics = set(range(self.num_subpics))

            self.buffer.set_fps(self.stream_fps)
            self.session_initialized = True

            print(
                f"[INIT] SPs={self.num_subpics} | fps={self.stream_fps:.3f} | "
                f"frames={self.frame_count} | startup_frames={self.buffer.startup_frame_count()} | "
                f"active={sorted(self.active_subpics)}"
            )
            return

        if object_type == OBJECT_CONFIG:
            self._register_subpic(subpic_id)
            self._push_config(
                subpic_id,
                fragment_idx,
                fragment_count,
                payload
            )
            return

        if object_type == OBJECT_SUBPIC:
            self._register_subpic(subpic_id)
            self._activate_media_stream(stream_id, subpic_id)

            accepted = self.buffer.push(
                frame_id=frame_id,
                subpic_id=subpic_id,
                stream_id=stream_id,
                fragment_idx=fragment_idx,
                fragment_count=fragment_count,
                payload=payload,
                receive_ns=recv_ns
            )

            if accepted:
                self._update_rx_record(
                    frame_id,
                    subpic_id,
                    stream_id,
                    fragment_idx,
                    fragment_count,
                    recv_ns,
                    size
                )
            else:
                print(
                    f"[BUFFER] DROP frame={frame_id} sp={subpic_id} "
                    f"stream={stream_id} frag={fragment_idx}/{fragment_count}"
                )
            return

        print(
            f"[RX] WARNING unknown object_type={object_type} "
            f"stream={stream_id} frame={frame_id} sp={subpic_id}"
        )

    # ========================================================
    # Playback
    # ========================================================

    def _playback_loop(self):
        while self.running:
            self._try_drain_closed_streams()

            expected_subpics = set(self._get_active_subpics())
            if not expected_subpics:
                time.sleep(0.001)
                continue

            item = self.buffer.pop_next_ready(
                expected_subpics=expected_subpics,
                now_ns=time.time_ns()
            )

            if item is None:
                time.sleep(0.001)
                continue

            frame_id = item["frame_id"]
            subpics = item["subpics"]
            frame_ready_ns = item["frame_ready_ns"]
            scheduled_play_ns = item["scheduled_play_ns"]
            actual_play_ns = item["actual_play_ns"]

            raw_buffer_frames = self.buffer.occupancy()
            playable_frames = self.buffer.playable_frames(expected_subpics)
            playable_buffer_ms = self.buffer.playable_buffer_ms(expected_subpics)

            print(
                f"[PLAYBACK] frame={frame_id} | "
                f"scheduled={scheduled_play_ns} | actual={actual_play_ns} | "
                f"ready={frame_ready_ns} | active={sorted(expected_subpics)} | "
                f"playable={playable_frames} ({playable_buffer_ms:.1f} ms)"
            )

            for spid in sorted(expected_subpics):
                obj = subpics.get(spid)

                if obj is None:
                    print(f"[ERROR] frame={frame_id} SP{spid} missing after frame_ready=True")
                    continue

                decode_start_ns = time.time_ns()
                decode = self.decoder.append_frame(frame_id, spid, obj["data"])
                decode_end_ns = time.time_ns()

                if decode["frames"]:
                    self._queue_decoded_frames(decode["frames"])

                rec = self._take_rx_record(
                    frame_id,
                    spid,
                    obj["stream_id"]
                )

                self._write_timing_row(
                    frame_id=frame_id,
                    spid=spid,
                    stream_id=obj["stream_id"],
                    rec=rec,
                    frame_ready_ns=frame_ready_ns,
                    scheduled_play_ns=scheduled_play_ns,
                    actual_play_ns=actual_play_ns,
                    decode_start_ns=decode_start_ns,
                    decode_end_ns=decode_end_ns,
                    expected_subpics=expected_subpics,
                    raw_buffer_frames=raw_buffer_frames,
                    playable_frames=playable_frames,
                    playable_buffer_ms=playable_buffer_ms,
                    played=1,
                    feed_success=int(decode["success"]),
                    decoded_now=decode["decoded_now"],
                    decoded_total=decode["decoded_total"],
                    error=decode["error"]
                )

                print(
                    f"  SP{spid}: "
                    f"{'FEED-OK' if decode['success'] else 'FAIL'} | "
                    f"recv={obj['receive_ns']} | decode={decode['decode_ms']:.3f} ms | "
                    f"out={decode['decoded_now']} total={decode['decoded_total']}"
                )

            self._try_drain_closed_streams()

    # ========================================================
    # Close / summary
    # ========================================================

    def close(self):
        self.running = False
        self.playback_thread.join(timeout=5)

        print("\n[Decoder] Final flush...")
        final_flushed = self.decoder.flush_all()

        for decoded_items in final_flushed.values():
            if decoded_items:
                self._queue_decoded_frames(decoded_items)

        self.frame_save_queue.join()
        self.frame_save_queue.put(None)
        self.frame_writer_thread.join(timeout=10)

        self._flush_unplayed_records()

        for f in self.files.values():
            f.flush()
            f.close()

        self.timing_csv.flush()
        self.timing_csv.close()

        print("\n" + "=" * 70)
        print("RECEIVER SUMMARY")
        print("=" * 70)
        print(f"Callbacks        : {self.packet_count}")
        print(f"Media bytes      : {self.media_bytes:,}")
        print(f"Config bytes     : {self.config_bytes:,}")
        print(f"Total bytes      : {self.total_bytes:,}")
        print(f"Streams          : {len(self.stream_bytes)}")
        print(f"Known subpics    : {self._get_known_subpics()}")
        print(f"Active subpics   : {self._get_active_subpics()}")
        print(f"Closed streams   : {sorted(self.closed_streams)}")
        print(f"Decoded counts   : {self.decoder.decoded_count}")
        print(f"Timing CSV       : {self.output_dir / 'stream_timing.csv'}")
        print(f"Output           : {self.output_dir}")
        print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="Online VVC-over-QUIC Receiver")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=15433)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--fps", type=float, default=DEFAULT_FPS)
    parser.add_argument("--playback-delay-ms", type=float, default=PLAYBACK_DELAY_MS)
    args = parser.parse_args()

    cert = (ROOT / ".." / "certs" / "server.crt").resolve()
    key = (ROOT / ".." / "certs" / "server.key").resolve()

    if not cert.exists() or not key.exists():
        raise FileNotFoundError(f"Certificate not found:\n  cert={cert}\n  key ={key}")

    receiver = VVCReceiver(
        args.output,
        fps=args.fps,
        playback_delay_ms=args.playback_delay_ms
    )

    server = quic_media.QuicMediaServer()
    server.configure_mtu(1200, 1500)
    server.set_recv_callback(receiver.on_receive)
    server.set_stream_close_callback(receiver.on_stream_close)

    print("=" * 70)
    print("Online VVC-over-QUIC Receiver")
    print("=" * 70)
    print(f"Host            : {args.host}")
    print(f"Port            : {args.port}")
    print(f"FPS             : {args.fps}")
    print(f"Playback buffer : {args.playback_delay_ms} ms media")
    print(f"Output          : {args.output}")
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
        raise RuntimeError("Failed to start QUIC server")

    try:
        print("[Server] Running. Press Ctrl+C after sender/playback finishes.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[Server] Stopping...")
    finally:
        server.set_recv_callback(None)
        server.set_stream_close_callback(None)
        server.stop_server()
        receiver.close()
        print("[Server] Done.")


if __name__ == "__main__":
    main()
