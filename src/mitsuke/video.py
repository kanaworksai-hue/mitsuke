"""Videos become a handful of searchable *segments* (shots), each with its own vector.

1. Sample one frame per second (OpenCV; long videos are sampled evenly, at most MAX_SAMPLES frames).
2. Cut where consecutive samples differ a lot (colour histogram distance) → shots, accurate to the sample step.
3. Long shots are split into pieces of at most MAX_SEGMENT seconds, so a slow pan still finds its later content.
4. The middle sample of each segment is the frame that gets embedded and shown as its thumbnail.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from PIL import Image

STEP = 1.0  # seconds between samples
MAX_SAMPLES = 900
MAX_SEGMENT = 8.0
MAX_SEGMENTS = 300
CUT = 0.35  # Bhattacharyya distance between HSV histograms that counts as a cut
SEQUENTIAL_MAX_S = 180  # shorter videos are decoded straight through (seeking is slow on long-GOP files)


@dataclass
class Segment:
    t0: float
    t1: float
    tm: float  # time of the representative frame
    image: Image.Image


@dataclass
class VideoInfo:
    width: int
    height: int
    duration: float
    fps: float


def _hist(cv2, bgr) -> np.ndarray:
    small = cv2.resize(bgr, (64, 36), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    h = cv2.calcHist([hsv], [0, 1, 2], None, [8, 4, 4], [0, 180, 0, 256, 0, 256])
    return cv2.normalize(h, h).flatten()


def _to_pil(cv2, bgr, max_side: int) -> Image.Image:
    h, w = bgr.shape[:2]
    if max(h, w) > max_side:
        s = max_side / max(h, w)
        bgr = cv2.resize(bgr, (max(1, round(w * s)), max(1, round(h * s))), interpolation=cv2.INTER_AREA)
    return Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))


def sample(path: str, max_side: int = 512) -> tuple[VideoInfo, list[tuple[float, Image.Image, np.ndarray]]]:
    """[(time_s, frame, histogram)] at ~STEP spacing."""
    import cv2

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise ValueError("cannot open video")
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if fps <= 0 or fps > 1000:
            fps = 25.0
        duration = n / fps if n > 0 else 0.0
        step = STEP
        if duration > MAX_SAMPLES * STEP:
            step = duration / MAX_SAMPLES
        out = []
        if duration and duration > SEQUENTIAL_MAX_S:
            t = step / 2
            while t < duration:
                cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
                ok, frame = cap.read()
                if ok:
                    out.append((t, _to_pil(cv2, frame, max_side), _hist(cv2, frame)))
                t += step
        else:
            every = max(1, round(step * fps))
            i = 0
            while True:
                if not cap.grab():
                    break
                if i % every == every // 2:  # middle of each step
                    ok, frame = cap.retrieve()
                    if ok:
                        out.append((i / fps, _to_pil(cv2, frame, max_side), _hist(cv2, frame)))
                i += 1
            if not duration:
                duration = i / fps
        if not out:
            raise ValueError("no decodable frames")
        return VideoInfo(w, h, round(duration, 3), round(fps, 3)), out
    finally:
        cap.release()


def segment(samples, duration: float) -> list[Segment]:
    import cv2

    if not samples:
        return []
    shots = [[0]]
    for i in range(1, len(samples)):
        d = cv2.compareHist(samples[i - 1][2], samples[i][2], cv2.HISTCMP_BHATTACHARYYA)
        if d > CUT:
            shots.append([i])
        else:
            shots[-1].append(i)
    times = [s[0] for s in samples]
    # Boundaries halfway between samples; the first starts at 0, the last ends at the video's end.
    bounds = [0.0] + [(times[i - 1] + times[i]) / 2 for i in range(1, len(times))] + [max(duration, times[-1])]
    segs = []
    for shot in shots:
        t0, t1 = bounds[shot[0]], bounds[shot[-1] + 1]
        parts = max(1, math.ceil((t1 - t0) / MAX_SEGMENT - 1e-9))
        per = len(shot) / parts
        for p in range(parts):
            idx = shot[int(p * per) : max(int(p * per) + 1, int((p + 1) * per))]
            mid = idx[len(idx) // 2]
            segs.append(Segment(round(bounds[idx[0]], 3), round(bounds[idx[-1] + 1], 3), round(times[mid], 3), samples[mid][1]))
    if len(segs) > MAX_SEGMENTS:  # keep an even spread
        keep = np.linspace(0, len(segs) - 1, MAX_SEGMENTS).round().astype(int)
        segs = [segs[i] for i in sorted(set(keep))]
    return segs


def load_segments(path: str, max_side: int = 512) -> tuple[VideoInfo, list[Segment]]:
    info, samples = sample(path, max_side)
    return info, segment(samples, info.duration)
