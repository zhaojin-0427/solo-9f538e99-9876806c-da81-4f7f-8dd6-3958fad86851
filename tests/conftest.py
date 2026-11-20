"""pytest 夹具：每个测试使用独立临时数据库。"""
from __future__ import annotations

import importlib
import io

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_grading.db"
    monkeypatch.setenv("GRADING_DB_PATH", str(db_path))

    import app.config as config
    import app.database as database
    import app.errors as errors
    import app.main as main
    import app.services as services
    import app.csv_utils as csv_utils
    import app.layout as layout
    import app.schemas as schemas
    import app.rotation as rotation

    for mod in (
        config,
        database,
        errors,
        csv_utils,
        rotation,
        schemas,
        layout,
        services,
        main,
    ):
        importlib.reload(mod)

    database.init_db()
    with TestClient(main.app) as c:
        yield c


def make_csv(rows: list[tuple[int, int, str]]) -> bytes:
    buf = io.StringIO()
    buf.write("x,y,bin\n")
    for x, y, b in rows:
        buf.write(f"{x},{y},{b}\n")
    return buf.getvalue().encode("utf-8")


def make_layout_csv(rows: list[tuple[str, int, int]]) -> bytes:
    buf = io.StringIO()
    buf.write("field_id,x,y\n")
    for fid, x, y in rows:
        buf.write(f"{fid},{x},{y}\n")
    return buf.getvalue().encode("utf-8")
