#!/usr/bin/env python3
import csv, subprocess
from pathlib import Path
from collections import defaultdict

# ============================================================
# Configuration
# ============================================================

ROOT = Path("/home/yuanzn/Documents/QUIC_Joint_Lifecycle_Scheduling")
RECV = ROOT / "python/output_res/vvc_receiver"
DECODER = ROOT / "VVCSoftware_VTM/bin/DecoderAppStatic"
OUTPUT = RECV / "decoded_subpics"

COMMON_STREAM = 1
SUBPIC_STREAMS = {0: 2, 1: 3, 2: 4, 3: 5}
DO_DECODE = True


# ============================================================
# Load received objects
# ============================================================

def load_objects():
    objects = []

    with open(RECV / "received_objects.csv", newline="") as f:
        reader = csv.DictReader(f)
        print(f"[CSV] header={reader.fieldnames}")

        for line_no, row in enumerate(reader, start=2):
            path = Path(row["file"])

            if not path.is_absolute():
                path = RECV / path

            if not path.exists():
                raise FileNotFoundError(
                    f"Object file missing at CSV line {line_no}: {path}"
                )

            objects.append({
                "seq": int(row["object_seq"]),
                "stream": int(row["stream_id"]),
                "frame": int(row["frame_id"]),
                "subpic": int(row["subpic_id"]),
                "type": int(row["object_type"]),
                "size": int(row["payload_len"]),
                "frag": int(row["fragment_idx"]),
                "frag_count": int(row["fragment_count"]),
                "path": path,
            })

    objects.sort(key=lambda x: x["seq"])

    print(f"[CSV] Loaded {len(objects)} received objects")
    return objects


def read_parts(parts):
    return b"".join(
        x["path"].read_bytes()
        for x in sorted(parts, key=lambda x: x["frag"])
    )


# ============================================================
# Separate configs + SP frames
# ============================================================

def organize(objects):
    configs = {}
    frames = defaultdict(lambda: defaultdict(list))

    # Config objects
    config_parts = defaultdict(list)

    for x in objects:
        if x["type"] == 1:
            config_parts[x["subpic"]].append(x)

        elif x["type"] == 2:
            frames[x["subpic"]][x["frame"]].append(x)

    for spid, parts in config_parts.items():
        configs[spid] = read_parts(parts)

    print("\n[CONFIG]")
    for spid in sorted(configs):
        print(f"SP{spid}: {len(configs[spid]):,} B")

    print("\n[MEDIA]")
    for spid in sorted(frames):
        fids = sorted(frames[spid])
        print(
            f"SP{spid}: frames={len(fids)} "
            f"range={fids[0]}..{fids[-1]}"
        )

    return configs, frames


# ============================================================
# Reconstruction
# ============================================================

def reconstruct(configs, frames, spid):
    output = OUTPUT / f"subpic_{spid}.vvc"

    if spid not in configs:
        raise RuntimeError(f"Missing config for SP{spid}")
    if spid not in frames:
        raise RuntimeError(f"Missing media frames for SP{spid}")

    frame_ids = sorted(frames[spid])

    # Check frame continuity
    expected = list(range(frame_ids[0], frame_ids[-1] + 1))
    missing = sorted(set(expected) - set(frame_ids))

    if missing:
        print(
            f"[WARN] SP{spid}: missing {len(missing)} frames: "
            f"{missing[:20]}{'...' if len(missing) > 20 else ''}"
        )

    with output.open("wb") as f:
        # VPS/SPS/PPS once
        f.write(configs[spid])

        # PH + VCL + remaining per-frame NALs
        for fid in frame_ids:
            f.write(read_parts(frames[spid][fid]))

    media_bytes = sum(
        sum(x["path"].stat().st_size for x in frames[spid][fid])
        for fid in frame_ids
    )

    print(
        f"[RECON] SP{spid}: "
        f"config={len(configs[spid]):,} B | "
        f"frames={len(frame_ids)} | "
        f"media={media_bytes:,} B | "
        f"total={output.stat().st_size:,} B"
    )

    return output


# ============================================================
# Decode verification
# ============================================================

def decode(spid, bitstream):
    yuv = OUTPUT / f"subpic_{spid}.yuv"
    log = OUTPUT / f"subpic_{spid}_decode.log"

    if yuv.exists():
        yuv.unlink()

    r = subprocess.run(
        [str(DECODER), "-b", str(bitstream), "-o", str(yuv)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )

    log.write_text(r.stdout)

    ok = (
        r.returncode == 0
        and yuv.exists()
        and yuv.stat().st_size > 0
    )

    print(
        f"[DECODE] SP{spid}: "
        f"{'SUCCESS' if ok else 'FAILED'} "
        f"(rc={r.returncode})"
    )

    if not ok:
        print(f"         log={log}")

    return ok


# ============================================================
# Main
# ============================================================

def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)

    if DO_DECODE and not DECODER.exists():
        raise FileNotFoundError(DECODER)

    objects = load_objects()
    configs, frames = organize(objects)

    print("\n" + "=" * 70)
    print("RECONSTRUCT + DECODE RECEIVED SUBPICTURES")
    print("=" * 70)

    results = {}

    for spid in sorted(SUBPIC_STREAMS):
        print(f"\n[SP{spid}]")

        # CFG_i + received SP_i frames
        bitstream = reconstruct(configs, frames, spid)

        # Decode reconstructed VVC bitstream
        results[spid] = decode(spid, bitstream)

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    for spid in sorted(results):
        bitstream = OUTPUT / f"subpic_{spid}.vvc"
        yuv = OUTPUT / f"subpic_{spid}.yuv"

        print(
            f"SP{spid}: {'SUCCESS' if results[spid] else 'FAILED'} | "
            f"VVC={bitstream.stat().st_size:,} B | "
            f"YUV={yuv.stat().st_size:,} B"
            if results[spid]
            else f"SP{spid}: FAILED"
        )

    print(f"\nOutput: {OUTPUT}")


if __name__ == "__main__":
    main()