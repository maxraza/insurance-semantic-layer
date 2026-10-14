from datetime import date

import duckdb
import pytest

from isl.generate import Config, generate, write_duckdb

SMALL = Config(seed=11, quotes_per_month=600)


@pytest.fixture(scope="session")
def tables():
    return generate(SMALL)


@pytest.fixture(scope="session")
def con(tables, tmp_path_factory):
    path = tmp_path_factory.mktemp("book") / "book.duckdb"
    write_duckdb(tables, path)
    c = duckdb.connect(str(path), read_only=True)
    yield c
    c.close()


@pytest.fixture
def tiny(tmp_path):
    """An empty book with the real schema, for hand-built scenarios."""
    path = tmp_path / "tiny.duckdb"
    write_duckdb({}, path)
    c = duckdb.connect(str(path))
    yield c
    c.close()


AS_OF = date(2025, 12, 31)
