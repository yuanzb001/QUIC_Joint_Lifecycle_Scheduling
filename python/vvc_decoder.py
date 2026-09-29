import time
import av
from collections import defaultdict, deque


class VVCDecoder:
    def __init__(self, output_dir=None, num_subpics=4):
        self.num_subpics = num_subpics

        # spid -> VPS/SPS/PPS bytes
        self.configs = {}

        # spid -> PyAV CodecContext
        self.codecs = {}

        # Cumulative decoded frame count
        self.decoded_count = {
            sp: 0
            for sp in range(num_subpics)
        }

        # --------------------------------------------------------
        # Frame-ID tracking
        #
        # Each submitted AU has a logical source frame_id.
        # VVC decoder may delay/reorder output, so we keep the
        # submitted IDs until actual decoded frames are produced.
        #
        # spid -> deque([frame_id, ...])
        # --------------------------------------------------------
        self.pending_frame_ids = defaultdict(deque)

    # ========================================================
    # Decoder lifecycle
    # ========================================================

    def _new_codec(self, spid):
        """
        Create a fresh PyAV VVC decoder for one subpicture.
        """

        spid = int(spid)

        # Drop old decoder if it still exists
        old = self.codecs.pop(spid, None)

        if old is not None:
            try:
                old.close()
            except Exception:
                pass

        # New decoder lifecycle -> old pending IDs must not leak
        self.pending_frame_ids[spid].clear()

        codec = av.CodecContext.create(
            "vvc",
            "r"
        )

        # Avoid additional frame-threading delay
        codec.thread_count = 1

        codec.open()

        self.codecs[spid] = codec

        self.decoded_count.setdefault(spid, 0)

        print(
            f"[Decoder] SP{spid}: "
            f"new PyAV VVC decoder"
        )

        return codec

    def _ensure_codec(self, spid):
        """
        Make sure decoder exists.

        If this SP was previously flushed/closed, create
        a new decoder and feed the stored config again.
        """

        spid = int(spid)

        if spid in self.codecs:
            return

        self._new_codec(spid)

        if spid in self.configs:

            frames = self._feed(
                spid,
                self.configs[spid]
            )

            # Normally config should produce no decoded frames.
            if frames:
                self.decoded_count[spid] += len(frames)

            print(
                f"[Decoder] SP{spid}: "
                f"restored config after reopen"
            )

    def reset(self, subpic_id):
        """
        Explicitly discard current decoder state and start
        a new decoding context using the stored config.
        """

        spid = int(subpic_id)

        self._new_codec(spid)

        if spid in self.configs:

            frames = self._feed(
                spid,
                self.configs[spid]
            )

            self.decoded_count[spid] += len(frames)

        print(
            f"[Decoder] SP{spid}: reset"
        )

    # ========================================================
    # Config
    # ========================================================

    def set_config(self, subpic_id, payload):
        """
        Store VPS/SPS/PPS and feed them into decoder.
        """

        spid = int(subpic_id)

        self.configs[spid] = bytes(payload)

        if spid not in self.codecs:
            self._new_codec(spid)

        try:

            decoded = self._feed(
                spid,
                self.configs[spid]
            )

            self.decoded_count[spid] += len(decoded)

            print(
                f"[Decoder] CFG SP{spid}: "
                f"{len(payload):,} B | "
                f"decoded={len(decoded)}"
            )

        except Exception as e:

            print(
                f"[Decoder] CFG SP{spid} error: "
                f"{type(e).__name__}: {e}"
            )

            raise

    def has_config(self, subpic_id):
        return int(subpic_id) in self.configs

    # ========================================================
    # Internal feed
    # ========================================================

    def _feed(self, spid, payload):
        """
        Feed byte stream into PyAV parser + VVC decoder.
        """

        spid = int(spid)

        codec = self.codecs[spid]

        frames = []

        packets = codec.parse(payload)

        for packet in packets:
            frames.extend(
                codec.decode(packet)
            )

        return frames

    # ========================================================
    # Bind decoded frames to logical frame IDs
    # ========================================================

    def _bind_frame_ids(self, spid, frames):
        """
        Associate actual decoded frames with previously submitted
        logical frame IDs.

        Returns:
            [
                {
                    "frame_id": ...,
                    "subpic_id": ...,
                    "frame": av.VideoFrame
                },
                ...
            ]
        """

        spid = int(spid)

        result = []

        for frame in frames:

            if not self.pending_frame_ids[spid]:
                raise RuntimeError(
                    f"SP{spid}: decoded frame produced "
                    f"without pending frame_id"
                )

            frame_id = self.pending_frame_ids[spid].popleft()

            result.append({
                "frame_id": frame_id,
                "subpic_id": spid,
                "frame": frame,
            })

        return result

    # ========================================================
    # Live frame input
    # ========================================================

    def append_frame(
        self,
        frame_id,
        subpic_id,
        payload
    ):
        spid = int(subpic_id)
        frame_id = int(frame_id)

        if spid not in self.configs:
            raise RuntimeError(
                f"Missing config for SP{spid}"
            )

        # After lifecycle flush the old codec is deleted.
        # Reopen automatically creates a fresh decoder and
        # restores VPS/SPS/PPS.
        self._ensure_codec(spid)

        # ----------------------------------------------------
        # Track logical input frame.
        #
        # The decoder may not output this frame immediately.
        # ----------------------------------------------------
        self.pending_frame_ids[spid].append(frame_id)

        start_ns = time.time_ns()

        try:

            frames = self._feed(
                spid,
                payload
            )

            decoded_items = self._bind_frame_ids(
                spid,
                frames
            )

            success = True
            error = ""

        except Exception as e:

            decoded_items = []
            success = False

            error = (
                f"{type(e).__name__}: {e}"
            )

            # Remove current input ID if it is still the newest
            # pending ID and decoding failed before consuming it.
            if (
                self.pending_frame_ids[spid]
                and self.pending_frame_ids[spid][-1] == frame_id
            ):
                self.pending_frame_ids[spid].pop()

        end_ns = time.time_ns()

        self.decoded_count[spid] += len(decoded_items)

        return {
            # Input AU information
            "input_frame_id": frame_id,
            "subpic_id": spid,

            "success": success,
            "input_bytes": len(payload),

            # Actual decoder output
            "decoded_now": len(decoded_items),
            "decoded_total": self.decoded_count[spid],

            "decode_start_ns": start_ns,
            "decode_end_ns": end_ns,
            "decode_ms": (
                end_ns - start_ns
            ) / 1e6,

            # IMPORTANT:
            # each item contains:
            # frame_id + subpic_id + av.VideoFrame
            "frames": decoded_items,

            "pending_frame_ids": len(
                self.pending_frame_ids[spid]
            ),

            "error": error,
        }

    # ========================================================
    # Flush one subpicture
    # ========================================================

    def flush_subpic(self, subpic_id):
        """
        Drain one SP decoder.

        Order:
            1. Flush PyAV parser with parse(b"")
            2. Decode packets released by parser
            3. codec.decode(None) drains VVC DPB
            4. Bind delayed decoded frames to pending frame IDs
            5. Delete decoder context

        Stored VPS/SPS/PPS remains cached for future reopen.
        """

        spid = int(subpic_id)

        codec = self.codecs.get(spid)

        if codec is None:

            print(
                f"[Decoder] SP{spid}: "
                f"flush skipped "
                f"(no active decoder)"
            )

            return []

        start_ns = time.time_ns()

        frames = []

        # ----------------------------------------------------
        # 1. Flush parser
        # ----------------------------------------------------

        try:

            packets = codec.parse(b"")

            for packet in packets:
                frames.extend(
                    codec.decode(packet)
                )

        except Exception as e:

            print(
                f"[Decoder] SP{spid}: "
                f"parser flush error: "
                f"{type(e).__name__}: {e}"
            )

        # ----------------------------------------------------
        # 2. Drain decoder DPB
        # ----------------------------------------------------

        try:

            frames.extend(
                codec.decode(None)
            )

        except Exception as e:

            print(
                f"[Decoder] SP{spid}: "
                f"decoder drain error: "
                f"{type(e).__name__}: {e}"
            )

        # ----------------------------------------------------
        # 3. Attach delayed output to correct frame IDs
        # ----------------------------------------------------

        decoded_items = self._bind_frame_ids(
            spid,
            frames
        )

        end_ns = time.time_ns()

        self.decoded_count[spid] += len(decoded_items)

        remaining_ids = len(
            self.pending_frame_ids[spid]
        )

        if remaining_ids != 0:
            print(
                f"[Decoder] WARNING SP{spid}: "
                f"{remaining_ids} frame IDs remain "
                f"after decoder flush"
            )

        # ----------------------------------------------------
        # 4. Decoder lifecycle finished
        # ----------------------------------------------------

        self.codecs.pop(
            spid,
            None
        )

        try:
            codec.close()
        except Exception:
            pass

        # Old lifecycle must not leak IDs into reopen
        self.pending_frame_ids[spid].clear()

        print(
            f"[Decoder] SP{spid}: FLUSH COMPLETE | "
            f"decoded={len(decoded_items)} | "
            f"total={self.decoded_count[spid]} | "
            f"time={(end_ns - start_ns) / 1e6:.3f} ms"
        )

        return decoded_items

    # ========================================================
    # Compatibility alias
    # ========================================================

    def flush(self, subpic_id):
        return self.flush_subpic(
            subpic_id
        )

    # ========================================================
    # Flush all active decoders
    # ========================================================

    def flush_all(self):
        """
        Drain all currently active PyAV decoder contexts.
        """

        result = {}

        for spid in list(self.codecs.keys()):

            result[spid] = (
                self.flush_subpic(spid)
            )

        return result