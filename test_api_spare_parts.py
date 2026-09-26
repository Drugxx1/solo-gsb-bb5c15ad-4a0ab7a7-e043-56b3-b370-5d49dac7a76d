"""关阀作业备件包采购预览 API 端到端测试: FastAPI TestClient + 临时 SQLite。

重点验证: 独立 API 可用、满足/不足/空清单三类结果、录入校验 422、
以及**只读**——不修改当前管网、不写入历史方案、不改变原方案。
"""
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")

import sys  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "app"))

from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402
from demo import DEMO_NETWORK  # noqa: E402

_client_cm = TestClient(main.app)
client = _client_cm.__enter__()  # 进入生命周期: lifespan 建表

ok = 0


def check(cond, msg):
    global ok
    assert cond, "FAIL: " + msg
    ok += 1


# 备援方案管网: 计划关闭 V1+V2
NET_FO = {
    "nodes": [
        {"id": "S", "role": "inlet"}, {"id": "M", "role": "normal"},
        {"id": "X", "role": "normal"}, {"id": "L", "role": "leak"},
        {"id": "H", "role": "must_keep"},
    ],
    "segments": [
        {"id": "p1", "endpoint_a": "S", "endpoint_b": "M"},
        {"id": "p2", "endpoint_a": "M", "endpoint_b": "L"},
        {"id": "p4", "endpoint_a": "M", "endpoint_b": "X"},
        {"id": "p3", "endpoint_a": "S", "endpoint_b": "H"},
    ],
    "valves": [
        {"id": "V1", "segment_id": "p1", "cost": 1},
        {"id": "V2", "segment_id": "p2", "cost": 2},
    ],
}
VD = {"V1": {"gasket": 3, "bolt": 2},
      "V2": {"gasket": 1, "seal": 4}}
PKGS = [
    {"id": "P1", "price": 10, "items": {"gasket": 4}},
    {"id": "P2", "price": 5, "items": {"bolt": 2, "seal": 4}},
]

# 健康检查
r = client.get("/api/health")
check(r.status_code == 200, "health")

# ---- 可行方案上的备件包采购预览 ----
r = client.post("/api/plan-failover", json=NET_FO)
check(r.status_code == 200, f"failover plan {r.status_code}")
fo = r.json()
check(fo["feasible"] and fo["closed_valves"] == ["V1", "V2"], "failover saved")
pid = fo["id"]
before = client.get(f"/api/plans/{pid}").json()
plans_before = client.get("/api/plans?limit=100").json()["plans"]
net_before = client.get("/api/network").json()

r = client.post(f"/api/plans/{pid}/spare-parts",
                json={"valve_demands": VD, "packages": PKGS})
check(r.status_code == 200, f"spare parts status {r.status_code} {r.text}")
res = r.json()
check(res["ok"] and res["satisfied"] and not res["empty"], "flags")
check(res["plan_id"] == pid and res["solver"] == "failover", "meta fields")
check(res["closed_valves"] == ["V1", "V2"], "closed valves echo")
check(res["selected_package_ids"] == ["P1", "P2"],
      f"selected {res['selected_package_ids']}")
check(res["total_price"] == 15, "total price")
check(res["merged_demand"] == {"gasket": 4, "bolt": 2, "seal": 4},
      "merged demand")
check(res["total_demand"] == 10 and res["arrival_total"] == 10
      and res["surplus_total"] == 0 and res["shortage_total"] == 0, "totals")
summary = {row["part_type"]: row for row in res["part_summary"]}
check(summary["gasket"] == {"part_type": "gasket", "demand": 4,
                            "arrival": 4, "surplus": 0, "shortage": 0},
      f"gasket summary {summary['gasket']}")
check(summary["seal"]["shortage"] == 0 and summary["bolt"]["surplus"] == 0,
      "other summaries")
# 各阀需求回显
vd_by_valve = {row["valve_id"]: row for row in res["valve_demands"]}
check(vd_by_valve["V1"]["segment_id"] == "p1", "valve echo segment")
check("只读" in res["message"], "readonly note")

