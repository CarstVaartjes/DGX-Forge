from vonk_control.code_host import InMemoryCodeHost


def test_code_host_rejects_force_update_and_duplicate_branch_content() -> None:
    host = InMemoryCodeHost(required_checks=())
    commit = host.create_change("dgx-control/one", "base", b"patch", "message", signed=True)
    assert host.create_change("dgx-control/one", "base", b"patch", "message", signed=True) == commit
    try:
        host.create_change("dgx-control/one", "other", b"different", "message", signed=True)
    except ValueError as error:
        assert "force" in str(error)
    else:
        raise AssertionError("branch rewrite was accepted")
