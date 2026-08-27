import asyncio
import json

import pytest

from shipment_automation import alibaba_session
from shipment_automation.alibaba_session import (
    ALIBABA_ACCOUNT_CHANGED_MESSAGE,
    ALIBABA_ACCOUNT_MISMATCH_MESSAGE,
    AlibabaAccountMismatchError,
    AlibabaPasswordVerificationError,
    SUBMIT_SELECTORS,
    _has_invalid_login_error,
    _is_logistics_detail_ready,
    try_alibaba_auto_login,
    verify_alibaba_logistics_account,
    wait_for_alibaba_logistics_detail,
)
from shipment_automation.config import (
    AlibabaLoginConfig,
    load_alibaba_login_config,
    load_alibaba_logistics_query_login_config,
)


def test_load_alibaba_login_config_from_env(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        """
ALIBABA_ACCOUNT="user@example.com"
ALIBABA_PASSWORD='secret value'
ALIBABA_AUTO_LOGIN=false
""",
        encoding="utf-8",
    )

    config = load_alibaba_login_config(env_path)

    assert config.account == "user@example.com"
    assert config.password == "secret value"
    assert config.auto_login is False
    assert config.has_credentials is True


def test_missing_alibaba_password_disables_credentials(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("ALIBABA_ACCOUNT=user@example.com\n", encoding="utf-8")

    config = load_alibaba_login_config(env_path)

    assert config.has_credentials is False


def test_logistics_query_credentials_are_isolated_from_order_credentials():
    config = load_alibaba_logistics_query_login_config(
        {
            "alibaba.account": "order@example.com",
            "alibaba.password": "order-secret",
            "alibaba.logistics_query.account": "query@example.com",
            "alibaba.logistics_query.password": "query-secret",
            "alibaba.logistics_query.auto_login": False,
        }
    )

    assert config.account == "query@example.com"
    assert config.password == "query-secret"
    assert config.auto_login is False


def test_logistics_query_credentials_do_not_fall_back_to_order_account():
    config = load_alibaba_logistics_query_login_config(
        {
            "alibaba.account": "legacy@example.com",
            "alibaba.password": "legacy-secret",
        }
    )

    assert config.account is None
    assert config.password is None
    assert config.has_credentials is False


class _AttestationPage:
    def __init__(self, payload: dict[str, str] | None = None):
        self.raw = json.dumps(payload) if payload is not None else ""

    async def evaluate(self, script, argument):
        if "getItem" in script:
            return self.raw
        if "setItem" in script:
            _key, self.raw = argument
            return None
        raise AssertionError(f"unexpected script: {script}")

    def payload(self) -> dict[str, str]:
        return json.loads(self.raw)


def test_first_logistics_query_binds_configured_account_without_identity_check():
    page = _AttestationPage()

    asyncio.run(
        verify_alibaba_logistics_account(page, " Query@BillyPrint.com ")
    )

    assert page.payload() == {
        "account": "query@billyprint.com",
        "fingerprint": "",
        "pending_account": "",
        "relogin_fingerprint": "",
    }


def test_same_configured_account_reuses_profile_without_cookie_or_page_check():
    original = {
        "account": "query@billyprint.com",
        "fingerprint": "old-cookie-fingerprint",
    }
    page = _AttestationPage(original)

    asyncio.run(
        verify_alibaba_logistics_account(page, "QUERY@billyprint.com")
    )

    assert page.payload() == original


def test_configured_account_change_requires_relogin_and_records_baseline(
    monkeypatch,
):
    async def fingerprint(_page):
        return "pre-login-session"

    monkeypatch.setattr(
        alibaba_session,
        "_alibaba_identity_cookie_fingerprint",
        fingerprint,
    )
    page = _AttestationPage(
        {
            "account": "old@billyprint.com",
            "fingerprint": "previously-bound-session",
        }
    )

    with pytest.raises(AlibabaAccountMismatchError) as error:
        asyncio.run(
            verify_alibaba_logistics_account(page, "new@billyprint.com")
        )

    assert str(error.value) == ALIBABA_ACCOUNT_CHANGED_MESSAGE
    assert str(error.value) == ALIBABA_ACCOUNT_MISMATCH_MESSAGE
    assert page.payload() == {
        "account": "old@billyprint.com",
        "fingerprint": "previously-bound-session",
        "pending_account": "new@billyprint.com",
        "relogin_fingerprint": "pre-login-session",
    }


def test_configured_account_change_stays_blocked_before_session_changes(
    monkeypatch,
):
    async def fingerprint(_page):
        return "pre-login-session"

    monkeypatch.setattr(
        alibaba_session,
        "_alibaba_identity_cookie_fingerprint",
        fingerprint,
    )
    page = _AttestationPage(
        {
            "account": "old@billyprint.com",
            "fingerprint": "previously-bound-session",
            "pending_account": "new@billyprint.com",
            "relogin_fingerprint": "pre-login-session",
        }
    )

    with pytest.raises(AlibabaAccountMismatchError):
        asyncio.run(
            verify_alibaba_logistics_account(page, "new@billyprint.com")
        )


def test_configured_account_change_rebinds_after_session_changes(monkeypatch):
    async def fingerprint(_page):
        return "post-login-session"

    monkeypatch.setattr(
        alibaba_session,
        "_alibaba_identity_cookie_fingerprint",
        fingerprint,
    )
    page = _AttestationPage(
        {
            "account": "old@billyprint.com",
            "fingerprint": "previously-bound-session",
            "pending_account": "new@billyprint.com",
            "relogin_fingerprint": "pre-login-session",
        }
    )

    asyncio.run(
        verify_alibaba_logistics_account(page, "new@billyprint.com")
    )

    assert page.payload() == {
        "account": "new@billyprint.com",
        "fingerprint": "post-login-session",
        "pending_account": "",
        "relogin_fingerprint": "",
    }


def test_fresh_login_rebinds_changed_configured_account(monkeypatch):
    async def fingerprint(_page):
        return "fresh-login-session"

    monkeypatch.setattr(
        alibaba_session,
        "_alibaba_identity_cookie_fingerprint",
        fingerprint,
    )
    page = _AttestationPage(
        {
            "account": "old@billyprint.com",
            "fingerprint": "old-session",
        }
    )

    asyncio.run(
        verify_alibaba_logistics_account(
            page,
            "new@billyprint.com",
            fresh_configured_login=True,
        )
    )

    assert page.payload()["account"] == "new@billyprint.com"
    assert page.payload()["fingerprint"] == "fresh-login-session"


def test_alibaba_detail_error_page_is_ready_for_parser():
    assert _is_logistics_detail_ready(
        "https://scm.alibaba.com/luyou/express/detail.htm?id=1789020252",
        "暂无数据",
    )


def test_alibaba_login_selectors_include_current_submit_button():
    assert "button.sif_form-submit" in SUBMIT_SELECTORS


def test_alibaba_auto_login_stops_before_submit_when_page_rewrites_password(
    monkeypatch,
):
    configured_password = "configured-P@ss_42"
    submit_calls = []

    class FakeInput:
        def __init__(self, *, rewritten: bool = False):
            self.value = ""
            self.rewritten = rewritten
            self.read_count = 0

        async def fill(self, value):
            self.value = value

        async def input_value(self, *, timeout):
            assert timeout == 2000
            self.read_count += 1
            if self.rewritten and self.read_count > 1:
                return "page-rewritten-value"
            return self.value

    account_input = FakeInput()
    password_input = FakeInput(rewritten=True)
    inputs = iter((account_input, password_input))

    async def first_visible(_scope, _selectors):
        return next(inputs)

    async def click_submit(_scope):
        submit_calls.append(True)
        return True

    class FakePage:
        frames = []

        async def wait_for_timeout(self, milliseconds):
            assert milliseconds == 150

    monkeypatch.setattr(
        alibaba_session,
        "_first_visible_locator",
        first_visible,
    )
    monkeypatch.setattr(
        alibaba_session,
        "_click_login_submit",
        click_submit,
    )

    with pytest.raises(
        AlibabaPasswordVerificationError,
        match="改写了密码框",
    ) as raised:
        asyncio.run(
            try_alibaba_auto_login(
                FakePage(),
                AlibabaLoginConfig(
                    account="order@example.com",
                    password=configured_password,
                ),
            )
        )

    assert submit_calls == []
    assert configured_password not in str(raised.value)


def test_alibaba_login_detects_invalid_credentials_message():
    assert _has_invalid_login_error("账号名或登录密码不正确")


def test_alibaba_first_verification_closes_dialog_and_retries_login_once(
    monkeypatch,
    capsys,
):
    texts = iter(
        [
            "阿里页面安全验证，请完成人机验证",
            "物流订单详情 订单状态 物流订单号",
        ]
    )

    async def body_text(_page):
        return next(texts)

    async def not_login(_page, _body_text=None):
        return False

    retries = []

    async def close_and_retry(page):
        retries.append(page)
        return True

    class FakePage:
        url = "https://scm.alibaba.com/luyou/express/detail.htm?id=1789020252"

        def __init__(self):
            self.waits: list[int] = []
            self.goto_urls: list[str] = []

        async def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

        async def goto(self, url, *, wait_until):
            assert wait_until == "domcontentloaded"
            self.goto_urls.append(url)

    monkeypatch.setattr(alibaba_session, "_safe_body_text", body_text)
    monkeypatch.setattr(alibaba_session, "is_alibaba_login_page", not_login)
    monkeypatch.setattr(
        alibaba_session,
        "close_verification_dialog_and_retry_login",
        close_and_retry,
    )
    page = FakePage()

    asyncio.run(
        wait_for_alibaba_logistics_detail(
            page,
            page.url,
            login_config=None,
            auto_login=False,
            timeout_sec=30,
        )
    )

    assert retries == [page]
    assert page.waits == []
    assert page.goto_urls == []
    output = capsys.readouterr().out
    assert "关闭验证弹窗并重新点击登录" in output
    assert "请在浏览器里手动处理" not in output


def test_alibaba_second_verification_requests_manual_login_then_continues(
    monkeypatch,
    capsys,
):
    texts = iter(
        [
            "阿里页面安全验证，请完成人机验证",
            "阿里页面安全验证，请完成人机验证",
            "物流订单详情 订单状态 物流订单号",
        ]
    )

    async def body_text(_page):
        return next(texts)

    async def not_login(_page, _body_text=None):
        return False

    retries = []
    manual_prompts = []

    async def close_and_retry(page):
        retries.append(page)
        return True

    async def manual_login(message):
        manual_prompts.append(message)
        return True

    class FakePage:
        url = "https://scm.alibaba.com/luyou/express/detail.htm?id=1789020252"

        def __init__(self):
            self.waits: list[int] = []
            self.goto_count = 0

        async def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

        async def goto(self, _url, *, wait_until):
            assert wait_until == "domcontentloaded"
            self.goto_count += 1

    monkeypatch.setattr(alibaba_session, "_safe_body_text", body_text)
    monkeypatch.setattr(alibaba_session, "is_alibaba_login_page", not_login)
    monkeypatch.setattr(
        alibaba_session,
        "close_verification_dialog_and_retry_login",
        close_and_retry,
    )
    page = FakePage()

    asyncio.run(
        wait_for_alibaba_logistics_detail(
            page,
            page.url,
            login_config=None,
            auto_login=False,
            timeout_sec=30,
            manual_login_callback=manual_login,
        )
    )

    assert retries == [page]
    assert len(manual_prompts) == 1
    assert "人工完成验证并登录" in manual_prompts[0]
    assert page.goto_count == 0
    assert page.waits == []
    output = capsys.readouterr().out
    assert output.count("关闭验证弹窗并重新点击登录") == 1
    assert output.count("验证码或安全验证再次出现") == 1
    assert "继续读取阿里物流订单信息" in output


def test_alibaba_post_login_verification_retries_login_then_prompts_manual(
    monkeypatch,
    capsys,
):
    texts = iter(
        [
            "登录 Password",
            "验证码",
            "验证码",
            "物流订单详情 订单状态 物流订单号",
        ]
    )

    async def body_text(_page):
        return next(texts)

    async def login_page(_page, body_text=None):
        return body_text in {"登录 Password", "验证码"}

    async def auto_login(_page, _config):
        return True

    retries = []
    manual_prompts = []

    async def close_and_retry(page):
        retries.append(page)
        return True

    async def manual_login(message):
        manual_prompts.append(message)
        page.url = (
            "https://scm.alibaba.com/luyou/express/detail.htm"
            "?id=1789020252"
        )
        return True

    verified = []

    async def verify_account(*_args, **kwargs):
        verified.append(kwargs)
        return None

    class FakePage:
        url = "https://login.alibaba.com/member/signin.htm"

        def __init__(self):
            self.waits: list[int] = []
            self.goto_count = 0

        async def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

        async def goto(self, _url, *, wait_until):
            assert wait_until == "domcontentloaded"
            self.goto_count += 1

    monkeypatch.setattr(alibaba_session, "_safe_body_text", body_text)
    monkeypatch.setattr(alibaba_session, "is_alibaba_login_page", login_page)
    monkeypatch.setattr(alibaba_session, "try_alibaba_auto_login", auto_login)
    monkeypatch.setattr(
        alibaba_session,
        "close_verification_dialog_and_retry_login",
        close_and_retry,
    )
    monkeypatch.setattr(
        alibaba_session,
        "verify_alibaba_logistics_account",
        verify_account,
    )
    page = FakePage()

    asyncio.run(
        wait_for_alibaba_logistics_detail(
            page,
            "https://scm.alibaba.com/luyou/express/detail.htm?id=1789020252",
            login_config=AlibabaLoginConfig(
                account="query@example.com",
                password="secret",
            ),
            timeout_sec=30,
            manual_login_callback=manual_login,
        )
    )

    assert page.goto_count == 1
    assert page.waits == [alibaba_session.POST_LOGIN_SUBMIT_DELAY_MS]
    assert retries == [page]
    assert len(manual_prompts) == 1
    output = capsys.readouterr().out
    assert "关闭验证弹窗并重新点击登录" in output
    assert "验证码或安全验证再次出现" in output
    assert verified == [{"fresh_configured_login": True}]


def test_close_verification_dialog_never_clicks_verification_fields():
    clicked = []
    waits = []
    close_selector = '[role="dialog"] button[aria-label="Close"]'
    submit_selector = "button.sif_form-submit"

    class FakeItem:
        def __init__(self, selector):
            self.selector = selector

        async def is_visible(self, timeout):
            assert timeout == 500
            return True

        async def click(self, timeout):
            assert timeout == 5000
            clicked.append(self.selector)

        async def inner_text(self, timeout):
            assert timeout == 500
            return "登录"

    class FakeLocator:
        def __init__(self, selector):
            self.selector = selector

        async def count(self):
            return int(self.selector in {close_selector, submit_selector})

        def nth(self, _index):
            return FakeItem(self.selector)

    class FakePage:
        frames = []

        def locator(self, selector):
            return FakeLocator(selector)

        async def wait_for_timeout(self, milliseconds):
            waits.append(milliseconds)

    completed = asyncio.run(
        alibaba_session.close_verification_dialog_and_retry_login(FakePage())
    )

    assert completed is True
    assert clicked == [close_selector, submit_selector]
    assert waits == [
        alibaba_session.VERIFICATION_DIALOG_CLOSE_DELAY_MS,
        alibaba_session.POST_LOGIN_SUBMIT_DELAY_MS,
    ]
    assert not any("input" in selector.lower() for selector in clicked)
