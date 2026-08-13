"""ORM モデルの集約。alembic の autogenerate がここを import して metadata を集める。"""

from adapter.database.models.audit_log import AuditEventType, AuditLog  # noqa: F401
from adapter.database.models.config_revision import ConfigRevision  # noqa: F401
from adapter.database.models.session import Session  # noqa: F401
from adapter.database.models.user import User, normalize_email  # noqa: F401
