"""曝光场布局与偏移叠场统计的端到端测试。"""
from __future__ import annotations

import sqlite3

from .conftest import make_csv, make_layout_csv


def _upload_first(client, slice_id, rows):
    return client.post(
        f"/slices/{slice_id}/first-test",
        files={"file": ("first.csv", make_csv(rows), "text/csv")},
    )


def _upload_batch(client, slice_id, batch_no, rotation, rows):
    return client.post(
        f"/slices/{slice_id}/batches/{batch_no}",
        data={"rotation": rotation},
        files={"file": ("retest.csv", make_csv(rows), "text/csv")},
    )


def _upload_layout(client, slice_id, w, h, defect_bins, expected, fields):
    return client.post(
        f"/slices/{slice_id}/layout",
        data={
            "field_width": w,
            "field_height": h,
            "defect_bins": defect_bins,
            "expected_layout_version": expected,
        },
        files={"file": ("layout.csv", make_layout_csv(fields), "text/csv")},
    )


def _offset_dict(client, slice_id, **params):
    params.setdefault("page_size", 5000)
    resp = client.get(f"/slices/{slice_id}/offsets", params=params)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return {(i["dx"], i["dy"]): i for i in body["items"]}, body


# 标准首测：F1 场全良（除 (1,0)=C），F2 场右半缺粒
FIRST_ROWS = [
    (0, 0, "A"), (1, 0, "C"),
    (0, 1, "A"), (1, 1, "A"),
    (2, 0, "A"), (2, 1, "C"),
]


def test_layout_upload_and_offset_stats(client):
    _upload_first(client, "W001", FIRST_ROWS)
    resp = _upload_layout(client, "W001", 2, 2, "C", 0, [("F1", 0, 0), ("F2", 2, 0)])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["layout_version"] == 1
    assert body["map_version"] == 1
    assert body["field_count"] == 2
    assert body["defect_bins"] == ["C"]
    assert body["recomputed"] is False

    offsets, page = _offset_dict(client, "W001")
    assert page["layout_version"] == 1
    assert page["map_version"] == 1
    assert page["field_width"] == 2 and page["field_height"] == 2
    assert page["defect_bins"] == ["C"]
    assert page["total"] == 4  # 2x2 全部偏移

    # (0,0)：F1(0,0)=A、F2(2,0)=A -> 实测 2，无不良
    assert offsets[(0, 0)]["measured"] == 2
    assert offsets[(0, 0)]["defective"] == 0
    assert offsets[(0, 0)]["defect_fields"] == []
    # (1,0)：F1(1,0)=C 不良；F2(3,0) 缺粒不计入实测
    assert offsets[(1, 0)]["measured"] == 1
    assert offsets[(1, 0)]["defective"] == 1
    assert offsets[(1, 0)]["defect_fields"] == ["F1"]
    # (0,1)：F1(0,1)=A、F2(2,1)=C -> 实测 2，不良 1（场 F2）
    assert offsets[(0, 1)]["measured"] == 2
    assert offsets[(0, 1)]["defective"] == 1
    assert offsets[(0, 1)]["defect_fields"] == ["F2"]
    # (1,1)：F1(1,1)=A；F2(3,1) 缺粒
    assert offsets[(1, 1)]["measured"] == 1
    assert offsets[(1, 1)]["defective"] == 0


def test_layout_requires_existing_slice(client):
    resp = _upload_layout(client, "NOPE", 2, 2, "C", 0, [("F1", 0, 0)])
    assert resp.status_code == 404
    assert "不存在" in resp.json()["detail"]


