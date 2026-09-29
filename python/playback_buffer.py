import time
import threading
from collections import defaultdict


class PlaybackBuffer:
    def __init__(self, fps=30.0):
        self.fps = float(fps)

        if self.fps <= 0:
            raise ValueError(
                f"Invalid FPS={self.fps}"
            )

        self.frame_interval_ns = int(
            1e9 / self.fps
        )

        # ----------------------------------------------------
        # frame_id
        #   -> subpic_id
        #       -> fragment_idx
        #           -> fragment info
        # ----------------------------------------------------
        self.frames = defaultdict(
            lambda: defaultdict(dict)
        )

        # Next frame expected by playback.
        self.next_frame = 0

        # Playback state.
        #
        # Before started:
        #   wait until frame 0 has all active SPs.
        #
        # After started:
        #   consume according to FPS.
        self.started = False

        # Scheduled playback time for next_frame.
        self.next_play_ns = None

        # Actual time playback started.
        self.playback_start_ns = None

        self.lock = threading.Lock()

    # ========================================================
    # FPS
    # ========================================================

    def set_fps(self, fps):
        """
        Update playback FPS from sender INIT information.

        This should normally be called before playback starts.
        """

        fps = float(fps)

        if fps <= 0:
            raise ValueError(
                f"Invalid FPS={fps}"
            )

        with self.lock:

            if self.started:
                raise RuntimeError(
                    "Cannot change FPS after playback started"
                )

            self.fps = fps

            self.frame_interval_ns = int(
                1e9 / self.fps
            )

        print(
            f"[PlaybackBuffer] "
            f"FPS={self.fps:.3f} | "
            f"interval="
            f"{self.frame_interval_ns / 1e6:.3f} ms"
        )

    # ========================================================
    # Push
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
            receive_ns
            if receive_ns is not None
            else time.time_ns()
        )

        if fragment_count <= 0:
            return False

        if (
            fragment_idx < 0
            or fragment_idx >= fragment_count
        ):
            return False

        with self.lock:

            # ------------------------------------------------
            # Playback has already moved beyond this frame.
            #
            # This should normally only happen for genuinely
            # stale data after lifecycle transitions.
            # ------------------------------------------------
            if frame_id < self.next_frame:
                return False

            parts = (
                self.frames[frame_id][subpic_id]
            )

            # ------------------------------------------------
            # Prevent fragments from an old QUIC stream and
            # a newly reopened stream from being mixed into
            # the same subpicture AU.
            # ------------------------------------------------
            if parts:

                existing_stream = (
                    self._parts_stream_id(parts)
                )

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
    # Fragment completeness
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
            and all(
                i in parts
                for i in range(count)
            )
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

    # ========================================================
    # Frame readiness
    # ========================================================

    def frame_ready(
        self,
        frame_id,
        expected_subpics
    ):
        expected = set(expected_subpics)

        if not expected:
            return False

        with self.lock:

            frame = self.frames.get(
                int(frame_id)
            )

            if not frame:
                return False

            return all(
                spid in frame
                and self._subpic_complete(
                    frame[spid]
                )
                for spid in expected
            )

    # ========================================================
    # Pop one frame
    # ========================================================

    def pop_frame(self, frame_id):
        with self.lock:

            frame = self.frames.pop(
                int(frame_id),
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

                "stream_id":
                    ordered[0]["stream_id"],

                # Complete AU receive time =
                # latest fragment receive time.
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
        """
        True when playback has started and the next frame
        should already be available for playback.

        Server can use this to detect a real stall.
        """

        now_ns = (
            now_ns
            if now_ns is not None
            else time.time_ns()
        )

        with self.lock:

            if not self.started:
                return False

            if self.next_play_ns is None:
                return False

            return now_ns >= self.next_play_ns

    # ========================================================
    # Pop next playback frame
    # ========================================================

    def pop_next_ready(
        self,
        expected_subpics,
        now_ns=None
    ):
        """
        Return the next playable frame.

        Startup:
            Wait until frame 0 contains every currently
            active subpicture. Then playback starts.

        Normal playback:
            Frames are consumed according to FPS.

            If playback time arrives but one or more active
            subpictures are missing, DO NOT advance the
            playback cursor.

            The receiver simply waits. The server can treat
            this period as a playback stall.

        Stall recovery:
            Once the missing active subpicture(s) arrive,
            the frame is played and the playback clock
            resumes from the recovery time.
        """

        expected = set(expected_subpics)

        if not expected:
            return None

        now_ns = (
            now_ns
            if now_ns is not None
            else time.time_ns()
        )

        frame_id = self.next_frame

        # ----------------------------------------------------
        # STARTUP
        # ----------------------------------------------------
        if not self.started:

            # Do not start playback until the first frame
            # contains ALL currently active SPs.
            if not self.frame_ready(
                frame_id,
                expected
            ):
                return None

            frame = self.pop_frame(
                frame_id
            )

            self.next_frame += 1

            with self.lock:
                self.started = True
                self.playback_start_ns = now_ns

                self.next_play_ns = (
                    now_ns
                    + self.frame_interval_ns
                )

            print(
                f"[PLAYBACK] START | "
                f"frame={frame_id} | "
                f"active={sorted(expected)} | "
                f"fps={self.fps:.3f}"
            )

            return frame_id, frame

        # ----------------------------------------------------
        # NORMAL PLAYBACK
        # ----------------------------------------------------

        # Not yet time to consume the next frame.
        with self.lock:
            next_play_ns = self.next_play_ns

        if (
            next_play_ns is not None
            and now_ns < next_play_ns
        ):
            return None

        # ----------------------------------------------------
        # Playback time has arrived.
        #
        # If an ACTIVE SP is missing:
        #
        #     WAIT.
        #
        # Do NOT:
        #     - drop the frame
        #     - advance next_frame
        #     - create an artificial deadline
        #
        # Server will detect this state as STALL.
        # ----------------------------------------------------
        if not self.frame_ready(
            frame_id,
            expected
        ):
            return None

        # ----------------------------------------------------
        # All currently active SPs are ready.
        # Playback can continue.
        # ----------------------------------------------------
        frame = self.pop_frame(
            frame_id
        )

        self.next_frame += 1

        # ----------------------------------------------------
        # Resume playback clock from NOW.
        #
        # This is important after a stall:
        #
        #     stall ends now
        #          ↓
        #     next frame is due after 1/FPS
        #
        # rather than trying to "catch up" all missed
        # playback timestamps immediately.
        # ----------------------------------------------------
        with self.lock:
            self.next_play_ns = (
                now_ns
                + self.frame_interval_ns
            )

        return frame_id, frame

    # ========================================================
    # Per-stream pending state
    # ========================================================

    def pending_stream_count(
        self,
        stream_id
    ):
        """
        Number of COMPLETE subpicture AUs belonging to
        stream_id that are still waiting in PlaybackBuffer.

        Used to determine whether a closed QUIC stream can
        safely flush its PyAV decoder.
        """

        stream_id = int(stream_id)
        count = 0

        with self.lock:

            for frame in self.frames.values():

                for parts in frame.values():

                    if not parts:
                        continue

                    if (
                        self._parts_stream_id(parts)
                        != stream_id
                    ):
                        continue

                    if self._subpic_complete(parts):
                        count += 1

        return count

    def has_pending_stream(
        self,
        stream_id
    ):
        """
        True if at least one COMPLETE AU from this stream
        is still waiting to be fed into PyAV.
        """

        return (
            self.pending_stream_count(
                stream_id
            ) > 0
        )

    def incomplete_stream_count(
        self,
        stream_id
    ):
        """
        Number of incomplete AUs/fragments belonging to
        this stream that remain in PlaybackBuffer.
        """

        stream_id = int(stream_id)
        count = 0

        with self.lock:

            for frame in self.frames.values():

                for parts in frame.values():

                    if not parts:
                        continue

                    if (
                        self._parts_stream_id(parts)
                        != stream_id
                    ):
                        continue

                    if not self._subpic_complete(parts):
                        count += 1

        return count

    def drop_incomplete_stream(
        self,
        stream_id
    ):
        """
        Remove incomplete AUs belonging to a CLOSED stream.

        This must only be called after QUIC confirms that the
        stream is closed, because no additional fragments can
        arrive after that point.
        """

        stream_id = int(stream_id)
        removed = 0

        with self.lock:

            empty_frames = []

            for frame_id, frame in self.frames.items():

                remove_subpics = []

                for spid, parts in frame.items():

                    if not parts:
                        continue

                    if (
                        self._parts_stream_id(parts)
                        != stream_id
                    ):
                        continue

                    if not self._subpic_complete(parts):
                        remove_subpics.append(
                            spid
                        )

                for spid in remove_subpics:
                    del frame[spid]
                    removed += 1

                if not frame:
                    empty_frames.append(
                        frame_id
                    )

            for frame_id in empty_frames:
                del self.frames[frame_id]

        return removed

    # ========================================================
    # Statistics / debug
    # ========================================================

    def occupancy(self):
        """
        Number of frame IDs currently buffered.
        """

        with self.lock:
            return len(self.frames)

    def missing_subpics(
        self,
        frame_id,
        expected_subpics
    ):
        """
        Return currently missing/incomplete ACTIVE SPs for
        one frame.

        This is for stall/debug statistics only.
        Missing SPs are NOT automatically dropped.
        """

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