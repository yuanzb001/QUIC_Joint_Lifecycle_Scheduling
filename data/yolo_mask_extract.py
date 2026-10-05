#!/usr/bin/env python3

import cv2
import numpy as np

from pathlib import Path
from ultralytics import YOLO


# ============================================================
# Configuration
# ============================================================

FRAME_ROOTS={
    # "HEVC_CTC_B":Path(
    #     "/share/HP_dataset/HEVC_CTC_B/Frames"
    # ),

    "Dataset2K":Path(
        "/share/HP_dataset/Dataset2k/Frames"
    ),
}

OUTPUT_ROOT=Path(
    "/home/yuanzn/Documents/QUIC_Joint_Lifecycle_Scheduling/"
    "data/semantic_feature"
)

MODEL_NAME="yolov8n-seg.pt"
CONF=0.25
CLASSES=None

IMAGE_EXTS={".png",".jpg",".jpeg"}


# ============================================================
# Video name
#
# BasketballDrive_1920x1080_50 -> BasketballDrive
# ============================================================

def get_video_name(folder_name):
    parts=folder_name.rsplit("_",2)

    if (
        len(parts)==3
        and "x" in parts[-2]
        and parts[-1].isdigit()
    ):
        return parts[0]

    return folder_name


# ============================================================
# YOLO segmentation -> binary union mask
# ============================================================

def extract_union_mask(model,image_path):
    frame=cv2.imread(str(image_path))

    if frame is None:
        raise RuntimeError(
            f"Failed to read: {image_path}"
        )

    h,w=frame.shape[:2]

    result=model.predict(
        source=frame,
        conf=CONF,
        classes=CLASSES,
        verbose=False
    )[0]

    if (
        result.masks is None
        or len(result.masks.data)==0
    ):
        return np.zeros(
            (h,w),
            dtype=np.uint8
        )

    masks=(
        result.masks.data
        .cpu()
        .numpy()
    )

    union=np.any(
        masks>0.5,
        axis=0
    ).astype(np.uint8)

    if union.shape!=(h,w):
        union=cv2.resize(
            union,
            (w,h),
            interpolation=cv2.INTER_NEAREST
        )

    return union


# ============================================================
# Process one video
# ============================================================

def process_video(
    model,
    dataset_name,
    frame_dir
):
    video_name=get_video_name(
        frame_dir.name
    )

    output_dir=(
        OUTPUT_ROOT
        /dataset_name
        /video_name
    )

    mask_dir=output_dir/"masks"

    mask_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    frame_files=sorted(
        p
        for p in frame_dir.iterdir()
        if (
            p.is_file()
            and p.suffix.lower() in IMAGE_EXTS
        )
    )

    if not frame_files:
        print(
            f"[SKIP] No frames: "
            f"{frame_dir}"
        )
        return

    print("\n"+"="*70)
    print(
        f"[DATASET] {dataset_name}"
    )
    print(
        f"[VIDEO] {video_name}"
    )
    print(
        f"[INPUT] {frame_dir}"
    )
    print(
        f"[OUTPUT] {mask_dir}"
    )
    print(
        f"[FRAMES] {len(frame_files)}"
    )
    print("="*70)

    for idx,frame_path in enumerate(
        frame_files
    ):
        output_path=(
            mask_dir
            /f"{frame_path.stem}.png"
        )

        if output_path.exists():
            print(
                f"[{idx+1:4d}/"
                f"{len(frame_files):4d}] "
                f"{frame_path.name} "
                f"-> exists"
            )
            continue

        mask=extract_union_mask(
            model,
            frame_path
        )

        ok=cv2.imwrite(
            str(output_path),
            mask*255
        )

        if not ok:
            raise RuntimeError(
                f"Failed to save: "
                f"{output_path}"
            )

        roi_ratio=float(mask.mean())

        print(
            f"[{idx+1:4d}/"
            f"{len(frame_files):4d}] "
            f"{frame_path.name} "
            f"roi={roi_ratio:.4f}"
        )


# ============================================================
# Process one dataset
# ============================================================

def process_dataset(
    model,
    dataset_name,
    frame_root
):
    if not frame_root.exists():
        print(
            f"[SKIP] Dataset root not found: "
            f"{frame_root}"
        )
        return

    video_dirs=sorted(
        p
        for p in frame_root.iterdir()
        if p.is_dir()
    )

    if not video_dirs:
        print(
            f"[SKIP] No video folders: "
            f"{frame_root}"
        )
        return

    print("\n"+"#"*70)
    print(
        f"[DATASET] {dataset_name}"
    )
    print(
        f"[ROOT] {frame_root}"
    )
    print(
        f"[VIDEOS] {len(video_dirs)}"
    )
    print("#"*70)

    for frame_dir in video_dirs:
        process_video(
            model,
            dataset_name,
            frame_dir
        )


# ============================================================
# Main
# ============================================================

def main():
    OUTPUT_ROOT.mkdir(
        parents=True,
        exist_ok=True
    )

    print(
        f"[MODEL] Loading "
        f"{MODEL_NAME}"
    )

    model=YOLO(
        MODEL_NAME
    )

    print(
        f"[OUTPUT ROOT] "
        f"{OUTPUT_ROOT}"
    )

    for (
        dataset_name,
        frame_root
    ) in FRAME_ROOTS.items():

        process_dataset(
            model,
            dataset_name,
            frame_root
        )

    print(
        "\n[DONE] "
        "All semantic masks completed."
    )


if __name__=="__main__":
    main()