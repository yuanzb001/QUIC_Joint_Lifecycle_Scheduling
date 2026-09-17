#!/usr/bin/env python3

import csv
import subprocess
from pathlib import Path


# ============================================================
# Configuration
# ============================================================

VTM_ROOT = Path(
    "/home/yuanzn/Documents/QUIC_Joint_Lifecycle_Scheduling/VVCSoftware_VTM"
)

VTM_BIN = VTM_ROOT / "bin"
VTM_ENCODER = VTM_BIN / "EncoderAppStatic"
EXTRACTOR = VTM_BIN / "BitstreamExtractorAppStatic"
DECODER = VTM_BIN / "DecoderAppStatic"

# Low Delay P
BASE_CFG = VTM_ROOT / "cfg/encoder_lowdelay_vtm_gop16.cfg"

INPUT_DIR = Path(
    "/home/yuanzn/Dataset/HEVC_CTC_B/HEVC_B_video"
)

OUTPUT_DIR = Path(
    "/home/yuanzn/Documents/QUIC_Joint_Lifecycle_Scheduling/"
    "data/vvc_bitstream/HEVC_CTC_classB/VVC_2x2"
)

WIDTH = 1920
HEIGHT = 1080
QP = 32

# YUV420 8-bit
BYTES_PER_FRAME = WIDTH * HEIGHT * 3 // 2

VIDEOS = {
    "BasketballDrive": {
        "file": "BasketballDrive_1920x1080_50.yuv",
        "fps": 50,
    },
    "BQTerrace": {
        "file": "BQTerrace_1920x1080_60.yuv",
        "fps": 60,
    },
    "Cactus": {
        "file": "Cactus_1920x1080_50.yuv",
        "fps": 50,
    },
    "Kimono": {
        "file": "Kimono_1920x1080_24.yuv",
        "fps": 24,
    },
    "ParkScene": {
        "file": "ParkScene_1920x1080_24.yuv",
        "fps": 24,
    },
}


# ============================================================
# YUV Information
# ============================================================

def get_frame_count(path):
    file_size = path.stat().st_size

    if file_size % BYTES_PER_FRAME != 0:
        raise ValueError(
            f"{path.name}: invalid YUV size.\n"
            f"File size       = {file_size} bytes\n"
            f"Bytes per frame = {BYTES_PER_FRAME} bytes\n"
            f"Remainder       = {file_size % BYTES_PER_FRAME} bytes"
        )

    return file_size // BYTES_PER_FRAME


# ============================================================
# VTM 2x2 Subpicture Configuration
# ============================================================

def make_subpic_cfg(fps, frames, input_file, bitstream):
    return f"""
# ============================================================
# Input
# ============================================================

InputFile                     : {input_file}
InputBitDepth                 : 8
InputChromaFormat             : 420

SourceWidth                   : {WIDTH}
SourceHeight                  : {HEIGHT}

FrameRate                     : {fps}
FramesToBeEncoded             : {frames}

BitstreamFile                 : {bitstream}
QP                            : {QP}

# ============================================================
# 2x2 Subpictures
# 1920x1080, CTU=128 -> 15x9 CTUs
# ============================================================

SubPicInfoPresentFlag                   : 1
NumSubPics                              : 4
SubPicSameSizeFlag                      : 0

SubPicCtuTopLeftX                       : 0 7 0 7
SubPicCtuTopLeftY                       : 0 0 4 4
SubPicWidth                             : 7 8 7 8
SubPicHeight                            : 4 4 5 5

SubPicTreatedAsPicFlag                  : 1 1 1 1
LoopFilterAcrossSubpicEnabledFlag       : 0 0 0 0

SubPicIdMappingExplicitlySignalledFlag  : 0
SubPicIdMappingInSpsFlag                : 0
SubPicIdLen                             : 0
SubPicId                                : 0

# ============================================================
# 2x2 Tiles / 4 rectangular slices
# ============================================================

EnablePicPartitioning                   : 1

TileColumnWidthArray                    : 7 8
TileRowHeightArray                      : 4 5

RasterScanSlices                        : 0
RectSliceFixedWidth                     : 1
RectSliceFixedHeight                    : 1

DisableLoopFilterAcrossTiles            : 1
DisableLoopFilterAcrossSlices           : 1
"""


# ============================================================
# Encode One Video
# ============================================================

