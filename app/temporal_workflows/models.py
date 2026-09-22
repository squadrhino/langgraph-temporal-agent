from dataclasses import dataclass


@dataclass
class OrderActionInput:
    order_number: int
    requested_by: str
    reason: str = ""


@dataclass
class ApprovalInput:
    approved: bool
    approved_by: str
