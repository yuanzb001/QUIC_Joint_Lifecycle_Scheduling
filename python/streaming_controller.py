#!/usr/bin/env python3

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from ultralytics import YOLO

try:
    from stable_baselines3 import PPO
except ImportError:
    PPO = None


# ============================================================
# Config
# ============================================================

ACTIVE_RATIOS = np.array([0.25, 0.50, 0.75, 1.00], dtype=np.float32)
LAMBDAS = np.array([0.00, 0.25, 0.50, 0.75, 1.00], dtype=np.float32)

P_MIN = 100
P_MAX = 60000
EPS = 1e-8


# ============================================================
# Mask Feature Extractor
# Offline YOLO mask + lightweight online feature extraction
# ============================================================

class MaskFeatureExtractor:

    def __init__(self,num_subpics,width,height,mask_dir):
        self.n=int(num_subpics)
        self.w=int(width)
        self.h=int(height)
        self.mask_dir=Path(mask_dir)

        if not self.mask_dir.exists():
            raise RuntimeError(
                f"Mask directory not found: {self.mask_dir}"
            )

        self.boxes=self._make_subpic_boxes()
        self.prev_mask=None
        self.prev_gray=None

    # --------------------------------------------------------
    # Subpicture geometry
    # --------------------------------------------------------

    def _make_subpic_boxes(self):
        # 2x2
        # CTU grid: 15 x 9
        # columns: 7 + 8
        # rows:    4 + 5
        if self.n==4:
            xs=[0,7*128,self.w]
            ys=[0,4*128,self.h]
    
        # 3x3
        # CTU grid: 15 x 9
        # columns: 5 + 5 + 5
        # rows:    3 + 3 + 3
        elif self.n==9:
            xs=[0,5*128,10*128,self.w]
            ys=[0,3*128,6*128,self.h]
    
        # 4x4
        # CTU grid: 15 x 9
        # columns: 4 + 4 + 4 + 3
        # rows:    2 + 2 + 2 + 3
        elif self.n==16:
            xs=[0,4*128,8*128,12*128,self.w]
            ys=[0,2*128,4*128,6*128,self.h]
    
        else:
            raise ValueError(
                f"Unsupported num_subpics={self.n}"
            )
    
        return [
            (xs[x],ys[y],xs[x+1],ys[y+1])
            for y in range(len(ys)-1)
            for x in range(len(xs)-1)
        ]

    # --------------------------------------------------------
    # Load mask
    #
    # fid=0 -> 000001.png
    # --------------------------------------------------------

    def _load_mask(self,fid):
        path=self.mask_dir/f"{fid+1:06d}.png"

        mask=cv2.imread(
            str(path),
            cv2.IMREAD_GRAYSCALE
        )

        if mask is None:
            raise RuntimeError(
                f"Failed to read mask: {path}"
            )

        if mask.shape!=(self.h,self.w):
            mask=cv2.resize(
                mask,
                (self.w,self.h),
                interpolation=cv2.INTER_NEAREST
            )

        return mask>0

    # --------------------------------------------------------
    # Per-SP ROI ratio
    # --------------------------------------------------------

    def _roi_ratio(self,mask):
        out=np.zeros(
            self.n,
            dtype=np.float32
        )

        for sid,(x1,y1,x2,y2) in enumerate(self.boxes):
            region=mask[y1:y2,x1:x2]

            if region.size:
                out[sid]=float(
                    region.mean()
                )

        return out

    # --------------------------------------------------------
    # Per-SP visual content change
    #
    # Grayscale difference inside semantic region
    # --------------------------------------------------------

    def _content_change(self,frame,mask):
        gray=cv2.cvtColor(
            frame,
            cv2.COLOR_BGR2GRAY
        ).astype(np.float32)

        change=np.zeros(
            self.n,
            dtype=np.float32
        )

        if self.prev_gray is None:
            return change,gray

        diff=np.abs(
            gray-self.prev_gray
        )/255.0

        semantic_region=np.logical_or(
            mask,
            self.prev_mask
        )

        for sid,(x1,y1,x2,y2) in enumerate(self.boxes):
            d=diff[y1:y2,x1:x2]
            m=semantic_region[y1:y2,x1:x2]

            if np.any(m):
                change[sid]=float(
                    d[m].mean()
                )

        return change,gray

    # --------------------------------------------------------
    # Per-frame feature extraction
    # --------------------------------------------------------

    def extract(self,fid,frame,frame_bytes):
        mask=self._load_mask(fid)

        roi=self._roi_ratio(
            mask
        )

        change,gray=self._content_change(
            frame,
            mask
        )

        self.prev_mask=mask
        self.prev_gray=gray

        return {
            "roi_ratio":{
                sid:float(roi[sid])
                for sid in range(self.n)
            },
            "content_change":{
                sid:float(change[sid])
                for sid in range(self.n)
            },
            "frame_bytes":{
                sid:int(frame_bytes[sid])
                for sid in range(self.n)
            }
        }

