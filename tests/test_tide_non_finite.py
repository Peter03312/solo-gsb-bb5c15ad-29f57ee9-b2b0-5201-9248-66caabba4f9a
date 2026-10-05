"""潮位数值有限性校验与历史异常修订处理（非有限值防护）测试。

背景：旧版本不限制 ±∞/NaN——极大 JSON 数字（如 ``1e309``）会被解析为
``Infinity`` 并写入不可变修订，导致：

1. 查询该修订时因 JSON 无法序列化 Infinity 而 500；
2. 规划按 ``depth + inf`` 判定，把本应受限的分钟误判为可通航；
3. 历史不可变修订中的异常值无法清除。

本轮修复：
- **写路径**：名义潮位与误差提交必须为有限数（误差另须非负），非法提交
  ``422 VALIDATION_ERROR``，整次不生成修订（Pydantic ``allow_inf_nan=False``
  与服务层双重校验，校验错误响应本身可 JSON 序列化、不报 500）；
- **读路径**：已存修订中的非有限分钟原样保留（不改写历史），凡查询命中
  异常分钟，或单船/批次规划、确认、潮位复核与改期计算需要该分钟，一律
  ``422 NON_FINITE_TIDE_DATA``（details 给修订号与分钟），不按安全处理、
  不报 500；
- **分叉恢复**：以旧异常修订分叉时，只有异常字段被有限新值覆盖才恢复该
  分钟；另一字段及未覆盖分钟仍按原规则继承异常、继续拒绝；
- 有效数据的修订继承、误差带判定与既有接口字段保持兼容。
"""
import math

from app.database import connect

from conftest import add_node, add_segment, seed_initial_revision


JSON_HEADERS = {"content-type": "application/json"}


def _level(lo, hi, v):
    return {m: v for m in range(lo, hi + 1)}


def _insert_legacy_bad_revision(deltas):
    """模拟旧缺陷遗留：直接向不可变存储写非有限差量行（HTTP 无法再写入）。

    deltas: [(minute, level, error), ...]，基于最新修订分叉。
    """
    conn = connect()
    try:
        base = conn.execute(
            "SELECT revision FROM tide_revisions ORDER BY revision DESC LIMIT 1"
        ).fetchone()["revision"]
        rev = conn.execute(
            "INSERT INTO tide_revisions(note, base_revision) VALUES (?, ?)",
            ("旧缺陷遗留的非有限值修订（测试构造）", base),
        ).lastrowid
        conn.executemany(
            "INSERT INTO tide_deltas(revision, minute, level, error)"
            " VALUES (?, ?, ?, ?)",
            [(rev, m, lvl, err) for m, lvl, err in deltas],
        )
        return rev
    finally:
        conn.commit()
        conn.close()


def _plan_body(rev, draft=1.0, start=0, window=20):
    return {"origin": "A", "destination": "B", "start_minute": start,
            "window_minutes": window, "draft": draft,
            "height": 0.0, "margin": 0.0, "revision": rev}


# ---------------------------------------------------------------------------
# 1. 写路径：±∞/NaN 一律 422 VALIDATION_ERROR，整次不生成修订
# ---------------------------------------------------------------------------

def test_correction_rejects_huge_json_number(client):
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    # 1e309 超出 double 范围，Python json 解析为 Infinity
    r = client.post("/api/tide/corrections",
                    content='{"corrections": {"5": 1e309}}', headers=JSON_HEADERS)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    # 错误响应必须可序列化（不携带裸 Infinity），且整次不生成修订
    assert [x["revision"] for x in client.get("/api/tide/revisions").json()] == [rev1]


def test_correction_rejects_negative_inf_and_nan(client):
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    r = client.post("/api/tide/corrections",
                    content='{"corrections": {"5": -1e309}}', headers=JSON_HEADERS)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    r = client.post("/api/tide/corrections",
                    content='{"corrections": {"5": NaN}}', headers=JSON_HEADERS)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert [x["revision"] for x in client.get("/api/tide/revisions").json()] == [rev1]


