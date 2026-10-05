#!/usr/bin/env python3
import cv2
import sys
import csv
import time
import ctypes
import argparse
import threading
import numpy as np

from pathlib import Path
from queue import Queue, Empty


# ============================================================
# Local Libraries
# ============================================================

ROOT=Path(__file__).resolve().parent
LIB_DIR=ROOT/"lib"

ctypes.CDLL(str(LIB_DIR/"libmsquic.so.2"),mode=ctypes.RTLD_GLOBAL)
sys.path.append(str(LIB_DIR))

from quic_connection import QuicConnection
import vvc_splitter

from live_streaming_client_util import (
    sha256,
    ReadyQueue,
    build_init_object,
    build_config_objects,
    build_frame_objects,
    find_idr_frames,
    find_next_idr,
    get_network_state,
    stats_thread_fn,
    create_streams,
    apply_priorities,
    deactivate_subpic,
    activate_subpic,
    YUVReader,
    FrameReader
)

# from streaming_controller import VideoFeatureExtractor, StreamingController
from streaming_controller import MaskFeatureExtractor, StreamingController

# ============================================================
# Configuration
# ============================================================

CONTROL_INTERVAL_S=1.0
INITIAL_WARMUP_S=1.0

DEFAULT_FPS=30.0

TEST_SP=0
CLOSE_FRAME=20
REOPEN_FRAME=30

GATE_RETRY_S=0.001


# ============================================================
# Sender Timing
# ============================================================

def log_sender(writer,counters,obj,video_id,trace_name,trace_idx,start_time,state):
    counters["sender"]+=1

    writer.writerow([
        counters["sender"],
        video_id,
        trace_name,
        trace_idx,
        f"{start_time:.6f}",
        f"{time.time()-start_time:.6f}",
        obj["stream_id"],
        obj["kind"],
        obj["subpic_id"],
        obj["frame_id"],
        obj.get("source_time_ns",0),
        obj.get("submit_complete_ns",0),
        obj["size"],
        obj["wire_size"],
        obj["priority"],
        state["rtt_us"],
        state["cwnd_bytes"],
        state["bytes_in_flight"],
        state["estimated_bandwidth_bps"]
    ])


def sender_gate_thread_timed(
    conn,queue,event,finished,writer,
    video_id,trace_name,trace_idx,start_time,counters,
    subpic_send_time_ns,timing_lock
):
    active=None

    while not finished.is_set() or not queue.empty() or active is not None:

        if active is None:
            active=queue.pop()

            if active is None:
                event.wait(GATE_RETRY_S)
                event.clear()
                continue

        try:
            state=get_network_state(conn)
            budget=max(0,state["cwnd_bytes"]-state["bytes_in_flight"])
        except Exception as e:
            print(f"[GATE] stats error: {e}")
            time.sleep(GATE_RETRY_S)
            continue

        if budget<=0:
            time.sleep(GATE_RETRY_S)
            continue

        offset=active["offset"]
        remaining=active["wire_size"]-offset
        send_n=min(remaining,budget)

        if send_n<=0:
            active=None
            continue

        chunk=active["wire_data"][offset:offset+send_n]
        send_attempt_ns=time.time_ns()

        if not active["stream"].send_chunk(chunk):
            print(
                f"[GATE] FAIL frame={active['frame_id']} "
                f"kind={active['kind']} sp={active['subpic_id']} "
                f"offset={offset} n={send_n}"
            )
            time.sleep(GATE_RETRY_S)
            continue

        # First successful MsQuic submission time for this frame/SP.
        if active["kind"]=="subpic":
            fid=int(active["frame_id"])
            sid=int(active["subpic_id"])

            if 0<=fid<subpic_send_time_ns.shape[0] and 0<=sid<subpic_send_time_ns.shape[1]:
                with timing_lock:
                    if subpic_send_time_ns[fid,sid]<0:
                        subpic_send_time_ns[fid,sid]=send_attempt_ns

        active["offset"]+=send_n
        remaining=active["wire_size"]-active["offset"]

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

        if remaining==0:
            active["submit_complete_ns"]=time.time_ns()

            log_sender(
                writer,counters,active,
                video_id,trace_name,trace_idx,start_time,state
            )

            print(
                f"[GATE] COMPLETE frame={active['frame_id']:3d} "
                f"kind={active['kind']} "
                f"sp={active['subpic_id']} "
                f"payload={active['size']}B"
            )

            active=None

        time.sleep(0.001)


