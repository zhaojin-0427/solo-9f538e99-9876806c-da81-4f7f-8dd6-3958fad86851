# 切割分拣判级图 API

按晶粒坐标取档位的判级图服务。复测台朝向录错时，可凭修订号改向，系统只重算受影响晶粒，
不会把档位写到别的晶粒上。支持上传曝光场布局，把各曝光场的相同格位叠起来统计
（实测粒数、不良粒数、不良场号），用于排查光罩缺陷。

- 技术栈：FastAPI + SQLite（WAL 模式，单文件持久化）
- 交互式文档：启动后访问 <http://localhost:8000/docs>

## 一、CSV 格式

UTF-8 编码，固定表头。

**首测 / 复测**：坐标是以**圆心为原点的整数坐标**，同份文件内坐标必须唯一：

```csv
x,y,bin
0,0,A
1,0,A
0,1,B
```

**曝光场布局**：每行是**唯一场号 + 该场左下晶粒坐标**，同份场号必须唯一：

```csv
field_id,x,y
F1,0,0
F2,10,0
```

## 二、核心规则

1. **首测**：每片仅一份，最多 20 万粒（`GRADING_MAX_FIRST_DIES` 可配）。
2. **复测批次**：同片批号唯一，按检测先后编号（批号越大越新），可乱序上传。
3. **旋转**：提交逆时针旋转角，仅允许 `0 / 90 / 180 / 270` 度。
   - 90°：`(x,y) -> (-y,x)`；180°：`(x,y) -> (-x,-y)`；270°：`(x,y) -> (y,-x)`
   - 旋转后的全部落点必须在首测图上，否则整批拒绝，原图与已入库批次不受影响。
4. **改向**：必须携带该批当前修订号（乐观并发控制）；过期返回 `409`。
   沿用上传时的原表重算，无需重传 CSV；只重算这批**原先覆盖 ∪ 现在覆盖**的晶粒。
5. **判级取值**：每粒取覆盖它的**最高批号**复测档位；无复测覆盖时取首测档位。
6. **修订号**：每次成功提交（首测入库 / 复测入库 / 改向）都让该片版本 +1，
   该版本号即返回的修订号，同时写入对应批次。
7. **一致性**：所有写操作在单事务内串行生效；读请求在单事务快照内完成，
   每页只读到同一个已提交状态，分页接口只读物化结果表 `current_map`，不重扫各批原表。
8. **持久化**：数据写入挂载卷上的 SQLite 文件，重启后直接读取已生效的图与统计。

## 二-b、曝光场布局与偏移统计

1. **布局上传**：按**已有片号**上传布局 CSV，随表以表单字段提交统一场宽、场高
   （各 1～64 格）、不良档位（逗号分隔）和**所见布局版号**（首次为 `0`）。
   每片最多 64 个**互不重叠**的矩形场（`GRADING_MAX_FIELDS_PER_SLICE` 可配）。
2. **两阶段启用**：先在写锁之外读取当前已提交图计算统计（期间复测上传/改向仍可提交）；
   启用时校验布局版号，**过期拒绝（409）**；若期间判级图有更新，
   **按最新已提交图重算统计后再启用**。全部写入在单事务内完成，
   启用失败整体回滚、**沿用原布局**。
3. **偏移统计**：以每个场左下格为原点起算偏移 `(dx, dy)`，聚合所有场的
   实测粒数、不良粒数与不良场号；**缺粒不计入实测数**。
   统计物化留存，分页查询只读 `offset_stat` / `offset_defect_field`，不重扫判级图。
   响应同时携带**布局版号**与统计对应的**判级图版本**。
4. **增量更新**：复测上传/改向只更新**档位变化所影响的**偏移统计，
   与判级图在**同一事务**内提交，任一写入失败两边都回滚。
5. **布局版号**：首次启用为 `1`，之后每次成功启用 +1；替换布局必须携带当前版号。

## 三、快速启动（Docker Compose）

```bash
docker compose up -d --build
# 查看状态 / 日志
docker compose ps
docker compose logs -f
# 停止（数据保留在命名卷 grading-data 中）
docker compose down
# 停止并删除数据
docker compose down -v
```

服务监听 `8000` 端口，数据库位于容器内 `/data/grading.db`。

> 说明：SQLite 的写串行化在**单进程单 uvicorn worker** 下由应用锁保证，
> compose 默认即单 worker。请勿把 worker 数调到 >1（多进程需要改用 C/S 数据库）。

