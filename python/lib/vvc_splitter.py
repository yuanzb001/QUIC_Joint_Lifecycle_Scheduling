import json, re, subprocess
from pathlib import Path

VTM_BIN = Path(__file__).resolve().parent.parent.parent / "VVCSoftware_VTM" / "bin"
EXTRACTOR = VTM_BIN / "BitstreamExtractorAppStatic"
DECODER = VTM_BIN / "DecoderAppStatic"

NAL_NAMES = {
    0: "TRAIL", 1: "STSA", 2: "RADL", 3: "RASL", 7: "IDR_W_RADL", 8: "IDR_N_LP",
    9: "CRA", 10: "GDR", 12: "OPI", 13: "DCI", 14: "VPS", 15: "SPS", 16: "PPS",
    17: "PREFIX_APS", 18: "SUFFIX_APS", 19: "PH", 20: "AUD", 21: "EOS", 22: "EOB",
    23: "PREFIX_SEI", 24: "SUFFIX_SEI", 25: "FD",
}


# ============================================================
# Annex-B
# ============================================================

def find_start_codes(data):
    result, i = [], 0
    while i < len(data) - 3:
        if data[i:i+4] == b"\x00\x00\x00\x01":
            result.append((i, 4))
            i += 4
            continue
        if data[i:i+3] == b"\x00\x00\x01":
            result.append((i, 3))
            i += 3
            continue
        i += 1
    return result


def parse_annexb(path):
    data = Path(path).read_bytes()
    starts = find_start_codes(data)

    if not starts:
        raise RuntimeError(f"No Annex-B start code found in {path}")

    nalus = []
    for i, (start, sc_len) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(data)
        payload_start = start + sc_len

        if payload_start + 2 > end:
            continue

        raw = data[start:end]
        nal_type = (data[payload_start + 1] >> 3) & 0x1F

        nalus.append({
            "index": len(nalus),
            "type": nal_type,
            "name": NAL_NAMES.get(nal_type, f"NAL_{nal_type}"),
            "raw": raw,
        })

    return nalus


def get_vcl_nalus(nalus):
    return [n for n in nalus if 0 <= n["type"] <= 11]

# ============================================================
# Transport split: initialization + per-frame SP data
# ============================================================

COMMON_NAL_TYPES = {14, 15, 16}  # VPS, SPS, PPS
PH_NAL_TYPE = 19


def split_transport_units(path):
    """
    Split one VTM-extracted subpicture bitstream into:
      config : VPS/SPS/PPS, sent once through common QUIC stream
      frames : remaining NALUs grouped by PH, sent through SP stream

    PH stays with its corresponding SP frame.
    """
    path = Path(path)
    nalus = parse_annexb(path)

    if not nalus:
        raise RuntimeError(f"No NALUs: {path}")

    config = b"".join(
        n["raw"] for n in nalus
        if n["type"] in COMMON_NAL_TYPES
    )

    media = [
        n for n in nalus
        if n["type"] not in COMMON_NAL_TYPES
    ]

    ph_pos = [i for i, n in enumerate(media) if n["type"] == PH_NAL_TYPE]

    if not ph_pos:
        raise RuntimeError(f"No PH found: {path}")

    frames = []

    for fid, start in enumerate(ph_pos):
        # Any non-config NALUs before first PH belong to first frame.
        begin = 0 if fid == 0 else start
        end = ph_pos[fid + 1] if fid + 1 < len(ph_pos) else len(media)

        frame_nalus = media[begin:end]
        data = b"".join(n["raw"] for n in frame_nalus)

        if not any(0 <= n["type"] <= 11 for n in frame_nalus):
            raise RuntimeError(
                f"No VCL in {path.name}, frame={fid}"
            )

        frames.append({
            "frame_id": fid,
            "data": data,
            "nal_types": [n["type"] for n in frame_nalus],
        })

    print(
        f"[Splitter] {path.name}: "
        f"config={len(config):,} B | "
        f"frames={len(frames)} | "
        f"media={sum(len(x['data']) for x in frames):,} B"
    )

    return {
        "config": config,
        "frames": frames,
    }


