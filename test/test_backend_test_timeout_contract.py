"""The coverage-bearing matrix arm needs its own job cap, not the cheap arm's.

`backend-test` runs the same four shards twice: once on 3.10 with `--no-cov`
and once on 3.12 with `--cov`. Tracing roughly doubles wall time, so the two
arms do measurably different amounts of work while `timeout-minutes` is a
job-level key shared by every matrix cell.

Measured from `completed_at - started_at` on three heads:

    head        3.10 (--no-cov)      3.12 (+cov)              ratio
    d94950d39   7.68 - 13.87 min     27.33 - 30.25 CANCELLED  2.18x
    760124a75   10.00 - 13.60 min    22.22 - 30.27 CANCELLED  2.23x
    92c3b5fff   12.02 - 13.83 min    18.32 - 27.13 min        1.96x

The cheap arm used at most 46% of a 30-minute budget; the coverage arm reached
101% and was cancelled. It is not confined to fork PRs either: of seven
consecutive pushes to main sampled on 2026-09-01, three had a 3.12 shard
cancelled at the wall and the rest finished at 28.3 to 28.9 minutes, which is
94 to 96% of the cap. A `cancelled` shard fails `Coverage Gate` closed, so a
green diff goes red for a reason no diff can account for.

The same file already accepts this principle: `backend-test-windows` carries
`timeout-minutes: 40` because "windows-latest runners run the same shards
measurably slower". That job's slowest observed shard was 18.03 minutes, 45% of
its cap. The coverage arm is further from its budget than Windows is from its,
and still shares 30 minutes.

Deliberately one-directional. It does not pin the cheap arm's number, does not
forbid raising both, and does not care which expression shape encodes the split.
It asserts only that the two arms are not capped identically and that the
coverage arm is given at least as much room as Windows already has, because the
alternative reading of the measurements ("the coverage arm is fine at 30") is
the one the cancellations disprove.

This guards a workflow, so it cannot be exercised by running the code it
describes. What it can do is fail when the differentiation is deleted, which is
the regression that matters: the value looks like a tidy-up candidate, and
collapsing it back to one scalar restores a red that surfaces days later on an
unrelated PR.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"

# The Windows arm's cap is the in-repo precedent for "this arm is slower, give
# it its own budget". It is a floor, not a target: the coverage arm's measured
# maximum already exceeds the Windows arm's, so anything below this is known to
# be too small before CI runs.
WINDOWS_PRECEDENT_MINUTES = 40

# ${{ <ctx>.<key> == '<value>' && <then> || <else> }}
_TERNARY = re.compile(
    r"^\$\{\{\s*matrix\.(?P<key>[\w-]+)\s*==\s*'(?P<value>[^']+)'"
    r"\s*&&\s*(?P<then>.+?)\s*\|\|\s*(?P<else_>.+?)\s*\}\}$"
)


def _workflow() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


def _coverage_arm() -> tuple[str, str, str]:
    """Locate the job whose pytest coverage flag is chosen by a matrix value.

    Returns (job id, matrix key, the value of that key that gets coverage).
    Derived rather than hardcoded: the job name, the matrix axis and the Python
    version have all changed before, and a hardcoded triple would keep passing
    against a job that no longer exists.
    """
    for job_id, job in _workflow()["jobs"].items():
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            run = str(step.get("run", ""))
            if "pytest" not in run:
                continue
            for expr in re.findall(r"\$\{\{[^}]+\}\}", run):
                parsed = _TERNARY.match(expr.strip())
                if not parsed:
                    continue
                then, else_ = parsed["then"], parsed["else_"]
                # One branch turns tracing on, the other explicitly off. That
                # pairing is what makes the two arms unequal work.
                if "--cov" in then and "--no-cov" in else_:
                    return job_id, parsed["key"], parsed["value"]
    raise AssertionError(
        "no job in ci.yml selects its pytest coverage flag from a matrix value. "
        "If coverage moved to its own job the arms no longer share a cap and "
        "this file should be deleted; if the expression shape changed, teach "
        "_coverage_arm the new shape rather than deleting the guard."
    )


def _cap_for(job: dict, key: str, value: str) -> int:
    """Resolve `timeout-minutes` for one matrix cell.

    A plain scalar resolves to itself for every cell, which is exactly the
    unfixed state, so this must not special-case it away.
    """
    raw = job.get("timeout-minutes")
    assert raw is not None, "the job declares no timeout-minutes at all"
    if isinstance(raw, int):
        return raw
    parsed = _TERNARY.match(str(raw).strip())
    assert parsed, (
        f"timeout-minutes is {raw!r}, which this test cannot resolve per arm. "
        f"Extend _TERNARY to cover the new shape; do not weaken the assertions "
        f"below, because an unresolvable value hides whether the arms differ."
    )
    assert parsed["key"] == key, (
        f"timeout-minutes keys off matrix.{parsed['key']} while the coverage "
        f"flag keys off matrix.{key}. Two different axes cannot be relied on to "
        f"agree about which cell is the expensive one."
    )
    branch = parsed["then"] if parsed["value"] == value else parsed["else_"]
    return int(branch)


def test_ci_still_has_a_coverage_conditional_backend_matrix() -> None:
    # Anti-vacuity. Every assertion below reads this triple, so a rename or a
    # move of coverage into its own job must fail loudly here rather than
    # quietly turning the rest of the file into a no-op.
    job_id, key, value = _coverage_arm()
    assert job_id and key and value


def test_the_coverage_arm_is_not_capped_like_the_cheap_arm() -> None:
    job_id, key, value = _coverage_arm()
    job = _workflow()["jobs"][job_id]
    other = [v for v in job["strategy"]["matrix"][key] if str(v) != value]
    assert other, f"matrix.{key} has no arm other than {value!r} to compare against"
    cov = _cap_for(job, key, value)
    for arm in other:
        cheap = _cap_for(job, key, str(arm))
        assert cov > cheap, (
            f"{job_id} caps matrix.{key}={value} (runs --cov) at {cov} minutes "
            f"and matrix.{key}={arm} (runs --no-cov) at {cheap}. Coverage costs "
            f"about 2x wall time here, so a shared cap cancels the coverage arm "
            f"at the wall while the cheap arm finishes under half the budget, "
            f"and a cancelled shard fails Coverage Gate closed."
        )


def test_the_coverage_arm_gets_at_least_the_windows_arms_budget() -> None:
    # The Windows job's 40 minutes covers a slowest-observed 18.03-minute shard.
    # The coverage arm's slowest observed shard was censored at the 30-minute
    # wall, so it is at least 30.3, already past what Windows needs. A cap below
    # the Windows precedent is therefore known to be short in advance.
    job_id, key, value = _coverage_arm()
    job = _workflow()["jobs"][job_id]
    cov = _cap_for(job, key, value)
    assert cov >= WINDOWS_PRECEDENT_MINUTES, (
        f"{job_id} gives the coverage arm {cov} minutes, under the "
        f"{WINDOWS_PRECEDENT_MINUTES} the Windows arm already gets for a "
        f"smaller measured gap (18.03 minutes used of 40, against at least "
        f"30.3 of 30 here)."
    )
