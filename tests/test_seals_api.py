"""离港前封签核对与放行凭证端到端测试。"""

import threading

from fastapi.testclient import TestClient

from app.main import app


TWIN_SLOTS = {
    "slots": [
        {"code": "A01", "stack": "A", "tier": 1, "capacity": 100, "arm": 2.0},
        {"code": "B01", "stack": "B", "tier": 1, "capacity": 100, "arm": 2.0},
    ],
    "adjacency": [],
    "incompatible_classes": [],
    "min_moment": -100000,
    "max_moment": 100000,
}
TWIN_MANIFEST = {
    "containers": [
        {"no": "C001", "weight": 10, "hazmat": None, "discharge_order": 1},
        {"no": "C003", "weight": 10, "hazmat": None, "discharge_order": 1},
    ]
}
# 预期封签清单：C001 -> SEAL-001，C003 -> SEAL-003
SEAL_ITEMS = [
    {"container_no": "C001", "expected_seal_no": "SEAL-001"},
    {"container_no": "C003", "expected_seal_no": "SEAL-003"},
]
# 第二艘船（独立槽位，避免与已确认方案争用槽位）
ALT_SLOTS = {
    "slots": [
        {"code": "C01", "stack": "C", "tier": 1, "capacity": 100, "arm": 2.0},
        {"code": "D01", "stack": "D", "tier": 1, "capacity": 100, "arm": 2.0},
    ],
    "adjacency": [],
    "incompatible_classes": [],
    "min_moment": -100000,
    "max_moment": 100000,
}


def _confirm(client, slots=None, manifest=None):
    sr = client.post("/api/slot-revisions", json=slots or TWIN_SLOTS) \
        .json()["slots_revision"]
    mr = client.post("/api/manifest-revisions", json=manifest or TWIN_MANIFEST) \
        .json()["manifest_revision"]
    plan = client.post("/api/plans/compute", json={}).json()
    r = client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    assert r.status_code == 200, r.text
    return plan


def _ev(seq, etype, cno, crane, minute):
    return {"seq": seq, "event_type": etype, "container_no": cno,
            "crane_no": crane, "minute": minute}


def _handover_ready(client, slots=None, handover_no="H-1"):
    """已确认方案 + 排程 + 全部完工实绩 + 交接凭证；返回 (plan, handover_id)。"""
    plan = _confirm(client, slots=slots)
    pid = plan["plan_id"]
    r = client.post(f"/api/plans/{pid}/schedules", json={
        "crane_count": 2,
        "entries": [
            {"no": "C001", "duration": 10, "release_minute": 0,
             "deadline_minute": 100},
            {"no": "C003", "duration": 10, "release_minute": 0,
             "deadline_minute": 100},
        ],
    })
    assert r.status_code == 201, r.text
    r = client.post(f"/api/plans/{pid}/actual-events", json={"events": [
        _ev(1, "start", "C001", 1, 0),
        _ev(2, "finish", "C001", 1, 10),
        _ev(3, "start", "C003", 2, 0),
        _ev(4, "finish", "C003", 2, 10),
    ]})
    assert r.status_code == 201, r.text
    h = client.post(f"/api/plans/{pid}/handovers", json={
        "handover_no": handover_no,
        "cutoff_minute": 30,
        "expected_actual_version": 1,
        "valid_seqs": [1, 2, 3, 4],
        "max_finish_delay_minutes": 5,
    })
    assert h.status_code == 201, h.text
    return plan, h.json()["handover_id"]


def _register_manifest(client, plan_id, items=None):
    r = client.post(f"/api/plans/{plan_id}/seal-manifest",
                    json={"items": items if items is not None else SEAL_ITEMS})
    assert r.status_code == 201, r.text
    return r.json()


def _scan(scan_no, cno, observed, minute):
    return {"scan_no": scan_no, "container_no": cno,
            "observed_seal_no": observed, "check_minute": minute}


def _scan_ok(client, plan_id, scan_no, cno, observed, minute):
    r = client.post(f"/api/plans/{plan_id}/seal-scans",
                    json=_scan(scan_no, cno, observed, minute))
    assert r.status_code == 201, r.text
    return r.json()


def _release_body(handover_id, application_no="REL-1", version=2, cutoff=30):
    return {
        "application_no": application_no,
        "handover_id": handover_id,
        "expected_scan_version": version,
        "cutoff_minute": cutoff,
    }


def _release_ready(client):
    """交接完成 + 清单冻结 + 两箱均匹配扫描；返回 (plan, handover_id)。"""
    plan, hid = _handover_ready(client)
    pid = plan["plan_id"]
    _register_manifest(client, pid)
    _scan_ok(client, pid, "S-1", "C001", "SEAL-001", 25)
    _scan_ok(client, pid, "S-2", "C003", "SEAL-003", 26)
    return plan, hid


# ---------- 预期清单登记 ----------


