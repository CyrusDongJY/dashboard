"""Explicit one-off imports of verified public ICI observations, not an adapter."""
import argparse
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path

from .capital import ICI_URL, record
from .model import NY, UTC, finite, stamp
from .store import Store

METHOD = "OFFICIAL_PAGE_VERIFIED_IMPORT"


def ici_records(archive, acquired_at):
    document = json.loads(archive)
    now = stamp(acquired_at)
    published = date.fromisoformat(document["publication_date"])
    if (document.get("source_url") != ICI_URL or document.get("unit") != "million_usd"
            or document.get("method") != METHOD
            or stamp(document["retrieved_at"]) > now
            or published > now.astimezone(NY).date()
            or not isinstance(document.get("source_notes"), list)
            or not all(isinstance(note, str) and len(note) <= 4000 for note in document["source_notes"])
            or not isinstance(document.get("structural_note_present"), bool)):
        raise ValueError("Unverified source, units, notes or dates")
    rows = document.get("observations")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError("Invalid observation count")
    output, seen = [], set()
    for row in rows:
        day = date.fromisoformat(row["date"])
        value = finite(row.get("value"))
        if day > published or day in seen or value is None or isinstance(row.get("value"), bool):
            raise ValueError("Invalid observation")
        seen.add(day)
        output.append(record("ici_domestic", str(day), value, now, ICI_URL, sha256(archive).hexdigest(), {
            "estimate": True, "structural_note_present": document["structural_note_present"],
            "source_notes": document["source_notes"], "publication_date": str(published),
            "page_retrieved_at": document["retrieved_at"], "acquisition_method": METHOD,
            "archive_digest_basis": "VERIFIED_TRANSCRIPTION_NOT_ORIGINAL_PAGE_BYTES",
            "revision_policy": "SOURCE_CURRENT_REVISED_VINTAGE",
            "interpretation": "共同基金现金净流量与ETF净发行的周估计；不得再加样本ETF流量"}))
    return sorted(output, key=lambda r: r["observation_date"])


def import_ici(store, path, expected_hash):
    raw = Path(path).read_bytes()
    if sha256(raw).hexdigest() != expected_hash:
        raise ValueError("Archive hash mismatch")
    with store.lock("verified-import"):
        with store.connect() as db:
            previous = db.execute("SELECT payload FROM captures WHERE key=? AND status='OK'",
                                  ("ici_flows",)).fetchall()
        if any(any(r.get("source_digest") == expected_hash for r in json.loads(row[0])) for row in previous):
            return {"status": "SKIPPED", "reason": "ARCHIVE_ALREADY_IMPORTED"}
        rows = ici_records(raw, datetime.now(UTC))
        store.append("ici_flows", "OK", rows, ICI_URL + " / 单次核验补录")
        return {"status": "IMPORTED", "observations": len(rows), "sha256": expected_hash,
                "automatic_source_recovered": False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--archive", required=True)
    p.add_argument("--sha256", required=True)
    p.add_argument("--state-dir", required=True)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    raw = Path(args.archive).read_bytes()
    if sha256(raw).hexdigest() != args.sha256:
        raise ValueError("Archive hash mismatch")
    rows = ici_records(raw, datetime.now(UTC))
    result = import_ici(Store(args.state_dir), args.archive, args.sha256) if args.apply else {
        "status": "DRY_RUN", "observations": len(rows)}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
