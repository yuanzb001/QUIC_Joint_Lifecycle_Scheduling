#!/usr/bin/env python3

import cv2
import numpy as np
from pathlib import Path


# ============================================================
# Configuration
# ============================================================

SEMANTIC_ROOT = Path(
    "/home/yuanzn/Documents/QUIC_Joint_Lifecycle_Scheduling/"
    "data/semantic_feature"
)

DATASETS = [
    "HEVC_CTC_B",
    "Dataset2k",
]

NUM_SUBPICS = 9


# ============================================================
# Subpicture geometry
# ============================================================

def make_subpic_boxes(width, height, num_subpics):
    grid = int(round(np.sqrt(num_subpics)))

    if grid * grid != num_subpics:
        raise ValueError(
            f"num_subpics must form square grid, got {num_subpics}"
        )

    xs = np.linspace(0, width, grid + 1, dtype=np.int32)
    ys = np.linspace(0, height, grid + 1, dtype=np.int32)

    return [
        (int(xs[x]), int(ys[y]), int(xs[x + 1]), int(ys[y + 1]))
        for y in range(grid)
        for x in range(grid)
    ]


# ============================================================
# Per-SP ratio
# ============================================================

def subpic_ratio(mask, boxes):
    out = np.zeros(len(boxes), dtype=np.float32)

    for sid, (x1, y1, x2, y2) in enumerate(boxes):
        region = mask[y1:y2, x1:x2]

        if region.size:
            out[sid] = float(region.mean())

    return out


# ============================================================
# One video
# ============================================================

def process_video(video_dir):
    mask_dir = video_dir / "masks"

    if not mask_dir.exists():
        print(f"[SKIP] No mask directory: {mask_dir}")
        return

    mask_files = sorted(
        p for p in mask_dir.iterdir()
        if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg"}
    )

    if not mask_files:
        print(f"[SKIP] No masks: {mask_dir}")
        return

    first = cv2.imread(
        str(mask_files[0]),
        cv2.IMREAD_GRAYSCALE
    )

    if first is None:
        raise RuntimeError(
            f"Failed to read mask: {mask_files[0]}"
        )

    height, width = first.shape
    boxes = make_subpic_boxes(
        width,
        height,
        NUM_SUBPICS
    )

    nframes = len(mask_files)

    roi = np.zeros(
        (nframes, NUM_SUBPICS),
        dtype=np.float32
    )

    change = np.zeros(
        (nframes, NUM_SUBPICS),
        dtype=np.float32
    )

    global_roi = np.zeros(
        nframes,
        dtype=np.float32
    )

    global_change = np.zeros(
        nframes,
        dtype=np.float32
    )

    frame_id = np.arange(
        nframes,
        dtype=np.int32
    )

    prev_mask = None

    print("\n" + "=" * 70)
    print(f"[VIDEO] {video_dir.name}")
    print(f"[MASK] {mask_dir}")
    print(f"[FRAMES] {nframes}")
    print(f"[SIZE] {width}x{height}")
    print(f"[SP] {NUM_SUBPICS}")
    print("=" * 70)

    for fid, mask_path in enumerate(mask_files):
        mask = cv2.imread(
            str(mask_path),
            cv2.IMREAD_GRAYSCALE
        )

        if mask is None:
            raise RuntimeError(
                f"Failed to read mask: {mask_path}"
            )

        mask = (
            mask > 127
        ).astype(np.uint8)

        # ROI
        roi[fid] = subpic_ratio(
            mask,
            boxes
        )

        global_roi[fid] = float(
            mask.mean()
        )

        # Change
        if prev_mask is None:
            diff = np.zeros_like(
                mask,
                dtype=np.uint8
            )
        else:
            diff = (
                mask != prev_mask
            ).astype(np.uint8)

        change[fid] = subpic_ratio(
            diff,
            boxes
        )

        global_change[fid] = float(
            diff.mean()
        )

        prev_mask = mask

        print(
            f"[{fid + 1:4d}/{nframes:4d}] "
            f"{mask_path.name} "
            f"roi={global_roi[fid]:.4f} "
            f"change={global_change[fid]:.4f}"
        )

    output_path = (
        video_dir
        / "semantic_features.npz"
    )

    np.savez_compressed(
        output_path,

        frame_id=frame_id,

        roi=roi,
        change=change,

        global_roi=global_roi,
        global_change=global_change,

        width=np.int32(width),
        height=np.int32(height),
        num_subpics=np.int32(NUM_SUBPICS),

        subpic_boxes=np.asarray(
            boxes,
            dtype=np.int32
        ),
    )

    print(
        f"[SAVE] {output_path}"
    )

    print(
        f"[SHAPE] "
        f"roi={roi.shape} "
        f"change={change.shape}"
    )


# ============================================================
# One dataset
# ============================================================

def process_dataset(dataset_name):
    dataset_root = (
        SEMANTIC_ROOT
        / dataset_name
    )

    if not dataset_root.exists():
        print(
            f"[SKIP] Dataset not found: "
            f"{dataset_root}"
        )
        return

    video_dirs = sorted(
        p for p in dataset_root.iterdir()
        if p.is_dir()
    )

    print("\n" + "#" * 70)
    print(f"[DATASET] {dataset_name}")
    print(f"[VIDEOS] {len(video_dirs)}")
    print("#" * 70)

    for video_dir in video_dirs:
        process_video(video_dir)


# ============================================================
# Main
# ============================================================

def main():
    for dataset_name in DATASETS:
        process_dataset(
            dataset_name
        )

    print(
        "\n[DONE] "
        "Semantic features completed."
    )


if __name__ == "__main__":
    main()