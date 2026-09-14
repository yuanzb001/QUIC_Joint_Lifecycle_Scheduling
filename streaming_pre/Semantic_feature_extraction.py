import cv2, csv, json, re, subprocess
import numpy as np
from pathlib import Path
from ultralytics import YOLO

# ============================================================
# Configuration
# ============================================================

FRAME_ROOT = Path("/home/yuanzn/Dataset/VidSTG-Dataset/200_sample_video/train")
VIDEO_ROOT = Path("/home/yuanzn/Dataset/VidSTG-Dataset/training_video")
VVC_ROOT = Path("/home/yuanzn/Documents/QUIC_Joint_Lifecycle_Scheduling/data/vvc_bitstream")
METADATA_ROOT = Path("/home/yuanzn/Documents/QUIC_Joint_Lifecycle_Scheduling/data/subpicture_metadata")
OUTPUT_ROOT = Path("/home/yuanzn/Documents/QUIC_Joint_Lifecycle_Scheduling/data/semantic_feature")
VTM_DECODER = Path("/home/yuanzn/Documents/QUIC_Joint_Lifecycle_Scheduling/VVCSoftware_VTM/bin/DecoderAppStatic")
VTM_TRACE_ROOT = METADATA_ROOT / "vtm_trace"

# VIDEO_LIST = ["10274570313"]
VIDEO_LIST = [
    "10274570313", "10607074085", "11425947806", "11585084764", "11784274676",
    "11971581374", "11976171036", "13028692904", "13313949275", "13495723174",
    "2401441565", "2484698353", "2507835175", "2558930195", "2564866084",
    "2622813876", "2682709910", "2713525734", "2759318164", "2771309743",
]

FRAME_DIR = "original"
VIDEO_EXT = ".mp4"
BITSTREAM_EXT = ".vvc"
SUBPIC_METADATA_NAME = "_subpictures.json"

MODEL_NAME = "yolov8n.pt"
YOLO_CONF = 0.25
YOLO_CLASSES = None


# ============================================================
# Paths
# ============================================================

def build_video_paths(video_name):
    out = OUTPUT_ROOT / video_name
    return {
        "frame_dir": FRAME_ROOT / video_name / FRAME_DIR,
        "video": VIDEO_ROOT / f"{video_name}{VIDEO_EXT}",
        "bitstream": VVC_ROOT / f"{video_name}{BITSTREAM_EXT}",
        "metadata": METADATA_ROOT / f"{video_name}{SUBPIC_METADATA_NAME}",
        "output_dir": out,
        "output_csv": out / "subpicture_features.csv",
        "output_yolo_json": out / "yolo_detections.json",
    }


def natural_sort_key(path):
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", path.name)]


def get_frame_files(frame_dir):
    return sorted(frame_dir.glob("*.png"), key=natural_sort_key)


def get_frame_number(frame_path, fallback_idx):
    nums = re.findall(r"\d+", frame_path.stem)
    return int(nums[-1]) if nums else fallback_idx


# ============================================================
# Video -> FPS only
# ============================================================

def read_video_fps(video_path):
    if not video_path.exists():
        raise FileNotFoundError(f"Video not found: {video_path}")

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    fps = float(cap.get(cv2.CAP_PROP_FPS))
    cap.release()

    if fps <= 0:
        raise RuntimeError(f"Invalid FPS: {video_path}")

    return fps


# ============================================================
# VTM Trace
# ============================================================

def run_vtm_trace(bitstream_path, trace_path):
    if not bitstream_path.exists():
        raise FileNotFoundError(f"Bitstream not found: {bitstream_path}")

    trace_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(VTM_DECODER),
        "-b", str(bitstream_path),
        f"--TraceFile={trace_path}",
        "--TraceRule=D_HEADER:poc>=0",
    ]

    print("[VTM]", " ".join(cmd))
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    if result.returncode != 0:
        raise RuntimeError(f"VTM Decoder failed:\n{result.stdout}")


def find_trace_value(text, key):
    m = re.search(rf"^{re.escape(key)}.*?:\s*(-?\d+)\s*$", text, re.MULTILINE)
    return int(m.group(1)) if m else None


def find_trace_values(text, key):
    pattern = rf"^{re.escape(key)}(?:\s*\[\s*i\s*\])?.*?:\s*(-?\d+)\s*$"
    return [int(v) for v in re.findall(pattern, text, re.MULTILINE)]


