"""Context, state lifecycle, and Asset management module for Me'ak."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

@dataclass
class UserAsset:
    asset_id: str
    title: str
    purchase_date: str
    warranty_info: str
    next_maintenance_date: str | None = None

class ConversationManager:
    def __init__(self):
        self.history: list[dict[str, str]] = []
        self.active_draft: Any | None = None
        self.user_assets: list[UserAsset] = []

    def append_message(self, role: str, content: str):
        self.history.append({"role": role, "content": content})

    def get_recent_context(self, limit: int = 5) -> str:
        recent = self.history[-limit:]
        return "\n".join([f"{m['role']}: {m['content']}" for m in recent])

    def complete_draft_to_asset(self, draft_data: dict[str, Any]) -> UserAsset:
        """Transforms a completed execution into a long-term managed Asset."""
        new_asset = UserAsset(
            asset_id=f"ast_{len(self.user_assets) + 1}",
            title=draft_data.get("title", "منتج/خدمة جديدة"),
            purchase_date="2026-09-14",
            warranty_info="ضمان لمدة سنة",
            next_maintenance_date="2026-12-14"
        )
        self.user_assets.append(new_asset)
        self.active_draft = None  # Clear active draft post-execution
        return new_asset