# ============================================================
# NPZ Timing Extension
# ============================================================

def save_controller_with_timing(
    controller,
    output_path,
    frame_source_time_ns,
    subpic_send_time_ns,
    idr_frames,
    nframes
):
    controller.save(output_path)

    with np.load(output_path, allow_pickle=True) as data:
        saved = {key: data[key] for key in data.files}

    # --------------------------------------------------------
    # Timing
    # --------------------------------------------------------

    saved["frame_source_time_ns"] = np.asarray(
        frame_source_time_ns,
        dtype=np.int64
    )

    saved["subpic_send_time_ns"] = np.asarray(
        subpic_send_time_ns,
        dtype=np.int64
    )

    # --------------------------------------------------------
    # IDR metadata
    # --------------------------------------------------------

    idr_frames = np.asarray(
        sorted(set(int(x) for x in idr_frames)),
        dtype=np.int64
    )

    saved["idr_frames"] = idr_frames

    is_idr = np.zeros(
        nframes,
        dtype=np.int8
    )

    valid_idr = idr_frames[
        (idr_frames >= 0) &
        (idr_frames < nframes)
    ]

    is_idr[valid_idr] = 1

    saved["is_idr"] = is_idr

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    np.savez(
        output_path,
        **saved
    )

    print(
        f"[TIMING] frame_source_time_ns "
        f"shape={saved['frame_source_time_ns'].shape}"
    )

    print(
        f"[TIMING] subpic_send_time_ns "
        f"shape={saved['subpic_send_time_ns'].shape}"
    )

    print(
        f"[IDR] frames={saved['idr_frames'].tolist()}"
    )

    print(
        f"[IDR] is_idr shape={saved['is_idr'].shape}"
    )


# ============================================================
# Live Streaming
# ============================================================