def test_error_revision_rejects_non_finite(client):
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    for raw in ("1e309", "-1e309", "NaN"):
        r = client.post("/api/tide/error-revisions",
                        content=f'{{"errors": {{"3": {raw}}}}}', headers=JSON_HEADERS)
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    # 非负约束保持有效
    r = client.post("/api/tide/error-revisions", json={"errors": {3: -0.01}})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    # 整次不生成修订
    assert [x["revision"] for x in client.get("/api/tide/revisions").json()] == [rev1]


def test_finite_values_still_accepted_at_range_boundary(client):
    # 1e308 仍是有限数：不得误伤合法的大数值
    seed_initial_revision(_level(0, 19, 3.0))
    r = client.post("/api/tide/corrections",
                    json={"corrections": {5: 1e308}})
    assert r.status_code == 201, r.text
    g = client.get(
        f"/api/tide/revisions/{r.json()['revision']}",
        params={"from_minute": 5, "to_minute": 5},
    ).json()
    assert g["levels"]["5"] == 1e308


def test_service_layer_also_rejects_non_finite(tmp_path, monkeypatch):
    """绕过 HTTP 直接调用服务层时，非有限值同样 422 且不留修订（防御纵深）。"""
    from app.database import connect, init_db
    from app.services.tide import create_correction_revision, create_error_revision
    from app.errors import ApiError

    conn = connect()
    init_db(conn)
    rev = conn.execute("INSERT INTO tide_revisions(note) VALUES ('r')").lastrowid
    conn.execute("INSERT INTO tide_values(revision, minute, level) VALUES (?, 0, 1.0)",
                 (rev,))
    conn.commit()
    for fn, payload in ((create_correction_revision, {1: float("inf")}),
                        (create_error_revision, {0: float("nan")})):
        try:
            fn(conn, payload, None, None)
            assert False, "应拒绝非有限值"
        except ApiError as exc:
            assert exc.status_code == 422 and exc.code == "VALIDATION_ERROR"
    # 事务已回滚，仍只有初始修订
    n = conn.execute("SELECT COUNT(*) AS c FROM tide_revisions").fetchone()["c"]
    assert n == 1
    conn.close()


# ---------------------------------------------------------------------------
# 2. 已存非有限修订：不改写历史；查询/规划/确认/批次/复核/改期 422
# ---------------------------------------------------------------------------

def test_get_revision_non_finite_minute_returns_422_not_500(client):
    seed_initial_revision(_level(0, 19, 3.0))
    bad = _insert_legacy_bad_revision([(5, float("inf"), 0.0)])

    # 命中异常分钟 -> 422，details 明确给出修订号与分钟
    r = client.get(f"/api/tide/revisions/{bad}",
                   params={"from_minute": 0, "to_minute": 19})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "NON_FINITE_TIDE_DATA"
    assert err["details"]["revision"] == bad
    assert err["details"]["non_finite_minutes"] == [5]
    assert err["details"]["total_non_finite"] == 1
    assert "level" in err["details"]["fields"]

    # 不含异常分钟的子区间仍正常返回（异常不污染其他分钟）
    r = client.get(f"/api/tide/revisions/{bad}",
                   params={"from_minute": 0, "to_minute": 4})
    assert r.status_code == 200
    assert set(int(k) for k in r.json()["levels"]) == {0, 1, 2, 3, 4}


def test_get_revision_non_finite_error_field_reported(client):
    seed_initial_revision(_level(0, 19, 3.0))
    bad = _insert_legacy_bad_revision([(6, 2.0, float("inf"))])
    r = client.get(f"/api/tide/revisions/{bad}",
                   params={"from_minute": 6, "to_minute": 6})
    assert r.status_code == 422
    details = r.json()["error"]["details"]
    assert details["non_finite_minutes"] == [6]
    assert details["fields"] == ["error"]


