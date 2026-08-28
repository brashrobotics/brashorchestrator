"""A Fleet Control stand-in implementing the target work-order contract."""

from __future__ import annotations

from tools.fleet_stub.server import (
    FaultInjection,
    FleetStub,
    FleetStubServer,
    serve,
)

__all__ = ["FaultInjection", "FleetStub", "FleetStubServer", "serve"]
