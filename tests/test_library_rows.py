"""The signature library rows hold nothing that depends on the clock (2026-10-25)."""
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load, run_main  # noqa: E402

D = load("insights.detect")
TZ = ZoneInfo("Europe/Ljubljana")


def _sig(i, count=12, last_seen=1_790_000_000.0, name=None):
    return D.Signature(id=i, phases="a", power={"a": 100.0}, duration_s=60.0, pf=None, count=count,
                       first_seen=last_seen - 86400, last_seen=last_seen, name=name, hour_wh=[25.0] * 24)


def test_rows_do_not_depend_on_the_clock_or_on_what_is_running():
    sigs = [_sig(1), _sig(2, count=40)]
    first = D.signature_rows(sigs, TZ)
    # a pass later, nothing filed: the rows are the same, so Home Assistant sends no state
    assert D.signature_rows(sigs, TZ) == first
    for row in first:
        assert "running" not in row and "last_seen_ago_s" not in row
        assert "last ran" not in row["description"] and "running now" not in row["description"]
        assert row["last_seen_ts"] == 1_790_000_000


def test_a_row_changes_only_when_its_signature_does():
    sig = _sig(1)
    before = D.signature_rows([sig], TZ)
    sig.count += 1
    sig.last_seen += 600
    after = D.signature_rows([sig], TZ)
    assert before != after and after[0]["last_seen_ts"] == before[0]["last_seen_ts"] + 600


def test_best_evidence_first_and_every_field_a_card_reads_is_there():
    rows = D.signature_rows([_sig(1, count=3), _sig(2, count=40)], TZ)
    assert rows[0]["evidence"] >= rows[1]["evidence"]
    need = {"id", "name", "description", "phases", "watts_by_phase", "count", "typical_duration_s", "last_seen",
            "last_seen_ts", "hour_wh", "day_wh", "where", "seen_by", "evidence", "guess", "where_confidence"}
    assert need <= set(rows[0])


if __name__ == "__main__":
    run_main(globals())
