import copy
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from backend.app import defects
from backend.app.main import PREFIX, create_app
from backend.tests import test_acceptance as ah
from backend.tests import test_patches as ph
from backend.tests import test_requirements as rh
from backend.tests import test_verification as vh

setup = rh.setup
prepared = vh.prepared
ready = vh.ready


def route(ex, suffix=""):
    return "/projects/" + ex[1] + "/defects" + suffix


def state(ex):
    r = ex[0].get(PREFIX + route(ex))
    assert r.status_code == 200, r.json()
    return r.json()


def issue(ex, **changes):
    body = dict(
        task_id="T1",
        version_id="V1",
        requirement_ids=["R1"],
        title="合成反例未返回预期结果",
        severity="high",
        reproduction_input="合成输入：重复提交当前记录",
        expected="应返回原记录且不重复保存",
        actual="实际返回错误并重复保存",
        responsible="隔离测试负责人",
        reason="合成工程测试，不代表真实业务缺陷",
        confirmed=True,
    )
    body.update(changes)
    key = body.pop("_key", None)
    return rh.post(ex[0], route(ex, "/issues"), body, key)


def change(ex, root, status, **changes):
    d = next(d for d in state(ex)["defects"] if d["id"] == root["id"])
    body = dict(
        defect_id=d["id"],
        previous_id=d["state"]["id"],
        expected_hash=d["state"]["content_hash"],
        status=status,
        reason="隔离测试明确判断，原事实保留",
        confirmed=True,
    )
    body.update(changes)
    key = body.pop("_key", None)
    return rh.post(ex[0], route(ex, "/transitions"), body, key)


def awaiting(ex):
    root = issue(ex).json()
    for status in ["confirmed", "in_progress", "awaiting_retest"]:
        r = change(ex, root, status)
        assert r.status_code == 200, r.json()
    return root


def retest(ex, root, report, **changes):
    d = next(d for d in state(ex)["defects"] if d["id"] == root["id"])
    body = dict(
        defect_id=d["id"],
        previous_id=d["state"]["id"],
        expected_hash=d["state"]["content_hash"],
        report_id=report["id"],
        report_hash=report["content_hash"],
        reproduction_input=d["issue"]["reproduction_input"],
        expected=d["issue"]["expected"],
        actual=d["issue"]["expected"]
        if report["payload"]["status"] == "passed"
        else "同一反例仍返回错误结果",
        reason="核对同一反例的实际结果，人工报告来源",
        confirmed=True,
    )
    body.update(changes)
    key = body.pop("_key", None)
    return rh.post(ex[0], route(ex, "/retests"), body, key)


def new_revision(ex):
    draft = ex[0].get(PREFIX + rh.req(ex[1])).json()["draft"]
    items = copy.deepcopy(draft["items"])
    items[0]["value"] += "，合成变更"
    r = rh.post(
        ex[0],
        rh.req(ex[1], "/revisions"),
        dict(expected_revision=draft["revision"], fields=draft["fields"], items=items),
    )
    assert r.status_code == 200, r.json()
    return state(ex)["impacts"][-1]


def review(ex, impact, **changes):
    body = dict(
        source_kind=impact["source_kind"],
        source_id=impact["source_id"],
        expected_snapshot_hash=impact["snapshot_hash"],
        previous_id=impact["review"]["id"] if impact["review"] else None,
        areas={
            k: dict(status="not_affected", reason="已核对合成变更范围，仅记录复核意见") for k in defects.AREAS
        },
        conclusions=[
            dict(asset_id=c["asset_id"], status="inheritable", reason="合成仅备注调整，仍需原来源版本校验")
            for c in impact["conclusions"]
        ],
        reason="隔离合成复核，不授予任何执行权限",
        confirmed=True,
    )
    body.update(changes)
    key = body.pop("_key", None)
    return rh.post(ex[0], route(ex, "/impacts"), body, key)


def read(ex, identifier):
    return ex[0].get(PREFIX + route(ex, "/assets/" + identifier))


def test_readonly_empty_auth_no_calls(setup, monkeypatch):
    monkeypatch.setattr(defects.contexts.models.httpx, "Client", lambda *a, **k: pytest.fail("no outbound"))
    assert not state(setup)["defects"] and not state(setup)["impacts"]
    with setup[3].state.sessions() as s:
        assert not defects.rows(s, setup[1])
    with TestClient(create_app(setup[2], setup[4])) as c:
        assert c.get(PREFIX + route(setup)).status_code == 401
    assert setup[4].calls == 0


