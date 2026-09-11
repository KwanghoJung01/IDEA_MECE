"""라우트 패키지."""
from .ideas import bp as ideas_bp  # noqa: F401
from .main import bp as main_bp  # noqa: F401
from .package import bp as package_bp  # noqa: F401
from .reports import bp as reports_bp  # noqa: F401
from .settings import bp as settings_bp  # noqa: F401

BLUEPRINTS = (main_bp, settings_bp, ideas_bp, reports_bp, package_bp)
