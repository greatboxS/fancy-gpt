import pytest

from fancy_gpt.browser import FakeBrowserDriver
from fancy_gpt.browser.binding import ResponseBindingTracker
from fancy_gpt.browser.errors import BrowserNotAuthenticatedError, BrowserPromptTooLargeError, BrowserTurnAmbiguousError, BrowserTurnTimeoutError
from fancy_gpt.models import ModelRequest
from fancy_gpt.providers import ChatGPTWebAutomationProvider


def request(prompt="hello"):
    return ModelRequest(request_id="r",stage="planner",title="t",prompt=prompt,response_schema={})


def test_fake_browser_provider_happy_path_and_single_submit():
    driver=FakeBrowserDriver(['{"ok":true}']); provider=ChatGPTWebAutomationProvider(driver,timeout_s=1)
    provider.start(); response=provider.execute(request()); provider.stop()
    assert response.raw_text=='{"ok":true}'
    assert len(driver.prompts)==1


def test_provider_fails_auth_timeout_and_prompt_size():
    driver=FakeBrowserDriver([],authenticated=False); provider=ChatGPTWebAutomationProvider(driver)
    with pytest.raises(BrowserNotAuthenticatedError): provider.start()
    driver=FakeBrowserDriver([]); provider=ChatGPTWebAutomationProvider(driver,timeout_s=1); provider.start()
    with pytest.raises(BrowserTurnTimeoutError): provider.execute(request())
    provider.stop()
    driver=FakeBrowserDriver(["x"]); provider=ChatGPTWebAutomationProvider(driver,max_prompt_chars=3); provider.start()
    with pytest.raises(BrowserPromptTooLargeError): provider.execute(request("1234"))
    provider.stop()


def test_response_binding_fails_closed_on_multiple_new_turns():
    tracker=ResponseBindingTracker(set(),stable_polls=1)
    with pytest.raises(BrowserTurnAmbiguousError):
        tracker.observe([("a","one"),("b","two")],stop_visible=False)
