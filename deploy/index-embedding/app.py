"""Isolated same-model GPU embedding service for offline index builds only."""
import os
from functools import lru_cache
from threading import Lock
import onnxruntime as ort
import torch  # Preload matching CUDA/cuDNN libraries from the pinned base image.
from fastapi import FastAPI
from fastembed import TextEmbedding

app = FastAPI()
model_name = os.getenv('FASTEMBED_MODEL', 'nomic-ai/nomic-embed-text-v1.5')
memory_limit = int(os.getenv('FASTEMBED_GPU_MEMORY_LIMIT', str(2 * 1024 ** 3)))
lock = Lock()

@lru_cache(maxsize=1)
def model():
    ort.preload_dlls()
    instance = TextEmbedding(
        model_name=model_name, cache_dir='/data/fastembed', threads=2,
        providers=[('CUDAExecutionProvider', {'gpu_mem_limit': memory_limit,
                                            'arena_extend_strategy': 'kSameAsRequested'})],
    )
    if 'CUDAExecutionProvider' not in instance.model.model.get_providers():
        raise RuntimeError('GPU provider unavailable; refusing silent CPU fallback')
    return instance

@app.get('/health')
def health():
    return {'providers': model().model.model.get_providers(), 'model': model_name}

@app.post('/api/embed')
def embed(payload: dict):
    inputs = payload.get('input', '')
    inputs = inputs if isinstance(inputs, list) else [str(inputs)]
    with lock:
        vectors = list(model().embed(inputs, batch_size=2))
    return {'model': model_name, 'embeddings': [v.tolist() for v in vectors]}