def test_register_manifest_success_frozen(client):
    plan, _ = _handover_ready(client)
    pid = plan["plan_id"]
    r = client.post(f"/api/plans/{pid}/seal-manifest", json={"items": SEAL_ITEMS})
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["plan_id"] == pid
    assert data["revision_no"] == 1
    assert data["status"] == "current"
    assert data["container_count"] == 2
    assert data["scan_version"] == 0
    assert data["fingerprint_algorithm"] == "sha256"
    assert len(data["manifest_fingerprint"]) == 64
    assert data["items"] == sorted(SEAL_ITEMS, key=lambda x: x["container_no"])
    # GET 一致
    g = client.get(f"/api/plans/{pid}/seal-manifest")
    assert g.status_code == 200 and g.json() == data


def test_manifest_same_content_retry_returns_original(client):
    plan, _ = _handover_ready(client)
    pid = plan["plan_id"]
    r1 = client.post(f"/api/plans/{pid}/seal-manifest", json={"items": SEAL_ITEMS})
    assert r1.status_code == 201
    # 同内容（即使条目顺序不同）重试 -> 200 返回原清单
    r2 = client.post(f"/api/plans/{pid}/seal-manifest",
                     json={"items": list(reversed(SEAL_ITEMS))})
    assert r2.status_code == 200
    assert r2.json()["manifest_fingerprint"] == r1.json()["manifest_fingerprint"]
    assert r2.json()["created_at"] == r1.json()["created_at"]


def test_manifest_different_content_conflicts(client):
    plan, _ = _handover_ready(client)
    pid = plan["plan_id"]
    _register_manifest(client, pid)
    altered = [dict(SEAL_ITEMS[0], expected_seal_no="SEAL-999"), SEAL_ITEMS[1]]
    r = client.post(f"/api/plans/{pid}/seal-manifest", json={"items": altered})
    assert r.status_code == 409 and "冻结" in r.json()["detail"]
    # 原清单不变
    g = client.get(f"/api/plans/{pid}/seal-manifest").json()
    assert {i["container_no"]: i["expected_seal_no"] for i in g["items"]} == {
        "C001": "SEAL-001", "C003": "SEAL-003"}


def test_manifest_must_cover_all_plan_boxes(client):
    plan, _ = _handover_ready(client)
    pid = plan["plan_id"]
    # 缺箱
    r = client.post(f"/api/plans/{pid}/seal-manifest",
                    json={"items": [SEAL_ITEMS[0]]})
    assert r.status_code == 422 and "缺少箱项" in r.json()["detail"]
    # 多箱（未知箱）
    r = client.post(f"/api/plans/{pid}/seal-manifest", json={"items": SEAL_ITEMS + [
        {"container_no": "C999", "expected_seal_no": "SEAL-999"}]})
    assert r.status_code == 422 and "多余箱项" in r.json()["detail"]
    assert client.get(f"/api/plans/{pid}/seal-manifest").status_code == 404


def test_manifest_payload_validation_422(client):
    plan, _ = _handover_ready(client)
    base = f"/api/plans/{plan['plan_id']}/seal-manifest"
    # 封签号互异
    r = client.post(base, json={"items": [
        {"container_no": "C001", "expected_seal_no": "SEAL-X"},
        {"container_no": "C003", "expected_seal_no": "SEAL-X"},
    ]})
    assert r.status_code == 422 and "互异" in str(r.json()["detail"])
    # 批内箱号重复
    r = client.post(base, json={"items": [
        {"container_no": "C001", "expected_seal_no": "SEAL-A"},
        {"container_no": "C001", "expected_seal_no": "SEAL-B"},
    ]})
    assert r.status_code == 422
    # 空白箱号/封签号
    r = client.post(base, json={"items": [
        {"container_no": "  ", "expected_seal_no": "SEAL-A"}]})
    assert r.status_code == 422
    r = client.post(base, json={"items": [
        {"container_no": "C001", "expected_seal_no": " "}]})
    assert r.status_code == 422


def test_manifest_requires_confirmed_plan(client):
    client.post("/api/slot-revisions", json=TWIN_SLOTS)
    client.post("/api/manifest-revisions", json=TWIN_MANIFEST)
    plan = client.post("/api/plans/compute", json={}).json()
    r = client.post(f"/api/plans/{plan['plan_id']}/seal-manifest",
                    json={"items": SEAL_ITEMS})
    assert r.status_code == 409 and "确认" in r.json()["detail"]
    assert client.post("/api/plans/nope/seal-manifest",
                       json={"items": SEAL_ITEMS}).status_code == 404


def test_manifest_readonly_after_supersede(client):
    plan, _ = _handover_ready(client)
    pid = plan["plan_id"]
    _register_manifest(client, pid)

    # 取代：新箱清单修订 -> amend -> confirm
    client.post("/api/manifest-revisions", json={
        "containers": [{"no": "C001", "weight": 15, "hazmat": None,
                        "discharge_order": 1}]
    })
    p2 = client.post(f"/api/plans/{pid}/amend", json={}).json()
    ok = client.post(f"/api/plans/{p2['plan_id']}/confirm", json={
        "slots_revision": 1, "manifest_revision": 2
    })
    assert ok.status_code == 200

    # 历史清单仍可查（historical），新登记/改写拒绝
    g = client.get(f"/api/plans/{pid}/seal-manifest")
    assert g.status_code == 200 and g.json()["status"] == "historical"
    r = client.post(f"/api/plans/{pid}/seal-manifest",
                    json={"items": SEAL_ITEMS})
    assert r.status_code == 200  # 同内容重试仍返回原清单
    altered = [dict(SEAL_ITEMS[0], expected_seal_no="SEAL-9"), SEAL_ITEMS[1]]
    r = client.post(f"/api/plans/{pid}/seal-manifest", json={"items": altered})
    assert r.status_code == 409