def get_vvc_frame_count(text):
    return text.count("=========== Picture Header ===========")

def get_first_sps_block(text):
    start = text.find("=========== Sequence Parameter Set  ===========")
    if start < 0:
        raise RuntimeError("Cannot find Sequence Parameter Set in VTM trace")

    end = text.find("===========", start + len("=========== Sequence Parameter Set  ==========="))
    return text[start:] if end < 0 else text[start:end]

# ============================================================
# VVC -> Subpicture Metadata
# ============================================================

def parse_subpictures_from_trace(text, width, height):
    sps = get_first_sps_block(text)

    num_minus1 = find_trace_value(sps, "sps_num_subpics_minus1")
    log2_ctu = find_trace_value(sps, "sps_log2_ctu_size_minus5")

    if num_minus1 is None:
        raise RuntimeError("Cannot find sps_num_subpics_minus1")
    if log2_ctu is None:
        raise RuntimeError("Cannot find sps_log2_ctu_size_minus5")

    n = num_minus1 + 1
    ctu = 1 << (log2_ctu + 5)
    pic_w_ctu = (width + ctu - 1) // ctu
    pic_h_ctu = (height + ctu - 1) // ctu

    xs = find_trace_values(sps, "sps_subpic_ctu_top_left_x")
    ys = find_trace_values(sps, "sps_subpic_ctu_top_left_y")
    ws = find_trace_values(sps, "sps_subpic_width_minus1")
    hs = find_trace_values(sps, "sps_subpic_height_minus1")

    if len(xs) != n - 1 or len(ys) != n - 1:
        raise RuntimeError(f"Unexpected top-left count: x={len(xs)}, y={len(ys)}, expected={n - 1}")
    if len(ws) != n - 1 or len(hs) != n - 1:
        raise RuntimeError(f"Unexpected size count: w={len(ws)}, h={len(hs)}, expected={n - 1}")

    subpictures = []

    for i in range(n):
        x_ctu, y_ctu = (0, 0) if i == 0 else (xs[i - 1], ys[i - 1])

        if i < n - 1:
            w_ctu, h_ctu = ws[i] + 1, hs[i] + 1
        else:
            w_ctu, h_ctu = pic_w_ctu - x_ctu, pic_h_ctu - y_ctu

        x, y = x_ctu * ctu, y_ctu * ctu
        w = min(w_ctu * ctu, width - x)
        h = min(h_ctu * ctu, height - y)

        if w <= 0 or h <= 0:
            raise RuntimeError(f"Invalid subpicture {i}: x={x}, y={y}, w={w}, h={h}")

        subpictures.append({
            "subpic_id": i,
            "x": int(x), "y": int(y),
            "width": int(w), "height": int(h),
        })

    return subpictures, ctu


def generate_subpicture_json_from_vtm(bitstream_path, metadata_path):
    trace_path = VTM_TRACE_ROOT / f"{bitstream_path.stem}_trace.txt"

    print(f"[INFO] Generating subpicture metadata from VTM: {bitstream_path.name}")
    run_vtm_trace(bitstream_path, trace_path)

    text = trace_path.read_text(errors="ignore")

    sps = get_first_sps_block(text)

    width = find_trace_value(sps, "sps_pic_width_max_in_luma_samples")
    height = find_trace_value(sps, "sps_pic_height_max_in_luma_samples")
    num_frames = get_vvc_frame_count(text)

    if width is None or height is None:
        raise RuntimeError(f"Cannot determine VVC resolution: {trace_path}")
    if num_frames <= 0:
        raise RuntimeError(f"Cannot determine VVC frame count: {trace_path}")

    subpictures, ctu = parse_subpictures_from_trace(text, width, height)

    data = {
        "frame_width": width,
        "frame_height": height,
        "num_frames": num_frames,
        "ctu_size": ctu,
        "num_subpictures": len(subpictures),
        "source_bitstream": str(bitstream_path),
        "subpictures": subpictures,
    }

    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    with open(metadata_path, "w") as f:
        json.dump(data, f, indent=2)

    print(f"[DONE] Generated metadata: {metadata_path}")
    print(f"  VVC frames : {num_frames}")

    for s in subpictures:
        print(f"  Subpic {s['subpic_id']}: x={s['x']} y={s['y']} w={s['width']} h={s['height']}")


