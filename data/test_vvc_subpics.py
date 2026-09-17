#!/usr/bin/env python3

import json
import shutil
import subprocess
from pathlib import Path
from collections import Counter


# ============================================================
# Configuration
# ============================================================

ROOT = Path("/home/yuanzn/Documents/QUIC_Joint_Lifecycle_Scheduling")

INPUT_VVC = (
    ROOT
    / "data/vvc_bitstream/HEVC_CTC_classB/VVC_2x2/"
      "BasketballDrive/BasketballDrive_QP32_50frames_2x2.vvc"
)

VTM_ROOT = ROOT / "VVCSoftware_VTM"
OUTPUT_DIR = ROOT / "output_res/subpic_test"

MAX_SUBPICS = 64


# ============================================================
# Drop control
# ============================================================

DROP_ENABLE = True

# Which extracted subpicture to test
DROP_SUBPIC = 0

# Temporal layer to drop
DROP_TID = 4

# Number of matching pictures to drop
DROP_COUNT = 1


# ============================================================
# Decoded extracted-subpicture format
#
# Verified from current SP0 YUV:
# 68,812,800 / 50 = 1,376,256 bytes/frame
# 896 * 512 * 3 = 1,376,256
#
# VTM 10-bit YUV420:
# 16-bit storage per sample
# ============================================================

SUBPIC_WIDTH = 896
SUBPIC_HEIGHT = 512
YUV_BIT_DEPTH = 10


# ============================================================
# VTM
# ============================================================

def find_executable(names):
    for name in names:
        p = VTM_ROOT / "bin" / name
        if p.is_file():
            return p

    for name in names:
        for p in VTM_ROOT.rglob(name):
            if p.is_file():
                return p

    return None


EXTRACTOR = find_executable([
    "BitstreamExtractorAppStatic",
    "BitstreamExtractorApp",
])

DECODER = find_executable([
    "DecoderAppStatic",
    "DecoderApp",
])


