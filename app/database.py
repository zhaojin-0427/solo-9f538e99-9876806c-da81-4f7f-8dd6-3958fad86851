"""SQLite 连接管理与表结构。

持久化模型说明（重启后仍可读取已生效的图与统计）：
- first_test   : 首测原表，每片一份，坐标唯一
- batch        : 复测批次主表，记录批号、旋转角、修订号
- batch_row    : 复测原表（上传时的原始整数坐标 + 档位），改向不改原表
- coverage     : 物化覆盖关系（旋转落点 -> 覆盖它的批次），改向只重算受影响晶粒
- current_map  : 当前判级图，按片分页只读这一张表，不重扫各批原表
- slice        : 片主表，version 为该片已提交状态的单调递增版本号
- layout       : 曝光场布局主表（每片一份启用中的布局），version 为布局版号，
                 map_version 为统计所对应的判级图版本
- layout_defect_bin : 该布局的不良档位集合
- field        : 布局内的曝光场（唯一场号 + 左下晶粒坐标）
- field_die    : 物化场格位 -> 晶粒坐标映射（含场不存在晶粒的缺粒位置），
                 复测/改向时按它定位受影响偏移
- offset_stat  : 物化偏移统计（实测粒数、不良粒数），分页查询只读此表
- offset_defect_field : 各偏移的不良场号明细
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Iterator

from .config import settings

# 所有写操作在进程内串行化，保证“上传和改向交错时按提交顺序生效”
write_lock = threading.RLock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        settings.db_path,
        timeout=10.0,
        detect_types=sqlite3.PARSE_DECLTYPES,
    )
    conn.row_factory = sqlite3.Row
    # busy_timeout 与 connect timeout 双保险，锁竞争时等待而不是立刻失败
    conn.execute(f"PRAGMA busy_timeout = {10_000}")
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL：读写互不阻塞，读请求始终看到某个已提交快照
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS slice (
    slice_id     TEXT PRIMARY KEY,
    current_ver  INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 首测原表：每片一份，同份坐标唯一
CREATE TABLE IF NOT EXISTS first_test (
    slice_id  TEXT NOT NULL,
    x         INTEGER NOT NULL,
    y         INTEGER NOT NULL,
    bin       TEXT NOT NULL,
    PRIMARY KEY (slice_id, x, y),
    FOREIGN KEY (slice_id) REFERENCES slice(slice_id)
);

-- 复测批次：同片批号唯一，按检测先后编号
CREATE TABLE IF NOT EXISTS batch (
    slice_id    TEXT NOT NULL,
    batch_no    INTEGER NOT NULL CHECK (batch_no > 0),
    rotation    INTEGER NOT NULL CHECK (rotation IN (0, 90, 180, 270)),
    revision    INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (slice_id, batch_no),
    FOREIGN KEY (slice_id) REFERENCES slice(slice_id)
);

-- 复测原表：保存上传时的原始坐标，改向时沿用，不重传
CREATE TABLE IF NOT EXISTS batch_row (
    slice_id  TEXT NOT NULL,
    batch_no  INTEGER NOT NULL,
    x         INTEGER NOT NULL,
    y         INTEGER NOT NULL,
    bin       TEXT NOT NULL,
    PRIMARY KEY (slice_id, batch_no, x, y),
    FOREIGN KEY (slice_id, batch_no) REFERENCES batch(slice_id, batch_no)
);

-- 物化覆盖：旋转后的复测落点与批次的对应关系
CREATE TABLE IF NOT EXISTS coverage (
    slice_id  TEXT NOT NULL,
    x         INTEGER NOT NULL,
    y         INTEGER NOT NULL,
    batch_no  INTEGER NOT NULL,
    bin       TEXT NOT NULL,
    PRIMARY KEY (slice_id, x, y, batch_no),
    FOREIGN KEY (slice_id, batch_no) REFERENCES batch(slice_id, batch_no)
);
CREATE INDEX IF NOT EXISTS idx_coverage_lookup
    ON coverage (slice_id, x, y);

-- 当前判级图：分页查询只读这一张物化结果表
CREATE TABLE IF NOT EXISTS current_map (
    slice_id  TEXT NOT NULL,
    x         INTEGER NOT NULL,
    y         INTEGER NOT NULL,
    bin       TEXT NOT NULL,
    source    TEXT NOT NULL CHECK (source IN ('first', 'retest')),
    batch_no  INTEGER,
    PRIMARY KEY (slice_id, x, y),
    FOREIGN KEY (slice_id) REFERENCES slice(slice_id)
);

-- 曝光场布局主表：每片一份启用中的布局；version 为布局版号（首次启用为 1），
-- map_version 表示当前统计对应哪个已提交判级图版本
CREATE TABLE IF NOT EXISTS layout (
    slice_id    TEXT PRIMARY KEY,
    version     INTEGER NOT NULL CHECK (version > 0),
    field_w     INTEGER NOT NULL CHECK (field_w BETWEEN 1 AND 64),
    field_h     INTEGER NOT NULL CHECK (field_h BETWEEN 1 AND 64),
    map_version INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (slice_id) REFERENCES slice(slice_id)
);

-- 布局的不良档位集合
CREATE TABLE IF NOT EXISTS layout_defect_bin (
    slice_id  TEXT NOT NULL,
    bin       TEXT NOT NULL,
    PRIMARY KEY (slice_id, bin),
    FOREIGN KEY (slice_id) REFERENCES slice(slice_id)
);

-- 曝光场：唯一场号 + 左下晶粒坐标；场宽高统一记录在 layout 上
CREATE TABLE IF NOT EXISTS field (
    slice_id  TEXT NOT NULL,
    field_id  TEXT NOT NULL,
    origin_x  INTEGER NOT NULL,
    origin_y  INTEGER NOT NULL,
    PRIMARY KEY (slice_id, field_id),
    FOREIGN KEY (slice_id) REFERENCES slice(slice_id)
);

-- 物化场格位 -> 晶粒坐标映射：每个 (场, 偏移) 一行；
-- 场互不重叠，故每个晶粒坐标至多属于一个场
CREATE TABLE IF NOT EXISTS field_die (
    slice_id  TEXT NOT NULL,
    x         INTEGER NOT NULL,
    y         INTEGER NOT NULL,
    field_id  TEXT NOT NULL,
    dx        INTEGER NOT NULL,
    dy        INTEGER NOT NULL,
    PRIMARY KEY (slice_id, x, y),
    FOREIGN KEY (slice_id) REFERENCES slice(slice_id)
);
CREATE INDEX IF NOT EXISTS idx_field_die_offset
    ON field_die (slice_id, dx, dy);

-- 物化偏移统计：每个偏移一行，分页查询只读此表；缺粒不计入实测数
CREATE TABLE IF NOT EXISTS offset_stat (
    slice_id  TEXT NOT NULL,
    dx        INTEGER NOT NULL,
    dy        INTEGER NOT NULL,
    measured  INTEGER NOT NULL,
    defective INTEGER NOT NULL,
    PRIMARY KEY (slice_id, dx, dy),
    FOREIGN KEY (slice_id) REFERENCES slice(slice_id)
);

-- 各偏移的不良场号明细
CREATE TABLE IF NOT EXISTS offset_defect_field (
    slice_id  TEXT NOT NULL,
    dx        INTEGER NOT NULL,
    dy        INTEGER NOT NULL,
    field_id  TEXT NOT NULL,
    PRIMARY KEY (slice_id, dx, dy, field_id),
    FOREIGN KEY (slice_id) REFERENCES slice(slice_id)
);
"""


def init_db() -> None:
    import os

    db_dir = os.path.dirname(settings.db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = _connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def get_conn() -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：每请求一个连接，请求结束关闭。"""
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()
