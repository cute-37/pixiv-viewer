"""Configuration round trip, always using disposable storage."""
from pathlib import Path
from tempfile import TemporaryDirectory

from utils.config_manager import ConfigManager, NetworkMount


def test_config_persistence(tmp_path):
    path = tmp_path / 'config.json'
    manager = ConfigManager(path)
    mount = NetworkMount(protocol='smb', host='example.invalid', path='share', mount_point='Z:', enabled=True)
    manager.update(network_mounts=[mount])
    assert manager.save()
    reloaded = ConfigManager(path)
    reloaded.load()
    assert reloaded.config.network_mounts == [mount]
    assert isinstance(reloaded.config.network_mounts[0], NetworkMount)
    assert reloaded.config.to_dict()['network_mounts'][0]['host'] == 'example.invalid'


if __name__ == '__main__':
    with TemporaryDirectory(prefix='viewer-config-test-') as directory:
        test_config_persistence(Path(directory))
    print('Configuration persistence passed using temporary storage.')
