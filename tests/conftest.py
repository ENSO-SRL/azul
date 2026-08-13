"""Shared pytest fixtures / test bootstrap.

The scheduler performs lazy (function-local) imports of its repositories and
gateway, so ``app.infrastructure.repo_impl`` is not imported at module load.
``unittest.mock.patch("app.infrastructure.repo_impl.X")`` resolves its target
string via ``importlib`` + ``getattr`` and fails if the submodule was never
imported. Import the modules eagerly here so patch targets always resolve.
"""

from __future__ import annotations

import app.infrastructure.repo_impl  # noqa: F401
import app.infrastructure.azul_gateway  # noqa: F401
