"""AEMO's P5MIN file: a row the parser cannot trust is skipped, never read as a price or a violation
(#2393: p5min.py's coverage, one behaviour per test). The rows are synthetic, in NEMWEB's own layout.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load import p5min

REGION_HEAD = (
    "I,P5MIN,REGIONSOLUTION,8,RUN_DATETIME,INTERVENTION,INTERVAL_DATETIME,REGIONID,RRP"
)
GOOD = "D,P5MIN,REGIONSOLUTION,8,2026/11/09 10:00:00,0,2026/11/09 10:05:00,QLD1,85.50"


def _csv(*rows: str) -> str:
    return "\n".join(rows)


def test_price_rows_that_cannot_be_trusted_are_skipped():
    # test_price_rows_that_cannot_be_trusted_are_skipped
    text = _csv(
        # before the header
        "D,P5MIN,REGIONSOLUTION,8,2026/11/09 10:00:00,0,2026/11/09 09:55:00,QLD1,999",
        REGION_HEAD,
        GOOD,
        # the intervention run (not what the market settles on)
        "D,P5MIN,REGIONSOLUTION,8,2026/11/09 10:00:00,1,2026/11/09 10:10:00,QLD1,15000",
        # an unreadable time
        "D,P5MIN,REGIONSOLUTION,8,2026/11/09 10:00:00,0,2026-11-09 10:15,QLD1,90",
        # no price
        "D,P5MIN,REGIONSOLUTION,8,2026/11/09 10:00:00,0,2026/11/09 10:20:00,QLD1,n/a",
        # a short row
        "D,P5MIN,REGIONSOLUTION,8,2026/11/09 10:00:00,0,2026/11/09 10:25:00,QLD1",
    )
    run = p5min.parse_region_solution(text, "qld1")
    assert run["forecast"] == [
        {
            "start": "2026-11-09T10:00:00+10:00",
            "end": "2026-11-09T10:05:00+10:00",
            "value": 0.0855,
        }
    ]


def test_only_real_violations_of_the_settled_run_are_evidence():
    # test_only_real_violations_of_the_settled_run_are_evidence
    text = _csv(
        "I,P5MIN,CASESOLUTION,2,RUN_DATETIME,INTERVENTION,"
        "TOTALGENERICVIOLATION,TOTALINTERCONNECTORVIOLATION",
        "D,P5MIN,CASESOLUTION,2,2026/11/09 10:00:00,1,9999,9999",  # intervention
        "D,P5MIN,CASESOLUTION,2,2026/11/09 10:00:00,0,12.5,",
        "I,P5MIN,CONSTRAINTSOLUTION,6,RUN_DATETIME,INTERVENTION,INTERVAL_DATETIME,"
        "CONSTRAINTID,RHS,MARGINALVALUE,VIOLATIONDEGREE",
        "D,P5MIN,CONSTRAINTSOLUTION,6,2026/11/09 10:00:00,0,2026/11/09 10:05:00,Q_A,100,-5,3.25",
        # not violated
        "D,P5MIN,CONSTRAINTSOLUTION,6,2026/11/09 10:00:00,0,2026/11/09 10:05:00,Q_ZERO,100,0,0",
        # an unreadable time
        "D,P5MIN,CONSTRAINTSOLUTION,6,2026/11/09 10:00:00,0,bad time,Q_TIME,100,0,4",
        # no degree
        "D,P5MIN,CONSTRAINTSOLUTION,6,2026/11/09 10:00:00,0,2026/11/09 10:05:00,Q_TEXT,x,y,z",
    )
    out = p5min.parse_constraints(text)
    assert out["case"] == {
        "total_generic_violation": 12.5,
        "total_interconnector_violation": None,
    }
    assert [v["constraint_id"] for v in out["violated"]] == ["Q_A"]


def test_a_run_without_the_region_has_no_first_interval_violations():
    # test_a_run_without_the_region_has_no_first_interval_violations
    assert p5min.parse_run(_csv(REGION_HEAD, GOOD), "NSW1") is None
    assert p5min.first_interval_violations(None) == []
    assert (
        p5min.first_interval_violations(
            {"forecast": [], "violated": [{"interval_end": "x"}]}
        )
        == []
    )
