# -*- coding=utf-8 -*-
import logging
import os
import textwrap

from middlewared.utils.shutil import globunlink

logger = logging.getLogger(__name__)

__all__ = ['globunlink_systemd_network', 'write_systemd_network_file']

PATH = '/run/systemd/network'


def globunlink_systemd_network(pattern):
    globunlink(f'{PATH}/{pattern}')


def write_systemd_network_file(path, content):
    os.makedirs(PATH, exist_ok=True)
    with open(f'{PATH}/{path}', 'w') as f:
        f.write(textwrap.dedent(content))
