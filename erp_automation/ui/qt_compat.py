from __future__ import annotations


class PySide6RequiredError(RuntimeError):
    """Raised only when the optional desktop interface is started."""


try:
    import PySide6 as _pyside6  # noqa: F401
except ImportError as exc:  # PySide6 may be absent or a Qt DLL may fail to load.
    PYSIDE6_AVAILABLE = False
    _PYSIDE6_IMPORT_ERROR: ImportError | None = exc
else:
    PYSIDE6_AVAILABLE = True
    _PYSIDE6_IMPORT_ERROR = None


def pyside6_error_message() -> str:
    base = (
        "未安装或无法加载 PySide6，桌面界面无法启动。"
        "核心模型和 CLI 不受影响。请在桌面应用环境安装兼容版本："
        "python -m pip install PySide6"
    )
    if _PYSIDE6_IMPORT_ERROR is None:
        return base
    return f"{base}（原始错误：{_PYSIDE6_IMPORT_ERROR}）"


def require_pyside6() -> None:
    if not PYSIDE6_AVAILABLE:
        raise PySide6RequiredError(pyside6_error_message())
