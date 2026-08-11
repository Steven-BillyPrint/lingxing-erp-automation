import asyncio

import pytest

from shipment_automation import alibaba_session
from shipment_automation.alibaba_session import (
    ALIBABA_ACCOUNT_MISMATCH_MESSAGE,
    ALIBABA_ACCOUNT_UNVERIFIED_MESSAGE,
    AlibabaAccountVerificationError,
    SUBMIT_SELECTORS,
    _has_invalid_login_error,
    _is_logistics_detail_ready,
    verify_alibaba_logistics_account,
    wait_for_alibaba_logistics_detail,
)
from shipment_automation.config import (
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

    with pytest.raises(AlibabaAccountVerificationError) as error:
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

    with pytest.raises(AlibabaAccountVerificationError) as error:
        asyncio.run(
            verify_alibaba_logistics_account(
                object(),
                "query@billyprint.com",
                fresh_configured_login=True,
                previous_fingerprint="same-session",
            )
        )

    assert str(error.value) == ALIBABA_ACCOUNT_UNVERIFIED_MESSAGE


def test_alibaba_detail_error_page_is_ready_for_parser():
    assert _is_logistics_detail_ready(
        "https://scm.alibaba.com/luyou/express/detail.htm?id=1789020252",
        "暂无数据",
    )


def test_alibaba_login_selectors_include_current_submit_button():
    assert "button.sif_form-submit" in SUBMIT_SELECTORS


def test_alibaba_login_detects_invalid_credentials_message():
    assert _has_invalid_login_error("账号名或登录密码不正确")


def test_alibaba_verification_waits_for_operator_then_resumes(
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

        async def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

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

    assert page.waits == [3000]
    assert "请在浏览器里手动处理" in capsys.readouterr().out