def test_creation_idempotency_severity_not_priority(ready):
    a = issue(ready, _key="same-new-defect").json()
    assert a["payload"]["status"] == "new"
    assert a["payload"]["issue"]["severity"] == "high"
    assert a["payload"]["issue"]["requirement_ids"] == ["R1"]
    assert issue(ready, _key="same-new-defect").json() == a
    assert issue(ready, _key="same-new-defect", severity="low").status_code == 409
    assert len(state(ready)["defects"]) == 1


@pytest.mark.parametrize(
    "changes",
    [
        dict(confirmed=False),
        dict(confirmed=1),
        dict(confirmed="true"),
        dict(role="admin"),
        dict(title="    "),
        dict(expected="    "),
        dict(actual="    "),
        dict(responsible="  "),
        dict(reason="    "),
        dict(severity="P0"),
        dict(requirement_ids=["R1", "R1"]),
        dict(reproduction_input="    "),
    ],
)
def test_contract_rejects(ready, changes):
    assert issue(ready, **changes).status_code == 422


@pytest.mark.parametrize(
    "changes", [dict(task_id="T99"), dict(version_id="V99"), dict(requirement_ids=["R99"])]
)
def test_own_task_version_requirements(ready, changes):
    assert issue(ready, **changes).status_code == 409


@pytest.mark.parametrize("field", ["title", "reproduction_input", "actual", "reason"])
def test_protected_content_rejected(ready, field):
    assert issue(ready, **{field: "不得记录秘密 " + ready[2].token}).status_code == 409
    assert not state(ready)["defects"]


def test_false_positive_reason_history_and_correction(ready):
    root = issue(ready).json()
    assert change(ready, root, "false_positive", reason="    ").status_code == 422
    assert change(ready, root, "false_positive", reason="审核原报告环境不一致，合成误报").status_code == 200
    assert not defects_gate(ready)[0]
    assert change(ready, root, "reopened", reason="审核纠正：新的资料证实需要处理").status_code == 200
    assert read(ready, root["id"]).json()["payload"]["status"] == "new"
    assert len(state(ready)["defects"][0]["history"]) == 3


def defects_gate(ex):
    with ex[3].state.sessions() as s:
        return defects.gate(s, ex[2], ex[1])


@pytest.mark.parametrize("target", ["closed", "awaiting_retest", "in_progress", "reopened", "new"])
def test_cannot_skip_workflow(ready, target):
    root = issue(ready).json()
    assert change(ready, root, target).status_code == 409


def test_close_requires_current_same_counterexample_and_reopen(ready):
    root = awaiting(ready)
    assert change(ready, root, "closed").json()["error"]["code"] == "RETEST_REQUIRED"
    plan = vh.plan(ready).json()
    passed = vh.report(ready, plan).json()
    rr = retest(ready, root, passed).json()
    assert rr["payload"]["report_source"] == "operator_report" and rr["current"]
    closed = change(ready, root, "closed").json()
    assert closed["payload"]["retest_id"] == rr["id"] and closed["current"]
    assert not defects_gate(ready)[0]
    assert change(ready, root, "reopened").status_code == 409
    failed = vh.report(ready, plan, status="failed", passed=2, failed=1, failures=["同一反例返回错误"]).json()
    assert not read(ready, closed["id"]).json()["current"]
    assert retest(ready, root, failed).status_code == 200
    assert change(ready, root, "reopened").status_code == 200
    assert read(ready, rr["id"]).json()["payload"]["report_status"] == "passed"
    assert defects_gate(ready)[0]


@pytest.mark.parametrize(
    "changes",
    [
        dict(reproduction_input="另一个不相关反例"),
        dict(expected="另一个不相关预期"),
        dict(actual="虽然报告通过但实际不是预期"),
        dict(report_hash="0" * 64),
    ],
)
def test_retest_binding(ready, changes):
    root = awaiting(ready)
    plan = vh.plan(ready).json()
    report = vh.report(ready, plan).json()
    assert retest(ready, root, report, **changes).status_code == 409


