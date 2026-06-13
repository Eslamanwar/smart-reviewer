"""smart-code-reviewer package.

Loads environment variables from a project-level ``.env`` file (if present)
as early as possible — before any submodule (``src.config``, ``src.tools.*``)
reads ``os.getenv`` at import time. Real process environment variables always
win over ``.env`` (``override=False``), so deployments that inject config the
usual way are unaffected.
"""

from pathlib import Path

from dotenv import load_dotenv

# Project root is the parent of this ``src`` package directory.
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_ENV_PATH, override=False)
