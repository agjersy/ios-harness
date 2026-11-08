INPUTS = {"text": "hi"}


def check(result, response):
    assert response["show"] == "Harness heard: hi"
