"""Contract lint (spec §9.3): the seven checks plus schema validation, adversarially."""
from __future__ import annotations

import copy

import pytest

from blimp.mesh.deck.contract_lint import lint_contract

GOOD = {
    "id": "T-0142", "title": "Webhook intake", "type": "build",
    "objective": "Webhook intake for WhatsApp messages behind a queue.",
    "why": {"mandate_id": "M-0001", "rationale": "the Boss asked for it"},
    "deliverables": [{"name": "PR", "kind": "pr"}],
    "acceptance_checks": [
        {"id": "c1", "kind": "test", "cmd": "pytest -q tests/intake", "expected": "exit 0"},
        {"id": "c2", "kind": "http", "url": "https://yourproduct.example/api/health", "expected": "200"},
        {"id": "c3", "kind": "sql_readonly", "cmd": "SELECT count(*) FROM mesh.tasks", "expected": ">= 1"},
    ],
    "definition_of_done": ["PR open", "checks green"],
    "owner": "duke", "reviewer": "svc-verifier",
    "capabilities": ["repo.read", "repo.worktree", "repo.branch"],
    "boundaries": {"non_goals": [], "forbidden": ["infra/"], "allowed_paths": ["src/intake"]},
    "risk_tier": "R2", "budget": {"tokens": 100000, "turns": 5}, "effort_class": "S",
    "worktree": "/home/user/blimp/worktrees/T-0142",
}


@pytest.fixture(autouse=True)
def _lint_root(monkeypatch):
    monkeypatch.setenv("BLIMP_WORKTREE_ROOTS", "/home/user/blimp/worktrees")


def lint(c, **kw):
    return lint_contract(c, existing_task_ids={"T-0100", "T-0101"}, **kw)


def mutate(**changes):
    c = copy.deepcopy(GOOD)
    for k, v in changes.items():
        c[k] = v
    return c


def test_good_contract_passes():
    ok, errs = lint(GOOD)
    assert ok, errs


@pytest.mark.parametrize("field", ["objective", "owner", "boundaries", "deliverables", "why", "budget"])
def test_missing_required_field_is_a_schema_error(field):
    c = copy.deepcopy(GOOD)
    del c[field]
    ok, errs = lint(c)
    assert not ok and any(e.startswith("0: schema") and field in e for e in errs), errs


def test_bad_id_pattern():
    ok, errs = lint(mutate(id="task-1"))
    assert not ok and any("0: schema id" in e for e in errs)


@pytest.mark.parametrize("cmd", ['pytest -q "unterminated', "   "])
def test_unrunnable_command_rejected(cmd):
    c = mutate(acceptance_checks=[{"id": "c1", "kind": "cmd", "cmd": cmd, "expected": "exit 0"}])
    ok, errs = lint(c)
    assert not ok and any(e.startswith("1:") for e in errs), errs


@pytest.mark.parametrize("url,why", [
    ("https://evil.example.com/x", "not on the http allowlist"),
    ("ftp://yourproduct.example", "absolute http(s) URL"),
    ("yourproduct.example/health", "absolute http(s) URL"),
    ("https://yourproduct.example.evil.com/", "not on the http allowlist"),  # suffix trick
])
def test_http_check_must_hit_the_allowlist(url, why):
    c = mutate(acceptance_checks=[{"id": "c1", "kind": "http", "url": url, "expected": "200"}])
    ok, errs = lint(c)
    assert not ok and any(why in e for e in errs), errs


def test_allowlist_subdomain_ok():
    c = mutate(acceptance_checks=[{"id": "c1", "kind": "http", "url": "https://preview-123.vercel.app/", "expected": "200"}])
    assert lint(c)[0]


@pytest.mark.parametrize("sql", [
    "DELETE FROM mesh.tasks",
    "SELECT 1; DROP TABLE mesh.tasks",
    "WITH x AS (DELETE FROM mesh.tasks RETURNING 1) SELECT * FROM x",
    "select pg_sleep(100)",
    "UPDATE mesh.tasks SET state='DONE'",
])
def test_sql_check_must_be_read_only(sql):
    c = mutate(acceptance_checks=[{"id": "c1", "kind": "sql_readonly", "cmd": sql, "expected": "0"}])
    ok, errs = lint(c)
    assert not ok and any("1:" in e and "sql" in e for e in errs), errs


def test_sql_select_with_comment_ok():
    c = mutate(acceptance_checks=[{"id": "c1", "kind": "sql_readonly", "cmd": "-- count\nSELECT count(*) FROM mesh.tasks;", "expected": "1"}])
    assert lint(c)[0]