# ---------- 现场扫描登记 ----------


def test_scan_registration_and_board(client):
    plan, _ = _handover_ready(client)
    pid = plan["plan_id"]
    _register_manifest(client, pid)
    r = client.post(f"/api/plans/{pid}/seal-scans",
                    json=_scan("S-1", "C001", "SEAL-001", 25))
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["scan_no"] == "S-1"
    assert data["plan_id"] == pid
    assert data["container_no"] == "C001"
    assert data["observed_seal_no"] == "SEAL-001"
    assert data["expected_seal_no"] == "SEAL-001"
    assert data["matched"] is True
    assert data["check_minute"] == 25
    assert data["scan_version"] == 1
    assert data["registered_at"]

    # 不符扫描留痕但不算通过
    r = client.post(f"/api/plans/{pid}/seal-scans",
                    json=_scan("S-2", "C003", "SEAL-WRONG", 26))
    assert r.status_code == 201
    assert r.json()["matched"] is False
    assert r.json()["scan_version"] == 2

    board = client.get(f"/api/plans/{pid}/seal-scans").json()
    assert board["scan_version"] == 2
    boxes = {b["container_no"]: b for b in board["boxes"]}
    assert boxes["C001"]["passed"] is True
    assert boxes["C001"]["latest_scan_no"] == "S-1"
    assert boxes["C003"]["passed"] is False
    assert boxes["C003"]["matched"] is False
    assert boxes["C003"]["observed_seal_no"] == "SEAL-WRONG"
    assert [s["scan_no"] for s in board["scans"]] == ["S-1", "S-2"]
    assert len(board["scans_fingerprint"]) == 64


def test_scan_unknown_container_rejected(client):
    plan, _ = _handover_ready(client)
    pid = plan["plan_id"]
    _register_manifest(client, pid)
    r = client.post(f"/api/plans/{pid}/seal-scans",
                    json=_scan("S-1", "C999", "SEAL-X", 10))
    assert r.status_code == 422 and "未知箱号" in r.json()["detail"]
    # 未留下痕迹
    board = client.get(f"/api/plans/{pid}/seal-scans").json()
    assert board["scan_version"] == 0 and board["scans"] == []


def test_scan_requires_manifest_and_confirmed_plan(client):
    plan, _ = _handover_ready(client)
    pid = plan["plan_id"]
    # 未登记清单 -> 409
    r = client.post(f"/api/plans/{pid}/seal-scans",
                    json=_scan("S-1", "C001", "SEAL-001", 10))
    assert r.status_code == 409 and "清单" in r.json()["detail"]
    # 草稿方案 -> 409
    client.post("/api/slot-revisions", json={
        "slots": [{"code": "Z01", "stack": "Z", "tier": 1, "capacity": 100,
                   "arm": 2.0}],
        "adjacency": [], "incompatible_classes": [],
        "min_moment": -100000, "max_moment": 100000,
    })
    client.post("/api/manifest-revisions", json={
        "containers": [{"no": "ZZ1", "weight": 10, "hazmat": None,
                        "discharge_order": 1}]
    })
    draft = client.post("/api/plans/compute", json={}).json()
    r = client.post(f"/api/plans/{draft['plan_id']}/seal-scans",
                    json=_scan("S-9", "ZZ1", "SEAL-Z", 1))
    assert r.status_code == 409
    assert client.post("/api/plans/nope/seal-scans",
                       json=_scan("S-8", "C001", "SEAL-001", 1)).status_code == 404


def test_scan_idempotent_retry_and_conflict(client):
    plan, _ = _handover_ready(client)
    pid = plan["plan_id"]
    _register_manifest(client, pid)
    r1 = client.post(f"/api/plans/{pid}/seal-scans",
                     json=_scan("S-1", "C001", "SEAL-001", 25))
    assert r1.status_code == 201
    # 同号同内容重试 -> 200 原记录（版本仍为首次登记后的 1）
    r2 = client.post(f"/api/plans/{pid}/seal-scans",
                     json=_scan("S-1", "C001", "SEAL-001", 25))
    assert r2.status_code == 200
    assert r2.json() == r1.json()
    # 同号异内容 -> 409（分钟/箱/封签任一不同）
    for altered in [
        _scan("S-1", "C001", "SEAL-001", 26),
        _scan("S-1", "C003", "SEAL-001", 25),
        _scan("S-1", "C001", "SEAL-002", 25),
    ]:
        rr = client.post(f"/api/plans/{pid}/seal-scans", json=altered)
        assert rr.status_code == 409, altered
    # 版本未被重试或冲突推进
    board = client.get(f"/api/plans/{pid}/seal-scans").json()
    assert board["scan_version"] == 1 and len(board["scans"]) == 1


