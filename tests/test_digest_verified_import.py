from hashlib import sha256
import json

import pytest

from liquidity_digest.capital import ICI_URL, build_capital, capital_lines
from liquidity_digest.model import plan
from liquidity_digest.store import Store
from liquidity_digest.verified_import import METHOD, ici_records, import_ici


def archive(**kwargs):
    return json.dumps(dict(source_url=ICI_URL, unit="million_usd", method=METHOD,
        publication_date="2026-10-28", retrieved_at="2026-10-29T14:00:00Z",
        source_notes=["结构性调整说明"], structural_note_present=True,
        observations=[{"date":"2026-10-21", "value":7090}], **kwargs)).encode()


def test_import_records_are_current_vintage_and_not_backdated():
    rows = ici_records(archive(), "2026-10-29T14:01:00Z")
    row = rows[0]
    assert row["value"] == 7090 and row["released_at"] is None
    assert row["available_at"] == row["acquired_at"] == "2026-10-29T14:01:00+00:00"
    assert row["details"]["acquisition_method"] == METHOD
    assert row["history_basis"] == "CURRENT_VINTAGE_NOT_PIT"
    p = plan("2026-10-29")
    capture = {"status":"OK", "captured_at":"2026-10-29T14:02:00Z", "data":rows}
    original = build_capital(p, {"ici_flows":capture})
    assert original["observations"][0]["value"] is None
    p["cutoff"] = "2026-10-29T14:03:00Z"
    correction = build_capital(p, {"ici_flows":capture})
    assert correction["observations"][0]["value"] == 7090
    assert "结构活动干扰" in correction["observations"][0]["state_cn"]
    assert "自动抓取尚未恢复" in "".join(capital_lines(correction))


@pytest.mark.parametrize("field,value", [("source_url","https://wrong.test"), ("unit","billion_usd"),
    ("method","AUTOMATIC"), ("retrieved_at","2026-10-30T14:00:00Z"),
    ("observations",[{"date":"2026-10-29","value":1}]),
    ("observations",[{"date":"2026-10-21","value":None}]),
    ("observations",[{"date":"2026-10-21","value":True}]),
    ("observations",[{"date":"2026-10-21","value":1}]*2)])
def test_invalid_manual_evidence_fails_closed(field, value):
    document=json.loads(archive())
    document[field]=value
    with pytest.raises(ValueError):
        ici_records(json.dumps(document).encode(), "2026-10-29T14:01:00Z")


def test_import_is_hash_verified_and_idempotent(tmp_path):
    document=json.loads(archive())
    document["publication_date"]="2020-10-28"
    document["retrieved_at"]="2020-10-29T14:00:00Z"
    document["observations"][0]["date"]="2020-10-21"
    path=tmp_path/"official.json"
    path.write_text(json.dumps(document))
    digest=sha256(path.read_bytes()).hexdigest()
    store=Store(tmp_path/"state")
    with pytest.raises(ValueError):
        import_ici(store,path,"wrong")
    assert import_ici(store,path,digest)["status"]=="IMPORTED"
    assert import_ici(store,path,digest)["status"]=="SKIPPED"
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]==0
