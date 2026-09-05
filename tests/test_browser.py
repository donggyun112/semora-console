"""Browser flows against a running console, driven by Playwright and a real model.

Skipped unless ``CONSOLE_URL`` points at a live console (``make up`` gives you one) and
Playwright is installed: ``uv sync --group browser && uv run playwright install chromium``.
Then ``CONSOLE_URL=http://localhost:8850 uv run --group browser pytest -q tests/test_browser.py``,
or ``make browser``. Every test drives the page the way a person does, so each takes a few
model round-trips; the whole file is minutes, not seconds.

What ``scripts/acceptance.py`` cannot see is what these check: that the buttons a person
presses do what the API does, that a reload lands back on the same run, and that a fork or a
recovery re-asks the gate exactly when the policy says so.
"""

from __future__ import annotations

import os

import pytest

playwright = pytest.importorskip("playwright.sync_api")

CONSOLE_URL = os.getenv("CONSOLE_URL")
LONG = 120_000  # a model round-trip through OpenRouter, generously

pytestmark = pytest.mark.skipif(not CONSOLE_URL, reason="set CONSOLE_URL to a running console")


@pytest.fixture
def page():
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(CONSOLE_URL)
        page.wait_for_selector("#run:not([disabled])", timeout=20_000)
        if not visible(page, "#launch"):
            back_to_launch(page)
        yield page
        assert not errors, errors
        browser.close()


def visible(page, selector: str) -> bool:
    return page.eval_on_selector(selector, "el => !el.classList.contains('hidden') && !el.hidden")


def back_to_launch(page) -> None:
    page.click(".brand")  # the wordmark leaves a finished run
    page.wait_for_selector("#launch:not(.hidden)", timeout=10_000)


def scene(page, number: int) -> None:
    """Pick a guide scene: scenario and policies in one click."""
    page.click(f"#guide button:has-text('{number}.')")


def chat(page) -> str:
    return page.inner_text("#chat-thread")


def wait_parked_or_done(page) -> None:
    page.wait_for_selector("#outcome-strip:not(.hidden), #approval:not(.hidden)", timeout=LONG)


def approve_if_asked(page) -> bool:
    """A fork or a recovery under the same policy re-asks the gate; a person approves again."""
    if visible(page, "#approval"):
        page.click("#approve")
        page.wait_for_selector("#outcome-strip:not(.hidden)", timeout=LONG)
        return True
    return False


def test_approve_reload_fork_recover(page):
    """Scene 2 approved, restored by a reload, forked from its input, then scene 4 recovered."""
    scene(page, 2)
    page.click("#run")
    page.wait_for_selector("#approval:not(.hidden)", timeout=LONG)
    page.click("#approve")
    page.wait_for_selector("#outcome-strip:not(.hidden)", timeout=LONG)
    branch = page.evaluate("localStorage.getItem('semora-console:run')")
    assert branch and branch.startswith("branch-"), branch
    assert '"status": "charged"' in chat(page)

    page.reload()
    page.wait_for_selector("#outcome-strip:not(.hidden)", timeout=30_000)
    assert not visible(page, "#launch"), "a restored run stays on the inspector"
    assert '"status": "charged"' in chat(page)

    page.click(".trace-fork button >> nth=0")  # 이 입력에서 분기
    page.wait_for_selector("#version-switcher:not(.hidden)", timeout=LONG)
    wait_parked_or_done(page)
    assert approve_if_asked(page), "the same policy parks the branch at the gate again"
    assert page.locator("#version-switcher button").count() == 2
    assert "PAYMENT DEDUPE" in chat(page), "the same request does not charge twice"

    back_to_launch(page)
    scene(page, 4)
    page.click("#run")
    page.wait_for_selector("#recovery:not(.hidden)", timeout=LONG)
    page.click("#recover")
    wait_parked_or_done(page)
    approve_if_asked(page)  # the worker died before the park was written, so the gate asks
    assert chat(page).count('"status": "charged"') == 1