def test_scan_no_globally_unique_across_plans(client):
    p1, _ = _handover_ready(client)
    _register_manifest(client, p1["plan_id"])
    _scan_ok(client, p1["plan_id"], "S-1", "C001", "SEAL-001", 25)

    # 第二艘船（独立槽位），同样箱清单
    other_slots = {
        "slots": [
            {"code": "C01", "stack": "C", "tier": 1, "capacity": 100, "arm": 2.0},
            {"code": "D01", "stack": "D", "tier": 1, "capacity": 100, "arm": 2.0},
        ],
        "adjacency": [], "incompatible_classes": [],
        "min_moment": -100000, "max_moment": 100000,
    }
    p2 = _confirm(client, slots=other_slots)
    _register_manifest(client, p2["plan_id"])
    # 同扫描号跨方案复用，即使内容相同也冲突
    r = client.post(f"/api/plans/{p2['plan_id']}/seal-scans",
                    json=_scan("S-1", "C001", "SEAL-001", 25))
    assert r.status_code == 409 and "另一方案" in r.json()["detail"]


def test_effective_scan_latest_minute_wins(client):
    plan, _ = _handover_ready(client)
    pid = plan["plan_id"]
    _register_manifest(client, pid)
    # 先匹配后不符：以检查分钟较晚的不符为准 -> 不通过
    _scan_ok(client, pid, "S-1", "C001", "SEAL-001", 20)
    _scan_ok(client, pid, "S-2", "C001", "SEAL-BAD", 25)
    boxes = {b["container_no"]: b
             for b in client.get(f"/api/plans/{pid}/seal-scans").json()["boxes"]}
    assert boxes["C001"]["latest_scan_no"] == "S-2"
    assert boxes["C001"]["passed"] is False
    # 再晚一次的匹配扫描覆盖 -> 通过
    _scan_ok(client, pid, "S-3", "C001", "SEAL-001", 28)
    boxes = {b["container_no"]: b
             for b in client.get(f"/api/plans/{pid}/seal-scans").json()["boxes"]}
    assert boxes["C001"]["latest_scan_no"] == "S-3"
    assert boxes["C001"]["passed"] is True


def test_effective_scan_same_minute_later_registration_wins(client):
    plan, _ = _handover_ready(client)
    pid = plan["plan_id"]
    _register_manifest(client, pid)
    _scan_ok(client, pid, "S-1", "C001", "SEAL-BAD", 25)  # 先登记：不符
    _scan_ok(client, pid, "S-2", "C001", "SEAL-001", 25)  # 同分钟后登记：匹配
    boxes = {b["container_no"]: b
             for b in client.get(f"/api/plans/{pid}/seal-scans").json()["boxes"]}
    assert boxes["C001"]["latest_scan_no"] == "S-2"
    assert boxes["C001"]["passed"] is True


def test_scan_readonly_after_supersede_but_retry_returns_original(client):
    plan, _ = _handover_ready(client)
    pid = plan["plan_id"]
    _register_manifest(client, pid)
    first = _scan_ok(client, pid, "S-1", "C001", "SEAL-001", 25)

    client.post("/api/manifest-revisions", json={
        "containers": [{"no": "C001", "weight": 15, "hazmat": None,
                        "discharge_order": 1}]
    })
    p2 = client.post(f"/api/plans/{pid}/amend", json={}).json()
    assert client.post(f"/api/plans/{p2['plan_id']}/confirm", json={
        "slots_revision": 1, "manifest_revision": 2
    }).status_code == 200

    # 新扫描拒绝（只读）；同号同内容重试仍返回原记录
    r = client.post(f"/api/plans/{pid}/seal-scans",
                    json=_scan("S-2", "C003", "SEAL-003", 26))
    assert r.status_code == 409
    r = client.post(f"/api/plans/{pid}/seal-scans",
                    json=_scan("S-1", "C001", "SEAL-001", 25))
    assert r.status_code == 200 and r.json() == first
    # 台账仍可查（historical）
    board = client.get(f"/api/plans/{pid}/seal-scans").json()
    assert board["status"] == "historical" and board["scan_version"] == 1


def test_scan_payload_validation_422(client):
    plan, _ = _handover_ready(client)
    pid = plan["plan_id"]
    _register_manifest(client, pid)
    base = f"/api/plans/{pid}/seal-scans"
    assert client.post(base, json=_scan(" ", "C001", "SEAL-001", 1)) \
        .status_code == 422
    assert client.post(base, json=_scan("S-x", " ", "SEAL-001", 1)) \
        .status_code == 422
    assert client.post(base, json=_scan("S-x", "C001", " ", 1)) \
        .status_code == 422
    assert client.post(base, json=_scan("S-x", "C001", "SEAL-001", -1)) \
        .status_code == 422
    body = _scan("S-x", "C001", "SEAL-001", 1)
    for key in ("scan_no", "container_no", "observed_seal_no", "check_minute"):
        bad = dict(body)
        del bad[key]
        assert client.post(base, json=bad).status_code == 422, key


