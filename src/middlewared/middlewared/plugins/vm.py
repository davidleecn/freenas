from middlewared.schema import accepts, Int, Str, Dict, List, Ref, Bool
from middlewared.service import filterable, CRUDService
from middlewared.utils import Nid, Popen

import errno
import gevent
import netif
import os
import subprocess
import sysctl


class VMManager(object):

    def __init__(self, service):
        self.service = service
        self.logger = self.service.logger
        self._vm = {}

    def start(self, id):
        vm = self.service.query([('id', '=', id)], {'get': True})
        self._vm[id] = VMSupervisor(self, vm)
        gevent.spawn(self._vm[id].run)

    def stop(self, id):
        supervisor = self._vm.get(id)
        if not supervisor:
            return False
        return supervisor.stop()

    def status(self, id):
        supervisor = self._vm.get(id)
        if supervisor and supervisor.running():
            return {
                'state': 'RUNNING',
            }
        else:
            return {
                'state': 'STOPPED',
            }


class VMSupervisor(object):

    def __init__(self, manager, vm):
        self.manager = manager
        self.logger = self.manager.logger
        self.vm = vm
        self.proc = None
        self.taps = []
        self.bhyve_error = None

    def run(self):
        args = [
            'bhyve',
            '-A',
            '-P',
            '-H',
            '-c', str(self.vm['vcpus']),
            '-m', str(self.vm['memory']),
            '-s', '0:0,hostbridge',
            '-s', '31,lpc',
            '-l', 'com1,/dev/nmdm{}A'.format(self.vm['id']),
        ]

        if self.vm['bootloader'] in ('UEFI', 'UEFI_CSM'):
            args += [
                '-l', 'bootrom,/usr/local/share/uefi-firmware/BHYVE_UEFI{}.fd'.format('_CSM' if self.vm['bootloader'] == 'UEFI_CSM' else ''),
            ]

        nid = Nid(3)
        for device in self.vm['devices']:
            if device['dtype'] == 'DISK':
                if device['attributes'].get('type') == 'AHCI':
                    args += ['-s', '{},ahci-hd,{}'.format(nid(), device['attributes']['path'])]
                else:
                    args += ['-s', '{},virtio-blk,{}'.format(nid(), device['attributes']['path'])]
            elif device['dtype'] == 'CDROM':
                args += ['-s', '{},ahci-cd,{}'.format(nid(), device['attributes']['path'])]
            elif device['dtype'] == 'NIC':
                tapname = netif.create_interface('tap')
                tap = netif.get_interface(tapname)
                tap.up()
                self.taps.append(tapname)
                # If Bridge
                if True:
                    bridge = None
                    for name, iface in list(netif.list_interfaces().items()):
                        if name.startswith('bridge'):
                            bridge = iface
                            break
                    if not bridge:
                        bridge = netif.get_interface(netif.create_interface('bridge'))
                    bridge.add_member(tapname)

                    defiface = Popen("route -nv show default|grep -w interface|awk '{ print $2 }'", stdout=subprocess.PIPE, shell=True).communicate()[0].strip()
                    if defiface and defiface not in bridge.members:
                        bridge.add_member(defiface)
                    bridge.up()
                if device['attributes'].get('type') == 'VIRTIO':
                    nictype = 'virtio-net'
                else:
                    nictype = 'e1000'
                args += ['-s', '{},{},{}'.format(nid(), nictype, tapname)]
            elif device['dtype'] == 'VNC':
                if device['attributes'].get('wait'):
                    wait = 'wait'
                else:
                    wait = ''

                vnc_port = int(device['attributes'].get('vnc_port', 5900 + self.vm['id']))

                args += [
                    '-s', '29,fbuf,tcp=0.0.0.0:{},w=1024,h=768,{}'.format(vnc_port, wait),
                    '-s', '30,xhci,tablet',
                ]

        args.append(self.vm['name'])

        self.logger.debug('Starting bhyve: {}'.format(' '.join(args)))
        self.proc = Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for line in self.proc.stdout:
            self.logger.debug('{}: {}'.format(self.vm['name'], line))

        # bhyve returns the following status code:
        # 0 - VM has been reset
        # 1 - VM has been powered off
        # 2 - VM has been halted
        # 3 - VM generated a triple fault
        # all other non-zero status codes are errors
        self.bhyve_error = self.proc.wait()
        if self.bhyve_error == 0:
            self.logger.info("===> REBOOTING VM: {0} ID: {1} BHYVE_CODE: {2}".format(self.vm['name'], self.vm['id'], self.bhyve_error))
            self.manager.stop(self.vm['id'])
            self.manager.start(self.vm['id'])
        elif self.bhyve_error in (1, 2, 3):
            self.logger.info("===> STOPPING VM: {0} ID: {1} BHYVE_CODE: {2}".format(self.vm['name'], self.vm['id'], self.bhyve_error))
            self.manager.stop(self.vm['id'])
            self.destroy_vm()
        elif self.bhyve_error not in (0, 1, 2, 3):
            self.destroy_vm()

    def destroy_vm(self):
        self.logger.warn("===> DESTROYING VM: {0} ID: {1} BHYVE_CODE: {2}".format(self.vm['name'], self.vm['id'], self.bhyve_error))
        bhyve_error = Popen(['bhyvectl', '--destroy', '--vm={}'.format(self.vm['name'])], stdout=subprocess.PIPE, stderr=subprocess.PIPE).wait()
        self.manager._vm.pop(self.vm['id'], None)
        self.destroy_tap()

    def destroy_tap(self):
        while self.taps:
            netif.destroy_interface(self.taps.pop())

    def stop(self):
        if self.proc:
            try:
                os.kill(self.proc.pid, 15)
            except ProcessLookupError as e:
                # Already stopped, process do not exist anymore
                if e.errno != errno.ESRCH:
                    raise
            self.destroy_tap()
            return True

    def running(self):
        if self.proc:
            try:
                os.kill(self.proc.pid, 0)
            except OSError:
                return False
            return True
        return False


