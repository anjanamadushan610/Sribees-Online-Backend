"""
Guardrails against the class of failure that took prod down on 2026-08-30: the
app container started but never became healthy because SQLAlchemy could not
configure its mappers / build the metadata at lifespan startup.

These do NOT touch a database — they exercise exactly the import + mapper-config
+ table-DDL-compilation path that ``init_db()`` runs, so a bad relationship or
column definition fails here in CI instead of on the EC2 box.
"""
import pytest


def test_all_models_import_and_mappers_configure():
    from sqlalchemy.orm import configure_mappers

    import app.models  # noqa: F401  — registers every model with Base.metadata

    configure_mappers()  # raises on any broken relationship / backref


def test_metadata_create_all_ddl_compiles():
    """Compile the CREATE TABLE DDL for every model against the Postgres dialect
    without executing it — catches an unrepresentable column type/constraint."""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    import app.models  # noqa: F401
    from app.config.database import Base

    for table in Base.metadata.sorted_tables:
        str(CreateTable(table).compile(dialect=postgresql.dialect()))


@pytest.mark.parametrize("table", ["orders", "order_status_history"])
def test_order_tables_have_expected_columns(table):
    import app.models  # noqa: F401
    from app.config.database import Base

    cols = set(Base.metadata.tables[table].columns.keys())
    if table == "orders":
        assert {"packed_at", "handed_to_courier_at", "delivery_failed_at"} <= cols
