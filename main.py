"""App Service entry point.

App Service routes traffic to port 8000 and expects `main:app`; the application
itself lives in the package.
"""

from llm_metering.ui.server import app

__all__ = ["app"]
