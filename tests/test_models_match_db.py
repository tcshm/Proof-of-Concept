# tests/test_models_match_db.py
"""The SQLAlchemy models and the migrated database describe the same columns."""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Base

COLUMNS_SQL = text(
    """
    SELECT table_name, column_name, is_nullable
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name <> 'alembic_version'
    """
)


async def test_every_table_has_a_model(db_session: AsyncSession) -> None:
    rows = (await db_session.execute(COLUMNS_SQL)).all()
    db_tables = {row.table_name for row in rows}
    assert db_tables == set(Base.metadata.tables)
    assert len(db_tables) == 22


async def test_columns_and_nullability_match(db_session: AsyncSession) -> None:
    rows = (await db_session.execute(COLUMNS_SQL)).all()
    db_columns: dict[str, dict[str, bool]] = defaultdict(dict)
    for row in rows:
        db_columns[row.table_name][row.column_name] = row.is_nullable == "YES"

    problems: list[str] = []
    for name, table in Base.metadata.tables.items():
        model_columns = {column.name: bool(column.nullable) for column in table.columns}
        missing_in_model = set(db_columns[name]) - set(model_columns)
        missing_in_db = set(model_columns) - set(db_columns[name])
        if missing_in_model:
            problems.append(f"{name}: columns missing in model: {sorted(missing_in_model)}")
        if missing_in_db:
            problems.append(f"{name}: columns missing in database: {sorted(missing_in_db)}")
        for column, nullable in model_columns.items():
            if column in db_columns[name] and db_columns[name][column] != nullable:
                problems.append(
                    f"{name}.{column}: model nullable={nullable}, "
                    f"database nullable={db_columns[name][column]}"
                )

    assert not problems, "\n".join(problems)