# ---------- 放行凭证签发 ----------


def test_issue_release_success_freezes_snapshot(client):
    plan, hid = _release_ready(client)
    pid = plan["plan_id"]
    r = client.post(f"/api/plans/{pid}/seal-releases",
                    json=_release_body(hid))
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["release_id"]
    assert data["application_no"] == "REL-1"
    assert data["plan_id"] == pid
    assert data["revision_no"] == 1
    assert data["handover_id"] == hid
    assert data["cutoff_minute"] == 30
    assert data["expected_scan_version"] == 2
    assert data["scan_version"] == 2
    assert len(data["scans_fingerprint"]) == 64
    assert data["fingerprint_algorithm"] == "sha256"
    boxes = {c["container_no"]: c for c in data["containers"]}
    assert boxes["C001"] == {
        "container_no": "C001", "expected_seal_no": "SEAL-001",
        "observed_seal_no": "SEAL-001", "check_minute": 25, "scan_no": "S-1"}
    assert boxes["C003"]["scan_no"] == "S-2"
    assert data["summary"]["container_count"] == 2
    assert data["summary"]["handover_id"] == hid
    assert data["still_valid"] is True
    assert data["plan_status"] == "confirmed"
    assert data["handover_matches_current_actuals"] is True
    assert data["matches_current_scans"] is True
    assert data["current_scan_version"] == 2
    assert data["current_scans_fingerprint"] == data["scans_fingerprint"]
    # GET 一致
    g = client.get(f"/api/seal-releases/{data['release_id']}")
    assert g.status_code == 200 and g.json() == data


def test_release_check_exactly_at_cutoff_allowed(client):
    plan, hid = _release_ready(client)
    pid = plan["plan_id"]
    r = client.post(f"/api/plans/{pid}/seal-releases",
                    json=_release_body(hid, cutoff=26))  # 最晚扫描 26，恰为截止
    assert r.status_code == 201, r.text


def test_release_missing_scan_rejected_422(client):
    plan, hid = _handover_ready(client)
    pid = plan["plan_id"]
    _register_manifest(client, pid)
    _scan_ok(client, pid, "S-1", "C001", "SEAL-001", 25)  # C003 缺扫
    r = client.post(f"/api/plans/{pid}/seal-releases",
                    json=_release_body(hid, version=1))
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["missing_scans"] == ["C003"]
    assert body["mismatched_containers"] == []
    assert body["after_cutoff"] == []
    assert client.get(f"/api/plans/{pid}/seal-releases").json() == []


def test_release_mismatched_effective_scan_rejected_422(client):
    plan, hid = _handover_ready(client)
    pid = plan["plan_id"]
    _register_manifest(client, pid)
    _scan_ok(client, pid, "S-1", "C001", "SEAL-001", 25)
    _scan_ok(client, pid, "S-2", "C003", "SEAL-WRONG", 26)  # 不符
    r = client.post(f"/api/plans/{pid}/seal-releases",
                    json=_release_body(hid))
    assert r.status_code == 422
    body = r.json()
    assert body["missing_scans"] == []
    assert [m["container_no"] for m in body["mismatched_containers"]] == ["C003"]
    m = body["mismatched_containers"][0]
    assert m["expected_seal_no"] == "SEAL-003"
    assert m["observed_seal_no"] == "SEAL-WRONG"
    assert m["scan_no"] == "S-2"
    # 留下不符痕迹但不影响再次申请
    assert client.get(f"/api/plans/{pid}/seal-scans").json()["scan_version"] == 2


def test_release_later_mismatch_overrides_earlier_match(client):
    """同箱以检查分钟较晚者为准：晚到的不符扫描使该箱不通过。"""
    plan, hid = _handover_ready(client)
    pid = plan["plan_id"]
    _register_manifest(client, pid)
    _scan_ok(client, pid, "S-1", "C001", "SEAL-001", 25)
    _scan_ok(client, pid, "S-2", "C003", "SEAL-003", 26)
    _scan_ok(client, pid, "S-3", "C003", "SEAL-BAD", 27)  # 更晚：不符
    r = client.post(f"/api/plans/{pid}/seal-releases",
                    json=_release_body(hid, version=3))
    assert r.status_code == 422
    assert [m["container_no"] for m in r.json()["mismatched_containers"]] == ["C003"]
    # 再晚一次的匹配扫描覆盖后可放行
    _scan_ok(client, pid, "S-4", "C003", "SEAL-003", 28)
    ok = client.post(f"/api/plans/{pid}/seal-releases",
                     json=_release_body(hid, version=4))
    assert ok.status_code == 201, ok.text
    c3 = next(c for c in ok.json()["containers"] if c["container_no"] == "C003")
    assert c3["scan_no"] == "S-4" and c3["check_minute"] == 28


