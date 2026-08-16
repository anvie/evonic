from dataclasses import dataclass
from typing import Optional

@dataclass
class Order:
    id: Optional[int] = None
    description: str = ""
    status: str = "pending"

    @classmethod
    def from_dict(cls, data: dict) -> "Order":
        return cls(
            id=data.get("id"),
            description=data.get("description", ""),
            status=data.get("status", "pending")
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "status": self.status,
        }
