"""plan_spare_parts 关阀作业备件包采购预览测试。

覆盖: 需求合并、最低总价组合、同价采购包编号字典序决胜、每包只买一次、
余量统计、全部包不足时逐类缺口、无备件需求空清单、录入校验。
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "app"))

from solver import (  # noqa: E402
    NetworkValidationError, plan_spare_parts, solve, solve_failover,
)

passed = 0


def check(cond, msg):
    global passed
    assert cond, "FAIL: " + msg
    passed += 1


def expect_errors(snapshot, closed, valve_demands, packages, keys):
    try:
        plan_spare_parts(snapshot, closed, valve_demands, packages)
    except NetworkValidationError as exc:
        joined = " | ".join(exc.errors)
        for key in keys:
            check(key in joined, f"error contains {key!r}; got: {exc.errors}")
        return
    raise AssertionError("should have raised: " + str(keys))


# ---- 备援方案管网: 计划关闭 V1+V2 ----
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
FO = solve_failover(NET_FO)
check(FO["feasible"] and FO["closed_valves"] == ["V1", "V2"],
      f"failover plan {FO.get('closed_valves')}")
CV = FO["closed_valves"]

# 1. 基本满足: 合并 V1/V2 需求后 gasket=4, bolt=2, seal=4; 必须 P1+P2
VD = {"V1": {"gasket": 3, "bolt": 2},
      "V2": {"gasket": 1, "seal": 4}}
PKGS = [
    {"id": "P1", "price": 10, "items": {"gasket": 4}},
    {"id": "P2", "price": 5, "items": {"bolt": 2, "seal": 4}},
]
r = plan_spare_parts(NET_FO, CV, VD, PKGS)
check(r["ok"] and r["satisfied"] and not r["empty"], "satisfied flags")
check(r["closed_valves"] == ["V1", "V2"], "closed echo")
check(r["part_types"] == ["bolt", "gasket", "seal"], "part types sorted")
check(r["merged_demand"] == {"gasket": 4, "bolt": 2, "seal": 4},
      f"merged {r['merged_demand']}")
check(r["selected_package_ids"] == ["P1", "P2"],
      f"selected {r['selected_package_ids']}")
check(r["total_price"] == 15, "total price")
check(r["combination_count"] == 4 and r["feasible_combination_count"] == 1,
      "combination counts")
check(r["total_demand"] == 10 and r["arrival_total"] == 10
      and r["surplus_total"] == 0 and r["shortage_total"] == 0, "totals")
summary = {row["part_type"]: row for row in r["part_summary"]}
for kid, d in [("bolt", 2), ("gasket", 4), ("seal", 4)]:
    row = summary[kid]
    check(row["demand"] == d and row["arrival"] == d
          and row["surplus"] == 0 and row["shortage"] == 0,
          f"summary {kid}")
# 各阀需求回显(含绑定管段)
vd_rows = {row["valve_id"]: row for row in r["valve_demands"]}
check(vd_rows["V1"]["segment_id"] == "p1"
      and {it["part_type"]: it["quantity"] for it in vd_rows["V1"]["items"]}
      == {"bolt": 2, "gasket": 3}, "V1 demand echo")
check({it["part_type"]: it["quantity"] for it in vd_rows["V2"]["items"]}
      == {"gasket": 1, "seal": 4}, "V2 demand echo")
# 选中包件数按全部需求种类展开(含 0 件种类)
p1 = next(p for p in r["selected_packages"] if p["id"] == "P1")
check({it["part_type"]: it["quantity"] for it in p1["items"]}
      == {"bolt": 0, "gasket": 4, "seal": 0}, "P1 full vector")
check("只读" in r["message"], "readonly note")

# 2. 多包组合比单包便宜: 每包只能买一次 => B+C 各一件恰好覆盖
r = plan_spare_parts(NET_FO, CV,
                     {"V1": {"gasket": 6}, "V2": {}},
                     [{"id": "A", "price": 10, "items": {"gasket": 6}},
                      {"id": "B", "price": 4, "items": {"gasket": 3}},
                      {"id": "C", "price": 4, "items": {"gasket": 3}}])
check(r["ok"] and r["selected_package_ids"] == ["B", "C"],
      f"combo wins {r['selected_package_ids']}")
check(r["total_price"] == 8, "combo price")
check(r["part_types"] == ["gasket"], "single part type")
check(r["feasible_combination_count"] == 5, f"feasible count {r}")  # A, AB, AC, BC, ABC

# 3. 同价字典序决胜: A、B 单价相同均可独力覆盖, 选 A
r = plan_spare_parts(NET_FO, CV,
                     {"V1": {"gasket": 2}, "V2": {}},
                     [{"id": "B", "price": 5, "items": {"gasket": 2}},
                      {"id": "A", "price": 5, "items": {"gasket": 2}}])
check(r["ok"] and r["selected_package_ids"] == ["A"],
      f"tie single {r['selected_package_ids']}")

# 3b. 同价组合决胜: {P1,P2} 与 {P3} 同价, 编号升序列表 ["P1","P2"] 更小
r = plan_spare_parts(NET_FO, CV,
                     {"V1": {"a": 2}, "V2": {"b": 2}},
                     [{"id": "P1", "price": 5, "items": {"a": 2}},
                      {"id": "P2", "price": 5, "items": {"b": 2}},
                      {"id": "P3", "price": 10, "items": {"a": 2, "b": 2}}])
check(r["ok"] and r["selected_package_ids"] == ["P1", "P2"],
      f"tie combo {r['selected_package_ids']}")
check(r["total_price"] == 10, "tie combo price")

# 4. 余量: 覆盖时到货超出需求 => 非负余量
r = plan_spare_parts(NET_FO, CV,
                     {"V1": {"gasket": 3}, "V2": {}},
                     [{"id": "P1", "price": 7, "items": {"gasket": 5}}])
check(r["ok"] and r["selected_package_ids"] == ["P1"], "surplus selection")
row = r["part_summary"][0]
check(row == {"part_type": "gasket", "demand": 3, "arrival": 5,
              "surplus": 2, "shortage": 0}, f"surplus row {row}")
check(r["surplus_total"] == 2 and r["shortage_total"] == 0
      and r["arrival_total"] == 5, "surplus totals")

# 5. 全部包仍不足: 逐类缺口(各类到货取全部包)
r = plan_spare_parts(NET_FO, CV, VD, [
    {"id": "P1", "price": 10, "items": {"gasket": 4}},
    {"id": "P2", "price": 5, "items": {"bolt": 1, "seal": 1}},
])
check(not r["ok"] and r["reason_code"] == "INSUFFICIENT_PACKAGES"
      and not r["satisfied"], "insufficient flags")
check(r["selected_package_ids"] == [] and r["selected_packages"] == []
      and r["total_price"] is None, "insufficient selection")
check(r["arrival_total"] == 6 and r["total_demand"] == 10
      and r["shortage_total"] == 4, "insufficient totals")
check({k: (v["arrival"], v["shortage"], v["surplus"])
       for k, v in ((row["part_type"], row) for row in r["part_summary"])}
      == {"gasket": (4, 0, 0), "bolt": (1, 1, 0), "seal": (1, 3, 0)},
      "per-type shortage")
# 空包列表同样判不足
r0 = plan_spare_parts(NET_FO, CV, VD, [])
check(not r0["ok"] and r0["reason_code"] == "INSUFFICIENT_PACKAGES"
      and r0["shortage_total"] == 10 and r0["combination_count"] == 1
      and r0["feasible_combination_count"] == 0, "no packages insufficient")

# 6. 无备件需求: 全部阀空需求 => 零费用空清单(不校验采购包)
r = plan_spare_parts(NET_FO, CV, {"V1": {}, "V2": {}},
                     [{"id": "P1", "price": 0, "items": {"nope": -1}}])
check(r["ok"] and r["empty"] and r["satisfied"], "empty flags")
check(r["selected_package_ids"] == [] and r["selected_packages"] == []
      and r["part_summary"] == [] and r["total_price"] == 0
      and r["merged_demand"] == {} and r["part_types"] == [], "empty payload")
check(r["total_demand"] == 0 and r["arrival_total"] == 0
      and r["surplus_total"] == 0 and r["shortage_total"] == 0, "empty zeros")
check("零费用空清单" in r["message"], "empty message")

# 6b. 计划不关闭任何阀门的可行方案(管网无漏点): 同样空清单
NET_NOLEAK = {
    "nodes": [{"id": "S", "role": "inlet"}, {"id": "A", "role": "normal"}],
    "segments": [{"id": "p1", "endpoint_a": "S", "endpoint_b": "A"}],
    "valves": [],
}
bp = solve(NET_NOLEAK)
check(bp["feasible"] and bp["closed_valves"] == [], "no-leak plan")
r = plan_spare_parts(NET_NOLEAK, [], {}, [])
check(r["ok"] and r["empty"] and r["closed_valves"] == []
      and r["total_price"] == 0, "no-valve empty")

# 7. 录入校验
expect_errors(NET_FO, CV, {"V1": {"gasket": 1}}, [],
              ["缺少计划阀门的备件需求: V2"])
expect_errors(NET_FO, CV, {"V1": {"gasket": 1}, "V2": {}, "V9": {}}, [],
              ["提供了非计划阀门的备件需求(多余编号): V9"])
for bad in [0, -1, 1.5, "5", True, None]:
    expect_errors(NET_FO, CV,
                  {"V1": {"gasket": bad}, "V2": {}}, [],
                  ["数量必须是正整数"])
# 同阀内同种类重复 => 合并累加
r = plan_spare_parts(NET_FO, CV,
                     {"V1": {"gasket": 1}, "V2": {"gasket": 2}},
                     [{"id": "P1", "price": 3, "items": {"gasket": 3}}])
check(r["ok"] and r["merged_demand"] == {"gasket": 3}, "same kind merge")
# 采购包超过 12 个
expect_errors(NET_FO, CV, {"V1": {"gasket": 1}, "V2": {}},
              [{"id": f"P{i}", "price": 1, "items": {"gasket": 1}}
               for i in range(13)],
              ["超过上限 12 个"])
# 采购包编号缺失/重复
expect_errors(NET_FO, CV, {"V1": {"gasket": 1}, "V2": {}},
              [{"price": 1, "items": {}}], ["缺少采购包编号"])
expect_errors(NET_FO, CV, {"V1": {"gasket": 1}, "V2": {}},
              [{"id": "A", "price": 1, "items": {}},
               {"id": "A", "price": 1, "items": {}}],
              ["采购包编号重复: A"])
# 价格非正整数
for bad in [0, -1, 1.5, "5", True, None]:
    expect_errors(NET_FO, CV, {"V1": {"gasket": 1}, "V2": {}},
                  [{"id": "A", "price": bad, "items": {}}],
                  ["价格必须是正整数"])
# 件数为负/小数/字符串/布尔(0 合法)
for bad in [-1, 1.5, "0", True, None]:
    expect_errors(NET_FO, CV, {"V1": {"gasket": 1}, "V2": {}},
                  [{"id": "A", "price": 1, "items": {"gasket": bad}}],
                  ["件数必须是非负整数"])
r = plan_spare_parts(NET_FO, CV, {"V1": {"gasket": 1}, "V2": {}},
                     [{"id": "A", "price": 1, "items": {"gasket": 0}}])
check(not r["ok"] and r["shortage_total"] == 1, "zero qty allowed, insufficient")
# 采购包出现未需求的备件种类
expect_errors(NET_FO, CV, {"V1": {"gasket": 1}, "V2": {}},
              [{"id": "A", "price": 1, "items": {"bolt": 1}}],
              ["备件种类 bolt 不在各阀合并后的备件需求中(多余种类)"])
# items 不是对象 / packages 不是数组
expect_errors(NET_FO, CV, {"V1": {"gasket": 1}, "V2": {}},
              [{"id": "A", "price": 1, "items": []}],
              ["备件件数必须是以备件种类为键的对象"])
try:
    plan_spare_parts(NET_FO, CV, {"V1": {"gasket": 1}, "V2": {}}, "x")
    check(False, "packages non-list should raise")
except NetworkValidationError as exc:
    check(any("packages 必须是数组" in e for e in exc.errors),
          f"packages list errors {exc.errors}")
# valve_demands 不是对象
try:
    plan_spare_parts(NET_FO, CV, [], [])
    check(False, "valve_demands non-dict should raise")
except NetworkValidationError as exc:
    check(any("valve_demands 必须是以阀门编号为键的对象" in e for e in exc.errors),
          f"valve_demands errors {exc.errors}")
# 计划阀门不在快照中
expect_errors(NET_FO, ["V1", "VX"],
              {"V1": {}, "VX": {}}, [], ["阀门 VX 不存在于方案快照"])
# 空备件种类名
expect_errors(NET_FO, CV, {"V1": {" ": 1}, "V2": {}}, [],
              ["备件种类名称不能为空"])
expect_errors(NET_FO, CV, {"V1": {"gasket": 1}, "V2": {}},
              [{"id": "A", "price": 1, "items": {" ": 1}}],
              ["备件种类名称不能为空"])

# 8. 快照不合法
try:
    plan_spare_parts({"nodes": [{"id": "S", "role": "inlet"}],
                      "segments": [{"id": "p1", "endpoint_a": "S",
                                    "endpoint_b": "ZZ"}],
                      "valves": []},
                     [], {}, [])
    check(False, "bad snapshot should raise")
except NetworkValidationError as exc:
    check(any("方案快照数据不合法" in e for e in exc.errors),
          f"snapshot errors {exc.errors}")

print(f"spare parts tests: {passed} checks passed")