# ---- 只读: 原方案、历史列表、当前管网均未改变 ----
check(client.get(f"/api/plans/{pid}").json() == before, "plan unchanged")
check(client.get("/api/plans?limit=100").json()["plans"] == plans_before,
      "no plan saved by spare preview")
check(client.get("/api/network").json() == net_before, "network untouched")

# ---- 同价字典序决胜: A、B 同价均可独力覆盖, 选 A ----
r = client.post(f"/api/plans/{pid}/spare-parts", json={
    "valve_demands": {"V1": {"gasket": 2}, "V2": {}},
    "packages": [
        {"id": "B", "price": 5, "items": {"gasket": 2}},
        {"id": "A", "price": 5, "items": {"gasket": 2}},
    ],
})
res = r.json()
check(r.status_code == 200 and res["selected_package_ids"] == ["A"],
      f"tie {res.get('selected_package_ids')}")
check(res["total_price"] == 5, "tie price")

# ---- 全部采购包仍不足: 逐类缺口 ----
r = client.post(f"/api/plans/{pid}/spare-parts", json={
    "valve_demands": VD,
    "packages": [
        {"id": "P1", "price": 10, "items": {"gasket": 4}},
        {"id": "P2", "price": 5, "items": {"bolt": 1, "seal": 1}},
    ],
})
check(r.status_code == 200, f"shortage status {r.status_code}")
res = r.json()
check(not res["ok"] and not res["satisfied"]
      and res["reason_code"] == "INSUFFICIENT_PACKAGES", "shortage flags")
check(res["total_demand"] == 10 and res["arrival_total"] == 6
      and res["shortage_total"] == 4 and res["surplus_total"] == 0,
      "shortage totals")
check(res["selected_package_ids"] == [] and res["total_price"] is None,
      "shortage selection/price")
by_kind = {row["part_type"]: (row["arrival"], row["shortage"])
           for row in res["part_summary"]}
check(by_kind == {"gasket": (4, 0), "bolt": (1, 1), "seal": (1, 3)},
      f"shortage per kind {by_kind}")

# ---- 无备件需求: 零费用空清单 ----
r = client.post(f"/api/plans/{pid}/spare-parts",
                json={"valve_demands": {"V1": {}, "V2": {}}, "packages": []})
check(r.status_code == 200, f"empty status {r.status_code}")
res = r.json()
check(res["ok"] and res["satisfied"] and res["empty"], "empty flags")
check(res["selected_package_ids"] == [] and res["selected_packages"] == []
      and res["part_summary"] == [] and res["merged_demand"] == {},
      "empty payload")
check(res["total_price"] == 0 and res["total_demand"] == 0
      and res["arrival_total"] == 0 and res["surplus_total"] == 0
      and res["shortage_total"] == 0, "empty zeros")
check("零费用空清单" in res["message"], "empty message")

# ---- 空请求体: 缺少全部计划阀门录入 ----
r = client.post(f"/api/plans/{pid}/spare-parts", json={})
check(r.status_code == 422, f"missing demands should 422, got {r.status_code}")
errs = r.json()["detail"]["errors"]
check(any("缺少计划阀门的备件需求: V1, V2" in e for e in errs),
      f"missing demand errors {errs}")

# ---- 方案不存在 ----
r = client.post("/api/plans/9999/spare-parts", json={})
check(r.status_code == 404, f"404 got {r.status_code}")

# ---- 不可行方案: 不能做备件包采购预览 ----
BAD = {
    "nodes": [
        {"id": "S", "role": "inlet"}, {"id": "L", "role": "leak"},
        {"id": "H", "role": "must_keep"},
    ],
    "segments": [
        {"id": "p1", "endpoint_a": "S", "endpoint_b": "L"},
        {"id": "p2", "endpoint_a": "S", "endpoint_b": "H"},
    ],
    "valves": [],
}
r = client.post("/api/plan", json=BAD)
bad_plan = r.json()
check(bad_plan["feasible"] is False, "infeasible plan saved")
bad_id = bad_plan["id"]
r = client.post(f"/api/plans/{bad_id}/spare-parts", json={})
check(r.status_code == 422, f"infeasible spare {r.status_code}")
errs = r.json()["detail"]["errors"]
check(any("不可行" in e and str(bad_id) in e for e in errs),
      f"infeasible msg {errs}")