def test_expected_must_be_concrete():
    c = mutate(acceptance_checks=[{"id": "c1", "kind": "test", "cmd": "pytest", "expected": "  "}])
    assert not lint(c)[0]


def test_only_manual_checks_rejected_for_build():
    c = mutate(acceptance_checks=[{"id": "c1", "kind": "manual_by_logan", "procedure": "look at it", "expected": "looks right"}])
    ok, errs = lint(c)
    assert not ok and any("non-manual" in e for e in errs)


def test_design_may_have_manual_only():
    c = mutate(type="design", acceptance_checks=[{"id": "c1", "kind": "review", "expected": "decision record accepted"}])
    assert lint(c)[0], lint(c)[1]


def test_risk_tier_below_capabilities():
    ok, errs = lint(mutate(risk_tier="R1"))
    assert not ok and any(e.startswith("2:") for e in errs)


def test_r3_requires_steps():
    c = mutate(risk_tier="R3", capabilities=["deploy.prod"])
    ok, errs = lint(c)
    assert not ok and any("r3_steps" in e for e in errs)
    c["r3_steps"] = [{"tool": "deploy.prod_deploy", "why": "ship it"}]
    assert lint(c)[0], lint(c)[1]


def test_budget_over_effort_cap():
    ok, errs = lint(mutate(budget={"tokens": 150001, "turns": 3}))
    assert not ok and any(e.startswith("3:") for e in errs)


def test_budget_over_owner_allowance():
    ok, errs = lint(GOOD, owner_remaining_monthly_tokens=50_000)
    assert not ok and any("allowance" in e for e in errs)


def test_missing_dependency():
    ok, errs = lint(mutate(depends_on=["T-9999"]))
    assert not ok and any("T-9999 does not exist" in e for e in errs)


def test_self_dependency():
    ok, errs = lint(mutate(depends_on=["T-0142"]))
    assert not ok and any("depends on itself" in e for e in errs)


def test_indirect_cycle_through_other_tasks():
    edges = {"T-0100": ["T-0101"], "T-0101": ["T-0142"]}
    ok, errs = lint(mutate(depends_on=["T-0100"]), dependency_edges=edges)
    assert not ok and any("cycle T-0142 -> T-0100 -> T-0101 -> T-0142" in e for e in errs), errs


def test_acyclic_graph_ok():
    edges = {"T-0100": ["T-0101"], "T-0101": []}
    assert lint(mutate(depends_on=["T-0100"]), dependency_edges=edges)[0]


def test_untraceable_task():
    ok, errs = lint(mutate(why={"rationale": "vibes"}))
    assert not ok and any(e.startswith("5:") for e in errs)


def test_maintenance_needs_no_trace():
    assert lint(mutate(type="maintenance", why={"rationale": "cleanup"}))[0]


@pytest.mark.parametrize("forbidden,allowed", [(["src"], ["src/intake"]), (["src/intake"], ["src"]), (["/src/"], ["src"])])
def test_forbidden_overlaps_allowed(forbidden, allowed):
    ok, errs = lint(mutate(boundaries={"non_goals": [], "forbidden": forbidden, "allowed_paths": allowed}))
    assert not ok and any(e.startswith("6:") for e in errs)


def test_client_delivery_requirements():
    c = mutate(type="client_delivery", client={"market": "SG"})
    ok, errs = lint(c)
    assert any("client_id" in e for e in errs) and any("consent_scope" in e for e in errs)


def test_never_myr_to_singapore():
    c = mutate(type="client_delivery", client={"client_id": "CL-x", "consent_scope": "delivery", "market": "SG", "currency": "MYR"})
    ok, errs = lint(c)
    assert not ok and any("must be SGD" in e for e in errs), errs


def test_client_currency_matches():
    c = mutate(type="client_delivery", client={"client_id": "CL-x", "consent_scope": "delivery", "market": "MY", "currency": "MYR"})
    assert lint(c)[0], lint(c)[1]


def test_command_checks_need_a_worktree():
    c = mutate()
    del c["worktree"]
    ok, errs = lint(c)
    assert not ok and any("worktree" in e for e in errs)
    assert not lint(mutate(worktree="relative/path"))[0]


@pytest.mark.parametrize("wt", ["/home/user", "/home/user/blimp/worktrees", "/home/user/blimp/worktrees/T-9999",
                                "/home/user/blimp/worktrees/T-0142/../T-0001", "/etc"])
def test_worktree_must_be_this_tasks_checkout(wt):
    ok, errs = lint(mutate(worktree=wt))
    assert not ok and any("worktree must be under" in e for e in errs), (wt, errs)


def test_worktree_with_slug_ok():
    assert lint(mutate(worktree="/home/user/blimp/worktrees/T-0142-intake/src"))[0]
