"""Role-spec schema and seat metadata.

Zero model/API calls. Metadata-only role specifications.
"""

from .rolespec import (
    SeatProfile,
    SEAT_CODES,
    AGENT_TO_SEAT,
    DEFAULT_BATTERY_BY_SEAT,
    DEFAULT_OUTPUT_FORM_BY_SEAT,
)

__all__ = [
    "SeatProfile",
    "SEAT_CODES",
    "AGENT_TO_SEAT",
    "DEFAULT_BATTERY_BY_SEAT",
    "DEFAULT_OUTPUT_FORM_BY_SEAT",
]