def test_release_effective_scan_after_cutoff_rejected_422(client):
    plan, hid = _handover_ready(client)
    pid = plan["plan_id"]
    _register_manifest(client, pid)
    _scan_ok(client, pid, "S-1", "C001", "SEAL-001", 25)
    _scan_ok(client, pid, "S-2", "C003", "SEAL-003", 31)  # 晚于截止 30
    r = client.post(f"/api/plans/{pid}/seal-releases",
                    json=_release_body(hid, cutoff=30))
    assert r.status_code == 422
    body = r.json()
    assert body["after_cutoff"] == [
        {"container_no": "C003", "check_minute": 31, "scan_no": "S-2"}]
    assert body["cutoff_minute"] == 30


def test_release_expected_version_stale_conflict(client):
    plan, hid = _release_ready(client)
    pid = plan["plan_id"]
    r = client.post(f"/api/plans/{pid}/seal-releases",
                    json=_release_body(hid, version=1))  # 当前为 2
    assert r.status_code == 409 and "版本" in r.json()["detail"]
    # 失败后同申请号修正内容可再签发（拒绝不占用申请号）
    ok = client.post(f"/api/plans/{pid}/seal-releases",
                     json=_release_body(hid))
    assert ok.status_code == 201


def test_release_handover_problems(client):
    plan, hid = _release_ready(client)
    pid = plan["plan_id"]
    # 未知交接凭证 -> 404
    r = client.post(f"/api/plans/{pid}/seal-releases",
                    json=_release_body("nope"))
    assert r.status_code == 404
    # 交接凭证属于另一方案 -> 422
    _, hid2 = _handover_ready(client, slots=ALT_SLOTS, handover_no="H-other")
    r = client.post(f"/api/plans/{pid}/seal-releases",
                    json=_release_body(hid2))
    assert r.status_code == 422 and "不属于" in r.json()["detail"]
    # 交接凭证签发后实绩被更正 -> 不再对应当前实绩 -> 409
    cc = client.post(f"/api/plans/{pid}/actual-events/correction", json={
        "seq": 2, "expected_version": 1, "request_id": "CORR-1",
        "event": {"seq": 2, "event_type": "finish", "container_no": "C001",
                  "crane_no": 1, "minute": 9},
    })
    assert cc.status_code == 201
    r = client.post(f"/api/plans/{pid}/seal-releases",
                    json=_release_body(hid, application_no="REL-9"))
    assert r.status_code == 409 and "实绩" in r.json()["detail"]
    # 重新交接（版本 2）后可放行
    h2 = client.post(f"/api/plans/{pid}/handovers", json={
        "handover_no": "H-2", "cutoff_minute": 30,
        "expected_actual_version": 2, "valid_seqs": [1, 2, 3, 4],
        "max_finish_delay_minutes": 5,
    })
    assert h2.status_code == 201
    ok = client.post(f"/api/plans/{pid}/seal-releases",
                     json=_release_body(h2.json()["handover_id"],
                                        application_no="REL-10"))
    assert ok.status_code == 201, ok.text


def test_release_plan_state_rejections(client):
    # 草稿方案 -> 409
    client.post("/api/slot-revisions", json=TWIN_SLOTS)
    client.post("/api/manifest-revisions", json=TWIN_MANIFEST)
    draft = client.post("/api/plans/compute", json={}).json()
    r = client.post(f"/api/plans/{draft['plan_id']}/seal-releases",
                    json=_release_body("any"))
    assert r.status_code == 409
    # 未知方案 -> 404
    assert client.post("/api/plans/nope/seal-releases",
                       json=_release_body("any")).status_code == 404
    # 已确认但无清单 -> 409
    plan, hid = _handover_ready(client)
    r = client.post(f"/api/plans/{plan['plan_id']}/seal-releases",
                    json=_release_body(hid, version=0))
    assert r.status_code == 409 and "清单" in r.json()["detail"]


def test_release_idempotent_retry_and_conflict(client):
    plan, hid = _release_ready(client)
    pid = plan["plan_id"]
    body = _release_body(hid)
    r1 = client.post(f"/api/plans/{pid}/seal-releases", json=body)
    assert r1.status_code == 201
    # 同申请号同内容重试 -> 200 原凭证
    r2 = client.post(f"/api/plans/{pid}/seal-releases", json=body)
    assert r2.status_code == 200
    assert r2.json()["release_id"] == r1.json()["release_id"]
    # 同号异内容 -> 409
    for key, altered in [("expected_scan_version", 1), ("cutoff_minute", 40)]:
        bad = _release_body(hid)
        bad[key] = altered
        rr = client.post(f"/api/plans/{pid}/seal-releases", json=bad)
        assert rr.status_code == 409 and "申请号" in rr.json()["detail"]
    bad = _release_body("other-handover-id")
    assert client.post(f"/api/plans/{pid}/seal-releases",
                       json=bad).status_code == 409
    # 仍只有一张凭证
    assert len(client.get(f"/api/plans/{pid}/seal-releases").json()) == 1