def test_layout_validation(client):
    _upload_first(client, "W001", FIRST_ROWS)

    # 场矩形重叠
    r = _upload_layout(client, "W001", 2, 2, "C", 0, [("F1", 0, 0), ("F2", 1, 1)])
    assert r.status_code == 422 and "重叠" in r.json()["detail"]

    # 场数超过 64
    many = [(f"F{i}", i * 10, 0) for i in range(65)]
    r = _upload_layout(client, "W001", 1, 1, "C", 0, many)
    assert r.status_code == 422 and "上限" in r.json()["detail"]

    # 宽高越界
    assert _upload_layout(client, "W001", 0, 2, "C", 0, [("F1", 0, 0)]).status_code == 400
    assert _upload_layout(client, "W001", 2, 65, "C", 0, [("F1", 0, 0)]).status_code == 400

    # 不良档位为空
    r = _upload_layout(client, "W001", 2, 2, " , ", 0, [("F1", 0, 0)])
    assert r.status_code == 400 and "不良档位" in r.json()["detail"]

    # 所见布局版号为负
    assert _upload_layout(client, "W001", 2, 2, "C", -1, [("F1", 0, 0)]).status_code == 400

    # 场号重复
    r = client.post(
        "/slices/W001/layout",
        data={"field_width": 1, "field_height": 1, "defect_bins": "C",
              "expected_layout_version": 0},
        files={"file": ("x.csv", b"field_id,x,y\nF1,0,0\nF1,5,5\n", "text/csv")},
    )
    assert r.status_code == 400 and "场号重复" in r.json()["detail"]

    # 表头错误
    r = client.post(
        "/slices/W001/layout",
        data={"field_width": 1, "field_height": 1, "defect_bins": "C",
              "expected_layout_version": 0},
        files={"file": ("x.csv", b"a,b,c\nF1,0,0\n", "text/csv")},
    )
    assert r.status_code == 400 and "表头" in r.json()["detail"]

    # 不良档位去重排序
    r = _upload_layout(client, "W001", 2, 2, "D,C,D", 0, [("F1", 0, 0), ("F2", 2, 0)])
    assert r.status_code == 200
    assert r.json()["defect_bins"] == ["C", "D"]


def test_layout_stale_version_rejected_and_keeps_original(client):
    _upload_first(client, "W001", FIRST_ROWS)
    # 首次上传携带非 0 版号 -> 过期拒绝
    r = _upload_layout(client, "W001", 2, 2, "C", 1, [("F1", 0, 0), ("F2", 2, 0)])
    assert r.status_code == 409 and "布局版号过期" in r.json()["detail"]

    ok = _upload_layout(client, "W001", 2, 2, "C", 0, [("F1", 0, 0), ("F2", 2, 0)])
    assert ok.status_code == 200 and ok.json()["layout_version"] == 1

    # 已启用 v1 后再带 0 -> 过期拒绝，原布局保留
    stale = _upload_layout(client, "W001", 1, 1, "C", 0, [("G1", 0, 0)])
    assert stale.status_code == 409
    offsets, page = _offset_dict(client, "W001")
    assert page["layout_version"] == 1 and page["total"] == 4
    assert offsets[(1, 0)]["defect_fields"] == ["F1"]


def test_layout_replace(client):
    _upload_first(client, "W001", [(0, 0, "A"), (1, 0, "C"), (0, 1, "A"), (1, 1, "A")])
    ok = _upload_layout(client, "W001", 2, 2, "C", 0, [("F1", 0, 0)])
    assert ok.json()["layout_version"] == 1

    r = _upload_layout(client, "W001", 1, 1, "C", 1, [("F9", 1, 0)])
    assert r.status_code == 200, r.text
    assert r.json()["layout_version"] == 2

    offsets, page = _offset_dict(client, "W001")
    assert page["layout_version"] == 2 and page["total"] == 1
    # 新布局只有 1x1 一个偏移：F9 的 (0,0) 对应晶粒 (1,0)=C
    assert offsets[(0, 0)]["measured"] == 1
    assert offsets[(0, 0)]["defect_fields"] == ["F9"]

    info = client.get("/slices/W001").json()
    assert info["layout"]["version"] == 2
    assert info["layout"]["field_count"] == 1
    assert info["layout"]["field_width"] == 1