def test_plan_rejects_non_finite_minute_instead_of_treating_as_safe(client):
    """无穷潮位不得把本应受限的分钟判为可通航。"""
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=6.0)
    seed_initial_revision(_level(0, 19, 2.0))   # 正常潮位：吃水 999 不可行
    bad = _insert_legacy_bad_revision([(5, float("inf"), 0.0)])

    r = client.post("/api/trips/plan", json=_plan_body(bad, draft=999.0))
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "NON_FINITE_TIDE_DATA"
    assert err["details"]["revision"] == bad
    assert err["details"]["non_finite_minutes"] == [5]

    # 绝不允许"按安全处理"：即使等待可避开异常分钟，窗口含异常也直接拒绝
    r = client.post("/api/trips/plan", json=_plan_body(bad, draft=1.0))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "NON_FINITE_TIDE_DATA"


def test_plan_non_finite_outside_window_does_not_block(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=9.0)
    seed_initial_revision(_level(0, 19, 2.0))
    bad = _insert_legacy_bad_revision([(100, float("inf"), 0.0)])
    # 异常分钟 100 在窗口 [0,20) 之外：规划照常
    r = client.post("/api/trips/plan", json=_plan_body(bad, draft=1.0))
    assert r.status_code == 200, r.text
    assert r.json()["arrival_minute"] == 2


def test_batch_plan_and_confirm_reject_non_finite(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=9.0)
    seed_initial_revision(_level(0, 19, 2.0))
    bad = _insert_legacy_bad_revision([(10, float("inf"), 0.0)])
    ships = [_plan_body(bad), _plan_body(bad)]

    r = client.post("/api/trips/batch/plan", json={"revision": bad, "ships": ships})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "NON_FINITE_TIDE_DATA"
    assert err["details"]["ship_index"] == 0

    leg = {"seg_id": 1, "origin": "A", "destination": "B",
           "start_minute": 0, "end_minute": 2, "travel_time": 2}
    body = {"revision": bad, "idempotency_key": "bad-batch-1",
            "ships": [{**ships[0], "itinerary": [leg]},
                      {**ships[1], "itinerary": [{**leg, "start_minute": 2,
                                                  "end_minute": 4}]}]}
    r = client.post("/api/trips/batch/confirm", json=body)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "NON_FINITE_TIDE_DATA"
    assert client.get("/api/trips").json() == []   # 全批不保存


def test_single_confirm_rejects_non_finite_latest_revision(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=9.0)
    rev1 = seed_initial_revision(_level(0, 19, 2.0))
    bad = _insert_legacy_bad_revision([(10, float("inf"), 0.0)])
    assert bad == rev1 + 1   # 异常修订即最新

    # 直接以异常修订规划即 422，无法取得可确认行程；手工构造确认载荷时，
    # 事务内复算同样 422（而不是 PLAN_MISMATCH / 500）
    leg = {"seg_id": 1, "origin": "A", "destination": "B",
           "start_minute": 0, "end_minute": 2, "travel_time": 2}
    payload = {**_plan_body(bad), "itinerary": [leg]}
    r = client.post("/api/trips/confirm", json=payload)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "NON_FINITE_TIDE_DATA"
    assert client.get("/api/trips").json() == []