def load_metadata(metadata_path):
    with open(metadata_path, "r") as f:
        data = json.load(f)

    required = ["frame_width", "frame_height", "num_frames", "subpictures"]
    if any(k not in data for k in required):
        raise ValueError(f"Invalid metadata: {metadata_path}")

    subpictures = []

    for i, s in enumerate(data["subpictures"]):
        x, y = int(s["x"]), int(s["y"])
        w, h = int(s["width"]), int(s["height"])

        subpictures.append({
            "subpic_id": s.get("subpic_id", i),
            "x1": x, "y1": y,
            "x2": x + w, "y2": y + h,
        })

    return data, subpictures


# ============================================================
# Geometry / YOLO
# ============================================================

def intersection_area(a, b):
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    x1, y1 = max(ax1, bx1), max(ay1, by1)
    x2, y2 = min(ax2, bx2), min(ay2, by2)
    return 0.0 if x2 <= x1 or y2 <= y1 else float((x2 - x1) * (y2 - y1))


def extract_detections(result):
    if result.boxes is None:
        return []

    boxes = result.boxes.xyxy.cpu().numpy()
    confs = result.boxes.conf.cpu().numpy()
    classes = result.boxes.cls.cpu().numpy()

    return [{
        "x1": float(b[0]), "y1": float(b[1]),
        "x2": float(b[2]), "y2": float(b[3]),
        "confidence": float(c), "class_id": int(cls),
    } for b, c, cls in zip(boxes, confs, classes)]


def validate_subpictures(subpictures, width, height):
    for s in subpictures:
        if s["x1"] < 0 or s["y1"] < 0 or s["x2"] > width or s["y2"] > height or s["x2"] <= s["x1"] or s["y2"] <= s["y1"]:
            raise ValueError(f"Invalid subpicture {s['subpic_id']}: {s}")


# ============================================================
# Semantic Features
# ============================================================

def compute_object_feature(subpic, detections):
    box = (subpic["x1"], subpic["y1"], subpic["x2"], subpic["y2"])
    area = float((subpic["x2"] - subpic["x1"]) * (subpic["y2"] - subpic["y1"]))
    total, weighted, count, max_conf = 0.0, 0.0, 0, 0.0

    for det in detections:
        overlap = intersection_area(box, (det["x1"], det["y1"], det["x2"], det["y2"]))
        if overlap <= 0:
            continue

        conf = det["confidence"]
        count += 1
        total += overlap
        weighted += overlap * conf
        max_conf = max(max_conf, conf)

    return {
        "object_count": count,
        "object_area_ratio": min(total / area, 1.0) if area else 0.0,
        "weighted_object_ratio": min(weighted / area, 1.0) if area else 0.0,
        "max_conf": max_conf,
    }


def compute_temporal_change(curr_frame, prev_frame, subpic):
    if prev_frame is None:
        return 0.0

    x1, y1, x2, y2 = subpic["x1"], subpic["y1"], subpic["x2"], subpic["y2"]
    curr = curr_frame[y1:y2, x1:x2]
    prev = prev_frame[y1:y2, x1:x2]

    if curr.size == 0 or prev.size == 0:
        return 0.0

    curr = cv2.cvtColor(curr, cv2.COLOR_BGR2GRAY)
    prev = cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY)
    return float(np.mean(cv2.absdiff(curr, prev)) / 255.0)


# ============================================================
# Process
# ============================================================

