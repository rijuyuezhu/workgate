"""Role ownership derived from canonical runtime configuration schemas."""

from .control import CONTROL_SETTING_NAMES, ControlConfig
from .executor import EXECUTOR_SETTING_NAMES, ExecutorConfig
from .role_config import SharedRoleConfig
from .settings import Settings

_SETTINGS_NAMES = frozenset(Settings.model_fields)
_CONTROL_CONFIG_NAMES = frozenset(ControlConfig.__dataclass_fields__)
_EXECUTOR_CONFIG_NAMES = frozenset(ExecutorConfig.__dataclass_fields__)
_SHARED_CONFIG_NAMES = frozenset(SharedRoleConfig.__dataclass_fields__)

SHARED_SETTING_NAMES = _SHARED_CONFIG_NAMES & _SETTINGS_NAMES
CONTROL_ONLY_SETTING_NAMES = CONTROL_SETTING_NAMES - SHARED_SETTING_NAMES
EXECUTOR_ONLY_SETTING_NAMES = EXECUTOR_SETTING_NAMES - SHARED_SETTING_NAMES


def validate_role_setting_names() -> None:
    """Fail closed when Settings changes without an explicit role config owner."""
    if SHARED_SETTING_NAMES != (CONTROL_SETTING_NAMES & EXECUTOR_SETTING_NAMES):
        raise RuntimeError(
            "SharedRoleConfig does not match the control/executor schema overlap"
        )
    classified = CONTROL_SETTING_NAMES | EXECUTOR_SETTING_NAMES
    missing = _SETTINGS_NAMES - classified
    extra = classified - _SETTINGS_NAMES
    if missing or extra:
        raise RuntimeError(
            "Settings role ownership mismatch: "
            f"missing={sorted(missing)}, extra={sorted(extra)}"
        )


validate_role_setting_names()
