import cv2
import csv
import json
import numpy as np
from pathlib import Path
from ultralytics import YOLO


# ============================================================
# Configuration
# ============================================================

ROOT = Path("/home/yuanzn/Documents/QUIC_network/video_data")

VIDEO_LIST = [
    "BasketballDrive",
    "BQTerrace",
    "Cactus",
    "Kimono",
]

ORIGINAL_DIR = "original"
BITSTREAM_DIR = "bitstream"
METADATA_DIR = "metadata"
OUTPUT_DIR = "semantic_features"

VIDEO_EXT = ".mp4"
BITSTREAM_EXT = ".vvc"
SUBPIC_METADATA_NAME = "subpictures.json"

MODEL_NAME = "yolov8n.pt"
YOLO_CONF = 0.25
YOLO_CLASSES = None


# ============================================================
# Path
# ============================================================

def build_video_paths(video_name):
    video_root = ROOT / video_name
    output_dir = video_root / OUTPUT_DIR

    return {
        "video_root": video_root,
        "original": video_root / ORIGINAL_DIR / f"{video_name}{VIDEO_EXT}",
        "bitstream": video_root / BITSTREAM_DIR / f"{video_name}{BITSTREAM_EXT}",
        "metadata": video_root / METADATA_DIR / SUBPIC_METADATA_NAME,
        "output_dir": output_dir,
        "output_csv": output_dir / "subpicture_features.csv",
    }


# ============================================================
# Subpicture Metadata
# ============================================================

def load_subpictures(metadata_path):
    if not metadata_path.exists():
        raise FileNotFoundError(f"Subpicture metadata not found: {metadata_path}")

    with open(metadata_path, "r") as f:
        data = json.load(f)

    if "subpictures" not in data:
        raise ValueError(f"'subpictures' not found in: {metadata_path}")

    subpictures = []

    for idx, subpic in enumerate(data["subpictures"]):
        x = int(subpic["x"])
        y = int(subpic["y"])
        w = int(subpic["width"])
        h = int(subpic["height"])

        subpictures.append({
            "subpic_id": subpic.get("subpic_id", idx),
            "x1": x, "y1": y,
            "x2": x + w, "y2": y + h,
        })

    return subpictures


# ============================================================
# Geometry
# ============================================================

def intersection_area(box_a, box_b):
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b

    x1, y1 = max(ax1, bx1), max(ay1, by1)
    x2, y2 = min(ax2, bx2), min(ay2, by2)

    if x2 <= x1 or y2 <= y1:
        return 0.0

    return float((x2 - x1) * (y2 - y1))


# ============================================================
# YOLO
# ============================================================

def extract_detections(result):
    if result.boxes is None:
        return []

    boxes = result.boxes.xyxy.cpu().numpy()
    confs = result.boxes.conf.cpu().numpy()
    classes = result.boxes.cls.cpu().numpy()

    detections = []

    for box, conf, cls_id in zip(boxes, confs, classes):
        x1, y1, x2, y2 = box

        detections.append({
            "x1": float(x1), "y1": float(y1),
            "x2": float(x2), "y2": float(y2),
            "conf": float(conf),
            "class_id": int(cls_id),
        })

    return detections


# ============================================================
# Object Feature
# ============================================================

def compute_object_feature(subpic, detections):
    sx1, sy1, sx2, sy2 = subpic["x1"], subpic["y1"], subpic["x2"], subpic["y2"]

    subpic_box = (sx1, sy1, sx2, sy2)
    subpic_area = float((sx2 - sx1) * (sy2 - sy1))

    total_object_area = 0.0
    weighted_object_area = 0.0
    object_count = 0
    max_conf = 0.0

    for det in detections:
        det_box = (det["x1"], det["y1"], det["x2"], det["y2"])
        overlap = intersection_area(subpic_box, det_box)

        if overlap <= 0:
            continue

        object_count += 1
        total_object_area += overlap
        weighted_object_area += overlap * det["conf"]
        max_conf = max(max_conf, det["conf"])

    if subpic_area > 0:
        object_area_ratio = total_object_area / subpic_area
        weighted_object_ratio = weighted_object_area / subpic_area
    else:
        object_area_ratio = 0.0
        weighted_object_ratio = 0.0

    return {
        "object_count": object_count,
        "object_area_ratio": min(object_area_ratio, 1.0),
        "weighted_object_ratio": min(weighted_object_ratio, 1.0),
        "max_conf": max_conf,
    }


