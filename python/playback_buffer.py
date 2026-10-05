import time
import threading
from collections import defaultdict


class PlaybackBuffer:

    def __init__(self, fps=30.0, playback_delay_ms=500.0):
        self.fps = float(fps)

        if self.fps <= 0:
            raise ValueError(f"Invalid FPS={self.fps}")

        self.playback_delay_ms = float(playback_delay_ms)
        self.playback_delay_ns = int(self.playback_delay_ms * 1e6)

        self.frame_interval_ns = int(1e9 / self.fps)

        # frame_id -> subpic_id -> fragment_idx -> fragment info
        self.frames = defaultdict(lambda: defaultdict(dict))

        self.next_frame = 0

        self.started = False
        self.waiting_for_frame = False

        self.first_receive_ns = None
        self.playback_start_ns = None
        self.next_play_ns = None

        self.lock = threading.Lock()

    # ========================================================
    # FPS / startup configuration
    # ========================================================

    def set_fps(self, fps):
        fps = float(fps)

        if fps <= 0:
            raise ValueError(f"Invalid FPS={fps}")

        with self.lock:
            if self.started:
                raise RuntimeError(
                    "Cannot change FPS after playback started"
                )

            self.fps = fps
            self.frame_interval_ns = int(1e9 / self.fps)

        print(
            f"[PlaybackBuffer] "
            f"FPS={self.fps:.3f} | "
            f"interval={self.frame_interval_ns / 1e6:.3f} ms | "
            f"startup_frames={self.startup_frame_count()}"
        )

    def startup_frame_count(self):
        return max(
            1,
            int(round(
                self.fps
                * self.playback_delay_ms
                / 1000.0
            ))
        )

    def startup_ready(self, expected_subpics):
        expected = set(expected_subpics)

        if not expected:
            return False

        n = self.startup_frame_count()

        for fid in range(n):
            if not self.frame_ready(fid, expected):
                return False

        return True

    # ========================================================
    # Push fragments
    # ========================================================

    def push(
        self,
        frame_id,
        subpic_id,
        stream_id,
        fragment_idx,
        fragment_count,
        payload,
        receive_ns=None
    ):
        frame_id = int(frame_id)
        subpic_id = int(subpic_id)
        stream_id = int(stream_id)
        fragment_idx = int(fragment_idx)
        fragment_count = int(fragment_count)

        receive_ns = (
            int(receive_ns)
            if receive_ns is not None
            else time.time_ns()
        )

        if fragment_count <= 0:
            return False

        if fragment_idx < 0 or fragment_idx >= fragment_count:
            return False

        with self.lock:

            if frame_id < self.next_frame:
                return False

            if self.first_receive_ns is None:
                self.first_receive_ns = receive_ns

            parts = self.frames[frame_id][subpic_id]

            if parts:
                existing_stream = self._parts_stream_id(parts)

                if (
                    existing_stream is not None
                    and existing_stream != stream_id
                ):
                    return False

            parts[fragment_idx] = {
                "data": bytes(payload),
                "stream_id": stream_id,
                "receive_ns": receive_ns,
                "fragment_count": fragment_count,
            }

        return True

    # ========================================================
    # Fragment / AU state
    # ========================================================

    @staticmethod
    def _subpic_complete(parts):
        if not parts:
            return False

        counts = {
            int(x["fragment_count"])
            for x in parts.values()
        }

        if len(counts) != 1:
            return False

        count = next(iter(counts))

        return (
            count > 0
            and len(parts) == count
            and all(i in parts for i in range(count))
        )

    @staticmethod
    def _parts_stream_id(parts):
        if not parts:
            return None

        stream_ids = {
            int(x["stream_id"])
            for x in parts.values()
        }

        if len(stream_ids) != 1:
            return None

        return next(iter(stream_ids))

    def subpic_receive_time_ns(self, frame_id, subpic_id):
        frame_id = int(frame_id)
        subpic_id = int(subpic_id)

        with self.lock:
            frame = self.frames.get(frame_id)

            if not frame:
                return None

            parts = frame.get(subpic_id)

            if not parts or not self._subpic_complete(parts):
                return None

            return max(
                x["receive_ns"]
                for x in parts.values()
            )

    # ========================================================
    # Frame readiness
    # ========================================================

    def frame_ready(self, frame_id, expected_subpics):
        expected = set(expected_subpics)

        if not expected:
            return False

        with self.lock:
            frame = self.frames.get(int(frame_id))

            if not frame:
                return False

            return all(
                spid in frame
                and self._subpic_complete(frame[spid])
                for spid in expected
            )

    def frame_ready_time_ns(self, frame_id, expected_subpics):
        expected = set(expected_subpics)

        if not expected:
            return None

        with self.lock:
            frame = self.frames.get(int(frame_id))

            if not frame:
                return None

            ready_times = []

            for spid in expected:
                parts = frame.get(spid)

                if not parts:
                    return None

                if not self._subpic_complete(parts):
                    return None

                ready_times.append(
                    max(
                        x["receive_ns"]
                        for x in parts.values()
                    )
                )

            return max(ready_times)

    # ========================================================
    # Playback-buffer occupancy
    # ========================================================

    def playable_frames(self, expected_subpics):
        expected = set(expected_subpics)

        if not expected:
            return 0

        count = 0
        fid = self.next_frame

        while self.frame_ready(fid, expected):
            count += 1
            fid += 1

        return count

    def playable_buffer_ms(self, expected_subpics):
        return (
            self.playable_frames(expected_subpics)
            / self.fps
            * 1000.0
        )

    def occupancy(self):
        """
        Raw number of frame IDs currently present in memory.
        This may include incomplete frames.
        """
        with self.lock:
            return len(self.frames)

    # ========================================================
    # Pop one complete frame
    # ========================================================

    def pop_frame(self, frame_id):
        frame_id = int(frame_id)

        with self.lock:
            frame = self.frames.pop(
                frame_id,
                None
            )

        if frame is None:
            return {}

        result = {}

        for spid, parts in frame.items():

            if not self._subpic_complete(parts):
                continue

            ordered = [
                parts[i]
                for i in sorted(parts)
            ]

            result[spid] = {
                "data": b"".join(
                    x["data"]
                    for x in ordered
                ),

                "stream_id": ordered[0]["stream_id"],

                # SP complete receive time
                "receive_ns": max(
                    x["receive_ns"]
                    for x in ordered
                ),

                "bytes": sum(
                    len(x["data"])
                    for x in ordered
                ),
            }

        return result

    # ========================================================
    # Playback timing
    # ========================================================

    def playback_due(self, now_ns=None):
        now_ns = (
            int(now_ns)
            if now_ns is not None
            else time.time_ns()
        )

        with self.lock:
            if not self.started:
                return False

            if self.next_play_ns is None:
                return False

            return now_ns >= self.next_play_ns

    def current_scheduled_play_ns(self):
        with self.lock:
            return self.next_play_ns

    # ========================================================
    # Main playback state machine
    # ========================================================

    def pop_next_ready(
        self,
        expected_subpics,
        now_ns=None
    ):
        expected = set(expected_subpics)

        if not expected:
            return None

        now_ns = (
            int(now_ns)
            if now_ns is not None
            else time.time_ns()
        )

        frame_id = self.next_frame

        # ====================================================
        # STARTUP
        #
        # playback_delay_ms means playable media buffered.
        #
        # Example:
        #   50 fps + 500 ms -> frames 0..24 must all be ready.
        # ====================================================

        if not self.started:

            if not self.startup_ready(expected):
                return None

            frame_ready_ns = self.frame_ready_time_ns(
                frame_id,
                expected
            )

            if frame_ready_ns is None:
                return None

            scheduled_play_ns = now_ns
            actual_play_ns = now_ns

            frame = self.pop_frame(frame_id)

            if not frame:
                return None

            self.next_frame += 1

            with self.lock:
                self.started = True
                self.waiting_for_frame = False

                self.playback_start_ns = actual_play_ns

                self.next_play_ns = (
                    scheduled_play_ns
                    + self.frame_interval_ns
                )

            print(
                f"[PLAYBACK] START | "
                f"frame={frame_id} | "
                f"startup_frames={self.startup_frame_count()} | "
                f"startup_buffer="
                f"{self.playback_delay_ms:.1f} ms | "
                f"fps={self.fps:.3f} | "
                f"active={sorted(expected)}"
            )

            return {
                "frame_id": frame_id,
                "subpics": frame,
                "frame_ready_ns": frame_ready_ns,
                "scheduled_play_ns": scheduled_play_ns,
                "actual_play_ns": actual_play_ns,
            }

        # ====================================================
        # NORMAL PLAYBACK
        # ====================================================

        with self.lock:
            scheduled_play_ns = self.next_play_ns

        if scheduled_play_ns is None:
            return None

        # Not time to play this frame yet.
        if now_ns < scheduled_play_ns:
            return None

        frame_ready_ns = self.frame_ready_time_ns(
            frame_id,
            expected
        )

        # ----------------------------------------------------
        # Playback deadline reached but frame is not ready.
        #
        # Buffer does NOT calculate stall here.
        # It only records that playback is waiting.
        # ----------------------------------------------------

        if frame_ready_ns is None:

            with self.lock:
                self.waiting_for_frame = True

            return None

        # ====================================================
        # Frame is playable
        # ====================================================

        frame = self.pop_frame(frame_id)

        if not frame:
            return None

        actual_play_ns = now_ns

        self.next_frame += 1

        with self.lock:

            if self.waiting_for_frame:

                # Real playback interruption happened.
                # Resume playback clock from recovery time.
                self.next_play_ns = (
                    actual_play_ns
                    + self.frame_interval_ns
                )

            else:

                # Normal playback.
                # Advance ideal media clock without accumulating
                # Python polling / scheduling jitter.
                self.next_play_ns = (
                    scheduled_play_ns
                    + self.frame_interval_ns
                )

            self.waiting_for_frame = False

        return {
            "frame_id": frame_id,
            "subpics": frame,
            "frame_ready_ns": frame_ready_ns,
            "scheduled_play_ns": scheduled_play_ns,
            "actual_play_ns": actual_play_ns,
        }

    # ========================================================
    # Stream lifecycle support
    # ========================================================

    def pending_stream_count(self, stream_id):
        stream_id = int(stream_id)
        count = 0

        with self.lock:

            for frame in self.frames.values():

                for parts in frame.values():

                    if not parts:
                        continue

                    if self._parts_stream_id(parts) != stream_id:
                        continue

                    if self._subpic_complete(parts):
                        count += 1

        return count

    def has_pending_stream(self, stream_id):
        return (
            self.pending_stream_count(stream_id)
            > 0
        )

    def incomplete_stream_count(self, stream_id):
        stream_id = int(stream_id)
        count = 0

        with self.lock:

            for frame in self.frames.values():

                for parts in frame.values():

                    if not parts:
                        continue

                    if self._parts_stream_id(parts) != stream_id:
                        continue

                    if not self._subpic_complete(parts):
                        count += 1

        return count

    def drop_incomplete_stream(self, stream_id):
        stream_id = int(stream_id)
        removed = 0

        with self.lock:
            empty_frames = []

            for frame_id, frame in self.frames.items():
                remove_subpics = []

                for spid, parts in frame.items():

                    if not parts:
                        continue

                    if self._parts_stream_id(parts) != stream_id:
                        continue

                    if not self._subpic_complete(parts):
                        remove_subpics.append(spid)

                for spid in remove_subpics:
                    del frame[spid]
                    removed += 1

                if not frame:
                    empty_frames.append(frame_id)

            for frame_id in empty_frames:
                del self.frames[frame_id]

        return removed

    # ========================================================
    # Debug
    # ========================================================

    def missing_subpics(
        self,
        frame_id,
        expected_subpics
    ):
        expected = set(expected_subpics)

        with self.lock:
            frame = self.frames.get(
                int(frame_id),
                {}
            )

            return [
                spid
                for spid in sorted(expected)
                if (
                    spid not in frame
                    or not self._subpic_complete(
                        frame[spid]
                    )
                )
            ]