class VMService(CRUDService):

    class Config:
        namespace = 'vm'

    def __init__(self, *args, **kwargs):
        super(VMService, self).__init__(*args, **kwargs)
        self._manager = VMManager(self)

    def flags(self):
        """Returns a dictionary with CPU flags for bhyve."""
        data = {}

        vmx = sysctl.filter('hw.vmm.vmx.initialized')
        data['intel_vmx'] = True if vmx and vmx[0].value else False

        ug = sysctl.filter('hw.vmm.vmx.cap.unrestricted_guest')
        data['unrestricted_guest'] = True if ug and ug[0].value else False

        rvi = sysctl.filter('hw.vmm.svm.features')
        data['amd_rvi'] = True if rvi and rvi[0].value != 0 else False

        asids = sysctl.filter('hw.vmm.svm.num_asids')
        data['amd_asids'] = True if asids and asids[0].value != 0 else False

        return data

    @accepts(Ref('query-filters'), Ref('query-options'))
    def query(self, filters=None, options=None):
        options = options or {}
        options['extend'] = 'vm._extend_vm'
        return self.middleware.call('datastore.query', 'vm.vm', filters, options)

    def _extend_vm(self, vm):
        vm['devices'] = []
        for device in self.middleware.call('datastore.query', 'vm.device', [('vm__id', '=', vm['id'])]):
            device.pop('id', None)
            device.pop('vm', None)
            vm['devices'].append(device)
        return vm

    @accepts(Dict(
        'data',
        Str('name'),
        Str('description'),
        Int('vcpus'),
        Int('memory'),
        Str('bootloader'),
        List('devices'),
        Bool('autostart'),
        ))
    def do_create(self, data):
        """Create a VM."""
        devices = data.pop('devices')
        pk = self.middleware.call('datastore.insert', 'vm.vm', data)

        for device in devices:
            device['vm'] = pk
            self.middleware.call('datastore.insert', 'vm.device', device)
        return pk

    @accepts(Int('id'), Dict(
        'data',
        Str('name'),
        Str('description'),
        Int('vcpus'),
        Int('memory'),
        Str('bootloader'),
        Bool('autostart'),
        ))
    def do_update(self, id, data):
        """Update all information of a specific VM."""
        return self.middleware.call('datastore.update', 'vm.vm', id, data)

    @accepts(Int('id'))
    def do_delete(self, id):
        """Delete a VM."""
        return self.middleware.call('datastore.delete', 'vm.vm', id)

    @accepts(Int('id'))
    def start(self, id):
        """Start a VM."""
        return self._manager.start(id)

    @accepts(Int('id'))
    def stop(self, id):
        """Stop a VM."""
        return self._manager.stop(id)

    @accepts(Int('id'))
    def status(self, id):
        """Get the status of a VM, if it is RUNNING or STOPPED."""
        return self._manager.status(id)


def kmod_load():
    kldstat = Popen(['/sbin/kldstat'], stdout=subprocess.PIPE).communicate()[0]
    if 'vmm.ko' not in kldstat:
        Popen(['/sbin/kldload', 'vmm'])
    if 'nmdm.ko' not in kldstat:
        Popen(['/sbin/kldload', 'nmdm'])


def _event_system_ready(middleware, event_type, args):
    """
    Method called when system is ready, supposed to start VMs
    flagged that way.
    """
    if args['id'] != 'ready':
        return

    for vm in middleware.call('vm.query', [('autostart', '=', True)]):
        middleware.call('vm.start', vm['id'])


def setup(middleware):
    gevent.spawn(kmod_load)
    middleware.event_subscribe('system', _event_system_ready)