def test_retest_upload_updates_stats_incrementally(client):
    rows = FIRST_ROWS + [(5, 5, "A")]
    _upload_first(client, "W001", rows)
    _upload_layout(client, "W001", 2, 2, "C", 0, [("F1", 0, 0), ("F2", 2, 0)])

    # 复测把 (0,0) 从 A 改成 C -> 偏移 (0,0) 新增不良场 F1
    r1 = _upload_batch(client, "W001", 1, 0, [(0, 0, "C")])
    assert r1.status_code == 201
    offsets, page = _offset_dict(client, "W001")
    assert page["map_version"] == 2 and page["layout_version"] == 1
    assert offsets[(0, 0)]["defective"] == 1
    assert offsets[(0, 0)]["defect_fields"] == ["F1"]
    assert offsets[(1, 0)]["defect_fields"] == ["F1"]  # 原有不良不变

    # 复测把 (1,0) 从 C 改回 A -> 偏移 (1,0) 不良消除
    r2 = _upload_batch(client, "W001", 2, 0, [(1, 0, "A")])
    assert r2.status_code == 201
    offsets, page = _offset_dict(client, "W001")
    assert page["map_version"] == 3
    assert offsets[(1, 0)]["defective"] == 0
    assert offsets[(1, 0)]["defect_fields"] == []
    assert offsets[(0, 0)]["defect_fields"] == ["F1"]  # 上一步结果保留

    # 复测落在任何场之外 (5,5) -> 统计不变，但统计仍同步到最新图版本
    r3 = _upload_batch(client, "W001", 3, 0, [(5, 5, "C")])
    assert r3.status_code == 201
    offsets, page = _offset_dict(client, "W001")
    assert page["map_version"] == 4
    assert offsets[(0, 0)]["defect_fields"] == ["F1"]
    assert offsets[(0, 0)]["measured"] == 2


def test_reorient_updates_stats_incrementally(client):
    rows = [(0, 0, "A"), (1, 0, "A"), (0, 1, "A"), (1, 1, "A"), (2, 0, "A"), (2, 1, "A")]
    _upload_first(client, "W001", rows)
    _upload_layout(client, "W001", 2, 2, "C", 0, [("F1", 0, 0), ("F2", 2, 0)])

    # 批 1 rotation=0：覆盖 (1,0) 置为不良
    up = _upload_batch(client, "W001", 1, 0, [(1, 0, "C")])
    rev = up.json()["revision"]
    offsets, _ = _offset_dict(client, "W001")
    assert offsets[(1, 0)]["defect_fields"] == ["F1"]

    # 改向 90°：(1,0)->(0,1)，不良从偏移 (1,0) 移到 (0,1)
    r = client.post(
        "/slices/W001/batches/1/reorient",
        data={"rotation": 90, "expected_revision": rev},
    )
    assert r.status_code == 200, r.text
    offsets, page = _offset_dict(client, "W001")
    assert page["map_version"] == 3
    assert offsets[(1, 0)]["defective"] == 0
    assert offsets[(1, 0)]["defect_fields"] == []
    assert offsets[(0, 1)]["defective"] == 1
    assert offsets[(0, 1)]["defect_fields"] == ["F1"]


def test_reorient_between_layout_read_and_enable(client, monkeypatch):
    """自测场景：改向发生在新布局读图后、启用前，启用时按最新图重算，核对不良场号。"""
    import app.database as database
    import app.layout as layout_mod
    import app.services as services_mod

    rows = [(0, 0, "A"), (1, 0, "A"), (0, 1, "A"), (1, 1, "A"), (2, 0, "A"), (2, 1, "A")]
    _upload_first(client, "W001", rows)
    _upload_layout(client, "W001", 2, 2, "C", 0, [("F1", 0, 0), ("F2", 2, 0)])
    up = _upload_batch(client, "W001", 1, 0, [(1, 0, "C")])
    rev = up.json()["revision"]  # 2；(1,0) 不良，属 F1 的偏移 (1,0)

    calls = []

    def hook(slice_id, map_version):
        calls.append((slice_id, map_version))
        # 新布局读图后、启用前：提交改向 (1,0)->(0,1)
        conn = database._connect()
        try:
            services_mod.reorient_batch(conn, slice_id, 1, 90, rev)
        finally:
            conn.close()

    monkeypatch.setattr(layout_mod, "map_read_hook", hook)

    resp = _upload_layout(client, "W001", 2, 2, "C", 1, [("G1", 0, 0), ("G2", 2, 0)])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert calls == [("W001", 2)]  # 读图时图版本为 2
    assert body["recomputed"] is True  # 期间图有更新，已按最新已提交图重算
    assert body["layout_version"] == 2
    assert body["map_version"] == 3  # 首测 1 + 复测 2 + 改向 3

    # 核对不良场号：改向后不良粒在 (0,1)，属新布局 G1 的偏移 (0,1)
    offsets, page = _offset_dict(client, "W001")
    assert page["layout_version"] == 2 and page["map_version"] == 3
    assert offsets[(0, 1)]["defective"] == 1
    assert offsets[(0, 1)]["defect_fields"] == ["G1"]
    assert offsets[(1, 0)]["defective"] == 0
    assert offsets[(1, 0)]["defect_fields"] == []


