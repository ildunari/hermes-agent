from hermes_cli.commands import resolve_command


def test_stats_alias_resolves_to_usage():
    command = resolve_command("stats")
    assert command is not None
    assert command.name == "usage"
    assert "stats" in command.aliases
    assert "card" in command.args_hint