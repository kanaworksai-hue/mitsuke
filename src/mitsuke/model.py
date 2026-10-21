"""Embedders: EmbeddingGemma 2 on MLX (the real one) and a deterministic fake for tests.

Both return float32, L2-normalised, 768-d numpy arrays:
    e = get_embedder(); e.embed_images([pil, ...], tokens=140); e.embed_texts(["雪の中の鳥居"])
`MITSUKE_EMBEDDER=fake` swaps in the fake (no model, no GPU), `MITSUKE_MODEL` overrides the MLX repo.
"""

from __future__ import annotations

import hashlib
import os

import numpy as np

DEFAULT_MODEL = "mlx-community/embeddinggemma-2-bf16"
DIM = 768
QUERY_PREFIX = "task: search result | query: "
QUALITY_TOKENS = {"fast": 70, "default": 140, "best": 280}


def _norm(x) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    return x / np.maximum(np.linalg.norm(x, axis=-1, keepdims=True), 1e-12)


class MLXEmbedder:
    batch_size = 4

    def __init__(self, repo: str | None = None):
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        self.name = repo or os.environ.get("MITSUKE_MODEL") or DEFAULT_MODEL
        if is_cached(self.name):  # progress bars only make sense for the first download
            from huggingface_hub.utils import disable_progress_bars

            disable_progress_bars()
        import mlx.core as mx
        from mlx_vlm import load

        self.mx = mx
        mx.set_cache_limit(1 << 30)  # keeps peak GPU memory around 3 GB at batch 4
        self.model, self.processor = load(self.name)
        self.tokenizer = self.processor.tokenizer

    def _run(self, inputs) -> np.ndarray:
        mx = self.mx
        feed = {k: mx.array(np.asarray(v)) for k, v in inputs.items() if not k.startswith("num_soft_tokens")}
        emb = self.model(**feed).text_embeds
        mx.eval(emb)
        return np.array(emb.astype(mx.float32))

    def embed_images(self, images, tokens: int = 140) -> np.ndarray:
        images = list(images)
        out = []
        for i in range(0, len(images), self.batch_size):
            chunk = images[i : i + self.batch_size]
            inputs = self.processor(
                text=["<|image|>"] * len(chunk),
                images=[[im] for im in chunk],
                return_tensors="np",
                padding=True,
                images_kwargs={"max_soft_tokens": tokens},
            )
            out.append(self._run(inputs))
        return _norm(np.concatenate(out)) if out else np.zeros((0, DIM), np.float32)

    def embed_texts(self, texts) -> np.ndarray:
        enc = self.tokenizer([QUERY_PREFIX + t for t in texts], padding=True, return_tensors="np")
        return _norm(self._run(dict(enc)))


class FakeEmbedder:
    """Deterministic stand-in: images map through their 8x8 colour layout (similar images → similar vectors),
    texts through a hash. Lets index/search tests run anywhere."""

    name = "fake"
    batch_size = 8

    def __init__(self):
        rng = np.random.default_rng(0)
        self.proj = rng.standard_normal((8 * 8 * 3, DIM)).astype(np.float32)

    def embed_images(self, images, tokens: int = 140) -> np.ndarray:
        rows = []
        for im in images:
            a = np.asarray(im.convert("RGB").resize((8, 8)), dtype=np.float32).reshape(-1) / 255.0 - 0.5
            rows.append(a @ self.proj)
        return _norm(np.array(rows)) if rows else np.zeros((0, DIM), np.float32)

    def embed_texts(self, texts) -> np.ndarray:
        rows = []
        for t in texts:
            seed = int.from_bytes(hashlib.sha1(t.encode()).digest()[:8], "little")
            rows.append(np.random.default_rng(seed).standard_normal(DIM))
        return _norm(np.array(rows))


def get_embedder(name: str | None = None):
    name = name or os.environ.get("MITSUKE_EMBEDDER", "mlx")
    if name == "fake":
        return FakeEmbedder()
    return MLXEmbedder()


def model_id() -> str:
    """Model identity stored with vectors, without loading anything."""
    if os.environ.get("MITSUKE_EMBEDDER") == "fake":
        return "fake"
    return os.environ.get("MITSUKE_MODEL") or DEFAULT_MODEL


def is_cached(repo: str) -> bool:
    try:
        from huggingface_hub import try_to_load_from_cache

        def has(f):
            return isinstance(try_to_load_from_cache(repo, f), str)

        return has("config.json") and (has("model.safetensors.index.json") or has("model.safetensors"))
    except Exception:  # noqa: BLE001
        return False