def encode_video(name, info):

    input_file = INPUT_DIR / info["file"]

    if not input_file.exists():
        print(f"[ERROR] Missing input: {input_file}")
        return None

    # --------------------------------------------------------
    # Automatically detect all frames
    # --------------------------------------------------------

    # frames = get_frame_count(input_file)
    frames = 50
    fps = info["fps"]
    duration = frames / fps

    # --------------------------------------------------------
    # Output paths
    # --------------------------------------------------------

    video_dir = OUTPUT_DIR / name
    video_dir.mkdir(parents=True, exist_ok=True)

    bitstream = video_dir / f"{name}_QP{QP}_50frames_2x2.vvc"
    cfg = video_dir / f"{name}_QP{QP}_50frames_2x2.cfg"
    log = video_dir / f"{name}_QP{QP}_50frames_2x2.log"

    # --------------------------------------------------------
    # Generate VTM configuration
    # --------------------------------------------------------

    cfg.write_text(
        make_subpic_cfg(
            fps,
            frames,
            input_file,
            bitstream,
        )
    )

    cmd = [
        str(VTM_ENCODER),
        "-c", str(BASE_CFG),
        "-c", str(cfg),
    ]

    print("\n" + "=" * 70)
    print(f"[ENCODE] {name}")
    print(f"Input    : {input_file}")
    print(f"Resolution: {WIDTH}x{HEIGHT}")
    print(f"FPS      : {fps}")
    print(f"Frames   : {frames}")
    print(f"Duration : {duration:.2f} s")
    print(f"QP       : {QP}")
    print(f"Mode     : Low Delay P")
    print(f"Subpics  : 2x2")
    print("=" * 70)

    # --------------------------------------------------------
    # Run VTM + display output + save log
    # --------------------------------------------------------

    with log.open("w") as f:

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        for line in process.stdout:
            print(line, end="")
            f.write(line)
            f.flush()

        returncode = process.wait()

    # --------------------------------------------------------
    # Check result
    # --------------------------------------------------------

    if returncode != 0:
        print(f"\n[FAILED] {name}")
        print(f"See log: {log}")
        return None

    if not bitstream.exists():
        print(f"[FAILED] Bitstream not generated: {bitstream}")
        return None

    # --------------------------------------------------------
    # Bitrate
    # --------------------------------------------------------

    size_bytes = bitstream.stat().st_size
    size_mb = size_bytes / (1024 * 1024)

    bitrate_mbps = (
        size_bytes * 8
        / duration
        / 1e6
    )

    print("\n" + "-" * 70)
    print(f"[DONE] {name}")
    print(f"Frames   : {frames}")
    print(f"Duration : {duration:.2f} s")
    print(f"Size     : {size_mb:.2f} MB")
    print(f"Bitrate  : {bitrate_mbps:.3f} Mbps")
    print("-" * 70)

    return {
        "video": name,
        "width": WIDTH,
        "height": HEIGHT,
        "fps": fps,
        "frames": frames,
        "duration_s": round(duration, 4),
        "qp": QP,
        "size_bytes": size_bytes,
        "size_MB": round(size_mb, 4),
        "bitrate_Mbps": round(bitrate_mbps, 4),
        "bitstream": str(bitstream),
    }


# ============================================================
# Main
# ============================================================

def main():

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # --------------------------------------------------------
    # Check VTM
    # --------------------------------------------------------

    if not VTM_ENCODER.exists():
        raise FileNotFoundError(
            f"VTM encoder not found:\n{VTM_ENCODER}"
        )

    if not BASE_CFG.exists():
        raise FileNotFoundError(
            f"VTM config not found:\n{BASE_CFG}"
        )

    print("=" * 70)
    print("VTM Batch Encoding")
    print("=" * 70)
    print(f"Encoder : {VTM_ENCODER}")
    print(f"Config  : {BASE_CFG}")
    print(f"QP      : {QP}")
    print(f"Input   : {INPUT_DIR}")
    print(f"Output  : {OUTPUT_DIR}")

    # --------------------------------------------------------
    # Encode all videos
    # --------------------------------------------------------

    results = []

    for name, info in VIDEOS.items():

        try:
            result = encode_video(name, info)

            if result:
                results.append(result)
            break

        except Exception as e:
            print(f"\n[ERROR] {name}: {e}")

    # --------------------------------------------------------
    # Save summary
    # --------------------------------------------------------

    if not results:
        print("\nNo successful encodes.")
        return

    csv_file = OUTPUT_DIR / f"summary_QP{QP}_2x2.csv"

    with csv_file.open("w", newline="") as f:

        writer = csv.DictWriter(
            f,
            fieldnames=results[0].keys(),
        )

        writer.writeheader()
        writer.writerows(results)

    # --------------------------------------------------------
    # Print summary
    # --------------------------------------------------------

    print("\n")
    print("=" * 85)
    print("FINAL SUMMARY")
    print("=" * 85)

    print(
        f"{'Video':<20}"
        f"{'FPS':>6}"
        f"{'Frames':>10}"
        f"{'Duration':>12}"
        f"{'Size MB':>12}"
        f"{'Mbps':>12}"
    )

    print("-" * 85)

    for r in results:

        print(
            f"{r['video']:<20}"
            f"{r['fps']:>6}"
            f"{r['frames']:>10}"
            f"{r['duration_s']:>11.2f}s"
            f"{r['size_MB']:>12.2f}"
            f"{r['bitrate_Mbps']:>12.3f}"
        )

    print("=" * 85)
    print(f"\nSummary saved to:\n{csv_file}")


if __name__ == "__main__":
    main()