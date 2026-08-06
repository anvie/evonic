from backend.channels.whatsapp import _reject_group_for_agent


def test_dm_only_agent_rejects_group_message():
    assert _reject_group_for_agent({"dm_only": 1}, True)


def test_dm_only_agent_accepts_direct_message():
    assert not _reject_group_for_agent({"dm_only": 1}, False)


def test_regular_agent_accepts_group_message():
    assert not _reject_group_for_agent({"dm_only": 0}, True)
    assert not _reject_group_for_agent({"dm_only": None}, True)


def test_missing_agent_never_rejects():
    assert not _reject_group_for_agent(None, True)