def test_release_application_no_global_across_plans(client):
    p1, hid1 = _release_ready(client)
    r1 = client.post(f"/api/plans/{p1['plan_id']}/seal-releases",
                     json=_release_body(hid1))
    assert r1.status_code == 201

    # 第二艘船（独立槽位），完成同样流程
    p2, hid2 = _handover_ready(client, slots=ALT_SLOTS, handover_no="H-2")
    pid2 = p2["plan_id"]
    _register_manifest(client, pid2)
    _scan_ok(client, pid2, "S2-1", "C001", "SEAL-001", 25)
    _scan_ok(client, pid2, "S2-2", "C003", "SEAL-003", 26)
    # 同申请号用于另一方案 -> 冲突
    r = client.post(f"/api/plans/{pid2}/seal-releases",
                    json=_release_body(hid2))
    assert r.status_code == 409 and "另一方案" in r.json()["detail"]
    # 新申请号可签发
    ok = client.post(f"/api/plans/{pid2}/seal-releases",
                     json=_release_body(hid2, application_no="REL-2"))
    assert ok.status_code == 201


def test_release_payload_validation_422(client):
    plan, hid = _release_ready(client)
    base = f"/api/plans/{plan['plan_id']}/seal-releases"

    def post(mod):
        body = _release_body(hid)
        body.update(mod)
        return client.post(base, json=body)

    assert post({"application_no": "  "}).status_code == 422
    assert post({"handover_id": " "}).status_code == 422
    assert post({"expected_scan_version": -1}).status_code == 422
    assert post({"cutoff_minute": -1}).status_code == 422
    for key in ("application_no", "handover_id", "expected_scan_version",
                "cutoff_minute"):
        body = _release_body(hid)
        del body[key]
        assert client.post(base, json=body).status_code == 422, key


# ---------- 凭证不可变 & 失效标示 ----------


def test_release_invalidated_by_later_scan_but_content_immutable(client):
    plan, hid = _release_ready(client)
    pid = plan["plan_id"]
    r = client.post(f"/api/plans/{pid}/seal-releases", json=_release_body(hid))
    assert r.status_code == 201
    rid = r.json()["release_id"]
    frozen_fp = r.json()["scans_fingerprint"]

    # 后续扫描（即使内容匹配）使凭证显示失效
    _scan_ok(client, pid, "S-3", "C001", "SEAL-001", 27)
    g = client.get(f"/api/seal-releases/{rid}").json()
    assert g["still_valid"] is False
    assert g["matches_current_scans"] is False
    assert g["current_scan_version"] == 3
    assert g["current_scans_fingerprint"] != frozen_fp
    assert g["handover_matches_current_actuals"] is True
    assert g["plan_status"] == "confirmed"
    # 历史内容不变
    assert g["scan_version"] == 2
    assert g["scans_fingerprint"] == frozen_fp
    assert {c["container_no"]: c["scan_no"] for c in g["containers"]} == {
        "C001": "S-1", "C003": "S-2"}
    # 同申请号同内容重试仍返回原凭证（200，不重新评估）
    retry = client.post(f"/api/plans/{pid}/seal-releases",
                        json=_release_body(hid))
    assert retry.status_code == 200
    assert retry.json()["release_id"] == rid
    # 基于新版本可再签发新凭证
    ok = client.post(f"/api/plans/{pid}/seal-releases",
                     json=_release_body(hid, application_no="REL-2", version=3))
    assert ok.status_code == 201
    assert ok.json()["still_valid"] is True
    listed = client.get(f"/api/plans/{pid}/seal-releases").json()
    assert [x["application_no"] for x in listed] == ["REL-1", "REL-2"]


def test_release_invalidated_by_actuals_correction(client):
    plan, hid = _release_ready(client)
    pid = plan["plan_id"]
    rid = client.post(f"/api/plans/{pid}/seal-releases",
                      json=_release_body(hid)).json()["release_id"]
    # 实绩更正 -> 交接凭证不再对应当前实绩 -> 放行凭证显示失效
    cc = client.post(f"/api/plans/{pid}/actual-events/correction", json={
        "seq": 2, "expected_version": 1, "request_id": "CORR-1",
        "event": {"seq": 2, "event_type": "finish", "container_no": "C001",
                  "crane_no": 1, "minute": 9},
    })
    assert cc.status_code == 201
    g = client.get(f"/api/seal-releases/{rid}").json()
    assert g["still_valid"] is False
    assert g["handover_matches_current_actuals"] is False
    assert g["matches_current_scans"] is True  # 扫描台账未变
    # 内容不变
    assert g["containers"][0]["observed_seal_no"] == "SEAL-001"


def test_release_invalidated_by_supersede_but_readable(client):
    plan, hid = _release_ready(client)
    pid = plan["plan_id"]
    rid = client.post(f"/api/plans/{pid}/seal-releases",
                      json=_release_body(hid)).json()["release_id"]

    client.post("/api/manifest-revisions", json={
        "containers": [{"no": "C001", "weight": 15, "hazmat": None,
                        "discharge_order": 1}]
    })
    p2 = client.post(f"/api/plans/{pid}/amend", json={}).json()
    assert client.post(f"/api/plans/{p2['plan_id']}/confirm", json={
        "slots_revision": 1, "manifest_revision": 2
    }).status_code == 200

    g = client.get(f"/api/seal-releases/{rid}").json()
    assert g["still_valid"] is False
    assert g["plan_status"] == "superseded"
    assert g["matches_current_scans"] is True
    # 历史凭证仍可读、内容不变
    assert g["release_id"] == rid
    assert len(g["containers"]) == 2
    # 被取代方案拒绝新签发
    r = client.post(f"/api/plans/{pid}/seal-releases",
                    json=_release_body(hid, application_no="REL-9"))
    assert r.status_code == 409