def live_streaming(
    conn,split_files,common_stream,sp_streams,priorities,
    writer,video_id,trace_name,trace_idx,start_time,counters,frame_dir
):
    active_subpics=set(sp_streams.keys())
    closing_subpics=set()

    info=split_files["bitstream_info"]

    width=int(info["width"])
    height=int(info["height"])
    fps=float(info.get("fps") or DEFAULT_FPS)

    if fps<=0:
        raise RuntimeError(f"Invalid FPS={fps}")

    nframes=int(split_files["frame_count"])
    num_subpics=len(sp_streams)

    interval=1.0/fps
    control_frames=max(1,int(round(fps*CONTROL_INTERVAL_S)))

    # ========================================================
    # IDR Metadata
    # ========================================================

    idr_frames = find_idr_frames(
        split_files,
        0
    )

    print(
        f"[IDR] detected frames={idr_frames}"
    )

    # ========================================================
    # Frame + Offline Semantic Mask
    # ========================================================

    reader=FrameReader(frame_dir)

    video_name=video_id.split("_QP")[0]

    mask_dir=(
        ROOT.parent
        /"data/semantic_feature"
        /"HEVC_CTC_B"
        /video_name
        /"masks"
    )

    extractor=MaskFeatureExtractor(
        num_subpics=num_subpics,
        width=width,
        height=height,
        mask_dir=mask_dir
    )

    controller=StreamingController(
        num_subpics=num_subpics,
        mode="collect"
    )

    print(f"[FEATURE] video={video_name}")
    print(f"[FEATURE] mask_dir={mask_dir}")

    # ========================================================
    # Feature Worker
    # ========================================================

    feature_queue=Queue()
    feature_finished=threading.Event()

    def feature_worker():
        while not feature_finished.is_set() or not feature_queue.empty():

            try:
                job=feature_queue.get(timeout=0.01)
            except Empty:
                continue

            if job is None:
                feature_queue.task_done()
                break

            fid=job["fid"]
            frame_bytes=job["frame_bytes"]
            state=job["state"]
            active_snapshot=job["active_subpics"]

            t=time.perf_counter_ns()

            frame=reader.read(fid)

            features=extractor.extract(
                fid,
                frame,
                frame_bytes
            )

            feature_ms=(
                time.perf_counter_ns()-t
            )/1e6

            controller.collect_frame(
                fid,
                features,
                active_snapshot,
                state
            )

            print(
                f"[FEATURE] frame={fid:3d} "
                f"time={feature_ms:.3f}ms "
                f"queue={feature_queue.qsize()}"
            )

            feature_queue.task_done()


    feature_thread=threading.Thread(
        target=feature_worker,
        daemon=True
    )

    feature_thread.start()

    # ========================================================
    # Timing Arrays
    # ========================================================

    frame_source_time_ns=np.full(
        nframes,
        -1,
        dtype=np.int64
    )

    subpic_send_time_ns=np.full(
        (nframes,num_subpics),
        -1,
        dtype=np.int64
    )

    timing_lock=threading.Lock()

    # ========================================================
    # Sender Gate
    # ========================================================

    queue=ReadyQueue()
    event=threading.Event()
    finished=threading.Event()

    sender_thread=threading.Thread(
        target=sender_gate_thread_timed,
        args=(
            conn,queue,event,finished,writer,
            video_id,trace_name,trace_idx,start_time,counters,
            subpic_send_time_ns,timing_lock
        ),
        daemon=True
    )

    sender_thread.start()

    # ========================================================
    # Initialization
    # ========================================================

    init_obj=build_init_object(
        num_subpics,
        fps,
        nframes,
        common_stream
    )

    config_objs=build_config_objects(
        split_files,
        common_stream
    )

    queue.push_many([init_obj])
    queue.push_many(config_objs)
    event.set()

    print(
        f"[INIT] num_subpics={num_subpics} "
        f"fps={fps:.3f} "
        f"frames={nframes}"
    )

    print(
        f"[CONFIG] objects={len(config_objs)} "
        f"bytes={sum(x['size'] for x in config_objs):,} "
        f"stream={common_stream.stream_id}"
    )

    while not queue.empty():
        time.sleep(0.001)

    # ========================================================
    # Live Source
    # ========================================================

    t0=time.monotonic()

    print(
        f"[Live] START fps={fps:.3f}, "
        f"interval={interval*1000:.3f}ms, "
        f"frames={nframes}, SPs={num_subpics}"
    )

    for fid in range(nframes):

        # ----------------------------------------------------
        # Fixed frame release
        #
        # IMPORTANT:
        # Timestamp is taken immediately at source release.
        # Feature extraction must NOT redefine source time.
        # ----------------------------------------------------

        target_time=t0+fid*interval
        wait=target_time-time.monotonic()

        if wait>0:
            time.sleep(wait)

        frame_source_ns=time.time_ns()
        frame_source_time_ns[fid]=frame_source_ns

        release_late_ms=max(
            0.0,
            (time.monotonic()-target_time)*1000.0
        )

        # ----------------------------------------------------
        # Per-SP encoded bytes
        # ----------------------------------------------------

        frame_bytes={}

        for sid in range(num_subpics):
            frame_bytes[sid]=len(
                split_files["frames"][sid][fid]["data"]
            )

        # ----------------------------------------------------
        # Network state snapshot
        # ----------------------------------------------------

        state=get_network_state(conn)

        # ----------------------------------------------------
        # Async feature job
        # ----------------------------------------------------

        feature_queue.put({
            "fid":fid,
            "frame_bytes":frame_bytes.copy(),
            "state":state.copy(),
            "active_subpics":set(active_subpics)
        })

        print(
            f"[SOURCE] frame={fid:3d} "
            f"bw={state['estimated_bandwidth_bps']} "
            f"rtt={state['rtt_us']}us "
            f"feature_queue={feature_queue.qsize()} "
            f"source_ns={frame_source_ns}"
        )

        # ----------------------------------------------------
        # Control Point
        # ----------------------------------------------------

        if (fid+1)%control_frames==0:
            control_idx=(fid+1)//control_frames

            print(
                f"[CTRL] control={control_idx} "
                f"frame={fid} "
                f"active={sorted(active_subpics)} "
                f"priority={priorities}"
            )

            # Later PPO:
            #
            # control_features=controller.get_control_state(
            #     control_frames
            # )
            #
            # decision=controller.update(
            #     control_features,
            #     active_subpics
            # )
            #
            # apply_priorities(
            #     sp_streams,
            #     priorities,
            #     decision.priorities,
            #     active_subpics
            # )

        # ----------------------------------------------------
        # Frame enters sender queue
        # ----------------------------------------------------

        objs=build_frame_objects(
            fid,
            split_files,
            sp_streams,
            priorities,
            active_subpics
        )

        for obj in objs:
            obj["source_time_ns"]=frame_source_ns

        queue.push_many(objs)
        event.set()

        print(
            f"[SOURCE] frame={fid:3d} "
            f"objects={len(objs)} "
            f"bytes={sum(x['size'] for x in objs):,} "
            f"active={sorted(active_subpics)} "
            f"queue={len(queue)}"
        )

    # ========================================================
    # Drain Feature Worker
    # ========================================================

    print(
        f"[FEATURE] waiting for "
        f"{feature_queue.qsize()} pending jobs"
    )

    feature_queue.join()

    feature_finished.set()
    feature_queue.put(None)
    feature_thread.join()

    print("[FEATURE] all feature jobs completed")


    # ========================================================
    # Drain Sender
    # ========================================================

    finished.set()
    event.set()
    sender_thread.join()

    print("[Live] Source finished and sender queue drained")

    # ========================================================
    # Source Pacing Summary
    # ========================================================

    valid=frame_source_time_ns[
        frame_source_time_ns>=0
    ]

    if len(valid)>1:
        source_intervals_ms=np.diff(valid)/1e6

        print(
            f"[PACING] target={1000.0/fps:.3f}ms "
            f"mean={source_intervals_ms.mean():.3f}ms "
            f"std={source_intervals_ms.std():.3f}ms "
            f"min={source_intervals_ms.min():.3f}ms "
            f"max={source_intervals_ms.max():.3f}ms "
            f"effective_fps={1000.0/source_intervals_ms.mean():.3f}"
        )

    # ========================================================
    # Save Training Data + Timing
    # ========================================================

    output_path=(
        Path("training_data")
        / video_id
        / "bw5Mbps.npz"
    )

    save_controller_with_timing(
        controller,
        output_path,
        frame_source_time_ns,
        subpic_send_time_ns,
        idr_frames,
        nframes
    )

    print(
        f"[CTRL] Dataset saved: "
        f"{output_path}"
    )


