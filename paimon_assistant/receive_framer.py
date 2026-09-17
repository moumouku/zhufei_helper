from dataclasses import dataclass


@dataclass(frozen=True)
class ReceivedEvent:
    received_at_ms: int
    payload: bytes
    raw_frame: bytes
