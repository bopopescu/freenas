from middlewared.schema import accepts, Int, Str, Dict, List, Bool, Patch
from middlewared.service import filterable, CRUDService, item_method, CallError
from middlewared.utils import Nid, Popen
from middlewared.client import Client

import asyncio
import errno
import netif
import os
import random
import stat
import subprocess
import sysctl


class VMManager(object):

    def __init__(self, service):
        self.service = service
        self.logger = self.service.logger
        self._vm = {}

    async def start(self, id):
        vm = await self.service.query([('id', '=', id)], {'get': True})
        self._vm[id] = VMSupervisor(self, vm)
        try:
            asyncio.ensure_future(self._vm[id].run())
            return True
        except:
            raise

    async def stop(self, id):
        supervisor = self._vm.get(id)
        if not supervisor:
            return False

        err = await supervisor.stop()
        return err

    async def restart(self, id):
        supervisor = self._vm.get(id)
        if supervisor:
            await supervisor.restart()
            return True
        else:
            return False

    async def status(self, id):
        supervisor = self._vm.get(id)
        if supervisor is None:
            vm = await self.service.query([('id', '=', id)], {'get': True})
            self._vm[id] = VMSupervisor(self, vm)
            supervisor = self._vm.get(id)

        if supervisor and await supervisor.running():
            return {
                'state': 'RUNNING',
            }
        else:
            return {
                'state': 'STOPPED',
            }

    async def clone(self, id):
        try:
            vm = await self.service.query([('id', '=', id)], {'get': True})
            return vm
        except IndexError:
            self.logger.error("VM does not exist.")
            return None


