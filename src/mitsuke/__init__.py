"""Mitsuke 見つけ — local, AI-native image search (EmbeddingGemma 2)."""

import os

__version__ = "0.3.0"

# OpenCV (video frames, EXR) only enables its EXR codec when this is set before the first `import cv2`,
# and mlx-vlm may import cv2 before we do.
os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")
# Decoder chatter ("moov atom not found", …) is noise for users; failures still surface as file errors.
os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")


def main() -> None:
    import sys

    from .cli import main as cli_main

    sys.exit(cli_main())
