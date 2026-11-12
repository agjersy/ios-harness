INPUTS = {"text": "call the bank", "day": "2027-01-15"}


def check(result, response):
    action = response["actions"][0]
    assert action["type"] == "reminder.create"
    assert action["due"] == "2027-01-15"
    assert action["shortcut"] == "Harness Act reminder.create"
    assert action["confirm"] is False