def run(cmd):
    return subprocess.run(
        [str(x) for x in cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


# ============================================================
# Annex-B parser
# ============================================================

def find_start_codes(data):
    starts = []
    i = 0

    while i < len(data) - 3:

        if data[i:i + 4] == b"\x00\x00\x00\x01":
            starts.append((i, 4))
            i += 4

        elif data[i:i + 3] == b"\x00\x00\x01":
            starts.append((i, 3))
            i += 3

        else:
            i += 1

    return starts


def split_annexb(data):
    starts = find_start_codes(data)
    nals = []

    for i, (pos, sc_len) in enumerate(starts):

        end = (
            starts[i + 1][0]
            if i + 1 < len(starts)
            else len(data)
        )

        full = data[pos:end]
        payload = data[pos + sc_len:end]

        if len(payload) < 2:
            continue

        h = int.from_bytes(payload[:2], "big")

        nal_type = (h >> 3) & 0x1F
        tid_plus1 = h & 0x07

        nals.append({
            "index": len(nals),
            "offset": pos,
            "size": len(full),
            "nal_type": nal_type,
            "layer_id": (h >> 8) & 0x3F,
            "tid": tid_plus1 - 1 if tid_plus1 else -1,
            "vcl": 0 <= nal_type <= 11,
            "data": full,
        })

    return nals


def parse_annexb_nals(path):
    return split_annexb(Path(path).read_bytes())


def inspect_bitstream(path, verbose=True):
    nals = parse_annexb_nals(path)
    vcl = [n for n in nals if n["vcl"]]

    if verbose:

        print("\nNAL type distribution:")

        for t, n in sorted(
            Counter(x["nal_type"] for x in nals).items()
        ):
            print(f"  NAL type {t:2d}: {n}")

    return {
        "bytes": Path(path).stat().st_size,
        "nal_count": len(nals),
        "vcl_count": len(vcl),
        "tid_count": dict(
            sorted(
                Counter(n["tid"] for n in vcl).items()
            )
        ),
    }


# ============================================================
# Extract / decode
# ============================================================

def extract_subpic(input_vvc, sid, output_vvc):

    result = run([
        EXTRACTOR,
        "-b", input_vvc,
        "-o", output_vvc,
        f"--SubPicIdx={sid}",
    ])

    ok = (
        result.returncode == 0
        and output_vvc.is_file()
        and output_vvc.stat().st_size > 0
    )

    return ok, result.stdout


def decode_bitstream(input_vvc, output_yuv, log_file):

    if output_yuv.exists():
        output_yuv.unlink()

    result = run([
        DECODER,
        "-b", input_vvc,
        "-o", output_yuv,
    ])

    log_file.write_text(
        result.stdout,
        encoding="utf-8",
        errors="replace",
    )

    ok = (
        result.returncode == 0
        and output_yuv.is_file()
        and output_yuv.stat().st_size > 0
    )

    return ok, result.returncode


# ============================================================
# YUV helpers
# ============================================================

def get_frame_bytes(width, height, bit_depth=10):

    if bit_depth > 8:
        bytes_per_sample = 2
    else:
        bytes_per_sample = 1

    # YUV420:
    # Y  = W*H
    # UV = W*H/2
    #
    # total samples = W*H*1.5
    return width * height * 3 // 2 * bytes_per_sample


def get_yuv_frame_count(
    path,
    width=SUBPIC_WIDTH,
    height=SUBPIC_HEIGHT,
    bit_depth=YUV_BIT_DEPTH,
):

    frame_bytes = get_frame_bytes(
        width,
        height,
        bit_depth,
    )

    size = Path(path).stat().st_size

    if size % frame_bytes != 0:
        raise RuntimeError(
            f"Invalid YUV size:\n"
            f"  File       : {path}\n"
            f"  Size       : {size:,} bytes\n"
            f"  Resolution : {width}x{height}\n"
            f"  Frame bytes: {frame_bytes:,}\n"
            f"  Remainder  : {size % frame_bytes:,}"
        )

    return size // frame_bytes


def make_black_yuv420_frame(
    width,
    height,
    bit_depth=10,
):
    """
    Create one limited-range black YUV420 frame.

    For 10-bit:
        Y = 64
        U = 512
        V = 512

    VTM decoded 10-bit YUV uses uint16 little-endian storage.
    """

    if bit_depth == 10:

        y_value = 64
        uv_value = 512

        y_sample = y_value.to_bytes(
            2,
            "little",
        )

        uv_sample = uv_value.to_bytes(
            2,
            "little",
        )

        y_plane = (
            y_sample
            * (width * height)
        )

        u_plane = (
            uv_sample
            * (width * height // 4)
        )

        v_plane = (
            uv_sample
            * (width * height // 4)
        )

    elif bit_depth == 8:

        y_value = 16
        uv_value = 128

        y_plane = bytes([y_value]) * (
            width * height
        )

        u_plane = bytes([uv_value]) * (
            width * height // 4
        )

        v_plane = bytes([uv_value]) * (
            width * height // 4
        )

    else:

        raise ValueError(
            f"Unsupported YUV bit depth: {bit_depth}"
        )

    return y_plane + u_plane + v_plane


def restore_dropped_frames_with_black(
    input_yuv,
    output_yuv,
    total_frames,
    dropped_pictures,
    width=SUBPIC_WIDTH,
    height=SUBPIC_HEIGHT,
    bit_depth=YUV_BIT_DEPTH,
):
    """
    Decoder output after dropping pictures contains fewer frames.

    Example:

        Original:
        0 1 2 3 4 5 ...

        Drop picture 2:
        0 1   3 4 5 ...

        Decoder YUV:
        0 1 3 4 5 ...

        Restored visualization:
        0 1 BLACK 3 4 5 ...

    This function restores the original timeline by inserting
    black frames at the dropped picture indices.
    """

    input_yuv = Path(input_yuv)
    output_yuv = Path(output_yuv)

    frame_bytes = get_frame_bytes(
        width,
        height,
        bit_depth,
    )

    data = input_yuv.read_bytes()

    if len(data) % frame_bytes != 0:

        raise RuntimeError(
            f"Decoded YUV size is invalid:\n"
            f"  Size        : {len(data):,}\n"
            f"  Frame bytes : {frame_bytes:,}\n"
            f"  Resolution  : {width}x{height}"
        )

    decoded_frames = (
        len(data) // frame_bytes
    )

    dropped_set = set(
        int(x) for x in dropped_pictures
    )

    expected_decoded = (
        total_frames - len(dropped_set)
    )

    print("\n[5] Reconstructing original timeline")
    print("-" * 70)

    print(
        f"Resolution      : "
        f"{width}x{height}"
    )

    print(
        f"Bit depth       : "
        f"{bit_depth}"
    )

    print(
        f"Bytes / frame   : "
        f"{frame_bytes:,}"
    )

    print(
        f"Original frames : "
        f"{total_frames}"
    )

    print(
        f"Decoded frames  : "
        f"{decoded_frames}"
    )

    print(
        f"Dropped pictures: "
        f"{sorted(dropped_set)}"
    )

    if decoded_frames != expected_decoded:

        raise RuntimeError(
            f"Decoded frame count mismatch.\n"
            f"Expected : {expected_decoded}\n"
            f"Actual   : {decoded_frames}"
        )

    black_frame = make_black_yuv420_frame(
        width,
        height,
        bit_depth,
    )

    if len(black_frame) != frame_bytes:

        raise RuntimeError(
            "Generated black-frame size mismatch."
        )

    src_idx = 0

    with output_yuv.open("wb") as f:

        for picture_idx in range(total_frames):

            if picture_idx in dropped_set:

                f.write(black_frame)

                print(
                    f"[BLACK] "
                    f"Picture={picture_idx:3d}"
                )

            else:

                start = (
                    src_idx * frame_bytes
                )

                end = (
                    start + frame_bytes
                )

                frame = data[start:end]

                if len(frame) != frame_bytes:

                    raise RuntimeError(
                        f"Missing decoded frame "
                        f"for Picture={picture_idx}"
                    )

                f.write(frame)

                src_idx += 1

    if src_idx != decoded_frames:

        raise RuntimeError(
            f"Not all decoded frames consumed: "
            f"{src_idx}/{decoded_frames}"
        )

    restored_frames = get_yuv_frame_count(
        output_yuv,
        width,
        height,
        bit_depth,
    )

    print(
        f"Restored frames : "
        f"{restored_frames}"
    )

    print(
        f"Output size     : "
        f"{output_yuv.stat().st_size:,} bytes"
    )

    print(
        f"Output YUV      : "
        f"{output_yuv}"
    )

    return {
        "output_yuv": str(output_yuv),
        "width": width,
        "height": height,
        "bit_depth": bit_depth,
        "frame_bytes": frame_bytes,
        "decoded_frames": decoded_frames,
        "restored_frames": restored_frames,
        "black_frames": sorted(dropped_set),
    }


# ============================================================
# Detect subpictures
# ============================================================

def detect_subpics(input_vvc):

    probe_dir = OUTPUT_DIR / "_probe"

    probe_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    valid = []

    print("\n[1] Detecting subpictures")
    print("-" * 70)

    for sid in range(MAX_SUBPICS):

        out = (
            probe_dir
            / f"probe_{sid}.vvc"
        )

        ok, _ = extract_subpic(
            input_vvc,
            sid,
            out,
        )

        if not ok:
            break

        info = inspect_bitstream(
            out,
            False,
        )

        if info["vcl_count"] == 0:
            break

        valid.append(sid)

        print(
            f"Subpic {sid}: "
            f"{info['bytes']:,} bytes, "
            f"{info['vcl_count']} VCL NALs"
        )

    shutil.rmtree(
        probe_dir,
        ignore_errors=True,
    )

    if not valid:
        raise RuntimeError(
            "No subpictures detected."
        )

    print(
        f"\nDetected number of subpictures: "
        f"{len(valid)}"
    )

    print(
        f"Subpic IDs: {valid}"
    )

    return valid


# ============================================================
# Full-stream picture map
# ============================================================

def build_picture_map(
    input_vvc,
    subpic_ids,
):

    nals = parse_annexb_nals(
        input_vvc
    )

    vcl_idx = [
        i
        for i, n in enumerate(nals)
        if n["vcl"]
    ]

    nsub = len(subpic_ids)

    if len(vcl_idx) % nsub:

        raise RuntimeError(
            f"Cannot group VCL NALs: "
            f"VCL={len(vcl_idx)}, "
            f"subpics={nsub}, "
            f"remainder="
            f"{len(vcl_idx) % nsub}"
        )

    pictures = []

    for pic_idx, start in enumerate(
        range(
            0,
            len(vcl_idx),
            nsub,
        )
    ):

        group = (
            vcl_idx[
                start:start + nsub
            ]
        )

        tids = [
            nals[i]["tid"]
            for i in group
        ]

        if len(set(tids)) != 1:

            raise RuntimeError(
                f"Picture {pic_idx}: "
                f"inconsistent TIds {tids}"
            )

        units = [
            {
                "subpic": sid,
                "nal_index": idx,
                "bytes": nals[idx]["size"],
                "tid": nals[idx]["tid"],
            }
            for sid, idx in zip(
                subpic_ids,
                group,
            )
        ]

        pictures.append({
            "picture": pic_idx,
            "tid": tids[0],
            "units": units,
        })

    return pictures


def print_picture_map(pictures):

    print(
        "\n[2] Picture / TId / "
        "Subpicture map"
    )

    print("-" * 70)

    for p in pictures:

        units = " ".join(
            f"SP{x['subpic']}:"
            f"{x['bytes']}B"
            for x in p["units"]
        )

        print(
            f"Picture "
            f"{p['picture']:3d} | "
            f"TId={p['tid']} | "
            f"{units}"
        )


# ============================================================
# Drop TId from ONE extracted subpicture
# ============================================================

def drop_tid_from_extracted_subpic(
    input_vvc,
    output_vvc,
    target_tid,
    drop_count,
):
    """
    Input must already contain ONE extracted subpicture.

    Example:
        subpic_0.vvc

    Drops the first N VCL NAL units whose TId equals target_tid.
    """

    if drop_count <= 0:
        raise ValueError(
            "DROP_COUNT must be > 0."
        )

    nals = parse_annexb_nals(
        input_vvc
    )

    dropped = []
    kept = []

    picture_idx = 0

    for nal_idx, nal in enumerate(nals):

        if not nal["vcl"]:

            kept.append(nal)

            continue

        cur_pic = picture_idx

        picture_idx += 1

        if (
            nal["tid"] == target_tid
            and len(dropped) < drop_count
        ):

            item = {
                "picture": cur_pic,
                "tid": nal["tid"],
                "nal_index": nal_idx,
                "bytes": nal["size"],
            }

            dropped.append(item)

            print(
                f"[DROP] "
                f"Picture={cur_pic:3d} | "
                f"TId={nal['tid']} | "
                f"NAL={nal_idx} | "
                f"{nal['size']:,} bytes"
            )

            continue

        kept.append(nal)

    if not dropped:

        raise RuntimeError(
            f"No VCL NAL with "
            f"TId={target_tid} found "
            f"in {input_vvc}"
        )

    if len(dropped) < drop_count:

        print(
            f"[WARNING] Requested "
            f"{drop_count}, only "
            f"{len(dropped)} "
            f"TId={target_tid} "
            f"NALs found."
        )

    output_vvc.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with output_vvc.open("wb") as f:

        for nal in kept:
            f.write(nal["data"])

    return dropped


# ============================================================
# Main
# ============================================================

def main():

    print("=" * 70)
    print(
        "VVC Extracted-Subpicture "
        "Dependency Test"
    )
    print("=" * 70)

    if not INPUT_VVC.is_file():
        raise FileNotFoundError(
            INPUT_VVC
        )

    if EXTRACTOR is None:
        raise FileNotFoundError(
            "BitstreamExtractorApp "
            "not found."
        )

    if DECODER is None:
        raise FileNotFoundError(
            "DecoderApp not found."
        )

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    print(
        f"Input     : {INPUT_VVC}"
    )

    print(
        f"Extractor : {EXTRACTOR}"
    )

    print(
        f"Decoder   : {DECODER}"
    )


    # ========================================================
    # Original stream
    # ========================================================

    original = inspect_bitstream(
        INPUT_VVC
    )

    print("\nOriginal bitstream")
    print("-" * 70)

    print(
        f"Size      : "
        f"{original['bytes']:,} bytes"
    )

    print(
        f"NAL units : "
        f"{original['nal_count']}"
    )

    print(
        f"VCL NALs  : "
        f"{original['vcl_count']}"
    )

    print(
        f"TId count : "
        f"{original['tid_count']}"
    )


    # ========================================================
    # Detect subpictures
    # ========================================================

    subpic_ids = detect_subpics(
        INPUT_VVC
    )


    # ========================================================
    # Picture map
    # ========================================================

    pictures = build_picture_map(
        INPUT_VVC,
        subpic_ids,
    )

    print_picture_map(
        pictures
    )

    pic_tid_count = Counter(
        p["tid"]
        for p in pictures
    )

    print(
        "\nPicture TId distribution:"
    )

    for tid, count in sorted(
        pic_tid_count.items()
    ):

        print(
            f"  TId {tid}: "
            f"{count} pictures"
        )


    # ========================================================
    # Extract + decode every subpicture
    # ========================================================

    print(
        "\n[3] Extracting and "
        "decoding subpictures"
    )

    print("-" * 70)

    summary = []
    subpic_files = {}

    for sid in subpic_ids:

        sub_vvc = (
            OUTPUT_DIR
            / f"subpic_{sid}.vvc"
        )

        sub_yuv = (
            OUTPUT_DIR
            / f"subpic_{sid}.yuv"
        )

        extract_log = (
            OUTPUT_DIR
            / f"subpic_{sid}_extract.log"
        )

        decode_log = (
            OUTPUT_DIR
            / f"subpic_{sid}_decode.log"
        )

        ok, log = extract_subpic(
            INPUT_VVC,
            sid,
            sub_vvc,
        )

        extract_log.write_text(
            log,
            encoding="utf-8",
            errors="replace",
        )

        if not ok:

            print(
                f"Subpic {sid}: "
                f"EXTRACT FAILED"
            )

            summary.append({
                "subpic": sid,
                "extract": False,
                "decode": False,
            })

            continue

        subpic_files[sid] = sub_vvc

        info = inspect_bitstream(
            sub_vvc,
            False,
        )

        decode_ok, rc = decode_bitstream(
            sub_vvc,
            sub_yuv,
            decode_log,
        )

        print(
            f"Subpic {sid}: "
            f"VCL={info['vcl_count']:3d} | "
            f"TId={info['tid_count']} | "
            f"Decode="
            f"{'SUCCESS' if decode_ok else 'FAILED'}"
        )

        summary.append({
            "subpic": sid,
            "extract": True,
            "decode": decode_ok,
            "decoder_returncode": rc,
            **info,
        })


    # ========================================================
    # Drop from extracted subpicture
    # ========================================================

    drop_result = {
        "enabled": False
    }

    if DROP_ENABLE:

        print(
            "\n[4] Extracted-subpicture "
            "TId drop test"
        )

        print("-" * 70)

        if DROP_SUBPIC not in subpic_files:

            raise RuntimeError(
                f"Subpic {DROP_SUBPIC} "
                f"was not successfully "
                f"extracted."
            )

        input_subpic = (
            subpic_files[
                DROP_SUBPIC
            ]
        )

        dropped_vvc = (
            OUTPUT_DIR
            / (
                f"subpic_{DROP_SUBPIC}_"
                f"drop_T{DROP_TID}_"
                f"N{DROP_COUNT}.vvc"
            )
        )

        dropped_yuv = (
            OUTPUT_DIR
            / (
                f"subpic_{DROP_SUBPIC}_"
                f"drop_T{DROP_TID}_"
                f"N{DROP_COUNT}.yuv"
            )
        )

        dropped_log = (
            OUTPUT_DIR
            / (
                f"subpic_{DROP_SUBPIC}_"
                f"drop_T{DROP_TID}_"
                f"N{DROP_COUNT}_"
                f"decode.log"
            )
        )

        blackfill_yuv = (
            OUTPUT_DIR
            / (
                f"subpic_{DROP_SUBPIC}_"
                f"drop_T{DROP_TID}_"
                f"N{DROP_COUNT}_"
                f"blackfill.yuv"
            )
        )

        before = inspect_bitstream(
            input_subpic,
            False,
        )

        print(
            f"Input Subpic  : "
            f"{input_subpic}"
        )

        print(
            f"Target Subpic : "
            f"{DROP_SUBPIC}"
        )

        print(
            f"Target TId    : "
            f"{DROP_TID}"
        )

        print(
            f"Drop count    : "
            f"{DROP_COUNT}"
        )

        print(
            f"Original VCL  : "
            f"{before['vcl_count']}"
        )

        print(
            f"Original TIds : "
            f"{before['tid_count']}"
        )

        print()

        dropped = (
            drop_tid_from_extracted_subpic(
                input_subpic,
                dropped_vvc,
                DROP_TID,
                DROP_COUNT,
            )
        )

        after = inspect_bitstream(
            dropped_vvc,
            False,
        )

        print(
            "\nModified bitstream"
        )

        print("-" * 70)

        print(
            f"Dropped NALs : "
            f"{len(dropped)}"
        )

        print(
            f"Original VCL : "
            f"{before['vcl_count']}"
        )

        print(
            f"Modified VCL : "
            f"{after['vcl_count']}"
        )

        print(
            f"Original TIds: "
            f"{before['tid_count']}"
        )

        print(
            f"Modified TIds: "
            f"{after['tid_count']}"
        )

        print(
            f"Size         : "
            f"{before['bytes']:,} -> "
            f"{after['bytes']:,} bytes"
        )

        print(
            f"Output VVC   : "
            f"{dropped_vvc}"
        )


        # ====================================================
        # Decode dropped stream
        # ====================================================

        decode_ok, rc = decode_bitstream(
            dropped_vvc,
            dropped_yuv,
            dropped_log,
        )

        print(
            "\nDecode result"
        )

        print("-" * 70)

        print(
            f"Decoder code : {rc}"
        )

        print(
            f"Decode       : "
            f"{'SUCCESS' if decode_ok else 'FAILED'}"
        )

        print(
            f"Output YUV   : "
            f"{dropped_yuv}"
        )

        print(
            f"Decode log   : "
            f"{dropped_log}"
        )


        # ====================================================
        # Restore original timeline with black frames
        # ====================================================

        blackfill_result = None

        if decode_ok:

            dropped_pictures = [
                x["picture"]
                for x in dropped
            ]

            blackfill_result = (
                restore_dropped_frames_with_black(
                    input_yuv=dropped_yuv,
                    output_yuv=blackfill_yuv,
                    total_frames=before["vcl_count"],
                    dropped_pictures=dropped_pictures,
                )
            )


        # ====================================================
        # Drop result
        # ====================================================

        drop_result = {
            "enabled": True,
            "target_subpic": DROP_SUBPIC,
            "target_tid": DROP_TID,
            "requested_count": DROP_COUNT,
            "actual_count": len(dropped),
            "dropped": dropped,
            "before": before,
            "after": after,
            "output_vvc": str(dropped_vvc),
            "output_yuv": str(dropped_yuv),
            "decode": decode_ok,
            "decoder_returncode": rc,
            "blackfill": blackfill_result,
        }


    # ========================================================
    # JSON
    # ========================================================

    summary_file = (
        OUTPUT_DIR
        / "summary.json"
    )

    summary_file.write_text(
        json.dumps(
            {
                "input": str(INPUT_VVC),
                "original": original,
                "num_subpics": len(
                    subpic_ids
                ),
                "subpic_ids": subpic_ids,
                "num_pictures": len(
                    pictures
                ),
                "picture_tid_count": dict(
                    sorted(
                        pic_tid_count.items()
                    )
                ),
                "subpictures": summary,
                "drop_test": drop_result,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


    # ========================================================
    # Final summary
    # ========================================================

    print(
        "\n" + "=" * 70
    )

    print("SUMMARY")

    print("=" * 70)

    print(
        f"Detected subpictures : "
        f"{len(subpic_ids)} "
        f"{subpic_ids}"
    )

    print(
        f"Detected pictures    : "
        f"{len(pictures)}"
    )

    print(
        f"Picture TIds         : "
        f"{dict(sorted(pic_tid_count.items()))}"
    )

    all_extract = all(
        x["extract"]
        for x in summary
    )

    all_decode = all(
        x["decode"]
        for x in summary
    )

    print(
        f"Subpic extraction    : "
        f"{'SUCCESS' if all_extract else 'FAILED'}"
    )

    print(
        f"Subpic decoding      : "
        f"{'SUCCESS' if all_decode else 'FAILED'}"
    )

    if DROP_ENABLE:

        print(
            f"SP{DROP_SUBPIC} "
            f"T{DROP_TID} drop   : "
            f"{'SUCCESS' if drop_result['decode'] else 'FAILED'}"
        )

        if (
            drop_result.get("blackfill")
            is not None
        ):

            black = (
                drop_result[
                    "blackfill"
                ]
            )

            print(
                f"Black-fill frames    : "
                f"{black['black_frames']}"
            )

            print(
                f"Restored frames      : "
                f"{black['restored_frames']}"
            )

            print(
                f"Black-fill YUV       : "
                f"{black['output_yuv']}"
            )

    print(
        f"Results              : "
        f"{OUTPUT_DIR}"
    )

    print(
        f"Summary              : "
        f"{summary_file}"
    )


if __name__ == "__main__":
    main()