def test_old_report_cannot_be_reused(ready):
    plan = vh.plan(ready).json()
    report = vh.report(ready, plan).json()
    root = awaiting(ready)
    assert retest(ready, root, report).status_code == 409


def test_failed_retest_does_not_close(ready):
    root = awaiting(ready)
    plan = vh.plan(ready).json()
    report = vh.report(ready, plan, status="failed", passed=2, failed=1, failures=["合成反例失败"]).json()
    assert retest(ready, root, report).status_code == 200
    assert change(ready, root, "closed").status_code == 409


def test_code_change_invalidates_close(ready):
    root = awaiting(ready)
    plan = vh.plan(ready).json()
    report = vh.report(ready, plan).json()
    assert retest(ready, root, report).status_code == 200
    closed = change(ready, root, "closed").json()
    f = ready[2].allowed_roots[0] / "demo" / "README.md"
    f.write_text(f.read_text() + "\n外部变更")
    assert not read(ready, closed["id"]).json()["current"]
    assert defects_gate(ready)[0]


def test_impact_seven_unknown_review_and_stale(ready):
    v = new_revision(ready)
    assert set(v["areas"]) == set(defects.AREAS) and all(
        a["status"] == "unknown" for a in v["areas"].values()
    )
    assert defects_gate(ready)[0]
    r = review(ready, v, _key="same-impact").json()
    assert r["current"]
    assert review(ready, v, _key="same-impact").json() == r
    assert not defects_gate(ready)[0]
    f = ready[2].allowed_roots[0] / "demo" / "README.md"
    f.write_text(f.read_text() + "\n改变影响范围")
    assert not read(ready, r["id"]).json()["current"]
    assert review(ready, v).status_code == 409
    current = state(ready)["impacts"][-1]
    rr = review(ready, current).json()
    assert rr["payload"]["previous_id"] == r["id"] and rr["current"]
    assert read(ready, r["id"]).json()["payload"]["areas"] == r["payload"]["areas"]


@pytest.mark.parametrize("area", defects.AREAS)
def test_missing_area_rejected(ready, area):
    v = new_revision(ready)
    values = copy.deepcopy(v["areas"])
    del values[area]
    assert review(ready, v, areas=values).status_code == 422


def test_unknown_saved_still_blocks(ready):
    v = new_revision(ready)
    r = review(ready, v, areas=v["areas"]).json()
    assert r["current"] and defects_gate(ready)[0]


def test_cross_project_reads_and_refs(ready):
    root = issue(ready).json()
    fake = (ready[0], "00000000-0000-0000-0000-000000000000", *ready[2:])
    assert read(fake, root["id"]).status_code == 404
    v = new_revision(ready)
    assert review(ready, v, source_id=root["id"]).status_code == 404
    assert (
        review(
            ready, v, conclusions=[dict(asset_id=root["id"], status="inheritable", reason="不属于结论候选")]
        ).status_code
        == 409
    )


def test_impact_marks_prior_conclusion_not_restores_validity(ready):
    plan = vh.plan(ready).json()
    report = vh.report(ready, plan).json()
    v = new_revision(ready)
    assert report["id"] in {c["asset_id"] for c in v["conclusions"]}
    assert not vh.read(ready, report["id"]).json()["current"]
    r = review(
        ready,
        v,
        conclusions=[dict(asset_id=report["id"], status="invalid", reason="本次需求变化使旧测试不再适用")],
    )
    assert r.status_code == 200, r.json()
    assert any("已失效" in s for s in vh.read(ready, report["id"]).json()["issues"])
    # Inherit is only an opinion; the independent baseline check still rejects old results.
    current = state(ready)["impacts"][-1]
    assert review(ready, current).status_code == 200
    assert not vh.read(ready, report["id"]).json()["current"]
    assert ready[4].calls == 0


def test_defect_blocks_new_package_and_stales_old(ready):
    old = ah.package(ready).json()
    root = issue(ready).json()
    assert not ah.read(ready, old["id"]).json()["current"]
    new = ah.package(ready).json()
    assert any("缺陷" in b for b in new["blockers"])
    assert ah.decision(ready, new, outcome="passed").status_code == 409
    assert change(ready, root, "false_positive").status_code == 200
    assert not ah.read(ready, new["id"]).json()["current"]


