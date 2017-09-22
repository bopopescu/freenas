import os
from urllib.request import urlretrieve


def test_config_save(conn):
    config = conn.rest.post('core/download', data=['config.save', [], 'freenas.db'])

    assert config.status_code == 200
    assert isinstance(config.json(), list) is True

    url = config.json()[1]
    rv = urlretrieve(f'http://{conn.conf.target_hostname()}{url}')
    stat = os.stat(rv[0])
    assert stat.st_size > 0
