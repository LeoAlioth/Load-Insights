"""The pair library rows hold nothing that depends on the clock (2026-10-25)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load, run_main  # noqa: E402

D = load("insights.detect")


def _pair(i, count=12, last_seen=1_790_000_000.0, name=None):
    return D.Pair(id=i, phases="a", power={"a": 100.0}, duration_s=60.0, pf=None, count=count,
                       first_seen=last_seen - 86400, last_seen=last_seen, name=name, hour_wh=[25.0] * 24)


def test_rows_do_not_depend_on_the_clock_or_on_what_is_running():
    pairs = [_pair(1), _pair(2, count=40)]
    first = D.card_rows(pairs)
    # a pass later, nothing filed: the rows are the same, so Home Assistant sends no state
    assert D.card_rows(pairs) == first
    for row in first:
        assert "running" not in row and "last_seen_ago_s" not in row
        assert row["last_seen_ts"] == 1_790_000_000


def test_a_row_changes_only_when_its_pair_does():
    pair = _pair(1)
    before = D.card_rows([pair])
    pair.count += 1
    pair.last_seen += 600
    after = D.card_rows([pair])
    assert before != after and after[0]["last_seen_ts"] == before[0]["last_seen_ts"] + 600


def test_best_evidence_first_and_only_the_fields_the_card_reads():
    """frontend/load-insights-card.js reads these and nothing else (2026-11-10: every field was 536 kB at Home)."""
    rows = D.card_rows([_pair(1, count=3), _pair(2, count=40)])
    assert rows[0]["evidence"] >= rows[1]["evidence"]
    assert set(rows[0]) == {"id", "name", "phases", "watts_by_phase", "count", "typical_duration_s", "typical_interval_s",
                            "interval_spread_s", "last_seen_ts", "evidence", "where", "guess"}
    assert set(rows[0]["guess"]) == {"kind", "confidence"} and "," not in rows[0]["where"]


if __name__ == "__main__":
    run_main(globals())