def test_restart_immutable_history(ready):
    root = awaiting(ready)
    v = new_revision(ready)
    review(ready, v)
    before = state(ready)
    with TestClient(create_app(ready[2], ready[4]), headers=dict(ready[0].headers)) as c:
        assert state((c, *ready[1:])) == before
        assert read((c, *ready[1:]), root["id"]).json()["payload"]["status"] == "new"


def test_two_transition_writers_one_wins(ready):
    root = issue(ready).json()

    def write(key):
        return rh.post(
            ready[0],
            route(ready, "/transitions"),
            dict(
                defect_id=root["id"],
                previous_id=root["id"],
                expected_hash=root["content_hash"],
                status="confirmed",
                reason="竞争写入保持前次版本",
                confirmed=True,
            ),
            key,
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(write, ["writer-one", "writer-two"])) == [200, 409]
    assert len(state(ready)["defects"][0]["history"]) == 2


candidate = ph.candidate


def test_patch_candidate_impact_then_apply_stales_review(candidate):
    asset = ph.preview(candidate)
    v = state(candidate)["impacts"][0]
    assert v["source_kind"] == "patch" and v["source_id"] == asset["id"]
    assert set(v["files"]) == {"one.txt", "two.txt"}
    assert not v["snapshot"]["runs"] and defects_gate(candidate)[0]
    r = review(candidate, v).json()
    assert r["current"] and not defects_gate(candidate)[0]
    assert ph.apply(candidate, asset).status_code == 200
    assert not read(candidate, r["id"]).json()["current"]
    assert state(candidate)["impacts"][0]["snapshot"]["runs"]
    assert (candidate[-1] / "one.txt").read_text() == "candidate first\n"


def test_impact_missing_conclusion_cannot_hide_old_failure(ready):
    p = vh.plan(ready).json()
    failed = vh.report(ready, p, status="failed", passed=2, failed=1, failures=["合成首次失败"]).json()
    v = new_revision(ready)
    assert review(ready, v, conclusions=[]).status_code == 409
    assert review(ready, v).status_code == 200
    assert vh.read(ready, failed["id"]).json()["payload"]["status"] == "failed"


def test_impact_snapshot_has_no_self_reference_from_new_results(ready):
    v = new_revision(ready)
    r = review(ready, v).json()
    # A later stage package is not a pre-change conclusion; no acceptance/review cycle.
    p = ah.package(ready).json()
    assert p["id"] not in state(ready)["impacts"][0]["snapshot"]["conclusions"]
    assert read(ready, r["id"]).json()["current"]


def test_retest_not_available_before_remediation(ready):
    root = issue(ready).json()
    p = vh.plan(ready).json()
    r = vh.report(ready, p).json()
    assert retest(ready, root, r).json()["error"]["code"] == "RETEST_NOT_READY"


def test_retest_same_key_and_transition_conflict(ready):
    root = awaiting(ready)
    p = vh.plan(ready).json()
    r = vh.report(ready, p).json()
    rr = retest(ready, root, r, _key="one-retest").json()
    assert retest(ready, root, r, _key="one-retest").json() == rr
    assert retest(ready, root, r, _key="one-retest", reason="不同理由不能重用请求标识").status_code == 409
    d = state(ready)["defects"][0]["state"]
    change(ready, root, "closed")
    assert (
        change(ready, root, "reopened", previous_id=d["id"], expected_hash=d["content_hash"]).status_code
        == 409
    )


def test_revoked_authorization_not_inherited(candidate):
    asset = ph.preview(candidate)
    v = state(candidate)["impacts"][0]
    r = review(candidate, v).json()
    grant = candidate[-3]
    from backend.tests import test_execution as eh

    assert (
        rh.post(
            candidate[0],
            eh.api(candidate[1], "/revoke"),
            dict(authorization_id=grant["id"], reason="撤销原合成文件范围"),
        ).status_code
        == 200
    )
    assert not read(candidate, r["id"]).json()["current"]
    current = state(candidate)["impacts"][0]
    assert review(candidate, current).status_code == 200
    assert ph.apply(candidate, asset).status_code == 409


def test_impact_two_writers_one_wins(ready):
    v = new_revision(ready)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda key: review(ready, v, _key=key).status_code, ["impact-writer-one", "impact-writer-two"]
            )
        )
    assert sorted(results) == [200, 409]