# ============================================================
# Video Feature Extractor
# Per-frame processing
# ============================================================

class VideoFeatureExtractor:

    def __init__(self, num_subpics, width, height, model="yolov8n-seg.pt", conf=0.25, classes=None, device=None):

        self.n = int(num_subpics)

        self.w = int(width)
        self.h = int(height)

        self.model = YOLO(model)

        self.conf = conf
        self.classes = classes
        self.device = device

        self.boxes = self._make_subpic_boxes()

        self.prev_mask = None


    # --------------------------------------------------------
    # SP Geometry
    # --------------------------------------------------------

    def _make_subpic_boxes(self):

        # 2x2 VVC partition
        # CTU grid = 15 x 9
        # columns  = 7 + 8
        # rows     = 4 + 5

        if self.n == 4:

            xs = [0, 7 * 128, self.w]
            ys = [0, 4 * 128, self.h]


        # 4x4 VVC partition
        # columns = 4 + 4 + 4 + 3
        # rows    = 2 + 2 + 2 + 3

        elif self.n == 16:

            xs = [0, 4 * 128, 8 * 128, 12 * 128, self.w]
            ys = [0, 2 * 128, 4 * 128, 6 * 128, self.h]

        else:

            raise ValueError(f"Unsupported num_subpics={self.n}")


        return [
            (xs[x], ys[y], xs[x + 1], ys[y + 1])
            for y in range(len(ys) - 1)
            for x in range(len(xs) - 1)
        ]


    # --------------------------------------------------------
    # YOLO Segmentation -> Binary Union Mask
    # --------------------------------------------------------

    def _get_mask(self, frame):

        result = self.model.predict(
            frame,
            conf=self.conf,
            classes=self.classes,
            retina_masks=True,
            device=self.device,
            verbose=False
        )[0]


        mask = np.zeros((self.h, self.w), dtype=np.uint8)


        if result.masks is None:
            return mask


        for m in result.masks.data.cpu().numpy():

            if m.shape != (self.h, self.w):

                m = cv2.resize(
                    m,
                    (self.w, self.h),
                    interpolation=cv2.INTER_NEAREST
                )


            mask |= (m > 0.5).astype(np.uint8)


        return mask


    # --------------------------------------------------------
    # Mask Ratio for Each SP
    # --------------------------------------------------------

    def _sp_ratio(self, mask):

        out = np.zeros(self.n, dtype=np.float32)


        for sid, (x1, y1, x2, y2) in enumerate(self.boxes):

            x1 = max(0, x1)
            x2 = min(self.w, x2)

            y1 = max(0, y1)
            y2 = min(self.h, y2)

            region = mask[y1:y2, x1:x2]


            if region.size > 0:
                out[sid] = region.mean()


        return out


    # --------------------------------------------------------
    # Per-frame Feature Extraction
    # --------------------------------------------------------

    def extract(self, frame, frame_bytes):

        mask = self._get_mask(frame)


        # Current semantic occupancy

        roi = self._sp_ratio(mask)


        # Semantic spatial change

        if self.prev_mask is None:

            change = np.zeros(self.n, dtype=np.float32)

        else:

            diff = (mask != self.prev_mask).astype(np.uint8)

            change = self._sp_ratio(diff)


        self.prev_mask = mask


        return {
            "roi_ratio": {
                sid: float(roi[sid])
                for sid in range(self.n)
            },

            "content_change": {
                sid: float(change[sid])
                for sid in range(self.n)
            },

            "frame_bytes": {
                sid: int(frame_bytes.get(sid, 0))
                for sid in range(self.n)
            }
        }


# ============================================================
# Streaming Decision
# ============================================================

@dataclass
class StreamingDecision:

    active: set
    priorities: dict

    life_action: int
    sched_action: int

    active_ratio: float
    lam: float

    life_scores: dict
    sched_scores: dict

    state: np.ndarray


# ============================================================
# Streaming Controller
#
# collect_frame():
#     called every video frame
#
# update():
#     called at control interval after PPO is trained
# ============================================================

