from scripts.smoke import run_smoke


def test_real_http_checkout_and_rewards_flow():
    assert run_smoke()["status"] == "passed"
