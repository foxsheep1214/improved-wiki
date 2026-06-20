"""Checkpoint management for pause/resume functionality (MEDIUM improvement #1).

Importers: ingest.py calls CheckpointManager for pause/resume support
User instruction: MEDIUM improvement #1 — pause/resume capability
Data schema: ingest_checkpoint.json with stage/progress/timestamp
"""

import json
import time
from pathlib import Path
from typing import Optional, Dict, Any


class CheckpointManager:
    """Manages ingest checkpoints for pause/resume functionality."""

    def __init__(self, runtime_dir: Path, ingest_id: str):
        self.runtime_dir = Path(runtime_dir)
        self.ingest_id = ingest_id
        self.checkpoint_file = self.runtime_dir / "ingest_checkpoint.json"

    def save_checkpoint(self, stage: str, progress: Dict[str, Any],
                       message: str = "") -> None:
        """Save checkpoint at current stage."""
        checkpoint = {
            "ingest_id": self.ingest_id,
            "timestamp": time.time(),
            "stage": stage,
            "progress": progress,
            "message": message,
        }

        with open(self.checkpoint_file, "w", encoding="utf-8") as f:
            json.dump(checkpoint, f, indent=2, ensure_ascii=False, default=str)

    def load_checkpoint(self) -> Optional[Dict[str, Any]]:
        """Load checkpoint if it exists."""
        if not self.checkpoint_file.exists():
            return None

        try:
            with open(self.checkpoint_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None

    def get_resume_point(self) -> Optional[str]:
        """Get the stage to resume from."""
        checkpoint = self.load_checkpoint()
        if not checkpoint:
            return None

        return checkpoint.get("stage")

    def is_resumable(self) -> bool:
        """Check if ingest can be resumed."""
        checkpoint = self.load_checkpoint()
        if not checkpoint:
            return False

        checkpoint_time = checkpoint.get("timestamp", 0)
        age_seconds = time.time() - checkpoint_time
        max_age = 7 * 24 * 3600  # 7 days

        return age_seconds < max_age

    def clear_checkpoint(self) -> None:
        """Clear checkpoint after successful completion."""
        if self.checkpoint_file.exists():
            self.checkpoint_file.unlink()

    def get_checkpoint_info(self) -> Optional[Dict[str, Any]]:
        """Get human-readable checkpoint info."""
        checkpoint = self.load_checkpoint()
        if not checkpoint:
            return None

        from datetime import datetime
        timestamp = checkpoint.get("timestamp", 0)
        stage = checkpoint.get("stage", "unknown")
        message = checkpoint.get("message", "")

        return {
            "stage": stage,
            "timestamp": datetime.fromtimestamp(timestamp).isoformat(),
            "message": message,
            "resumable": self.is_resumable(),
        }


class SigintHandler:
    """Handle Ctrl+C gracefully by saving checkpoint before exit."""

    def __init__(self, checkpoint_manager: CheckpointManager):
        self.checkpoint_manager = checkpoint_manager
        self.interrupted = False

    def __call__(self, signum, frame):
        """Signal handler for SIGINT."""
        self.interrupted = True
        print("\n\n⏸️  Interrupted by user (Ctrl+C)")
        print("💾 Saving checkpoint...")
        print("✅ You can resume this ingest later\n")
        exit(0)
