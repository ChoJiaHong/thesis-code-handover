from __future__ import annotations

from core.request_dropper import RequestDropper, RequestTimeoutError
from processor.single_processor import SingleFrameProcessor


class DroppableSingleProcessor:
    """Wraps a :class:`SingleFrameProcessor` with request dropping logic.

    This wrapper ensures that requests exceeding the configured wait time
    are discarded before GPU resources are used. Services should interact
    with this class instead of ``RequestDropper`` directly so the dropping
    behaviour remains encapsulated within business logic.
    """

    def __init__(self, worker, dropper: RequestDropper | None = None):
        self._processor = SingleFrameProcessor(worker)
        self._dropper = dropper or RequestDropper()

    def mark(self) -> float:
        """Mark the enqueue time for an incoming request."""
        return self._dropper.mark()

    def predict(self, frame, enqueue_time: float):
        """Run prediction if the request has not timed out."""
        return self._dropper.run(self._processor.predict, enqueue_time, frame)