def test_approval_card_edits_the_arguments(page):
    """Garbage is refused on the field; an edited amount is what runs."""
    scene(page, 2)
    page.click("#run")
    page.wait_for_selector("#approval:not(.hidden)", timeout=LONG)
    page.wait_for_function("document.querySelector('#approval-args').value.includes('amount')")
    seeded = page.input_value("#approval-args")
    assert '"amount": "49"' in seeded, seeded

    page.fill("#approval-args", "not json")
    page.click("#approve")
    assert page.evaluate("document.querySelector('#approval-args').validationMessage")
    assert visible(page, "#approval"), "nothing was sent"

    page.fill("#approval-args", seeded.replace('"49"', '"5"'))
    with page.expect_request(lambda r: r.url.endswith("/api/resume") and r.method == "POST") as req:
        page.click("#approve")
    assert req.value.post_data_json["args"]["amount"] == "5"
    page.wait_for_selector("#outcome-strip:not(.hidden)", timeout=LONG)
    assert '"amount": "5"' in chat(page)


def test_deny_reload_while_parked_abort(page):
    scene(page, 2)
    page.click("#run")
    page.wait_for_selector("#approval:not(.hidden)", timeout=LONG)
    page.click("#deny")
    page.wait_for_selector("#outcome-strip:not(.hidden)", timeout=LONG)
    assert '"status": "charged"' not in chat(page)
    assert "실행 안 됨" in chat(page)

    back_to_launch(page)
    scene(page, 2)
    page.click("#run")
    page.wait_for_selector("#approval:not(.hidden)", timeout=LONG)
    page.reload()
    page.wait_for_selector("#approval:not(.hidden)", timeout=30_000), "the park survives a reload"
    assert '"amount"' in page.input_value("#approval-args")
    page.click("#approve")
    page.wait_for_selector("#outcome-strip:not(.hidden)", timeout=LONG)
    assert chat(page).count('"status": "charged"') == 1

    back_to_launch(page)
    scene(page, 2)
    page.click("#run")
    page.wait_for_selector("#approval:not(.hidden)", timeout=LONG)
    page.click("#abort")
    page.wait_for_function(
        "() => !['실행 중', '승인 대기'].includes(document.querySelector('#run-status').textContent.trim())",
        timeout=LONG,
    )
    assert '"status": "charged"' not in chat(page)


def test_indeterminate_rejournal_and_menu_scenario(page):
    scene(page, 5)  # 청구 도중 장애: the payment leaves, its record does not
    page.click("#run")
    page.wait_for_selector("#recovery:not(.hidden)", timeout=LONG)
    page.click("#recover")
    page.wait_for_selector("#run-error:not(.hidden)", timeout=LONG)
    assert "나갔을 수도" in page.inner_text("#run-error")
    assert '"status": "charged"' not in chat(page), "no result was invented"
    page.click("#return-draft")
    page.wait_for_selector("#launch:not(.hidden)", timeout=10_000)

    scene(page, 6)  # 마스킹 후 분기
    page.click("#run")
    page.wait_for_selector("#outcome-strip:not(.hidden)", timeout=LONG)
    assert "RESULT MASK" in chat(page)
    page.click("#policy-open")
    page.wait_for_selector("#policy-drawer[open]")
    if page.is_checked('#units input[value="pii_mask"]'):
        page.click('#units input[value="pii_mask"]')
    page.click("#policy-close")
    # "툴 결과에서 분기" continues from the journaled, masked result on purpose. This button
    # re-journals the recorded raw result under the new policy and asks nobody to approve.
    page.locator(".trace-fork button", has_text="기록된 결과에 새 정책만 적용").first.click()
    page.wait_for_selector("#version-switcher:not(.hidden)", timeout=LONG)
    page.wait_for_selector("#outcome-strip:not(.hidden)", timeout=LONG)
    assert "jane@doe.io" in chat(page) and "CALL REPLAY" in chat(page)

    back_to_launch(page)
    page.click("#scenario-trigger")
    page.click(".scenario-option:has-text('노트 저장')")
    page.click("#run")
    page.wait_for_selector("#outcome-strip:not(.hidden)", timeout=LONG)
    assert "remember_note" in chat(page)


def test_policy_drawer_composes_without_a_scenario_list(page):
    page.click("#launch-policy-open")
    page.wait_for_selector("#policy-drawer[open]")
    assert page.query_selector("#scenarios") is None
    assert page.query_selector_all("#policy-drawer input[type=radio]") == []
    before = page.inner_text("#compose-summary")
    page.click('#units input[value="pii_mask"]')
    assert page.inner_text("#compose-summary") != before
    page.click("#policy-close")
    assert "pii_mask" in page.inner_text("#launch-policies")
