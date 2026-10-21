"""Real-model checks (any Apple Silicon Mac with the weights cached): `pytest -m model`."""

import numpy as np
import pytest

pytestmark = pytest.mark.model


@pytest.fixture(scope="module")
def embedder():
    pytest.importorskip("mlx_vlm")
    from mitsuke.model import MLXEmbedder

    return MLXEmbedder()


def test_reference_vectors(embedder):
    from mitsuke.doctor import REF_PATH, REF_TEXT, probe_image

    ref = np.load(REF_PATH)
    assert float(embedder.embed_images([probe_image()], tokens=140)[0] @ ref["image"].astype(np.float32)) > 0.99
    assert float(embedder.embed_texts([REF_TEXT])[0] @ ref["text"].astype(np.float32)) > 0.99


def test_text_matches_picture(embedder):
    from mitsuke.doctor import probe_image

    iv = embedder.embed_images([probe_image()], tokens=140)[0]
    tv = embedder.embed_texts(["赤い丸と青い四角", "a photo of a cat in the snow"])
    assert iv @ tv[0] > iv @ tv[1]