def test_release_query_endpoints(client):
    plan, hid = _release_ready(client)
    pid = plan["plan_id"]
    rid = client.post(f"/api/plans/{pid}/seal-releases",
                      json=_release_body(hid)).json()["release_id"]
    # 方案内查询
    g = client.get(f"/api/plans/{pid}/seal-releases/{rid}")
    assert g.status_code == 200 and g.json()["release_id"] == rid
    # 不属于该方案 -> 404
    plan2, _ = _handover_ready(client, slots=ALT_SLOTS, handover_no="H-other")
    assert client.get(f"/api/plans/{plan2['plan_id']}/seal-releases/{rid}") \
        .status_code == 404
    # 未知凭证/未知方案 -> 404
    assert client.get("/api/seal-releases/nope").status_code == 404
    assert client.get(f"/api/plans/{pid}/seal-releases/nope").status_code == 404
    assert client.get("/api/plans/nope/seal-releases").status_code == 404
    # 列表
    listed = client.get(f"/api/plans/{pid}/seal-releases").json()
    assert len(listed) == 1 and listed[0]["release_id"] == rid


# ---------- 并发 ----------


def test_concurrent_same_application_no_one_wins(client):
    plan, hid = _release_ready(client)
    pid = plan["plan_id"]
    results = []
    barrier = threading.Barrier(2)

    def issue():
        local = TestClient(app)
        barrier.wait()
        r = local.post(f"/api/plans/{pid}/seal-releases",
                       json=_release_body(hid, application_no="REL-C"))
        results.append(r.status_code)

    threads = [threading.Thread(target=issue) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200, 201]
    assert len(client.get(f"/api/plans/{pid}/seal-releases").json()) == 1


def test_concurrent_same_scan_no_one_wins(client):
    plan, _ = _handover_ready(client)
    pid = plan["plan_id"]
    _register_manifest(client, pid)
    results = []
    barrier = threading.Barrier(2)

    def scan():
        local = TestClient(app)
        barrier.wait()
        r = local.post(f"/api/plans/{pid}/seal-scans",
                       json=_scan("S-C", "C001", "SEAL-001", 25))
        results.append(r.status_code)

    threads = [threading.Thread(target=scan) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200, 201]
    board = client.get(f"/api/plans/{pid}/seal-scans").json()
    assert board["scan_version"] == 1 and len(board["scans"]) == 1


def test_concurrent_scan_and_release_leaves_no_stale_snapshot(client):
    """签发写锁内复核：与新扫描并发时，凭证不得固化混合快照。

    两种合法线性化：放行先于扫描生效（201，固化版本 2，随后因新扫描显示
    失效）；或扫描先生效（放行按旧期望版本 2 判定 -> 409 版本过期，或锁内
    发现 C003 生效扫描不符 -> 422）。关键不变量：凡 201 凭证的扫描版本与
    逐箱结果必然自洽（绝不固化版本 2 却已含新扫描的混合快照）。
    """
    plan, hid = _release_ready(client)
    pid = plan["plan_id"]
    results = {}
    barrier = threading.Barrier(2)

    def issue():
        local = TestClient(app)
        barrier.wait()
        r = local.post(f"/api/plans/{pid}/seal-releases",
                       json=_release_body(hid, application_no="REL-C"))
        results["release"] = r.status_code

    def scan():
        local = TestClient(app)
        barrier.wait()
        r = local.post(f"/api/plans/{pid}/seal-scans",
                       json=_scan("S-C", "C003", "SEAL-BAD", 27))
        results["scan"] = r.status_code

    t1 = threading.Thread(target=issue)
    t2 = threading.Thread(target=scan)
    t1.start(); t2.start(); t1.join(); t2.join()

    assert results["scan"] == 201
    board = client.get(f"/api/plans/{pid}/seal-scans").json()
    assert board["scan_version"] == 3
    listed = client.get(f"/api/plans/{pid}/seal-releases").json()
    if results["release"] == 201:
        # 放行先生效：固化版本 2 与 S-2 结果；新扫描使其显示失效
        assert len(listed) == 1
        cert = listed[0]
        assert cert["scan_version"] == 2
        assert {c["container_no"]: c["scan_no"] for c in cert["containers"]} == {
            "C001": "S-1", "C003": "S-2"}
        assert cert["still_valid"] is False
        assert cert["matches_current_scans"] is False
        assert cert["current_scan_version"] == 3
    else:
        # 扫描先生效：版本过期 409 或生效扫描不符 422，不生成凭证
        assert results["release"] in (409, 422)
        assert listed == []
