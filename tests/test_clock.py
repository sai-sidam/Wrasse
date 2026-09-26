from datetime import datetime, timedelta, timezone

from wrasse import clock

TZ = timezone(timedelta(hours=-7))
REF = datetime(2026, 9, 26, 14, 5, tzinfo=TZ)


def test_parse_deadline_forms():
    assert clock.parse_deadline("17:00", REF) == REF.replace(hour=17, minute=0)
    assert clock.parse_deadline("5pm", REF) == REF.replace(hour=17, minute=0)
    assert clock.parse_deadline("5:30 PM", REF) == REF.replace(hour=17, minute=30)
    assert clock.parse_deadline("in 2h", REF) == REF + timedelta(hours=2)
    assert clock.parse_deadline("90m", REF) == REF + timedelta(minutes=90)
    assert clock.parse_deadline("1h30m", REF) == REF + timedelta(minutes=90)
    assert clock.parse_deadline("60 minutes", REF) == REF + timedelta(minutes=60)
    assert clock.parse_deadline("9am", REF) == REF.replace(hour=9, minute=0) + timedelta(days=1)  # past → tomorrow
    assert clock.parse_deadline("2026-09-26T18:00:00-07:00", REF).hour == 18
    for bad in ("", None, "soon", "25:00", "whenever"):
        assert clock.parse_deadline(bad, REF) is None


def test_clock_line():
    plan = {"deadline": REF.replace(hour=17, minute=0).isoformat(), "current_step_id": "s2",
            "steps": [{"id": "s1", "title": "Add category", "est_min": 20, "status": "done"},
                      {"id": "s2", "title": "Monthly total report", "est_min": 40, "status": "doing"},
                      {"id": "s3", "title": "CSV", "est_min": 20, "status": "todo"},
                      {"id": "s4", "title": "Validation", "est_min": 20, "status": "todo"}]}
    assert clock.clock_line(plan, REF) == \
        "⏱ 14:05 · deadline 17:00 · 2h55m left · Step 2/4 'Monthly total report' est 40m"
    assert "OVER by 5m" in clock.clock_line(plan, REF.replace(hour=17, minute=5))
    assert clock.clock_line(None, REF) == "⏱ 14:05"


def test_budget():
    assert clock.budget_minutes(100) == 80
    assert clock.fmt_minutes(45) == "45m" and clock.fmt_minutes(125) == "2h05m"