def test_retest_upload_between_layout_read_and_enable(client, monkeypatch):
    """新布局读图后、启用前提交复测上传，同样按最新图重算后启用。"""
    import app.database as database
    import app.layout as layout_mod
    import app.services as services_mod
    from app.csv_utils import DieRow

    rows = [(0, 0, "A"), (1, 0, "A"), (0, 1, "A"), (1, 1, "A"), (2, 0, "A"), (2, 1, "A")]
    _upload_first(client, "W001", rows)
    _upload_layout(client, "W001", 2, 2, "C", 0, [("F1", 0, 0), ("F2", 2, 0)])

    def hook(slice_id, map_version):
        conn = database._connect()
        try:
            services_mod.submit_batch(conn, slice_id, 1, 0, [DieRow(x=2, y=0, bin="C")])
        finally:
            conn.close()

    monkeypatch.setattr(layout_mod, "map_read_hook", hook)

    resp = _upload_layout(client, "W001", 2, 2, "C", 1, [("G1", 0, 0), ("G2", 2, 0)])
    assert resp.status_code == 200, resp.text
    assert resp.json()["recomputed"] is True
    assert resp.json()["map_version"] == 2

    offsets, _ = _offset_dict(client, "W001")
    # 复测把 (2,0) 置为不良，属新布局 G2 的偏移 (0,0)
    assert offsets[(0, 0)]["defect_fields"] == ["G2"]


def test_offsets_pagination_and_version_check(client):
    rows = [(i, j, "A") for i in range(4) for j in range(3)]
    _upload_first(client, "W001", rows)
    _upload_layout(client, "W001", 4, 3, "C", 0, [("F1", 0, 0)])

    p1 = client.get("/slices/W001/offsets", params={"page": 1, "page_size": 5}).json()
    assert p1["total"] == 12 and len(p1["items"]) == 5
    assert [(i["dx"], i["dy"]) for i in p1["items"]] == [
        (0, 0), (0, 1), (0, 2), (1, 0), (1, 1)
    ]
    p3 = client.get("/slices/W001/offsets", params={"page": 3, "page_size": 5}).json()
    assert len(p3["items"]) == 2

    # 布局版号不一致 -> 409，一致 -> 200
    conflict = client.get("/slices/W001/offsets", params={"layout_version": 9})
    assert conflict.status_code == 409
    ok = client.get("/slices/W001/offsets", params={"layout_version": 1})
    assert ok.status_code == 200


def test_offsets_without_layout_and_unknown_slice(client):
    assert client.get("/slices/UNKNOWN/offsets").status_code == 404
    _upload_first(client, "W001", [(0, 0, "A")])
    r = client.get("/slices/W001/offsets")
    assert r.status_code == 404 and "尚未启用" in r.json()["detail"]


def test_max_field_dim_single_page(client):
    # 64x64 最大场：4096 个偏移一页取回，不良场号明细分批查询仍正确
    rows = [(i, j, "A") for i in range(64) for j in range(64)]
    rows[0] = (0, 0, "C")  # 左下格不良
    _upload_first(client, "W001", rows)
    r = _upload_layout(client, "W001", 64, 64, "C", 0, [("BIG", 0, 0)])
    assert r.status_code == 200, r.text

    offsets, page = _offset_dict(client, "W001", page_size=5000)
    assert page["total"] == 4096 and len(page["items"]) == 4096
    assert offsets[(0, 0)]["measured"] == 1
    assert offsets[(0, 0)]["defective"] == 1
    assert offsets[(0, 0)]["defect_fields"] == ["BIG"]
    assert offsets[(63, 63)]["measured"] == 1
    assert offsets[(63, 63)]["defective"] == 0