def process_video(video_name, model):
    p = build_video_paths(video_name)

    print("\n" + "=" * 70)
    print(f"Video     : {video_name}")
    print(f"Frames    : {p['frame_dir']}")
    print(f"Video     : {p['video']}")
    print(f"Bitstream : {p['bitstream']}")
    print(f"Metadata  : {p['metadata']}")
    print("=" * 70)

    if not p["frame_dir"].exists():
        print(f"[ERROR] Frame directory not found: {p['frame_dir']}")
        return

    frame_files = get_frame_files(p["frame_dir"])
    if not frame_files:
        print(f"[ERROR] No PNG frames found: {p['frame_dir']}")
        return

    try:
        fps = read_video_fps(p["video"])

        # Regenerate metadata so VVC frame count/resolution are always included.
        generate_subpicture_json_from_vtm(p["bitstream"], p["metadata"])

        meta, subpictures = load_metadata(p["metadata"])
        width, height = int(meta["frame_width"]), int(meta["frame_height"])
        vvc_total_frames = int(meta["num_frames"])
        png_total_frames = len(frame_files)

        validate_subpictures(subpictures, width, height)

    except Exception as e:
        print(f"[ERROR] {e}")
        return

    print(f"Resolution  : {width} x {height}")
    print(f"FPS         : {fps:.3f}")
    print(f"VVC frames  : {vvc_total_frames}")
    print(f"PNG frames  : {png_total_frames}")
    print(f"Duration    : {vvc_total_frames / fps:.3f} s")
    print(f"Subpictures : {len(subpictures)}")

    if vvc_total_frames != png_total_frames:
        print("[WARNING] VVC frame count and PNG frame count differ.")

    p["output_dir"].mkdir(parents=True, exist_ok=True)

    fields = [
        "video", "frame_idx", "frame_number", "frame_name", "timestamp_sec",
        "subpic_id", "x1", "y1", "x2", "y2",
        "object_count", "object_area_ratio", "weighted_object_ratio",
        "max_conf", "temporal_change",
    ]

    yolo_json = {
        "video_info": {
            "video_name": video_name,
            "video_path": str(p["video"]),
            "frame_directory": str(p["frame_dir"]),
            "bitstream_path": str(p["bitstream"]),
            "width": width,
            "height": height,
            "fps": fps,
            "vvc_total_frames": vvc_total_frames,
            "png_total_frames": png_total_frames,
            "duration_sec": vvc_total_frames / fps,
        },
        "yolo_config": {
            "model": MODEL_NAME,
            "confidence_threshold": YOLO_CONF,
            "classes": YOLO_CLASSES,
        },
        "frames": [],
    }

    previous_frame = None

    with open(p["output_csv"], "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()

        for frame_idx, frame_path in enumerate(frame_files):
            frame = cv2.imread(str(frame_path))
            if frame is None:
                print(f"[WARNING] Cannot read: {frame_path}")
                continue

            if frame.shape[1] != width or frame.shape[0] != height:
                raise RuntimeError(
                    f"PNG resolution mismatch: {frame_path.name} "
                    f"{frame.shape[1]}x{frame.shape[0]} != VVC {width}x{height}"
                )

            frame_number = get_frame_number(frame_path, frame_idx)
            timestamp_sec = frame_idx / fps

            result = model.predict(
                source=frame,
                conf=YOLO_CONF,
                classes=YOLO_CLASSES,
                verbose=False,
            )[0]

            detections = extract_detections(result)

            yolo_json["frames"].append({
                "frame_idx": frame_idx,
                "frame_number": frame_number,
                "frame_name": frame_path.name,
                "timestamp_sec": timestamp_sec,
                "num_objects": len(detections),
                "detections": detections,
            })

            for subpic in subpictures:
                obj = compute_object_feature(subpic, detections)
                change = compute_temporal_change(frame, previous_frame, subpic)

                writer.writerow({
                    "video": video_name,
                    "frame_idx": frame_idx,
                    "frame_number": frame_number,
                    "frame_name": frame_path.name,
                    "timestamp_sec": timestamp_sec,
                    "subpic_id": subpic["subpic_id"],
                    "x1": subpic["x1"], "y1": subpic["y1"],
                    "x2": subpic["x2"], "y2": subpic["y2"],
                    **obj,
                    "temporal_change": change,
                })

            previous_frame = frame

            if (frame_idx + 1) % 100 == 0 or frame_idx + 1 == png_total_frames:
                print(f"Processed: {frame_idx + 1}/{png_total_frames}")

    with open(p["output_yolo_json"], "w") as f:
        json.dump(yolo_json, f, indent=2)

    print(f"[DONE] CSV : {p['output_csv']}")
    print(f"[DONE] JSON: {p['output_yolo_json']}")


# ============================================================
# Main
# ============================================================

def main():
    print(f"Loading YOLO model: {MODEL_NAME}")
    model = YOLO(MODEL_NAME)

    for video_name in VIDEO_LIST:
        try:
            process_video(video_name, model)
        except Exception as e:
            print(f"[ERROR] {video_name} failed: {e}")

    print("\nAll videos finished.")


if __name__ == "__main__":
    main()