# ============================================================
# Process Video
# ============================================================

def process_video(
    conn,filepath,frame_dir,sender_writer,network_writer,
    stats_interval,video_id,trace_name,trace_idx,start_time,counters
):
    print(f"\n{'='*70}")
    print(f"Processing: {filepath}")
    print("="*70)

    split_files=vvc_splitter.split(
        filepath,
        Path("split_temp_dir")/video_id
    )

    info=split_files["bitstream_info"]
    num_sp=int(split_files["num_subpics"])

    print(
        f"[Client] VVC={info['width']}x{info['height']} "
        f"FPS={info.get('fps')} "
        f"frames={info['frame_count']} "
        f"SPs={num_sp}"
    )

    # ========================================================
    # Validate SP bitstreams
    # ========================================================

    for sid,path in enumerate(split_files["subpics"]):
        p=Path(path)

        if not p.exists() or p.stat().st_size==0:
            raise RuntimeError(f"Invalid SP{sid}: {p}")

        print(
            f"[Client] SP{sid}: "
            f"{p.stat().st_size:,}B "
            f"SHA256={sha256(p)[:16]}..."
        )

    # ========================================================
    # Validate original frames
    # ========================================================

    frame_dir=Path(frame_dir)

    if not frame_dir.exists():
        raise RuntimeError(
            f"Frame directory not found: {frame_dir}"
        )

    first_frame=frame_dir/"000001.png"

    if not first_frame.exists():
        raise RuntimeError(
            f"First frame not found: {first_frame}"
        )

    print(f"[Client] Frame directory: {frame_dir}")

    # ========================================================
    # QUIC Streams
    # ========================================================

    common,sp_streams,priorities=create_streams(
        conn,
        num_sp
    )

    # ========================================================
    # Network Statistics
    # ========================================================

    stop=threading.Event()

    stats_thread=threading.Thread(
        target=stats_thread_fn,
        args=(
            conn,
            network_writer,
            stats_interval,
            stop,
            video_id,
            trace_name,
            trace_idx,
            start_time,
            counters
        ),
        daemon=True
    )
    stats_thread.start()

    # ========================================================
    # Streaming
    # ========================================================

    try:
        live_streaming(
            conn,
            split_files,
            common,
            sp_streams,
            priorities,
            sender_writer,
            video_id,
            trace_name,
            trace_idx,
            start_time,
            counters,
            frame_dir
        )

    finally:
        stop.set()
        stats_thread.join()

        print("[Live] Closing streams")

        common.close()

        for sid,stream in sp_streams.items():
            print(
                f"[Live] Close SP{sid} "
                f"stream={stream.stream_id}"
            )
            stream.close()