def test_slice_info_includes_layout(client):
    _upload_first(client, "W001", FIRST_ROWS)
    info = client.get("/slices/W001").json()
    assert info["layout"] is None

    _upload_layout(client, "W001", 2, 2, "C", 0, [("F1", 0, 0), ("F2", 2, 0)])
    info = client.get("/slices/W001").json()
    assert info["layout"]["version"] == 1
    assert info["layout"]["field_count"] == 2
    assert info["layout"]["field_width"] == 2
    assert info["layout"]["defect_bins"] == ["C"]
    assert info["layout"]["map_version"] == 1


def test_enable_failure_keeps_original_layout(client, monkeypatch):
    _upload_first(client, "W001", FIRST_ROWS)
    _upload_layout(client, "W001", 2, 2, "C", 0, [("F1", 0, 0), ("F2", 2, 0)])

    import app.layout as layout_mod

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("injected enable failure")

    monkeypatch.setattr(layout_mod, "_replace_layout_tx", boom)
    r = _upload_layout(client, "W001", 1, 1, "C", 1, [("G1", 0, 0)])
    assert r.status_code == 500
    assert "沿用原布局" in r.json()["detail"]

    # 启用失败：原布局与统计完整保留
    offsets, page = _offset_dict(client, "W001")
    assert page["layout_version"] == 1 and page["total"] == 4
    assert offsets[(1, 0)]["defect_fields"] == ["F1"]
    info = client.get("/slices/W001").json()
    assert info["layout"]["version"] == 1


def test_stat_update_failure_rolls_back_map_and_stats(client, monkeypatch):
    _upload_first(client, "W001", FIRST_ROWS)
    _upload_layout(client, "W001", 2, 2, "C", 0, [("F1", 0, 0), ("F2", 2, 0)])

    import app.layout as layout_mod

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("injected stat failure")

    monkeypatch.setattr(layout_mod, "apply_bin_changes", boom)
    r = _upload_batch(client, "W001", 1, 0, [(0, 0, "C")])
    assert r.status_code == 500

    # 任一写入失败两边都回滚：判级图未变、批次未入库、统计未变
    m = client.get("/slices/W001/map", params={"page_size": 100}).json()
    assert all(d["source"] == "first" for d in m["items"])
    assert next(d for d in m["items"] if (d["x"], d["y"]) == (0, 0))["bin"] == "A"
    info = client.get("/slices/W001").json()
    assert info["batches"] == []
    offsets, page = _offset_dict(client, "W001")
    assert page["map_version"] == 1
    assert offsets[(0, 0)]["defective"] == 0


def test_layout_persistence_across_restart(client, tmp_path, monkeypatch):
    _upload_first(client, "PERSIST", FIRST_ROWS)
    _upload_layout(client, "PERSIST", 2, 2, "C", 0, [("F1", 0, 0), ("F2", 2, 0)])
    _upload_batch(client, "PERSIST", 1, 0, [(0, 0, "C")])
    before, page_before = _offset_dict(client, "PERSIST")
    assert page_before["map_version"] == 2

    # 模拟重启：重新初始化应用（同一数据库文件）
    import importlib

    import app.config as config
    import app.database as database
    import app.errors as errors
    import app.main as main
    import app.services as services
    import app.csv_utils as csv_utils
    import app.layout as layout_mod
    import app.schemas as schemas
    import app.rotation as rotation

    for mod in (
        config, database, errors, csv_utils, rotation, schemas, layout_mod, services, main,
    ):
        importlib.reload(mod)
    database.init_db()
    from fastapi.testclient import TestClient

    with TestClient(main.app) as new_client:
        after, page_after = _offset_dict(new_client, "PERSIST")
        # 重启后统计与版本保持一致
        assert page_after["layout_version"] == page_before["layout_version"] == 1
        assert page_after["map_version"] == page_before["map_version"] == 2
        assert after == before
        assert after[(0, 0)]["defect_fields"] == ["F1"]
        info = new_client.get("/slices/PERSIST").json()
        assert info["layout"]["version"] == 1
        assert info["layout"]["map_version"] == 2


def test_docs_include_layout_endpoints(client):
    openapi = client.get("/openapi.json").json()
    assert "/slices/{slice_id}/layout" in openapi["paths"]
    assert "/slices/{slice_id}/offsets" in openapi["paths"]