def build_transport_split(subpic_files):
    """
    Build transport representation for all extracted subpictures.

    Common QUIC stream:
        config[SP0], config[SP1], ...

    SP QUIC streams:
        SPi frame0, frame1, ...
    """
    units = {
        sid: split_transport_units(path)
        for sid, path in enumerate(subpic_files)
    }

    counts = {sid: len(x["frames"]) for sid, x in units.items()}

    if len(set(counts.values())) != 1:
        raise RuntimeError(f"Subpicture frame-count mismatch: {counts}")

    return {
        "configs": {
            sid: x["config"]
            for sid, x in units.items()
        },
        "frames": {
            sid: x["frames"]
            for sid, x in units.items()
        },
        "frame_count": next(iter(counts.values())),
    }


def nal_payload_without_start_code(raw):
    if raw.startswith(b"\x00\x00\x00\x01"):
        return raw[4:]
    if raw.startswith(b"\x00\x00\x01"):
        return raw[3:]
    return raw


# ============================================================
# VTM Trace
# ============================================================

def find_trace_value(text, key):
    pattern = rf"^{re.escape(key)}.*?:\s*(-?\d+)\s*$"
    m = re.search(pattern, text, re.MULTILINE)
    return int(m.group(1)) if m else None


def find_first_trace_value(text, keys):
    for key in keys:
        value = find_trace_value(text, key)
        if value is not None:
            return value, key
    return None, None


def get_first_sps_block(text):
    marker = "=========== Sequence Parameter Set  ==========="
    start = text.find(marker)

    if start < 0:
        raise RuntimeError("Cannot find Sequence Parameter Set in VTM trace")

    end = text.find("===========", start + len(marker))
    return text[start:] if end < 0 else text[start:end]


def extract_fps_from_trace(text):
    time_scale, time_scale_key = find_first_trace_value(text, [
        "vui_time_scale",
        "general_time_scale",
        "hrd_time_scale",
        "time_scale",
    ])

    num_units, num_units_key = find_first_trace_value(text, [
        "vui_num_units_in_tick",
        "general_num_units_in_tick",
        "hrd_num_units_in_tick",
        "num_units_in_tick",
    ])

    if time_scale is not None and num_units is not None and num_units > 0:
        fps = time_scale / num_units
        print(
            f"[Splitter] Timing: {time_scale_key}={time_scale}, "
            f"{num_units_key}={num_units}, FPS={fps:.3f}"
        )
        return float(fps)

    direct_patterns = [
        r"(?im)^\s*frame[_ ]?rate\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)",
        r"(?im)^\s*frameRate\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)",
        r"(?im)^\s*fps\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)",
    ]

    for pattern in direct_patterns:
        m = re.search(pattern, text)
        if m:
            return float(m.group(1))

    return None


# ============================================================
# VTM -> Bitstream Information
# ============================================================

def inspect_bitstream(input_vvc):
    input_vvc = Path(input_vvc)

    if not input_vvc.exists():
        raise FileNotFoundError(f"VVC bitstream not found: {input_vvc}")

    trace_path = input_vvc.parent / f".{input_vvc.stem}_splitter_trace.txt"
    cmd = [
        str(DECODER), "-b", str(input_vvc),
        f"--TraceFile={trace_path}",
        "--TraceRule=D_HEADER:poc>=0",
    ]

    print(f"[Splitter] Inspecting VVC: {input_vvc.name}")

    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if result.returncode != 0:
        raise RuntimeError(f"DecoderApp failed while inspecting {input_vvc}")
    if not trace_path.exists():
        raise RuntimeError(f"VTM trace not generated: {trace_path}")

    text = trace_path.read_text(errors="ignore")
    sps = get_first_sps_block(text)

    num_minus1 = find_trace_value(sps, "sps_num_subpics_minus1")
    width = find_trace_value(sps, "sps_pic_width_max_in_luma_samples")
    height = find_trace_value(sps, "sps_pic_height_max_in_luma_samples")
    log2_ctu = find_trace_value(sps, "sps_log2_ctu_size_minus5")

    fps = extract_fps_from_trace(text)
    frame_count = text.count("=========== Picture Header ===========")

    if num_minus1 is None:
        raise RuntimeError("Cannot find sps_num_subpics_minus1 in SPS")

    if fps is None:
        print("[Splitter] VVC timing unavailable, FPS=None")

    try:
        trace_path.unlink()
    except OSError:
        pass

    info = {
        "num_subpics": num_minus1 + 1,
        "width": width,
        "height": height,
        "ctu_size": (1 << (log2_ctu + 5)) if log2_ctu is not None else None,
        "fps": fps,
        "frame_count": frame_count,
    }

    fps_str = f"{fps:.3f}" if fps is not None else "N/A"

    print(
        f"[Splitter] VVC info: {width}x{height}, FPS={fps_str}, frames={frame_count}, "
        f"subpictures={info['num_subpics']}, CTU={info['ctu_size']}"
    )

    return info


