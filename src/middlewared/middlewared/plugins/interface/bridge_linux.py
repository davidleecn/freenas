from middlewared.service import Service

from .bridge_base import InterfaceBridgeBase
from .systemd_network import globunlink_systemd_network, write_systemd_network_file


class InterfaceService(Service, InterfaceBridgeBase):

    class Config:
        namespace_alias = 'interfaces'

    def bridge_setup(self, bridge):
        name = bridge['interface']['int_interface']

        write_systemd_network_file(f'{name}.netdev', f'''\
            [NetDev]
            Name={name}
            Kind=bridge
        ''')

        globunlink_systemd_network(f'{name}-*.network')

        for member in set(bridge['members']):
            write_systemd_network_file(f'{name}-{member}.network', f'''\
                [Match]
                Name={member}
                
                [Network]
                Bridge={bridge}
            ''')
