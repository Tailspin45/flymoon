"""
E2 — 3D-Conv transit classifier: model definition, training, and ONNX export.

Architecture: lightweight 3D-ConvNet (~48 K parameters)
  Input:  (B, 1, 15, 160, 90)  — grayscale, T=15 frames, H=160, W=90
  Output: (B, 2)               — [logit_no_transit, logit_transit]

Requires: torch, onnx, onnxscript  (training-only; not needed at inference time)

Usage
-----
    # Step 1: extract real clips
    python -m training.extract_clips

    # Step 2: generate synthetic clips (augmentation)
    python -m training.synthetic_gen --n_pos 400 --n_neg 400

    # Step 3: train + export to models/transit_classifier.onnx
    python -m training.train_model

    # Step 4: validate
    python -m training.evaluate
"""

from __future__ import annotations

import argparse
import glob
import json
import random
import sys
from pathlib import Path
from typing import List, Tuple

import numpy as np

CLIP_T = 15
CLIP_H = 160
CLIP_W = 90
DATA_DIR = Path("data/training")
MODEL_OUT = Path("models/transit_classifier.onnx")
META_OUT = Path("models/transit_classifier.json")


# ── Model ─────────────────────────────────────────────────────────────────────


def build_model():
    """Return a TransitCNN instance (~48 K params)."""
    try:
        import torch.nn as nn
    except ImportError:
        print(
            "PyTorch is required for training. Install with: pip install -r requirements.txt",
            file=sys.stderr,
        )
        sys.exit(1)

    class TransitCNN(nn.Module):
        def __init__(self):
            super().__init__()
            # Block 1 — (B,1,15,160,90) → (B,8,15,80,45) after pool
            self.conv1 = nn.Conv3d(1, 8, kernel_size=3, padding=1, bias=False)
            self.bn1 = nn.BatchNorm3d(8)
            self.pool1 = nn.MaxPool3d(kernel_size=(1, 2, 2))

            # Block 2 — → (B,16,7,40,22) after pool
            self.conv2 = nn.Conv3d(8, 16, kernel_size=3, padding=1, bias=False)
            self.bn2 = nn.BatchNorm3d(16)
            self.pool2 = nn.MaxPool3d(kernel_size=(2, 2, 2))

            # Block 3 — → (B,32,1,5,2) after adaptive pool (= 320 features)
            self.conv3 = nn.Conv3d(16, 32, kernel_size=3, padding=1, bias=False)
            self.bn3 = nn.BatchNorm3d(32)
            self.pool3 = nn.AdaptiveAvgPool3d((1, 5, 2))

            self.drop = nn.Dropout(p=0.4)
            self.fc1 = nn.Linear(32 * 1 * 5 * 2, 64)
            self.fc2 = nn.Linear(64, 2)

        def forward(self, x):
            import torch.nn.functional as F

            x = self.pool1(F.relu(self.bn1(self.conv1(x))))
            x = self.pool2(F.relu(self.bn2(self.conv2(x))))
            x = self.pool3(F.relu(self.bn3(self.conv3(x))))
            x = x.flatten(1)
            x = F.relu(self.fc1(self.drop(x)))
            return self.fc2(x)

    model = TransitCNN()
    total = sum(p.numel() for p in model.parameters())
    print(f"Model: TransitCNN ({total:,} parameters)")
    return model


# ── Dataset ───────────────────────────────────────────────────────────────────


