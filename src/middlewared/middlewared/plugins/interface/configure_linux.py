from middlewared.service import Service

from .configure_base import InterfaceConfigureBase
from .systemd_network import globunlink_systemd_network, write_systemd_network_file


class InterfaceService(Service, InterfaceConfigureBase):

    class Config:
        namespace_alias = 'interfaces'

    def configure(self, data, aliases, wait_dhcp=False, **kwargs):
        name = data['int_interface']

        if (
            not self.middleware.call_sync('system.is_freenas') and
            self.middleware.call_sync('failover.node') == 'B'
        ):
            ipv4_field = 'int_ipv4address_b'
            ipv6_field = 'int_ipv6address'
            alias_ipv4_field = 'alias_v4address_b'
            alias_ipv6_field = 'alias_v6address_b'
        else:
            ipv4_field = 'int_ipv4address'
            ipv6_field = 'int_ipv6address'
            alias_ipv4_field = 'alias_v4address'
            alias_ipv6_field = 'alias_v6address'

        config = [
            '[Match]',
            f'Name={name}',
            '[Network]',
            f'DHCP={"yes" if data["int_dhcp"] else "no"}',
            '[Address]',
        ]

        if not data['int_dhcp'] and data[ipv4_field]:
            config.append(f'Address={data[ipv4_field]}/{data["int_v4netmaskbit"]}')
        if data[ipv6_field]:
            config.append(f'Address={data[ipv6_field]}/{data["int_v6netmaskbit"]}')

        for alias in aliases:
            if alias[alias_ipv4_field]:
                config.append(f'{alias[alias_ipv4_field]}/{alias["alias_v4netmaskbit"],}')
            if alias[alias_ipv6_field]:
                config.append(f'{alias[alias_ipv6_field]}/{alias["alias_v6netmaskbit"],}')

            # TODO:
            # if alias['alias_vip']:
            #     addrs_database.add(self.alias_to_addr({
            #         'address': alias['alias_vip'],
            #         'netmask': '32',
            #         'vhid': data['int_vhid'],
            #     }))

        # TODO: CARP

        # TODO:
        # Apply interface options specified in GUI
        # if data['int_options']:
        #     self.logger.info('{}: applying {}'.format(name, data['int_options']))
        #     proc = subprocess.Popen(['/sbin/ifconfig', name] + shlex.split(data['int_options']),
        #                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        #                             close_fds=True)
        #     err = proc.communicate()[1].decode()
        #     if err:
        #         self.logger.info('{}: error applying: {}'.format(name, err))

        config.append('[Link]')

        # In case there is no MTU in interface and it is currently
        # different than the default of 1500, revert it
        if not kwargs.get('skip_mtu'):
            config.append(f'MTUBytes={data["int_mtu"] or 1500}')

        write_systemd_network_file(f'{name}.network', '\n'.join(config))

    def autoconfigure(self, iface, wait_dhcp):
        globunlink_systemd_network(f'{iface.name}.*')
        globunlink_systemd_network(f'{iface.name}-*')

    def unconfigure(self, iface, cloned_interfaces, parent_interfaces):
        globunlink_systemd_network(f'{iface.name}.network')
