"""サンプル ORM モデル。実モデルを足す時の雛形（不要なら消してよい）。"""

from sqlalchemy.orm import Mapped, mapped_column

from adapter.database.base import Base


class ExampleItem(Base):
    __tablename__ = "example_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(nullable=False)