class ClipDataset:
    """Simple in-memory dataset of .npz clip files."""

    def __init__(self, data_dir: Path, augment: bool = True):
        self.augment = augment
        self.samples: List[Tuple[np.ndarray, int]] = []

        pos_files = glob.glob(str(data_dir / "positives" / "*.npz"))
        neg_files = glob.glob(str(data_dir / "negatives" / "*.npz"))

        if not pos_files:
            raise FileNotFoundError(
                f"No positive clips found in {data_dir / 'positives'}. "
                "Run training.extract_clips and/or training.synthetic_gen first."
            )

        print(f"Loading {len(pos_files)} positive clips …")
        for f in pos_files:
            clip = np.load(f)["clip"].astype(np.float32) / 255.0
            clip = self._normalize(clip)
            self.samples.append((clip, 1))

        print(f"Loading {len(neg_files)} negative clips …")
        for f in neg_files:
            clip = np.load(f)["clip"].astype(np.float32) / 255.0
            clip = self._normalize(clip)
            self.samples.append((clip, 0))

        random.shuffle(self.samples)
        pos_n = sum(1 for _, l in self.samples if l == 1)
        neg_n = len(self.samples) - pos_n
        print(f"Total: {len(self.samples)} clips ({pos_n} pos, {neg_n} neg)")

    @staticmethod
    def _normalize(clip: np.ndarray) -> np.ndarray:
        """Per-clip zero-mean unit-variance normalisation."""
        mu = clip.mean()
        std = clip.std() + 1e-6
        return ((clip - mu) / std).astype(np.float32)

    def _augment(self, clip: np.ndarray) -> np.ndarray:
        """Light augmentation: horizontal flip, time-reverse, brightness jitter."""
        if random.random() < 0.5:
            clip = clip[:, :, ::-1].copy()  # horizontal flip (W axis)
        if random.random() < 0.5:
            clip = clip[::-1, :, :].copy()  # time reversal
        clip += np.random.normal(0, 0.02, clip.shape).astype(np.float32)
        return clip

    def __len__(self):
        return len(self.samples)

    def get_batch(self, indices: List[int]):
        xs, ys = [], []
        for i in indices:
            clip, label = self.samples[i]
            if self.augment and label == 1:
                clip = self._augment(clip)
            xs.append(clip[np.newaxis, :, :, :])  # add channel dim → (1,T,H,W)
            ys.append(label)
        return np.stack(xs, axis=0), np.array(ys, dtype=np.int64)  # (B,1,T,H,W)


# ── Training loop ─────────────────────────────────────────────────────────────