# ============================================================
# VTM Bitstream Extractor
# ============================================================

def run_extractor(input_vvc, output_vvc, subpic_idx):
    cmd = [
        str(EXTRACTOR), "-b", str(input_vvc), "-o", str(output_vvc),
        f"--SubPicIdx={subpic_idx}",
    ]

    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if result.returncode != 0:
        raise RuntimeError(f"BitstreamExtractorApp failed for SubPicIdx={subpic_idx}")


# ============================================================
# Split: independently decodable extracted subpictures
# ============================================================

def split(input_vvc, output_dir):
    input_vvc, output_dir = Path(input_vvc), Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    info = inspect_bitstream(input_vvc)
    num_subpics = info["num_subpics"]
    fps = info.get("fps")
    fps_str = f"{fps:.3f}" if fps is not None else "N/A"

    print(
        f"[Splitter] VVC={info['width']}x{info['height']}, FPS={fps_str}, "
        f"frames={info['frame_count']}, subpictures={num_subpics}"
    )

    subpic_files = []

    for sid in range(num_subpics):
        out = output_dir / f"subpic_{sid}.vvc"
        print(f"[Splitter] Extracting SP{sid}/{num_subpics - 1} -> {out.name}")

        run_extractor(input_vvc, out, sid)

        if not out.exists() or out.stat().st_size == 0:
            raise RuntimeError(f"Invalid extracted subpicture: {out}")

        nalus = parse_annexb(out)
        vcl = get_vcl_nalus(nalus)

        print(
            f"[Splitter] SP{sid}: "
            f"{out.stat().st_size:,} bytes | "
            f"{len(nalus)} NALUs | {len(vcl)} VCL"
        )

        subpic_files.append(str(out))

    transport = build_transport_split(subpic_files)

    print(f"[Splitter] Split complete: {output_dir}")
    
    return {
        "subpics": subpic_files,
        "configs": transport["configs"],
        "frames": transport["frames"],
        "num_subpics": num_subpics,
        "frame_count": transport["frame_count"],
        "bitstream_info": info,
    }

# ============================================================
# Merge
# ============================================================

def merge(input_dir, output_vvc):
    input_dir = Path(input_dir)
    output_vvc = Path(output_vvc)
    manifest_path = input_dir / "manifest.json"

    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    manifest_data = json.loads(manifest_path.read_text())

    if isinstance(manifest_data, dict):
        manifest = manifest_data["nalus"]
        num_subpics = int(manifest_data["num_subpics"])
    else:
        manifest = manifest_data
        ids = []

        for item in manifest:
            m = re.fullmatch(r"subpic_(\d+)", item["source"])
            if m:
                ids.append(int(m.group(1)))

        num_subpics = max(ids) + 1 if ids else 0

    print(f"[Splitter] Merging {num_subpics} subpictures from {input_dir} -> {output_vvc}")

    streams = {"common": parse_annexb(input_dir / "common.vvc")}

    for sid in range(num_subpics):
        streams[f"subpic_{sid}"] = parse_annexb(input_dir / f"subpic_{sid}.vvc")

    with open(output_vvc, "wb") as out:
        for item in manifest:
            source = item["source"]
            local_index = item["local_index"]
            out.write(streams[source][local_index]["raw"])

    print("[Splitter] Merge complete!")