## 四、接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/slices/{slice_id}/first-test` | 上传首测 CSV（multipart，字段 `file`） |
| POST | `/slices/{slice_id}/batches/{batch_no}` | 上传复测 CSV（表单 `rotation` + 文件 `file`） |
| POST | `/slices/{slice_id}/batches/{batch_no}/reorient` | 改向（表单 `rotation`、`expected_revision`） |
| GET | `/slices/{slice_id}/map` | 分页取当前图（`page`、`page_size`、可选 `version`） |
| POST | `/slices/{slice_id}/layout` | 上传并启用曝光场布局（表单 `field_width`、`field_height`、`defect_bins`、`expected_layout_version` + 文件 `file`） |
| GET | `/slices/{slice_id}/offsets` | 分页取各偏移叠场统计（`page`、`page_size`、可选 `layout_version`） |
| GET | `/slices/{slice_id}` | 片信息与各批复测修订号、角度及启用中的布局 |
| GET | `/health` | 健康检查 |

### curl 示例

```bash
# 1) 首测
curl -X POST http://localhost:8000/slices/W001/first-test \
  -F "file=@first.csv"
# -> {"slice_id":"W001","revision":1,"die_count":5}

# 2) 复测批号 1，逆时针旋转 90°
curl -X POST http://localhost:8000/slices/W001/batches/1 \
  -F "rotation=90" -F "file=@retest.csv"
# -> {"slice_id":"W001","batch_no":1,"rotation":90,"revision":2,"die_count":3}

# 3) 改向（带上一步返回的修订号 2）
curl -X POST http://localhost:8000/slices/W001/batches/1/reorient \
  -F "rotation=180" -F "expected_revision=2"
# -> {"slice_id":"W001","batch_no":1,"rotation":180,"revision":3,"affected_dies":6}

# 4) 分页读当前图
curl "http://localhost:8000/slices/W001/map?page=1&page_size=500"
# 翻页时可带 version 校验一致性，版本变化返回 409：
curl "http://localhost:8000/slices/W001/map?page=2&page_size=500&version=3"

# 5) 上传并启用曝光场布局（首次所见版号为 0）
curl -X POST http://localhost:8000/slices/W001/layout \
  -F "field_width=2" -F "field_height=2" -F "defect_bins=C,D" \
  -F "expected_layout_version=0" -F "file=@layout.csv"
# -> {"slice_id":"W001","layout_version":1,"map_version":3,"field_count":2,
#     "field_width":2,"field_height":2,"defect_bins":["C","D"],"recomputed":false}

# 6) 分页读各偏移的叠场统计
curl "http://localhost:8000/slices/W001/offsets?page=1&page_size=500"
```

`map` 每项：

```json
{"x": 0, "y": 1, "bin": "R1", "source": "retest", "batch_no": 2}
```

`source` 为 `first`（首测）或 `retest`（复测）；复测时 `batch_no` 为实际生效批号。

`offsets` 每项：

```json
{"dx": 1, "dy": 0, "measured": 2, "defective": 1, "defect_fields": ["F2"]}
```

响应头部的 `layout_version` 为统计所属布局版号，`map_version` 为统计对应的判级图版本；
翻页时可带 `layout_version` 校验一致性，版号变化返回 `409`。

### 错误码约定

| 状态码 | 场景 |
| --- | --- |
| 400 | CSV 格式错误、旋转角非法、场宽/高越界、不良档位为空、分页参数非法 |
| 404 | 片不存在 / 片尚未启用布局 |
| 409 | 重复首测 / 重复批号 / 修订号过期 / 期望版本不匹配 / 布局版号过期 |
| 422 | 片尚无首测、旋转或改向后落点不在首测图上、超过 20 万粒上限、场数超 64、场矩形重叠 |
| 500 | 写入失败（事务回滚，原图、批次与原布局保留） |

## 五、数据模型（SQLite）

| 表 | 作用 |
| --- | --- |
| `slice` | 片主表，`current_ver` 为单调递增的已提交版本号 |
| `first_test` | 首测原表，`(slice_id,x,y)` 唯一 |
| `batch` | 批次主表：批号、当前旋转角、修订号 |
| `batch_row` | 复测原表（上传时原始坐标），改向沿用，不重传 |
| `coverage` | 物化覆盖关系（旋转落点 → 批号/档位） |
| `current_map` | 物化当前判级图，分页查询只读此表 |
| `layout` | 启用中的布局（每片一份）：布局版号、统一场宽/高、统计对应的判级图版本 |
| `layout_defect_bin` | 该布局的不良档位集合 |
| `field` | 曝光场：唯一场号 + 左下晶粒坐标 |
| `field_die` | 物化场格位 → 晶粒坐标映射，增量更新时定位受影响偏移 |
| `offset_stat` | 物化偏移统计（实测/不良粒数），分页查询只读此表 |
| `offset_defect_field` | 各偏移的不良场号明细 |

## 六、本地开发与测试

```bash
pip install -r requirements.txt
export GRADING_DB_PATH=./data/grading.db
uvicorn app.main:app --reload
pytest -q
```
