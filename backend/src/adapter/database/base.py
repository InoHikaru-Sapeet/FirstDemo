"""SQLAlchemy の宣言的ベース。全 ORM モデルはこれを継承する。"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
