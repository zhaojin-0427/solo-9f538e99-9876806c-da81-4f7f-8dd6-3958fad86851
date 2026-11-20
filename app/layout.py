"""曝光场布局与偏移叠场统计。

排查光罩缺陷时，把各曝光场的相同格位叠起来看：以每个场左下晶粒为原点，
按 (dx, dy) 偏移聚合所有场的实测粒数、不良粒数与不良场号。

两阶段启用（新布局计算期间复测上传/改向仍可提交）：
1. 读图计算：不加写锁，在只读事务里取当前已提交图快照，算出全部偏移统计；
2. 启用：持写锁开事务，校验所见布局版号（过期拒绝），若期间判级图有更新，
   按最新已提交图重算统计后再启用；全部写入在单事务内完成，
   任一失败整体回滚，沿用原布局。

增量更新：复测上传/改向提交时，只对档位发生变化的晶粒，经 field_die 定位
受影响偏移，同步维护 offset_stat / offset_defect_field，与判级图同事务提交。
"""
from __future__ import annotations

import sqlite3
from typing import Callable

from .config import settings
from .csv_utils import LayoutRow
from .database import write_lock
from .errors import BAD_REQUEST, CONFLICT, INTERNAL, NOT_FOUND, UNPROCESSABLE, ServiceError

# 测试挂钩：新布局读图后、启用前调用，参数为 (片号, 读到的判级图版本)。
# 用于在自测中注入一次并发的复测上传/改向，验证“按最新已提交图重算后再启用”。
map_read_hook: Callable[[str, int], None] | None = None


class _Stats:
    """一次布局统计计算的中间结果。"""

    __slots__ = ("field_die_rows", "offset_stat_rows", "defect_field_rows")

    def __init__(
        self,
        field_die_rows: list[tuple[int, int, str, int, int]],
        offset_stat_rows: list[tuple[int, int, int, int]],
        defect_field_rows: list[tuple[int, int, str]],
    ) -> None:
        self.field_die_rows = field_die_rows
        self.offset_stat_rows = offset_stat_rows
        self.defect_field_rows = defect_field_rows


def _parse_defect_bins(raw: str) -> list[str]:
    """解析逗号分隔的不良档位，去重后排序返回。"""
    bins: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        b = part.strip()
        if not b:
            continue
        if len(b) > settings.max_bin_len:
            raise ServiceError(BAD_REQUEST, f"不良档位长度超过 {settings.max_bin_len}: {b}")
        if b not in seen:
            seen.add(b)
            bins.append(b)
    if not bins:
        raise ServiceError(BAD_REQUEST, "不良档位不能为空")
    return sorted(bins)


def _check_overlap(fields: list[LayoutRow], w: int, h: int) -> None:
    """校验所有场矩形互不重叠（场数 <= 64，逐对检查即可）。"""
    rects = [(f.x, f.y, f.field_id) for f in fields]
    for i in range(len(rects)):
        ax, ay, aid = rects[i]
        for j in range(i + 1, len(rects)):
            bx, by, bid = rects[j]
            if ax < bx + w and bx < ax + w and ay < by + h and by < ay + h:
                raise ServiceError(
                    UNPROCESSABLE,
                    f"场 {aid} 与场 {bid} 的矩形区域重叠，各场必须互不重叠",
                )


def _compute_stats(
    fields: list[LayoutRow],
    w: int,
    h: int,
    defect_set: set[str],
    dies: dict[tuple[int, int], str],
) -> _Stats:
    """按当前图计算全部偏移统计。缺粒（图上无此晶粒）不计入实测数。"""
    field_die_rows: list[tuple[int, int, str, int, int]] = []
    measured = [[0] * h for _ in range(w)]
    defect_fields: dict[tuple[int, int], list[str]] = {}
    for f in fields:
        for dx in range(w):
            for dy in range(h):
                x, y = f.x + dx, f.y + dy
                field_die_rows.append((x, y, f.field_id, dx, dy))
                b = dies.get((x, y))
                if b is None:
                    continue  # 缺粒不计入实测数
                measured[dx][dy] += 1
                if b in defect_set:
                    defect_fields.setdefault((dx, dy), []).append(f.field_id)
    offset_stat_rows = [
        (dx, dy, measured[dx][dy], len(defect_fields.get((dx, dy), ())))
        for dx in range(w)
        for dy in range(h)
    ]
    defect_field_rows = [
        (dx, dy, fid) for (dx, dy), fids in defect_fields.items() for fid in fids
    ]
    return _Stats(field_die_rows, offset_stat_rows, defect_field_rows)