# ============================================================
# Main
# ============================================================

def main():
    host="127.0.0.1"
    port=8000
    file=""

    input_dir=Path(
        "/share/HP_dataset/HEVC_CTC_B/vvc_bitstream/VVC_3x3/BasketballDrive"
    )

    frame_dir=Path(
        "/share/HP_dataset/HEVC_CTC_B/Frames/BasketballDrive_1920x1080_50"
    )

    stats_interval=0.1
    trace_name="unknown_trace"
    loop_count=1

    # ========================================================
    # Input Bitstreams
    # ========================================================

    if file:
        files=[file]

    else:
        files=sorted(
            str(f)
            for f in Path(input_dir).rglob("*.vvc")
            if "common" not in f.name
            and "subpic_" not in f.name
        )

    if not files:
        raise RuntimeError("No VVC files found")

    # ========================================================
    # Validate Original Frames
    # ========================================================

    if not frame_dir.exists():
        raise RuntimeError(
            f"Frame directory not found: {frame_dir}"
        )

    first_frame=frame_dir/"000001.png"

    if not first_frame.exists():
        raise RuntimeError(
            f"First frame not found: {first_frame}"
        )

    print(f"[Input] frame_dir={frame_dir}")

    # ========================================================
    # NS-3 Synchronization
    # ========================================================

    start_time=time.time()
    trace_idx=0

    try:
        lines=Path("/tmp/ns3_sync.txt").read_text().splitlines()

        if lines:
            start_time=float(lines[0])

        if len(lines)>1:
            trace_idx=int(lines[1])

        print(
            f"[Sync] start={start_time}, "
            f"trace={trace_idx}"
        )

    except Exception as e:
        print(f"[Sync] Warning: {e}")

    # ========================================================
    # Logs
    # ========================================================

    log_dir=Path("logs")
    log_dir.mkdir(exist_ok=True)

    sender_file=open(
        log_dir/"sender_log.csv",
        "w",
        newline=""
    )

    network_file=open(
        log_dir/"network_log.csv",
        "w",
        newline=""
    )

    sender_writer=csv.writer(sender_file)
    network_writer=csv.writer(network_file)

    sender_writer.writerow([
        "seq_num",
        "video_id",
        "trace_name",
        "trace_start_index",
        "start_time",
        "match_time",
        "stream_id",
        "kind",
        "subpic_id",
        "frame_id",
        "source_time_ns",
        "submit_complete_ns",
        "payload_bytes",
        "wire_bytes",
        "priority",
        "rtt_us",
        "cwnd_bytes",
        "bytes_in_flight",
        "estimated_bandwidth_bps"
    ])

    network_writer.writerow([
        "seq_num",
        "video_id",
        "trace_name",
        "trace_start_index",
        "start_time",
        "match_time",
        "rtt_us",
        "cwnd_bytes",
        "total_bytes_sent",
        "stream_bytes_sent",
        "estimated_bandwidth_bps",
        "bytes_in_flight",
        "posted_bytes",
        "ideal_bytes"
    ])

    counters={
        "network":0,
        "sender":0
    }

    # ========================================================
    # QUIC Connection
    # ========================================================

    conn=QuicConnection(
        host=host,
        port=port,
        scheduling_scheme=0
    )

    conn._client.configure_mtu(
        1200,
        1500
    )

    # ========================================================
    # Run
    # ========================================================

    try:
        for loop in range(loop_count):
            print(
                f"\n=== Loop "
                f"{loop+1}/{loop_count} ==="
            )

            for filepath in files:
                process_video(
                    conn,
                    filepath,
                    frame_dir,
                    sender_writer,
                    network_writer,
                    stats_interval,
                    Path(filepath).stem,
                    trace_name,
                    trace_idx,
                    start_time,
                    counters
                )

                time.sleep(60)

    finally:
        conn.disconnect(force=True)

        sender_file.close()
        network_file.close()

    print("\n=== Streaming completed ===")


if __name__=="__main__":
    main()