class StreamingController:

    def __init__(self, num_subpics, mode="collect", lifecycle_model=None, scheduling_model=None, deterministic=True):

        self.n = int(num_subpics)

        self.mode = mode
        self.det = deterministic


        if lifecycle_model and PPO is None:
            raise ImportError("stable-baselines3 is required to load lifecycle PPO model")

        if scheduling_model and PPO is None:
            raise ImportError("stable-baselines3 is required to load scheduling PPO model")


        self.life = PPO.load(str(Path(lifecycle_model))) if lifecycle_model else None
        self.sched = PPO.load(str(Path(scheduling_model))) if scheduling_model else None


        # Raw frame-level dataset

        self.data = {
            "frame_id": [],
            "roi": [],
            "change": [],
            "frame_bytes": [],
            "active": [],
            "bandwidth_bps": [],
            "rtt_us": [],
            "cwnd_bytes": [],
            "bytes_in_flight": []
        }


    # ========================================================
    # Frame-level Raw Data Collection
    # ========================================================

    def collect_frame(self, fid, features, active_subpics, network_state):

        roi = np.array(
            [features["roi_ratio"].get(i, 0.0) for i in range(self.n)],
            dtype=np.float32
        )


        change = np.array(
            [features["content_change"].get(i, 0.0) for i in range(self.n)],
            dtype=np.float32
        )


        frame_bytes = np.array(
            [features["frame_bytes"].get(i, 0) for i in range(self.n)],
            dtype=np.int32
        )


        active = np.array(
            [1 if i in active_subpics else 0 for i in range(self.n)],
            dtype=np.uint8
        )


        self.data["frame_id"].append(int(fid))

        self.data["roi"].append(roi)
        self.data["change"].append(change)

        self.data["frame_bytes"].append(frame_bytes)
        self.data["active"].append(active)

        self.data["bandwidth_bps"].append(
            float(network_state.get("estimated_bandwidth_bps", 0))
        )

        self.data["rtt_us"].append(
            float(network_state.get("rtt_us", 0))
        )

        self.data["cwnd_bytes"].append(
            float(network_state.get("cwnd_bytes", 0))
        )

        self.data["bytes_in_flight"].append(
            float(network_state.get("bytes_in_flight", 0))
        )


    # ========================================================
    # Save Raw Training Dataset
    # ========================================================

    def save(self, path):

        if not self.data["frame_id"]:
            print("[CTRL] No training data collected")
            return


        path = Path(path)

        path.parent.mkdir(
            parents=True,
            exist_ok=True
        )


        np.savez(
            path,

            frame_id=np.asarray(
                self.data["frame_id"],
                dtype=np.int64
            ),

            roi=np.asarray(
                self.data["roi"],
                dtype=np.float32
            ),

            change=np.asarray(
                self.data["change"],
                dtype=np.float32
            ),

            frame_bytes=np.asarray(
                self.data["frame_bytes"],
                dtype=np.int32
            ),

            active=np.asarray(
                self.data["active"],
                dtype=np.uint8
            ),

            bandwidth_bps=np.asarray(
                self.data["bandwidth_bps"],
                dtype=np.float32
            ),

            rtt_us=np.asarray(
                self.data["rtt_us"],
                dtype=np.float32
            ),

            cwnd_bytes=np.asarray(
                self.data["cwnd_bytes"],
                dtype=np.float32
            ),

            bytes_in_flight=np.asarray(
                self.data["bytes_in_flight"],
                dtype=np.float32
            )
        )


        print(f"[CTRL] training data saved: {path}")

        print(
            f"[CTRL] samples={len(self.data['frame_id'])} "
            f"SPs={self.n}"
        )


    # ========================================================
    # PPO State Construction
    #
    # Used later after frame-level data is aggregated into
    # one control interval.
    # ========================================================

    def _features(self, roi, change, bitrate, active, net):

        active = set(active)


        r = np.array(
            [roi.get(i, 0.0) for i in range(self.n)],
            dtype=np.float32
        ).clip(0, 1)


        c = np.array(
            [change.get(i, 0.0) for i in range(self.n)],
            dtype=np.float32
        ).clip(0, 1)


        b = np.array(
            [bitrate.get(i, 0.0) for i in range(self.n)],
            dtype=np.float32
        )


        x = np.array(
            [1.0 if i in active else 0.0 for i in range(self.n)],
            dtype=np.float32
        )


        # ----------------------------------------------------
        # Normalize SP bitrate
        # ----------------------------------------------------

        bn = b / max(float(b.max()), 1.0)

        bn = bn.clip(
            0,
            1
        )


        # ----------------------------------------------------
        # Relative bandwidth
        #
        # total source bitrate demand vs estimated bandwidth
        # ----------------------------------------------------

        demand = max(
            float(b.sum()),
            1.0
        )


        bw = float(
            net.get(
                "estimated_bandwidth_bps",
                0
            )
        )


        bw = min(
            max(
                bw / demand,
                0
            ),
            2
        ) / 2


        # ----------------------------------------------------
        # RTT
        # ----------------------------------------------------

        rtt = float(
            net.get(
                "rtt_us",
                0
            )
        )


        rtt = min(
            max(
                rtt / 500000.0,
                0
            ),
            1
        )


        # ----------------------------------------------------
        # Congestion
        # ----------------------------------------------------

        cwnd = max(
            float(
                net.get(
                    "cwnd_bytes",
                    1
                )
            ),
            1
        )


        bif = max(
            float(
                net.get(
                    "bytes_in_flight",
                    0
                )
            ),
            0
        )


        congestion = min(
            bif / cwnd,
            2
        ) / 2


        # ----------------------------------------------------
        # Final State
        # ----------------------------------------------------

        state = np.concatenate([
            np.stack(
                [r, c, bn, x],
                axis=1
            ).reshape(-1),

            [
                bw,
                rtt,
                congestion
            ]
        ]).astype(np.float32)


        return r, c, b, bn, state


    # ========================================================
    # PPO Action
    # ========================================================

    def _act(self, model, state, default, n):

        if model is None:
            return default


        action, _ = model.predict(
            state,
            deterministic=self.det
        )


        return int(
            np.clip(
                np.asarray(action).item(),
                0,
                n - 1
            )
        )


    # ========================================================
    # Scheduling Score -> MsQuic Priority
    #
    # Smaller numeric priority = higher priority
    # ========================================================

    @staticmethod
    def _to_priority(scores):

        if not scores:
            return {}


        ids = list(scores)


        values = np.array(
            [scores[i] for i in ids],
            dtype=np.float32
        )


        if float(values.max() - values.min()) < EPS:

            return {
                sid: (P_MIN + P_MAX) // 2
                for sid in ids
            }


        normalized = (
            values - values.min()
        ) / (
            values.max() - values.min()
        )


        priorities = (
            P_MAX
            - normalized * (P_MAX - P_MIN)
        )


        return {
            sid: int(round(priority))
            for sid, priority in zip(ids, priorities)
        }


    # ========================================================
    # Control Decision
    #
    # Called later every control interval.
    #
    # Expected features:
    #
    # roi_ratio
    # content_change
    # bitrate_bps
    # ========================================================

    def update(self, fid, features, active_subpics, network_state):

        if self.mode == "collect":
            return None


        roi = features["roi_ratio"]

        change = features["content_change"]

        bitrate = features["bitrate_bps"]


        r, c, b, bn, state = self._features(
            roi,
            change,
            bitrate,
            active_subpics,
            network_state
        )


        # ====================================================
        # Level 1: Lifecycle
        # ====================================================

        life_action = self._act(
            self.life,
            state,
            len(ACTIVE_RATIOS) - 1,
            len(ACTIVE_RATIOS)
        )


        active_ratio = float(
            ACTIVE_RATIOS[
                life_action
            ]
        )


        k = max(
            1,
            int(
                np.ceil(
                    active_ratio
                    * self.n
                )
            )
        )


        # ----------------------------------------------------
        # Lifecycle ranking score
        # ----------------------------------------------------

        life_score = (
            0.50 * r
            + 0.35 * c
            - 0.15 * bn
        )


        active = set(
            map(
                int,
                np.argsort(
                    -life_score
                )[:k]
            )
        )


        # ====================================================
        # Level 2: Scheduling
        # ====================================================

        sched_action = self._act(
            self.sched,
            state,
            2,
            len(LAMBDAS)
        )


        lam = float(
            LAMBDAS[
                sched_action
            ]
        )


        sched_score = (
            lam * r
            + (1.0 - lam) * c
        )


        sched_scores = {
            sid: float(
                sched_score[sid]
            )
            for sid in active
        }


        # ====================================================
        # Decision
        # ====================================================

        return StreamingDecision(
            active=active,

            priorities=self._to_priority(
                sched_scores
            ),

            life_action=life_action,

            sched_action=sched_action,

            active_ratio=active_ratio,

            lam=lam,

            life_scores={
                sid: float(
                    life_score[sid]
                )
                for sid in range(self.n)
            },

            sched_scores=sched_scores,

            state=state
        )