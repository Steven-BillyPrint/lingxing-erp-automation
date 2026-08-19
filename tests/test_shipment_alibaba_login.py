import asyncio

import pytest

from shipment_automation import alibaba_session
from shipment_automation.alibaba_session import (
    ALIBABA_ACCOUNT_MISMATCH_MESSAGE,
    ALIBABA_ACCOUNT_UNVERIFIED_MESSAGE,
    AlibabaAccountMismatchError,
    AlibabaAccountUnverifiedError,
    SUBMIT_SELECTORS,
    _has_invalid_login_error,
    _is_logistics_detail_ready,
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


def test_logistics_query_rejects_observed_different_account(monkeypatch):
    async def observed(_page):
        return {"evelyn@billyprint.com"}

    monkeypatch.setattr(alibaba_session, "_observed_alibaba_accounts", observed)

    with pytest.raises(AlibabaAccountMismatchError) as error:
        asyncio.run(
            verify_alibaba_logistics_account(
                object(),
                "query@billyprint.com",
            )
        )

    assert str(error.value) == ALIBABA_ACCOUNT_MISMATCH_MESSAGE


def test_fresh_login_rejects_unchanged_stale_identity_cookie(monkeypatch):
    async def observed(_page):
        return set()

    async def fingerprint(_page):
        return "same-session"

    monkeypatch.setattr(alibaba_session, "_observed_alibaba_accounts", observed)
    monkeypatch.setattr(
        alibaba_session,
        "_alibaba_identity_cookie_fingerprint",
        fingerprint,
    )

    with pytest.raises(AlibabaAccountUnverifiedError) as error:
        asyncio.run(
            verify_alibaba_logistics_account(
                object(),
                "query@billyprint.com",
                fresh_configured_login=True,
                previous_fingerprint="same-session",
            )
        )

    assert str(error.value) == ALIBABA_ACCOUNT_UNVERIFIED_MESSAGE


def test_account_verification_retries_transient_unverified_page(monkeypatch):
    attempts = 0

    async def observed(_page):
        nonlocal attempts
        attempts += 1
        return set() if attempts == 1 else {"query@billyprint.com"}

    async def fingerprint(_page):
        return ""

    class FakePage:
        def __init__(self):
            self.waits: list[int] = []

        async def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

    monkeypatch.setattr(alibaba_session, "_observed_alibaba_accounts", observed)
    monkeypatch.setattr(
        alibaba_session,
        "_alibaba_identity_cookie_fingerprint",
        fingerprint,
    )
    page = FakePage()

    asyncio.run(
        verify_alibaba_logistics_account(
            page,
            "query@billyprint.com",
        )
    )

    assert attempts == 2
    assert page.waits == [
        alibaba_session.ALIBABA_ACCOUNT_VERIFICATION_RETRY_DELAYS_MS[0]
    ]


def test_alibaba_detail_error_page_is_ready_for_parser():
    assert _is_logistics_detail_ready(
        "https://scm.alibaba.com/luyou/express/detail.htm?id=1789020252",
        "暂无数据",
    )


def test_alibaba_login_selectors_include_current_submit_button():
    assert "button.sif_form-submit" in SUBMIT_SELECTORS


def test_alibaba_login_detects_invalid_credentials_message():
    assert _has_invalid_login_error("账号名或登录密码不正确")


def test_alibaba_verification_retries_once_before_interrupting_operator(
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

    assert page.waits == [alibaba_session.TRANSIENT_VERIFICATION_RETRY_DELAY_MS]
    assert page.goto_urls == [page.url]
    output = capsys.readouterr().out
    assert "自动重试一次" in output
    assert "请在浏览器里手动处理" not in output


def test_alibaba_verification_prompts_only_after_automatic_retry_fails(
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

    assert page.goto_count == 1
    assert page.waits == [
        alibaba_session.TRANSIENT_VERIFICATION_RETRY_DELAY_MS,
        alibaba_session.ALIBABA_DETAIL_POLL_INTERVAL_MS,
    ]
    output = capsys.readouterr().out
    assert output.count("自动重试一次") == 1
    assert output.count("请在浏览器里手动处理") == 1


def test_alibaba_post_login_verification_is_retried_before_manual_prompt(
    monkeypatch,
    capsys,
):
    texts = iter(
        [
            "登录 Password",
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

    async def fingerprint(_page):
        return "pre-login"

    async def verify_account(*_args, **_kwargs):
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
            if self.goto_count == 2:
                self.url = (
                    "https://scm.alibaba.com/luyou/express/detail.htm"
                    "?id=1789020252"
                )

    monkeypatch.setattr(alibaba_session, "_safe_body_text", body_text)
    monkeypatch.setattr(alibaba_session, "is_alibaba_login_page", login_page)
    monkeypatch.setattr(alibaba_session, "try_alibaba_auto_login", auto_login)
    monkeypatch.setattr(
        alibaba_session,
        "_alibaba_identity_cookie_fingerprint",
        fingerprint,
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
        )
    )

    assert page.goto_count == 2
    assert page.waits == [
        1_800,
        alibaba_session.TRANSIENT_VERIFICATION_RETRY_DELAY_MS,
    ]
    output = capsys.readouterr().out
    assert "自动重试一次" in output
    assert "请在浏览器里手动处理" not in output