# ============================================================
# Temporal Change
# ============================================================

def compute_temporal_change(current_frame, previous_frame, subpic):
    if previous_frame is None:
        return 0.0

    x1, y1, x2, y2 = subpic["x1"], subpic["y1"], subpic["x2"], subpic["y2"]

    curr = current_frame[y1:y2, x1:x2]
    prev = previous_frame[y1:y2, x1:x2]

    if curr.size == 0 or prev.size == 0:
        return 0.0

    curr_gray = cv2.cvtColor(curr, cv2.COLOR_BGR2GRAY)
    prev_gray = cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY)

    return float(np.mean(cv2.absdiff(curr_gray, prev_gray)) / 255.0)


# ============================================================
# Validation
# ============================================================

def validate_subpictures(subpictures, frame_width, frame_height):
    for s in subpictures:
        x1, y1, x2, y2 = s["x1"], s["y1"], s["x2"], s["y2"]

        if x1 < 0 or y1 < 0 or x2 > frame_width or y2 > frame_height or x2 <= x1 or y2 <= y1:
            raise ValueError(f"Invalid subpicture {s['subpic_id']}: {s}")


# ============================================================
# Process One Video
# ============================================================

def process_video(video_name, model):
    paths = build_video_paths(video_name)

    print("\n" + "=" * 70)
    print(f"Video     : {video_name}")
    print(f"Original  : {paths['original']}")
    print(f"Bitstream : {paths['bitstream']}")
    print(f"Metadata  : {paths['metadata']}")
    print(f"Output    : {paths['output_csv']}")
    print("=" * 70)

    if not paths["original"].exists():
        print(f"[ERROR] Original video not found: {paths['original']}")
        return

    if not paths["bitstream"].exists():
        print(f"[WARNING] Bitstream not found: {paths['bitstream']}")

    try:
        subpictures = load_subpictures(paths["metadata"])
    except Exception as e:
        print(f"[ERROR] Failed to load metadata: {e}")
        return

    cap = cv2.VideoCapture(str(paths["original"]))

    if not cap.isOpened():
        print(f"[ERROR] Cannot open video: {paths['original']}")
        return

    frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(cap.get(cv2.CAP_PROP_FPS))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    validate_subpictures(subpictures, frame_width, frame_height)

    print(f"Resolution : {frame_width} x {frame_height}")
    print(f"FPS        : {fps:.3f}")
    print(f"Frames     : {total_frames}")
    print(f"Subpictures: {len(subpictures)}")

    paths["output_dir"].mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "video", "frame_idx", "timestamp_sec", "subpic_id",
        "x1", "y1", "x2", "y2",
        "object_count", "object_area_ratio",
        "weighted_object_ratio", "max_conf",
        "temporal_change",
    ]

    previous_frame = None
    frame_idx = 0

    with open(paths["output_csv"], "w", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()

        while True:
            ret, frame = cap.read()

            if not ret:
                break

            result = model.predict(
                source=frame,
                conf=YOLO_CONF,
                classes=YOLO_CLASSES,
                verbose=False,
            )[0]

            detections = extract_detections(result)

            for subpic in subpictures:
                obj = compute_object_feature(subpic, detections)
                change = compute_temporal_change(frame, previous_frame, subpic)

                writer.writerow({
                    "video": video_name,
                    "frame_idx": frame_idx,
                    "timestamp_sec": frame_idx / fps if fps > 0 else 0.0,
                    "subpic_id": subpic["subpic_id"],
                    "x1": subpic["x1"],
                    "y1": subpic["y1"],
                    "x2": subpic["x2"],
                    "y2": subpic["y2"],
                    "object_count": obj["object_count"],
                    "object_area_ratio": obj["object_area_ratio"],
                    "weighted_object_ratio": obj["weighted_object_ratio"],
                    "max_conf": obj["max_conf"],
                    "temporal_change": change,
                })

            previous_frame = frame.copy()
            frame_idx += 1

            if frame_idx % 100 == 0 or frame_idx == total_frames:
                print(f"Processed: {frame_idx}/{total_frames}")

    cap.release()

    print(f"[DONE] Saved: {paths['output_csv']}")


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