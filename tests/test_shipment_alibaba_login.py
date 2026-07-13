from shipment_automation.alibaba_session import SUBMIT_SELECTORS, _has_invalid_login_error, _is_logistics_detail_ready
from shipment_automation.config import load_alibaba_login_config


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


def test_alibaba_detail_error_page_is_ready_for_parser():
    assert _is_logistics_detail_ready(
        "https://scm.alibaba.com/luyou/express/detail.htm?id=1789020252",
        "暂无数据",
    )


def test_alibaba_login_selectors_include_current_submit_button():
    assert "button.sif_form-submit" in SUBMIT_SELECTORS


def test_alibaba_login_detects_invalid_credentials_message():
    assert _has_invalid_login_error("账号名或登录密码不正确")
