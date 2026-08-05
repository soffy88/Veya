"""veya.server — Layer 4 service assembly & gateway (SPEC §8, project service layer).

This package is the **project service layer**: it assembles the mounted 3O main
libraries (``platform/3O/*``) into runnable services via ``ServiceManifest`` +
dependency injection, and exposes them through REST / SSE / CLI / IM surfaces.

Boundaries (must be kept):
- Layer 4 code lives here — **never** inside the 3O main libraries.
- 3O main libraries never import ``veya.*`` (enforced by the 3O lint).
- All element access goes through ``veya.platform`` (lazy, graceful when the
  heavy optional deps of a main library are not installed).
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