def _load_map(cur: sqlite3.Cursor, slice_id: str) -> tuple[int, dict[tuple[int, int], str]]:
    """在当前事务里读取片版本与整张判级图。片不存在返回 None 版本由调用方处理。"""
    cur.execute("SELECT current_ver FROM slice WHERE slice_id = ?", (slice_id,))
    row = cur.fetchone()
    if row is None:
        return -1, {}
    version = int(row[0])
    cur.execute("SELECT x, y, bin FROM current_map WHERE slice_id = ?", (slice_id,))
    dies = {(int(r[0]), int(r[1])): str(r[2]) for r in cur.fetchall()}
    return version, dies


def _replace_layout_tx(
    cur: sqlite3.Cursor,
    slice_id: str,
    layout_version: int,
    w: int,
    h: int,
    defect_bins: list[str],
    fields: list[LayoutRow],
    stats: _Stats,
    map_version: int,
) -> None:
    """在事务内整体替换布局与统计（先删后插，失败随事务回滚）。"""
    for table in (
        "offset_defect_field",
        "offset_stat",
        "field_die",
        "field",
        "layout_defect_bin",
        "layout",
    ):
        cur.execute(f"DELETE FROM {table} WHERE slice_id = ?", (slice_id,))

    cur.execute(
        "INSERT INTO layout(slice_id, version, field_w, field_h, map_version) "
        "VALUES (?, ?, ?, ?, ?)",
        (slice_id, layout_version, w, h, map_version),
    )
    cur.executemany(
        "INSERT INTO layout_defect_bin(slice_id, bin) VALUES (?, ?)",
        [(slice_id, b) for b in defect_bins],
    )
    cur.executemany(
        "INSERT INTO field(slice_id, field_id, origin_x, origin_y) VALUES (?, ?, ?, ?)",
        [(slice_id, f.field_id, f.x, f.y) for f in fields],
    )
    cur.executemany(
        "INSERT INTO field_die(slice_id, x, y, field_id, dx, dy) VALUES (?, ?, ?, ?, ?, ?)",
        [(slice_id, x, y, fid, dx, dy) for (x, y, fid, dx, dy) in stats.field_die_rows],
    )
    cur.executemany(
        "INSERT INTO offset_stat(slice_id, dx, dy, measured, defective) VALUES (?, ?, ?, ?, ?)",
        [(slice_id, dx, dy, m, d) for (dx, dy, m, d) in stats.offset_stat_rows],
    )
    cur.executemany(
        "INSERT INTO offset_defect_field(slice_id, dx, dy, field_id) VALUES (?, ?, ?, ?)",
        [(slice_id, dx, dy, fid) for (dx, dy, fid) in stats.defect_field_rows],
    )


def upload_layout(
    conn: sqlite3.Connection,
    slice_id: str,
    field_width: int,
    field_height: int,
    defect_bins_raw: str,
    expected_layout_version: int,
    fields: list[LayoutRow],
) -> dict:
    """上传并启用曝光场布局：两阶段（读图计算 -> 校验版号后启用）。"""
    if not (1 <= field_width <= settings.max_field_dim):
        raise ServiceError(
            BAD_REQUEST, f"场宽需在 1 到 {settings.max_field_dim} 格之间"
        )
    if not (1 <= field_height <= settings.max_field_dim):
        raise ServiceError(
            BAD_REQUEST, f"场高需在 1 到 {settings.max_field_dim} 格之间"
        )
    if expected_layout_version < 0:
        raise ServiceError(BAD_REQUEST, "所见布局版号不能为负，首次上传为 0")
    defect_bins = _parse_defect_bins(defect_bins_raw)
    if len(fields) > settings.max_fields_per_slice:
        raise ServiceError(
            UNPROCESSABLE,
            f"场数 {len(fields)} 超过每片上限 {settings.max_fields_per_slice}",
        )
    _check_overlap(fields, field_width, field_height)
    defect_set = set(defect_bins)

    # 阶段一：读当前已提交图并计算统计（不加写锁，复测上传/改向可并发提交）
    cur = conn.cursor()
    cur.execute("BEGIN")
    try:
        snapshot_version, dies = _load_map(cur, slice_id)
        if snapshot_version < 0:
            raise ServiceError(NOT_FOUND, f"片 {slice_id} 不存在，请先上传首测")
    finally:
        conn.rollback()
    stats = _compute_stats(fields, field_width, field_height, defect_set, dies)

    # 测试挂钩：读图后、启用前，便于注入一次并发的图更新
    if map_read_hook is not None:
        map_read_hook(slice_id, snapshot_version)

    # 阶段二：持写锁启用。若期间图有更新，按最新已提交图重算后再启用
    recomputed = False
    with write_lock:
        try:
            cur.execute("BEGIN IMMEDIATE")
            map_version, _ = _load_map(cur, slice_id)
            if map_version < 0:
                raise ServiceError(NOT_FOUND, f"片 {slice_id} 不存在，请先上传首测")

            cur.execute(
                "SELECT version FROM layout WHERE slice_id = ?", (slice_id,)
            )
            row = cur.fetchone()
            current_layout_version = int(row[0]) if row is not None else 0
            if current_layout_version != expected_layout_version:
                raise ServiceError(
                    CONFLICT,
                    f"布局版号过期: 当前版号为 {current_layout_version}，"
                    f"提交携带的是 {expected_layout_version}",
                )

            if map_version != snapshot_version:
                # 期间判级图有更新：按最新已提交图重算统计后再启用
                _, dies = _load_map(cur, slice_id)
                stats = _compute_stats(
                    fields, field_width, field_height, defect_set, dies
                )
                recomputed = True

            new_version = current_layout_version + 1
            _replace_layout_tx(
                cur,
                slice_id,
                new_version,
                field_width,
                field_height,
                defect_bins,
                fields,
                stats,
                map_version,
            )
            conn.commit()
        except ServiceError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise ServiceError(500, f"启用失败，沿用原布局: {exc}") from exc

    return {
        "slice_id": slice_id,
        "layout_version": new_version,
        "map_version": map_version,
        "field_count": len(fields),
        "field_width": field_width,
        "field_height": field_height,
        "defect_bins": defect_bins,
        "recomputed": recomputed,
    }