class VMSupervisor(object):

    def __init__(self, manager, vm):
        self.manager = manager
        self.logger = self.manager.logger
        self.vm = vm
        self.proc = None
        self.web_proc = None
        self.taps = []
        self.bhyve_error = None

    async def run(self):
        args = [
            'bhyve',
            '-H',
            '-w',
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
            if device['dtype'] == 'DISK' or device['dtype'] == 'RAW':

                disk_sector_size = device['attributes'].get('sectorsize', 0)
                if disk_sector_size > 0:
                    sectorsize_args = ",sectorsize=" + str(disk_sector_size)
                else:
                    sectorsize_args = ""

                if device['attributes'].get('type') == 'AHCI':
                    args += ['-s', '{},ahci-hd,{}{}'.format(nid(), device['attributes']['path'], sectorsize_args)]
                else:
                    args += ['-s', '{},virtio-blk,{}{}'.format(nid(), device['attributes']['path'], sectorsize_args)]
            elif device['dtype'] == 'CDROM':
                args += ['-s', '{},ahci-cd,{}'.format(nid(), device['attributes']['path'])]
            elif device['dtype'] == 'NIC':
                attach_iface = device['attributes'].get('nic_attach')

                self.logger.debug('====> NIC_ATTACH: {0}'.format(attach_iface))

                tapname = netif.create_interface('tap')
                tap = netif.get_interface(tapname)
                tap.up()
                self.taps.append(tapname)
                await self.bridge_setup(tapname, tap, attach_iface)

                if device['attributes'].get('type') == 'VIRTIO':
                    nictype = 'virtio-net'
                else:
                    nictype = 'e1000'
                mac_address = device['attributes'].get('mac', None)

                # By default we add one NIC and the MAC address is an empty string.
                # Issue: 24222
                if mac_address == "":
                    mac_address = None

                if mac_address == '00:a0:98:FF:FF:FF' or mac_address is None:
                    args += ['-s', '{},{},{},mac={}'.format(nid(), nictype, tapname, self.random_mac())]
                else:
                    args += ['-s', '{},{},{},mac={}'.format(nid(), nictype, tapname, mac_address)]
            elif device['dtype'] == 'VNC':
                if device['attributes'].get('wait'):
                    wait = 'wait'
                else:
                    wait = ''

                vnc_resolution = device['attributes'].get('vnc_resolution', None)
                vnc_port = int(device['attributes'].get('vnc_port', 5900 + self.vm['id']))
                vnc_bind = device['attributes'].get('vnc_bind', '0.0.0.0')
                vnc_password = device['attributes'].get('vnc_password', None)
                vnc_web = device['attributes'].get('vnc_web', None)

                vnc_password_args = ""
                if vnc_password:
                    vnc_password_args = ",password=" + vnc_password

                if vnc_resolution is None:
                    args += [
                        '-s', '29,fbuf,tcp={}:{},w=1024,h=768{},{}'.format(vnc_bind, vnc_port, vnc_password_args, wait),
                        '-s', '30,xhci,tablet',
                    ]
                else:
                    vnc_resolution = vnc_resolution.split('x')
                    width = vnc_resolution[0]
                    height = vnc_resolution[1]
                    args += [
                        '-s', '29,fbuf,tcp={}:{},w={},h={}{},{}'.format(vnc_bind, vnc_port, width, height, vnc_password_args, wait),
                        '-s', '30,xhci,tablet',
                    ]

        args.append(self.vm['name'])

        self.logger.debug('Starting bhyve: {}'.format(' '.join(args)))
        self.proc = await Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        if vnc_web:
            split_port = int(str(vnc_port)[:2]) - 1
            vnc_web_port = str(split_port) + str(vnc_port)[2:]

            web_bind = ':{}'.format(vnc_web_port) if vnc_bind is '0.0.0.0' else '{}:{}'.format(vnc_bind, vnc_web_port)

            self.web_proc = await Popen(['/usr/local/libexec/novnc/utils/websockify/run', '--web',
                    '/usr/local/libexec/novnc/', '--wrap-mode=exit',
                    web_bind, '{}:{}'.format(vnc_bind, vnc_port)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.logger.debug("==> Start WEBVNC at port {} with pid number {}".format(vnc_web_port, self.web_proc.pid))


        while True:
            line = await self.proc.stdout.readline()
            if line == b'':
                break
            self.logger.debug('{}: {}'.format(self.vm['name'], line.decode()))

        # bhyve returns the following status code:
        # 0 - VM has been reset
        # 1 - VM has been powered off
        # 2 - VM has been halted
        # 3 - VM generated a triple fault
        # all other non-zero status codes are errors
        self.bhyve_error = await self.proc.wait()
        if self.bhyve_error == 0:
            self.logger.info("===> Rebooting VM: {0} ID: {1} BHYVE_CODE: {2}".format(self.vm['name'], self.vm['id'], self.bhyve_error))
            await self.manager.restart(self.vm['id'])
            await self.manager.start(self.vm['id'])
        elif self.bhyve_error == 1:
            # XXX: Need a better way to handle the vmm destroy.
            self.logger.info("===> Powered off VM: {0} ID: {1} BHYVE_CODE: {2}".format(self.vm['name'], self.vm['id'], self.bhyve_error))
            await self.destroy_vm()
        elif self.bhyve_error in (2, 3):
            self.logger.info("===> Stopping VM: {0} ID: {1} BHYVE_CODE: {2}".format(self.vm['name'], self.vm['id'], self.bhyve_error))
            await self.manager.stop(self.vm['id'])
        elif self.bhyve_error not in (0, 1, 2, 3, None):
            self.logger.info("===> Error VM: {0} ID: {1} BHYVE_CODE: {2}".format(self.vm['name'], self.vm['id'], self.bhyve_error))
            await self.destroy_vm()

    async def destroy_vm(self):
        self.logger.warn("===> Destroying VM: {0} ID: {1} BHYVE_CODE: {2}".format(self.vm['name'], self.vm['id'], self.bhyve_error))
        # XXX: We need to catch the bhyvectl return error.
        bhyve_error = await (await Popen(['bhyvectl', '--destroy', '--vm={}'.format(self.vm['name'])], stdout=subprocess.PIPE, stderr=subprocess.PIPE)).wait()
        self.manager._vm.pop(self.vm['id'], None)
        await self.kill_bhyve_web()
        self.destroy_tap()

    def destroy_tap(self):
        while self.taps:
            netif.destroy_interface(self.taps.pop())

    def set_tap_mtu(self, iface, tap):
        if iface.mtu > tap.mtu:
            tap.mtu = iface.mtu
        return tap

    async def bridge_setup(self, tapname, tap, attach_iface):
        if attach_iface is None:
            # XXX: backward compatibility prior to 11.1-RELEASE.
            try:
                attach_iface = netif.RoutingTable().default_route_ipv4.interface
            except:
                return

        if_bridge = []
        bridge_enabled = False

        for brgname, iface in list(netif.list_interfaces().items()):
            if brgname.startswith('bridge'):
                if_bridge.append(iface)

        if if_bridge:
            for bridge in if_bridge:
                if attach_iface in bridge.members:
                    bridge_enabled = True
                    self.set_tap_mtu(bridge, tap)
                    bridge.add_member(tapname)
                    break

        if bridge_enabled is False:
            bridge = netif.get_interface(netif.create_interface('bridge'))
            self.set_tap_mtu(bridge, tap)
            bridge.add_member(tapname)
            bridge.add_member(attach_iface)
            bridge.up()

    def random_mac(self):
        mac_address = [0x00, 0xa0, 0x98, random.randint(0x00, 0x7f), random.randint(0x00, 0xff), random.randint(0x00, 0xff)]
        return ':'.join(["%02x" % x for x in mac_address])

    async def kill_bhyve_pid(self):
        if self.proc:
            try:
                os.kill(self.proc.pid, 15)
            except ProcessLookupError as e:
                # Already stopped, process do not exist anymore
                if e.errno != errno.ESRCH:
                    raise

            await self.destroy_vm()
            return True

    async def kill_bhyve_web(self):
        if self.web_proc:
            try:
                self.logger.debug("==> Killing WEBVNC: {}".format(self.web_proc.pid))
                os.kill(self.web_proc.pid, 15)
            except ProcessLookupError as e:
                if e.errno != errno.ESRCH:
                    raise
            return True

    async def restart(self):
        bhyve_error = await (await Popen(['bhyvectl', '--force-reset', '--vm={}'.format(self.vm['name'])], stdout=subprocess.PIPE, stderr=subprocess.PIPE)).wait()
        self.logger.debug("==> Reset VM: {0} ID: {1} BHYVE_CODE: {2}".format(self.vm['name'], self.vm['id'], bhyve_error))
        self.destroy_tap()
        await self.kill_bhyve_web()

    async def stop(self):
        bhyve_error = await (await Popen(['bhyvectl', '--force-poweroff', '--vm={}'.format(self.vm['name'])], stdout=subprocess.PIPE, stderr=subprocess.PIPE)).wait()
        self.logger.debug("===> Stopping VM: {0} ID: {1} BHYVE_CODE: {2}".format(self.vm['name'], self.vm['id'], self.bhyve_error))

        if bhyve_error:
            self.logger.error("===> Stopping VM error: {0}".format(bhyve_error))

        return await self.kill_bhyve_pid()

    async def running(self):
        bhyve_error = await (await Popen(['bhyvectl', '--vm={}'.format(self.vm['name'])], stdout=subprocess.PIPE, stderr=subprocess.PIPE)).wait()
        if bhyve_error == 0:
            if self.proc:
                try:
                    os.kill(self.proc.pid, 0)
                except OSError:
                    self.logger.error("===> VMM {0} is running without bhyve process.".format(self.vm['name']))
                    return False
                return True
            else:
                # XXX: We return true for now to keep the vm.status sane.
                # It is necessary handle in a better way the bhyve process associated with the vmm.
                return True
        elif bhyve_error == 1:
            return False


class VMService(CRUDService):

    class Config:
        namespace = 'vm'

    def __init__(self, *args, **kwargs):
        super(VMService, self).__init__(*args, **kwargs)
        self._manager = VMManager(self)

    @accepts()
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

    @accepts()
    def identify_hypervisor(self):
        """
        Identify Hypervisors that might work nested with bhyve.

        Returns:
                bool: True if compatible otherwise False.
        """
        compatible_hp = ('VMwareVMware', 'Microsoft Hv', 'KVMKVMKVM', 'bhyve bhyve')
        identify_hp = sysctl.filter('hw.hv_vendor')[0].value.strip()

        if identify_hp in compatible_hp:
            return True
        return False

    @filterable
    async def query(self, filters=None, options=None):
        options = options or {}
        options['extend'] = 'vm._extend_vm'
        return await self.middleware.call('datastore.query', 'vm.vm', filters, options)

    async def _extend_vm(self, vm):
        vm['devices'] = []
        for device in await self.middleware.call('datastore.query', 'vm.device', [('vm__id', '=', vm['id'])]):
            device.pop('id', None)
            device.pop('vm', None)
            vm['devices'].append(device)
        return vm

    @accepts(Int('id'))
    async def get_vnc(self, id):
        """
        Get the vnc devices from a given guest.

        Returns:
            list(dict): with all attributes of the vnc device or an empty list.
        """
        vnc_devices = []
        for device in await self.middleware.call('datastore.query', 'vm.device', [('vm__id', '=', id)]):
            if device['dtype'] == 'VNC':
                vnc = device['attributes']
                vnc_devices.append(vnc)
        return vnc_devices

    @accepts(Int('id'))
    async def get_attached_iface(self, id):
        """
        Get the attached physical interfaces from a given guest.

        Returns:
            list: will return a list with all attached phisycal interfaces or otherwise False.
        """
        ifaces = []
        for device in await self.middleware.call('datastore.query', 'vm.device', [('vm__id', '=', id)]):
            if device['dtype'] == 'NIC':
                if_attached = device['attributes'].get('nic_attach')
                if if_attached:
                    ifaces.append(if_attached)

        if ifaces:
            return ifaces
        else:
            return False

    @accepts(Int('id'))
    async def get_console(self, id):
        """
        Get the console device from a given guest.

        Returns:
            str: with the device path or False.
        """
        try:
            guest_status = await self.status(id)
        except:
            guest_status = None

        if guest_status and guest_status['state'] == 'RUNNING':
            device = "/dev/nmdm{0}B".format(id)
            if stat.S_ISCHR(os.stat(device).st_mode) is True:
                    return device

        return False

    @accepts(Dict(
        'vm_create',
        Str('name'),
        Str('description'),
        Int('vcpus'),
        Int('memory'),
        Str('bootloader'),
        List('devices'),
        Bool('autostart'),
        register=True,
        ))
    async def do_create(self, data):
        """Create a VM."""

        devices = data.pop('devices')
        pk = await self.middleware.call('datastore.insert', 'vm.vm', data)

        for device in devices:
            device['vm'] = pk
            await self.middleware.call('datastore.insert', 'vm.device', device)
        return pk

    async def __do_update_devices(self, id, devices):
        if devices and isinstance(devices, list) is True:
            device_query = await self.middleware.call('datastore.query', 'vm.device', [('vm__id', '=', int(id))])

            # Make sure both list has the same size.
            if len(device_query) != len(devices):
                return False

            get_devices = []
            for q in device_query:
                q.pop('vm')
                get_devices.append(q)

            while len(devices) > 0:
                update_item = devices.pop(0)
                old_item = get_devices.pop(0)
                if old_item['dtype'] == update_item['dtype']:
                    old_item['attributes'] = update_item['attributes']
                    device_id = old_item.pop('id')
                    await self.middleware.call('datastore.update', 'vm.device', device_id, old_item)
            return True

    @accepts(Int('id'), Patch(
        'vm_create',
        'vm_update',
        ('attr', {'update': True}),
    ))
    async def do_update(self, id, data):
        """Update all information of a specific VM."""
        devices = data.pop('devices', None)
        if devices:
            update_devices = await self.__do_update_devices(id, devices)
        if data:
            return await self.middleware.call('datastore.update', 'vm.vm', id, data)
        else:
            return update_devices

    @accepts(Int('id'),
        Dict('devices', additional_attrs=True),
    )
    async def create_device(self, id, data):
        """Create a new device in an existing vm."""
        devices_type = ('NIC', 'DISK', 'CDROM', 'VNC', 'RAW')
        devices = data.get('devices', None)

        if devices:
            devices[0].update({"vm": id})
            dtype = devices[0].get('dtype', None)
            if dtype in devices_type and isinstance(devices, list) is True:
                devices = devices[0]
                await self.middleware.call('datastore.insert', 'vm.device', devices)
                return True
            else:
                return False
        else:
            return False

    @accepts(Int('id'))
    async def do_delete(self, id):
        """Delete a VM."""
        status = await self.status(id)
        if isinstance(status, dict):
            if status.get('state') == "RUNNING":
                stop_vm = await self.stop(id)
        try:
            return await self.middleware.call('datastore.delete', 'vm.vm', id)
        except Exception as err:
            self.logger.error("===> {0}".format(err))
            return False

    @item_method
    @accepts(Int('id'))
    async def start(self, id):
        """Start a VM."""
        try:
            return await self._manager.start(id)
        except Exception as err:
            self.logger.error("===> {0}".format(err))
            return False

    @item_method
    @accepts(Int('id'))
    async def stop(self, id):
        """Stop a VM."""
        try:
            return await self._manager.stop(id)
        except Exception as err:
            self.logger.error("===> {0}".format(err))
            return False

    @item_method
    @accepts(Int('id'))
    async def restart(self, id):
        """Restart a VM."""
        try:
            return await self._manager.restart(id)
        except Exception as err:
            self.logger.error("===> {0}".format(err))
            return False

    @item_method
    @accepts(Int('id'))
    async def status(self, id):
        """Get the status of a VM, if it is RUNNING or STOPPED."""
        try:
            return await self._manager.status(id)
        except Exception as err:
            self.logger.error("===> {0}".format(err))
            return False

    async def __find_clone(self, name):
        data = await self.middleware.call('vm.query', [], {'order_by': ['name']})
        clone_index = 0
        next_name = ""
        for vm_name in data:
            if name in vm_name['name'] and '_clone' in vm_name['name']:
                name_index = int(vm_name['name'][-1])
                next_name = vm_name['name'][:-1]
                if name_index >= clone_index:
                    clone_index = int(name_index) + 1

        if next_name:
            next_name = next_name + str(clone_index)
        else:
            next_name = name + '_clone' + str(clone_index)

        return next_name

    @accepts(Int('id'))
    async def clone(self, id):
        vm = await self._manager.clone(id)

        if vm is None:
            raise CallError('Cannot clone a VM that does not exist.', errno.EINVAL)

        origin_name = vm['name']
        del vm['id']

        vm['name'] = await self.__find_clone(vm['name'])

        for item in vm['devices']:
            if item['dtype'] == 'NIC':
                if 'mac' in item['attributes']:
                    del item['attributes']['mac']
            if item['dtype'] == 'VNC':
                if 'vnc_port' in item['attributes']:
                    del item['attributes']['vnc_port']
            if item['dtype'] == 'DISK':
                disk_src_path = '/'.join(item['attributes']['path'].split('/dev/zvol/')[-1:])
                disk_snapshot_name = vm['name']
                disk_snapshot_path = disk_src_path + '@' + disk_snapshot_name
                clone_dst_path = disk_src_path + '_' + vm['name']

                data = {'dataset': disk_src_path, 'name': disk_snapshot_name}
                await self.middleware.call('zfs.snapshot.create', data)

                data = {'snapshot': disk_snapshot_path, 'dataset_dst': clone_dst_path}
                await self.middleware.call('zfs.snapshot.clone', data)

                item['attributes']['path'] = '/dev/zvol/' + clone_dst_path
            if item['dtype'] == 'RAW':
                item['attributes']['path'] = ''
                self.logger.warn("For RAW disk you need copy it manually inside your NAS.")

        await self.create(vm)
        self.logger.info("VM cloned from {0} to {1}".format(origin_name, vm['name']))

        return True

    @accepts(Int('id'))
    async def get_vnc_web(self, id):
        """
            Get the VNC URL from a given VM.

            Returns:
                list: With all URL available.
        """
        vnc_web = []
        vnc_devices = await self.get_vnc(id)

        for vnc_device in await self.get_vnc(id):
            if vnc_device.get('vnc_web', None) is True:
                vnc_port = vnc_device.get('vnc_port', None)
                #  XXX: Create a method for web port.
                split_port = int(str(vnc_port)[:2]) - 1
                vnc_web_port = str(split_port) + str(vnc_port)[2:]
                bind_ip = vnc_device.get('vnc_bind', None)
                vnc_web.append('http://{}:{}/vnc_auto.html'.format(bind_ip, vnc_web_port))

        return vnc_web


async def kmod_load():
    kldstat = (await (await Popen(['/sbin/kldstat'], stdout=subprocess.PIPE)).communicate())[0].decode()
    if 'vmm.ko' not in kldstat:
        await Popen(['/sbin/kldload', 'vmm'])
    if 'nmdm.ko' not in kldstat:
        await Popen(['/sbin/kldload', 'nmdm'])


async def __event_system_ready(middleware, event_type, args):
    """
    Method called when system is ready, supposed to start VMs
    flagged that way.
    """
    if args['id'] != 'ready':
        return

    for vm in await middleware.call('vm.query', [('autostart', '=', True)]):
        await middleware.call('vm.start', vm['id'])


def setup(middleware):
    asyncio.ensure_future(kmod_load())
    middleware.event_subscribe('system', __event_system_ready)
