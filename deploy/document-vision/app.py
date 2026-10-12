"""Isolated, bounded GPU recognizer for formulas and document elements."""
import base64
import io
import os
from functools import lru_cache
from threading import Lock
from pathlib import Path
from PIL import Image, ImageOps
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import torch
from transformers import AutoProcessor, AutoModelForImageTextToText

app = FastAPI(title='Document vision')
MODEL = 'PaddlePaddle/PaddleOCR-VL-1.6'
REVISION = 'c5630abae1d940eafe0697512a0325494b02ab42'
lock = Lock()
Image.MAX_IMAGE_PIXELS = 40_000_000

class ParseRequest(BaseModel):
    image_base64: str = Field(max_length=45_000_000)
    task: str = 'ocr'

@lru_cache(maxsize=1)
def model():
    if not torch.cuda.is_available():
        raise RuntimeError('CUDA is required for this isolated recognizer')
    torch.cuda.set_per_process_memory_fraction(float(os.getenv('VISION_GPU_MEMORY_FRACTION', '0.065')))
    torch.set_num_threads(2)
    directory = Path(os.getenv('HF_HOME', '/data/models')) / 'hub' / 'models--PaddlePaddle--PaddleOCR-VL-1.6' / 'snapshots' / REVISION
    if not (directory / 'config.json').is_file():
        raise RuntimeError('Pinned recognition checkpoint is not installed')
    processor = AutoProcessor.from_pretrained(directory)
    instance = AutoModelForImageTextToText.from_pretrained(
        directory, torch_dtype=torch.bfloat16,
        attn_implementation='sdpa',
    ).to('cuda').eval()
    return processor, instance

@app.get('/health')
def health():
    model()
    return {'model': MODEL, 'revision': REVISION, 'device': 'cuda'}

@app.post('/parse')
def parse(payload: ParseRequest):
    prompts = {'ocr': 'OCR:', 'formula': 'Formula Recognition:',
               'table': 'Table Recognition:', 'chart': 'Chart Recognition:'}
    if payload.task not in prompts:
        raise HTTPException(422, 'Unsupported recognition task')
    try:
        raw = base64.b64decode(payload.image_base64, validate=True)
        with Image.open(io.BytesIO(raw)) as source:
            image = ImageOps.exif_transpose(source).convert('RGB')
            image.thumbnail((1800, 1800))
        with lock, torch.inference_mode():
            processor, instance = model()
            inputs = processor.apply_chat_template(
                [{'role': 'user', 'content': [{'type': 'image', 'image': image},
                 {'type': 'text', 'text': prompts[payload.task]}]}],
                add_generation_prompt=True, tokenize=True, return_dict=True,
                return_tensors='pt',
            ).to(instance.device)
            output = instance.generate(**inputs, max_new_tokens=4096, do_sample=False)
            generated = output[0][inputs['input_ids'].shape[-1]:]
            text = processor.decode(generated, skip_special_tokens=True).strip()
            if len(generated) >= 4096:
                raise HTTPException(422, 'Recognition output is incomplete; split the image')
            del inputs, output, generated
        return {'text': text, 'model': MODEL, 'revision': REVISION, 'task': payload.task}
    except HTTPException:
        raise
    except (ValueError, OSError, Image.DecompressionBombError) as exc:
        raise HTTPException(422, type(exc).__name__) from exc
    except torch.cuda.OutOfMemoryError as exc:
        torch.cuda.empty_cache()
        raise HTTPException(503, 'Recognition GPU memory budget exceeded') from exc
