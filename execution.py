from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class ExecutionResult:
    accepted: bool
    order_id: Optional[str]
    notes: str


class ExecutionEngine:
    def __init__(self, live_mode: bool = False):
        self.live_mode = live_mode
        self.has_live_creds = all(
            os.getenv(k) for k in [
                "POLY_HOST",
                "POLY_CHAIN_ID",
                "POLY_PRIVATE_KEY",
                "POLY_API_KEY",
                "POLY_API_SECRET",
                "POLY_API_PASSPHRASE",
            ]
        )

    def submit(self, order: Dict[str, Any]) -> ExecutionResult:
        if not self.live_mode:
            return ExecutionResult(True, f"paper-{order['signal_id']}", "paper mode")

        if not self.has_live_creds:
            return ExecutionResult(False, None, "missing live credentials; refused order")

        # Replace this stub with the official Polymarket Python CLOB client.
        # Kept as a safety stub so the scaffold never pretends to be fully live without you wiring it deliberately.
        return ExecutionResult(False, None, "live mode requested, but Polymarket client integration placeholder is still active")
