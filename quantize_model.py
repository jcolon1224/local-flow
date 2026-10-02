"""Make a smaller (quantized) copy of the Whisper model for faster loading.

Usage: .venv/bin/python quantize_model.py [bits]   (default 8)
Writes models/whisper-small.en-q<bits>/ ; point config.json "model" at it.
"""
import json
import shutil
import sys
from pathlib import Path

import mlx.core as mx
import mlx.nn as nn
from huggingface_hub import snapshot_download
from mlx.utils import tree_flatten
from mlx_whisper.load_models import load_model

SOURCE = "mlx-community/whisper-small.en-mlx"
bits = int(sys.argv[1]) if len(sys.argv) > 1 else 8
out = Path(__file__).parent / "models" / f"whisper-small.en-q{bits}"

src = Path(snapshot_download(SOURCE))
model = load_model(SOURCE, dtype=mx.float16)
quant = {"group_size": 64, "bits": bits}
nn.quantize(model, **quant,
            class_predicate=lambda p, m: isinstance(m, (nn.Linear, nn.Embedding))
            and m.weight.shape[-1] % 64 == 0)

out.mkdir(parents=True, exist_ok=True)
mx.save_safetensors(str(out / "weights.safetensors"), dict(tree_flatten(model.parameters())))
config = json.loads((src / "config.json").read_text())
config["quantization"] = quant
(out / "config.json").write_text(json.dumps(config, indent=2))
print(out, f"{(out / 'weights.safetensors').stat().st_size / 1e6:.0f} MB")