# ---- 录入校验: 422 + detail.errors ----
def expect_422(body, keys):
    rr = client.post(f"/api/plans/{pid}/spare-parts", json=body)
    check(rr.status_code == 422, f"422 for {keys}: got {rr.status_code} {rr.text}")
    es = rr.json()["detail"]["errors"]
    joined = " | ".join(es)
    for key in keys:
        check(key in joined, f"error contains {key!r}; got: {es}")


# 缺少 / 多余阀门
expect_422({"valve_demands": {"V1": {"gasket": 1}}},
           ["缺少计划阀门的备件需求: V2"])
expect_422({"valve_demands": {"V1": {}, "V2": {}, "V9": {}}},
           ["提供了非计划阀门的备件需求(多余编号): V9"])
# 数量非正整数
for bad in [0, -1, 1.5, True, None]:
    expect_422({"valve_demands": {"V1": {"gasket": bad}, "V2": {}}},
               ["数量必须是正整数"])
# 采购包超过 12 个
r = client.post(f"/api/plans/{pid}/spare-parts", json={
    "valve_demands": VD,
    "packages": [{"id": f"P{i}", "price": 1, "items": {"gasket": 1}}
                 for i in range(13)],
})
check(r.status_code == 422, f"13 packages 422 got {r.status_code}")
errs = r.json()["detail"]["errors"]
check(any("超过上限 12 个" in e for e in errs), f"13 packages errors {errs}")
# 编号缺失 / 重复(有合并需求时才校验采购包)
expect_422({"valve_demands": {"V1": {"gasket": 1}, "V2": {}},
            "packages": [{"price": 1, "items": {}}]},
           ["缺少采购包编号"])
expect_422({"valve_demands": {"V1": {"gasket": 1}, "V2": {}},
            "packages": [{"id": "A", "price": 1, "items": {}},
                         {"id": "A", "price": 1, "items": {}}]},
           ["采购包编号重复: A"])
# 价格非正整数
for bad in [0, -1, 1.5, True, None]:
    expect_422({"valve_demands": {"V1": {"gasket": 1}, "V2": {}},
                "packages": [{"id": "A", "price": bad, "items": {}}]},
               ["价格必须是正整数"])
# 件数非法
for bad in [-1, 1.5, True, None]:
    expect_422({"valve_demands": {"V1": {"gasket": 1}, "V2": {}},
                "packages": [{"id": "A", "price": 1,
                              "items": {"gasket": bad}}]},
               ["件数必须是非负整数"])
# 多余备件种类
expect_422({"valve_demands": {"V1": {"gasket": 1}, "V2": {}},
            "packages": [{"id": "A", "price": 1, "items": {"bolt": 1}}]},
           ["备件种类 bolt 不在各阀合并后的备件需求中(多余种类)"])
# packages 非数组 / 元素非对象由 Pydantic(字段类型 list[dict])直接 422;
# solver 内部的同类兜底校验在单元测试 test_spare_parts.py 中覆盖。
for bad_body in ["x", [1]]:
    rr = client.post(f"/api/plans/{pid}/spare-parts", json={
        "valve_demands": {"V1": {"gasket": 1}, "V2": {}}, "packages": bad_body})
    check(rr.status_code == 422, f"packages malformed should 422: {rr.status_code}")

# ---- 预览未新增任何历史方案(只读) ----
plans = client.get("/api/plans?limit=100").json()["plans"]
ids = sorted(p["id"] for p in plans)
check(ids == sorted(set([pid, bad_id])), f"plan ids {ids}")

print(f"spare parts API tests: {ok} checks passed")
