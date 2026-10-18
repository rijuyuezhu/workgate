from workgate.platform.process import new_process_group_kwargs


def test_new_process_group_kwargs_uses_posix_session() -> None:
    assert new_process_group_kwargs() == {"start_new_session": True}
