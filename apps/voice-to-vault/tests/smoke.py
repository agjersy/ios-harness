INPUTS = {"audio": "tests/sample.m4a", "day": "2026-01-01"}


def check(result, response):
    assert result["note"].startswith("## Voice memo")
    assert response["dry_run_writes"][0]["path"].endswith("2026-01-01.md")