def apply_bin_changes(
    cur: sqlite3.Cursor, slice_id: str, map_version: int
) -> int:
    """复测/改向提交时增量维护偏移统计（与判级图同事务）。

    依赖调用方已建好的 TEMP 表 bin_change(slice_id, x, y, old_bin, new_bin)，
    只处理档位实际变化且落在某个场内的晶粒；返回受影响偏移数。
    片上没有启用中的布局时什么都不做。
    """
    cur.execute("SELECT 1 FROM layout WHERE slice_id = ?", (slice_id,))
    if cur.fetchone() is None:
        return 0

    # 1) 档位变化的晶粒先摘除其不良场号记录
    cur.execute(
        """
        DELETE FROM offset_defect_field
         WHERE slice_id = ?
           AND (dx, dy, field_id) IN (
                SELECT fd.dx, fd.dy, fd.field_id
                  FROM bin_change bc
                  JOIN field_die fd
                    ON fd.slice_id = bc.slice_id AND fd.x = bc.x AND fd.y = bc.y
                 WHERE bc.slice_id = ?)
        """,
        (slice_id, slice_id),
    )
    # 2) 新档位仍属不良的重新登记
    cur.execute(
        """
        INSERT OR IGNORE INTO offset_defect_field(slice_id, dx, dy, field_id)
        SELECT bc.slice_id, fd.dx, fd.dy, fd.field_id
          FROM bin_change bc
          JOIN field_die fd
            ON fd.slice_id = bc.slice_id AND fd.x = bc.x AND fd.y = bc.y
          JOIN layout_defect_bin db
            ON db.slice_id = bc.slice_id AND db.bin = bc.new_bin
         WHERE bc.slice_id = ?
        """,
        (slice_id,),
    )
    # 3) 只重算受影响偏移的不良粒数（实测数不变：晶粒集合自首测后固定）
    cur.execute(
        """
        UPDATE offset_stat
           SET defective = (
               SELECT COUNT(*) FROM offset_defect_field odf
                WHERE odf.slice_id = offset_stat.slice_id
                  AND odf.dx = offset_stat.dx
                  AND odf.dy = offset_stat.dy)
         WHERE slice_id = ?
           AND (dx, dy) IN (
                SELECT DISTINCT fd.dx, fd.dy
                  FROM bin_change bc
                  JOIN field_die fd
                    ON fd.slice_id = bc.slice_id AND fd.x = bc.x AND fd.y = bc.y
                 WHERE bc.slice_id = ?)
        """,
        (slice_id, slice_id),
    )
    affected_offsets = cur.rowcount
    # 4) 统计已同步到本次提交的判级图版本
    cur.execute(
        "UPDATE layout SET map_version = ? WHERE slice_id = ?",
        (map_version, slice_id),
    )
    return int(affected_offsets)


