"""Independent safety policy enforcement.

This package never consults the LLM and never accepts a policy override from a
generated plan. It does not replace a safety-certified low-level controller or a
physical emergency stop.
"""

from __future__ import annotations

__version__ = "0.1.0"