def test_review_and_reschedule_reject_non_finite_minute(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=9.0)
    rev1 = seed_initial_revision(_level(0, 19, 2.0))
    # 在正常修订 rev1 下确认一条行程
    planned = client.post("/api/trips/plan",
                          json=_plan_body(rev1)).json()
    trip = client.post("/api/trips/confirm", json=planned).json()["trip_id"]

    bad = _insert_legacy_bad_revision([(10, float("inf"), 0.0)])

    # 只读复核命中异常分钟 -> 422（不按安全返回 safe=true，也不 500）
    r = client.post(f"/api/trips/{trip}/review",
                    json={"target_revision": bad})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "NON_FINITE_TIDE_DATA"
    assert err["details"]["non_finite_minutes"] == [10]

    # 改期确认同样 422、状态不变
    r = client.post(
        f"/api/trips/{trip}/reschedule",
        json={"target_revision": bad, "departure_minutes": [0],
              "idempotency_key": "rs-bad-1"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "NON_FINITE_TIDE_DATA"
    # 原行程未作废
    assert client.get(f"/api/trips/{trip}").json()["voided_at"] is None


# ---------------------------------------------------------------------------
# 3. 分叉恢复：只有异常字段被有限新值覆盖才恢复该分钟
# ---------------------------------------------------------------------------

def test_fork_recovers_minute_only_when_bad_field_overwritten(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=1, depth=6.0)
    seed_initial_revision(_level(0, 19, 2.0))
    # 分钟 5：level=inf；分钟 6：error=inf
    bad = _insert_legacy_bad_revision([
        (5, float("inf"), 0.0),
        (6, 2.0, float("inf")),
    ])

    # 只更正分钟 5 的名义潮位：分钟 5 恢复（误差 0 沿继承为有限），分钟 6 仍异常
    healed_lvl = client.post(
        "/api/tide/corrections",
        json={"corrections": {5: 2.0}, "base_revision": bad},
    ).json()["revision"]
    r = client.post("/api/trips/plan", json=_plan_body(healed_lvl))
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "NON_FINITE_TIDE_DATA"
    assert err["details"]["non_finite_minutes"] == [6]
    assert err["details"]["fields"] == ["error"]

    # 再以有限误差覆盖分钟 6：全窗口恢复，误差带判定恢复正常
    healed = client.post(
        "/api/tide/error-revisions",
        json={"errors": {6: 0.0}, "base_revision": healed_lvl},
    ).json()["revision"]
    r = client.get(f"/api/tide/revisions/{healed}",
                   params={"from_minute": 0, "to_minute": 9})
    assert r.status_code == 200
    r = client.post("/api/trips/plan", json=_plan_body(healed, draft=9.0, window=10))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NO_FEASIBLE_ITINERARY"
    r = client.post("/api/trips/plan", json=_plan_body(healed, draft=1.0, window=10))
    assert r.status_code == 200
    assert r.json()["arrival_minute"] == 1


def test_error_revision_does_not_heal_non_finite_level(client):
    """异常字段是名义潮位时，误差修订（只覆盖误差）不能恢复该分钟。"""
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=1, depth=6.0)
    seed_initial_revision(_level(0, 9, 2.0))
    bad = _insert_legacy_bad_revision([(5, float("inf"), 0.0)])

    # 给分钟 5 提交有限误差：名义潮位仍沿继承为 inf -> 该分钟仍拒绝
    half = client.post(
        "/api/tide/error-revisions",
        json={"errors": {5: 0.3}, "base_revision": bad},
    ).json()["revision"]
    r = client.get(f"/api/tide/revisions/{half}",
                   params={"from_minute": 5, "to_minute": 5})
    assert r.status_code == 422
    assert r.json()["error"]["details"]["fields"] == ["level"]

    # 其余字段（分钟 6 的误差）及未覆盖分钟仍按原规则继承：
    # 再更正分钟 5 的名义潮位后完全恢复
    healed = client.post(
        "/api/tide/corrections",
        json={"corrections": {5: 2.0}, "base_revision": half},
    ).json()["revision"]
    g = client.get(f"/api/tide/revisions/{healed}",
                   params={"from_minute": 5, "to_minute": 5}).json()
    assert g["levels"]["5"] == 2.0 and g["errors"]["5"] == 0.3


def test_uncovered_minutes_keep_inheriting_anomaly(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=1, depth=6.0)
    seed_initial_revision(_level(0, 9, 2.0))
    bad = _insert_legacy_bad_revision([(5, float("-inf"), 0.0)])

    # 分叉只覆盖其他分钟（7）：分钟 5 仍沿链继承 -inf
    fork = client.post(
        "/api/tide/corrections",
        json={"corrections": {7: 2.5}, "base_revision": bad},
    ).json()["revision"]
    r = client.get(f"/api/tide/revisions/{fork}",
                   params={"from_minute": 0, "to_minute": 9})
    assert r.status_code == 422
    assert r.json()["error"]["details"]["non_finite_minutes"] == [5]
    # 分钟 7 的有限覆盖值本身可在不含异常分钟的查询中读到
    g = client.get(f"/api/tide/revisions/{fork}",
                   params={"from_minute": 7, "to_minute": 7}).json()
    assert g["levels"]["7"] == 2.5


def test_immutable_bad_revision_is_never_rewritten(client):
    """异常修订本身与它在 tide_deltas 中的行保持原样（历史不改写）。"""
    seed_initial_revision(_level(0, 9, 2.0))
    bad = _insert_legacy_bad_revision([(5, float("inf"), 0.0)])
    client.post("/api/tide/corrections",
                json={"corrections": {5: 2.0}, "base_revision": bad})
    conn = connect()
    try:
        row = conn.execute(
            "SELECT level, error FROM tide_deltas WHERE revision = ? AND minute = 5",
            (bad,),
        ).fetchone()
    finally:
        conn.close()
    assert math.isinf(row["level"]) and row["error"] == 0.0


# ---------------------------------------------------------------------------
# 4. 兼容性：有效数据的修订继承与误差带判定不受影响
# ---------------------------------------------------------------------------

def test_normal_chain_remains_compatible(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=4.0)
    rev1 = seed_initial_revision({0: 2.6, **_level(1, 19, 3.0)})
    rev2 = client.post("/api/tide/error-revisions",
                       json={"errors": {0: 0.2}}).json()["revision"]
    # 误差带判定不变：4+2.6-0.2=6.4 < 6.5 -> 等至分钟 1
    r = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B", "start_minute": 0,
        "window_minutes": 20, "draft": 6.0, "height": 0.0, "margin": 0.5,
        "revision": rev2,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["revision"] == rev2
    assert body["itinerary"][0]["start_minute"] == 1
    # 修订列表接口不读逐分钟值，异常修订存在时也照常工作
    revs = client.get("/api/tide/revisions").json()
    assert [x["revision"] for x in revs] == [rev1, rev2]


def test_startup_with_legacy_non_finite_db(tmp_path, monkeypatch):
    """含非有限历史值的库启动建表/迁移正常（服务保持可用）。"""
    import sqlite3
    from app import config
    from app.database import connect, init_db
    from fastapi.testclient import TestClient
    from app.main import app

    db_file = tmp_path / "with_inf.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE tide_revisions (revision INTEGER PRIMARY KEY"
                 " AUTOINCREMENT, created_at TEXT NOT NULL DEFAULT"
                 " (datetime('now')), note TEXT)")
    conn.execute("CREATE TABLE tide_values (revision INTEGER NOT NULL,"
                 " minute INTEGER NOT NULL, level REAL NOT NULL,"
                 " error REAL NOT NULL DEFAULT 0.0,"
                 " PRIMARY KEY (revision, minute))")
    conn.execute("INSERT INTO tide_revisions(revision, note) VALUES (1, '含 inf 的旧库')")
    conn.executemany(
        "INSERT INTO tide_values(revision, minute, level, error) VALUES (1, ?, ?, ?)",
        [(m, float("inf") if m == 3 else 2.0, 0.0) for m in range(5)],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    conn = connect()
    init_db(conn)          # 建表/迁移不报错
    conn.close()

    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        # 不含异常分钟的范围查询可用
        r = c.get("/api/tide/revisions/1",
                  params={"from_minute": 0, "to_minute": 2})
        assert r.status_code == 200
        # 命中异常分钟 -> 422（明确拒绝而非 500）
        r = c.get("/api/tide/revisions/1",
                  params={"from_minute": 0, "to_minute": 4})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "NON_FINITE_TIDE_DATA"