def get_layout_summary(cur: sqlite3.Cursor, slice_id: str) -> dict | None:
    """片信息里的布局摘要；无布局返回 None。"""
    cur.execute(
        "SELECT version, field_w, field_h, map_version FROM layout WHERE slice_id = ?",
        (slice_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    cur.execute("SELECT COUNT(*) FROM field WHERE slice_id = ?", (slice_id,))
    field_count = int(cur.fetchone()[0])
    cur.execute(
        "SELECT bin FROM layout_defect_bin WHERE slice_id = ? ORDER BY bin",
        (slice_id,),
    )
    defect_bins = [str(r[0]) for r in cur.fetchall()]
    return {
        "version": int(row[0]),
        "field_count": field_count,
        "field_width": int(row[1]),
        "field_height": int(row[2]),
        "defect_bins": defect_bins,
        "map_version": int(row[3]),
    }


def get_offset_page(
    conn: sqlite3.Connection,
    slice_id: str,
    page: int,
    page_size: int,
    expected_layout_version: int | None = None,
) -> dict:
    """分页取各偏移的叠场统计，只读物化统计表，不重扫判级图。"""
    if page < 1:
        raise ServiceError(BAD_REQUEST, "页码 page 从 1 开始")
    if page_size < 1 or page_size > settings.max_page_size:
        raise ServiceError(
            BAD_REQUEST, f"页大小需在 1 到 {settings.max_page_size} 之间"
        )

    cur = conn.cursor()
    cur.execute("BEGIN")
    try:
        cur.execute(
            "SELECT version, field_w, field_h, map_version FROM layout WHERE slice_id = ?",
            (slice_id,),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute("SELECT 1 FROM slice WHERE slice_id = ?", (slice_id,))
            if cur.fetchone() is None:
                raise ServiceError(NOT_FOUND, f"片 {slice_id} 不存在")
            raise ServiceError(NOT_FOUND, f"片 {slice_id} 尚未启用曝光场布局")
        layout_version, w, h, map_version = (int(row[0]), int(row[1]), int(row[2]), int(row[3]))
        if expected_layout_version is not None and expected_layout_version != layout_version:
            raise ServiceError(
                CONFLICT,
                f"布局版号已变化: 当前 {layout_version}，请求期望 {expected_layout_version}",
            )

        cur.execute(
            "SELECT bin FROM layout_defect_bin WHERE slice_id = ? ORDER BY bin",
            (slice_id,),
        )
        defect_bins = [str(r[0]) for r in cur.fetchall()]

        cur.execute(
            "SELECT COUNT(*) FROM offset_stat WHERE slice_id = ?", (slice_id,)
        )
        total = int(cur.fetchone()[0])
        offset = (page - 1) * page_size
        cur.execute(
            "SELECT dx, dy, measured, defective FROM offset_stat "
            "WHERE slice_id = ? ORDER BY dx, dy LIMIT ? OFFSET ?",
            (slice_id, page_size, offset),
        )
        page_rows = cur.fetchall()

        # 本页偏移的不良场号明细（分批 IN 查询，避免参数过多）
        defect_map: dict[tuple[int, int], list[str]] = {}
        page_offsets = [(int(r[0]), int(r[1])) for r in page_rows]
        chunk = 500
        for i in range(0, len(page_offsets), chunk):
            part = page_offsets[i : i + chunk]
            placeholders = ",".join("(?,?)" for _ in part)
            params: list = [slice_id]
            for dx, dy in part:
                params.extend([dx, dy])
            cur.execute(
                f"SELECT dx, dy, field_id FROM offset_defect_field "
                f"WHERE slice_id = ? AND (dx, dy) IN (VALUES {placeholders}) "
                f"ORDER BY field_id",
                params,
            )
            for dx, dy, fid in cur.fetchall():
                defect_map.setdefault((int(dx), int(dy)), []).append(str(fid))

        items = [
            {
                "dx": int(r[0]),
                "dy": int(r[1]),
                "measured": int(r[2]),
                "defective": int(r[3]),
                "defect_fields": defect_map.get((int(r[0]), int(r[1])), []),
            }
            for r in page_rows
        ]
    finally:
        # 只读事务直接回滚结束（不产生任何写入）
        conn.rollback()

    return {
        "slice_id": slice_id,
        "layout_version": layout_version,
        "map_version": map_version,
        "field_width": w,
        "field_height": h,
        "defect_bins": defect_bins,
        "page": page,
        "page_size": page_size,
        "total": total,
        "items": items,
    }
