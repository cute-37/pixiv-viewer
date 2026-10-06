from utils.config_manager import ConfigManager


def test_default_config(tmp_path):
    config = ConfigManager(tmp_path / 'config.json')
    assert config.config.theme == 'light'
    assert config.config.thumbnail_size == 150


def test_explicit_config_instances_are_independent(tmp_path):
    first = ConfigManager(tmp_path / 'first.json')
    second = ConfigManager(tmp_path / 'second.json')
    first.update(theme='dark')
    second.load()
    assert second.config.theme == 'light'