def train(
    data_dir: Path = DATA_DIR,
    model_out: Path = MODEL_OUT,
    meta_out: Path = META_OUT,
    epochs: int = 40,
    batch_size: int = 16,
    lr: float = 3e-4,
    val_split: float = 0.15,
    patience: int = 8,
    device_str: str = "auto",
) -> None:
    try:
        import torch
        import torch.nn as nn
        import torch.optim as optim
    except ImportError:
        print(
            "PyTorch is required for training. Install with: pip install -r requirements.txt",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        import onnx  # noqa: F401
    except ImportError:
        print(
            "ONNX export dependency is missing. Install with: pip install -r requirements.txt",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        import onnxscript  # noqa: F401
    except ImportError:
        print(
            "ONNXScript is required for torch.onnx export. Install with: pip install -r requirements.txt",
            file=sys.stderr,
        )
        sys.exit(1)

    # MPS (Apple GPU) does not implement Conv3d — first forward throws at model(x).
    if device_str == "auto":
        if torch.cuda.is_available():
            device = torch.device("cuda")
        else:
            device = torch.device("cpu")
            if torch.backends.mps.is_available():
                print(
                    "training.train_model: MPS skipped (Conv3d unsupported); using CPU.",
                    file=sys.stderr,
                )
    else:
        device = torch.device(device_str)
        if device.type == "mps":
            print(
                "training.train_model: --device mps unsupported (Conv3d); using CPU.",
                file=sys.stderr,
            )
            device = torch.device("cpu")
    print(f"Device: {device}")

    # Dataset
    dataset = ClipDataset(data_dir, augment=True)
    n = len(dataset)
    n_val = max(1, int(n * val_split))
    n_train = n - n_val
    indices = list(range(n))
    random.shuffle(indices)
    train_idx, val_idx = indices[:n_train], indices[n_train:]

    # Class weights for imbalanced datasets
    n_pos = sum(1 for i in train_idx if dataset.samples[i][1] == 1)
    n_neg = n_train - n_pos
    if n_pos > 0 and n_neg > 0:
        w = torch.tensor([n_pos / n_train, n_neg / n_train], dtype=torch.float32).to(
            device
        )
    else:
        w = None

    model = build_model().to(device)
    optimizer = optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)
    criterion = nn.CrossEntropyLoss(weight=w)

    best_val_loss = float("inf")
    patience_counter = 0
    best_state = None

    model_out.parent.mkdir(parents=True, exist_ok=True)

    for epoch in range(1, epochs + 1):
        # ── Train ────────────────────────────────────────────────────────────
        model.train()
        random.shuffle(train_idx)
        train_loss = train_correct = 0
        for b_start in range(0, n_train, batch_size):
            batch_ids = train_idx[b_start : b_start + batch_size]
            x_np, y_np = dataset.get_batch(batch_ids)
            x = torch.from_numpy(x_np).to(device)
            y = torch.from_numpy(y_np).to(device)
            optimizer.zero_grad()
            logits = model(x)
            loss = criterion(logits, y)
            loss.backward()
            optimizer.step()
            train_loss += loss.item() * len(batch_ids)
            train_correct += (logits.argmax(1) == y).sum().item()
        scheduler.step()

        # ── Validate ─────────────────────────────────────────────────────────
        model.eval()
        val_loss = val_correct = 0
        with torch.no_grad():
            for b_start in range(0, n_val, batch_size):
                batch_ids = val_idx[b_start : b_start + batch_size]
                x_np, y_np = dataset.get_batch(batch_ids)
                x = torch.from_numpy(x_np).to(device)
                y = torch.from_numpy(y_np).to(device)
                logits = model(x)
                val_loss += criterion(logits, y).item() * len(batch_ids)
                val_correct += (logits.argmax(1) == y).sum().item()

        tl = train_loss / n_train
        vl = val_loss / n_val
        ta = train_correct / n_train * 100
        va = val_correct / n_val * 100
        marker = " *" if vl < best_val_loss else ""
        print(
            f"  Epoch {epoch:02d}/{epochs}  train {tl:.4f} ({ta:.1f}%)  "
            f"val {vl:.4f} ({va:.1f}%){marker}"
        )

        if vl < best_val_loss:
            best_val_loss = vl
            best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}
            patience_counter = 0
        else:
            patience_counter += 1
            if patience_counter >= patience:
                print(f"  Early stop at epoch {epoch}")
                break

    # Restore best weights
    if best_state:
        model.load_state_dict(best_state)

    # ── ONNX export ──────────────────────────────────────────────────────────
    model.eval().cpu()
    dummy = torch.zeros(1, 1, CLIP_T, CLIP_H, CLIP_W)
    torch.onnx.export(
        model,
        dummy,
        str(model_out),
        opset_version=17,
        dynamo=False,
        input_names=["frames"],
        output_names=["logits"],
        dynamic_axes={"frames": {0: "batch"}},
    )
    print(f"\nONNX model saved → {model_out}")

    # Metadata
    meta = {
        "clip_t": CLIP_T,
        "clip_h": CLIP_H,
        "clip_w": CLIP_W,
        "classes": ["no_transit", "transit"],
        "val_accuracy": round(val_correct / n_val * 100, 2),
        "best_val_loss": round(best_val_loss, 5),
        "n_train": n_train,
        "n_val": n_val,
    }
    meta_out.parent.mkdir(parents=True, exist_ok=True)
    with open(meta_out, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"Metadata saved → {meta_out}")


# ── Entry point ───────────────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser(
        description="Train transit classifier and export to ONNX"
    )
    ap.add_argument("--data", default=str(DATA_DIR), help="Training data directory")
    ap.add_argument("--out", default=str(MODEL_OUT), help="Output ONNX model path")
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument(
        "--device",
        default="auto",
        help="cpu | cuda | auto (auto: cuda if available else cpu; mps not used — Conv3d)",
    )
    args = ap.parse_args()
    train(
        data_dir=Path(args.data),
        model_out=Path(args.out),
        epochs=args.epochs,
        batch_size=args.batch,
        lr=args.lr,
        device_str=args.device,
    )


if __name__ == "__main__":
    main()
