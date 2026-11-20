"""Every SQL example in docs/schema.md runs through assay_sql."""
from __future__ import annotations

import re
from pathlib import Path

from assay import Ledger

DOC = Path(__file__).resolve().parent.parent / "docs" / "schema.md"


def test_schema_examples(lead: Ledger, worker: Ledger) -> None:
    w2 = lead.as_agent("worker-2")
    ids = []
    for s, (w, opt) in enumerate([(worker, "slsqp"), (w2, "slsqp"), (worker, "penalty")]):
        ids.append(w.record_experiment({"init": "hex", "optimizer": opt}, {"score": 2.0 + s}, seed=s).id)
    worker.record_experiment({"init": "grid", "optimizer": "penalty"}, {}, run_status="error", notes="boom")
    c = worker.propose_claim("hypothesis", "hex helps", evidence=[(ids[0], "supports")])  # type: ignore[list-item]
    w2.propose_claim("hypothesis", "hex helps", evidence=[(ids[1], "supports")])  # type: ignore[list-item]
    lead.decide(c.id, "supported", "metric", "ok", ids[:2])  # type: ignore[arg-type]
    queries = re.findall(r"```sql\n(.*?)```", DOC.read_text(), re.S)
    assert len(queries) == 10
    for q in queries:
        r = lead.sql(q)
        assert "rows" in r, q
    assert lead.sql(queries[0])["rows"][0]["n_support_authors"] == 2


def test_autoresearch_template_matches_package() -> None:
    root = Path(__file__).resolve().parent.parent
    packaged = root / "src" / "assay" / "integrations" / "autoresearch" / "templates" / "program.assay.md"
    copy = root / "integrations" / "autoresearch" / "template" / "program.assay.md"
    assert packaged.read_text() == copy.read_text()
    text = packaged.read_text()
    assert "MIT" in text and "0. assay ar replicate --auto" in text and "NEVER STOP" in text


def test_readmes_say_what_assay_fits() -> None:
    root = Path(__file__).resolve().parent.parent
    en = (root / "README.md").read_text()
    ko = (root / "README.ko.md").read_text()
    assert en.index("## What it fits") < en.index("## What it is not")
    assert ko.index("## 맞는 일") < ko.index("## 무엇이 아닌가")
    for words in (["A number per attempt", "Re-runnable", "One space, explored together", "code implementation"],
                  ["시도마다 숫자 지표", "재실행 가능", "같은 공간의 동시 탐색", "코드 구현"]):
        text = en if words[0].isascii() else ko
        assert all(w in text for w in words), words
