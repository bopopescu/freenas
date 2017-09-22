import pickle
import datetime
import hashlib
import imp
import logging
import os
import socket
import subprocess
import time

from django.utils.translation import ugettext_lazy as _
import pysnmp.hlapi
import pysnmp.smi

from freenasUI.common.locks import lock
from freenasUI.common.system import send_mail, get_sw_version, service_enabled
from freenasUI.freeadmin.hook import HookMetaclass
from freenasUI.middleware.client import client, ClientException
from freenasUI.middleware.notifier import notifier
from freenasUI.system.models import Alert as mAlert, Support
from freenasUI.support.utils import get_license

from lxml import etree

log = logging.getLogger('system.alert')


def alert_node():
    from freenasUI.middleware.notifier import notifier
    _n = notifier()
    if _n.is_freenas():
        return 'A'
    if not _n.failover_licensed():
        return 'A'
    node = _n.failover_node()
    if not node or node == 'MANUAL':
        return 'A'
    return node


class BaseAlertMetaclass(type):

    def __new__(cls, name, *args, **kwargs):
        klass = type.__new__(cls, name, *args, **kwargs)
        if name.endswith('Alert'):
            klass.name = name[:-5]
        return klass


class BaseAlert(object, metaclass=BaseAlertMetaclass):

    alert = None
    interval = 0
    fire_once = False
    name = None

    def __init__(self, alert):
        self.alert = alert

    def run(self):
        """
        Returns a list of Alert objects
        """
        raise NotImplementedError


class Alert(object):

    OK = 'OK'
    CRIT = 'CRIT'
    WARN = 'WARN'

    def __init__(self, level, message, id=None, dismiss=False, hardware=False):
        self._level = level
        self._message = message
        self._dismiss = dismiss
        self._hardware = hardware
        if id is None:
            self._id = hashlib.md5(message.encode('utf8')).hexdigest()
        else:
            self._id = id
        self._timestamp = int(time.time())

    def __repr__(self):
        return '<Alert: %s>' % self._id

    def __str__(self):
        return str(self._message)

    def __unicode__(self):
        return self._message

    def __eq__(self, other):
        return self.getId() == other.getId()

    def __ne__(self, other):
        return self.getId() != other.getId()

    def __gt__(self, other):
        return self.getId() > other.getId()

    def __ge__(self, other):
        return self.getId() >= other.getId()

    def __lt__(self, other):
        return self.getId() < other.getId()

    def __le__(self, other):
        return self.getId() <= other.getId()

    def getId(self):
        return self._id

    def getLevel(self):
        return self._level

    def getMessage(self):
        return self._message

    def setDismiss(self, value):
        self._dismiss = value

    def getDismiss(self):
        return self._dismiss

    def setHardware(self, value):
        self._hardware = value

    def getHardware(self):
        return self._hardware

    def getTimestamp(self):
        return self._timestamp

    def setTimestamp(self, value):
        self._timestamp = value

    def getDatetime(self):
        return datetime.datetime.fromtimestamp(self._timestamp)


class SnmpTrapSender:
    def __init__(self):
        self.snmp_engine = pysnmp.hlapi.SnmpEngine()
        self.auth_data = pysnmp.hlapi.CommunityData("public")
        self.transport_target = pysnmp.hlapi.UdpTransportTarget(("localhost", 162))
        self.context_data = pysnmp.hlapi.ContextData()

        mib_builder = pysnmp.smi.builder.MibBuilder()
        mib_sources = mib_builder.getMibSources() + (pysnmp.smi.builder.DirMibSource("/usr/local/share/pysnmp/mibs"),)
        mib_builder.setMibSources(*mib_sources)
        mib_builder.loadModules("FREENAS-MIB")
        mib_view_controller = pysnmp.smi.view.MibViewController(mib_builder)
        self.snmp_alert = pysnmp.hlapi.ObjectIdentity("FREENAS-MIB", "alert"). \
            resolveWithMib(mib_view_controller)
        self.snmp_alert_level = pysnmp.hlapi.ObjectIdentity("FREENAS-MIB", "alertLevel"). \
            resolveWithMib(mib_view_controller)
        self.snmp_alert_message = pysnmp.hlapi.ObjectIdentity("FREENAS-MIB", "alertMessage"). \
            resolveWithMib(mib_view_controller)

    def send_trap(self, level, message):
        error_indication, error_status, error_index, var_binds = next(
            pysnmp.hlapi.sendNotification(
                self.snmp_engine,
                self.auth_data,
                self.transport_target,
                self.context_data,
                "trap",
                pysnmp.hlapi.NotificationType(self.snmp_alert).addVarBinds(
                    (pysnmp.hlapi.ObjectIdentifier(self.snmp_alert_level), pysnmp.hlapi.OctetString(level)),
                    (pysnmp.hlapi.ObjectIdentifier(self.snmp_alert_message), pysnmp.hlapi.OctetString(message))
                )
            )
        )

        if error_indication:
            log.error(f'Failed to send SNMP trap: {error_indication}')
            return False

        return True


class AlertPlugins(metaclass=HookMetaclass):

    ALERT_FILE = '/var/tmp/alert'

    def __init__(self):
        self.basepath = os.path.abspath(
            os.path.dirname(__file__)
        )
        self.modspath = os.path.join(self.basepath, 'alertmods/')
        self.mods = []

        self.snmp_trap_sender = SnmpTrapSender()

    def rescan(self):
        self.mods = []
        for f in sorted(os.listdir(self.modspath)):
            if f.startswith('__') or not f.endswith('.py'):
                continue

            f = f.replace('.py', '')
            fp, pathname, description = imp.find_module(f, [self.modspath])

            try:
                imp.load_module(f, fp, pathname, description)
            except:
                log.error("Failed to load alert plugin: %s", f, exc_info=True)
            finally:
                if fp:
                    fp.close()

    def register(self, klass):
        instance = klass(self)
        self.mods.append(instance)

    def email(self, alerts):
        node = alert_node()
        dismisseds = [a.message_id for a in mAlert.objects.filter(node=node)]
        msgs = []
        for alert in alerts:
            if alert.getId() not in dismisseds:
                """
                This is all to allow <a> tags in alert messages.
                We need to strip out all the tags so we can send a
                plain text email.
                """
                msg = str(alert)
                msgnode = etree.fromstring('<msg>{}</msg>'.format(msg))
                for i in msgnode.xpath('//a'):
                    new = etree.Element('span')
                    new.text = '{} ({})'.format(i.text, i.attrib['href'])
                    msgnode.replace(i, new)

                etree.strip_tags(msgnode, '*')
                msgs.append(msgnode.text)
        if len(msgs) == 0:
            return

        hostname = socket.gethostname()
        send_mail(
            subject='%s: %s' % (
                hostname,
                _("Critical Alerts"),
            ),
            text='\n'.join(msgs)
        )

    def ticket(self, support, alerts):
        node = alert_node()
        dismisseds = [a.message_id for a in mAlert.objects.filter(node=node)]
        msgs = []
        for alert in alerts:
            if alert.getId() not in dismisseds:
                msgs.append(str(alert))
        if len(msgs) == 0:
            return

        serial = subprocess.Popen(
            ['/usr/local/sbin/dmidecode', '-s', 'system-serial-number'],
            stdout=subprocess.PIPE,
            encoding='utf8',
        ).communicate()[0].split('\n')[0].upper()

        license, reason = get_license()
        if license:
            company = license.customer_name.decode()
        else:
            company = 'Unknown'

        for name, verbose_name in (
            ('name', 'Contact Name'),
            ('title', 'Contact Title'),
            ('email', 'Contact E-mail'),
            ('phone', 'Contact Phone'),
            ('secondary_name', 'Secondary Contact Name'),
            ('secondary_title', 'Secondary Contact Title'),
            ('secondary_email', 'Secondary Contact E-mail'),
            ('secondary_phone', 'Secondary Contact Phone'),
        ):
            value = getattr(support, name)
            if value:
                msgs += ['', '{}: {}'.format(verbose_name, value)]

        with client as c:
            try:
                rv = c.call('support.new_ticket', {
                    'title': 'Automatic alert (%s)' % serial,
                    'body': '\n'.join(msgs),
                    'version': get_sw_version().split('-', 1)[-1],
                    'debug': False,
                    'company': company,
                    'serial': serial,
                    'department': 20,
                    'category': 'Hardware',
                    'criticality': 'Loss of Functionality',
                    'environment': 'Production',
                    'name': 'Automatic Alert',
                    'email': 'auto-support@ixsystems.com',
                    'phone': '-',
                }, job=True)
                log.debug(f'Automatic alert ticket successfully created: {rv["url"]}')
            except ClientException as e:
                log.error(f'Failed to create a support ticket: {e.error}')

    @lock('/tmp/.alertrun')
    def run(self):

        _n = notifier()
        # Skip for standby node
        if (
            not _n.is_freenas() and _n.failover_licensed() and
            _n.failover_status() == 'BACKUP'
        ):
            return []

        obj = None
        if os.path.exists(self.ALERT_FILE):
            with open(self.ALERT_FILE, 'rb') as f:
                try:
                    obj = pickle.load(f)
                except Exception:
                    log.debug('Failed to load alert state file', exc_info=True)

        if not obj:
            results = {}
        else:
            results = obj['results']
        rvs = []
        node = alert_node()
        dismisseds = [a.message_id for a in mAlert.objects.filter(node=node)]
        ids = []
        for instance in self.mods:
            try:
                if instance.name in results:
                    if instance.fire_once:
                        continue
                    if results.get(instance.name).get(
                        'lastrun'
                    ) > time.time() - (instance.interval * 60):
                        if results.get(instance.name).get('alerts'):
                            for alert in results.get(instance.name).get('alerts'):
                                ids.append(alert.getId())
                                rvs.append(alert)
                        continue
                rv = instance.run()
                if rv:
                    alerts = [_f for _f in rv if _f]
                    for alert in alerts:
                        ids.append(alert.getId())
                        if instance.name in results:
                            found = False
                            for i in (results[instance.name]['alerts'] or []):
                                if alert == i:
                                    found = i
                                    break
                            if found is not False:
                                alert.setTimestamp(found.getTimestamp())

                        if alert.getId() in dismisseds:
                            alert.setDismiss(True)
                    rvs.extend(alerts)
                results[instance.name] = {
                    'lastrun': int(time.time()),
                    'alerts': rv,
                }

            except Exception as e:
                log.debug("Alert module '%s' failed: %s", instance, e, exc_info=True)
                log.error("Alert module '%s' failed: %s", instance, e)

        qs = mAlert.objects.exclude(message_id__in=ids, node=node)
        if qs.exists():
            qs.delete()
        crits = sorted([a for a in rvs if a and a.getLevel() == Alert.CRIT])
        if obj and crits:
            lastcrits = sorted([
                a for a in obj['alerts'] if a and a.getLevel() == Alert.CRIT
            ])
            if crits == lastcrits:
                crits = []

        if crits:
            self.email(crits)

        new_alerts = sorted([
            a
            for a in rvs
            if a and (not obj or a not in obj['alerts'])
        ])

        if service_enabled("snmp"):
            for a in new_alerts:
                self.snmp_trap_sender.send_trap(a.getLevel(), str(a))

        if not notifier().is_freenas():
            # Automatically create ticket for new alerts tagged as possible
            # hardware problem
            hardware = sorted([a for a in rvs if a and a.getHardware()])
            if obj and hardware:
                lasthardware = sorted([
                    a for a in obj['alerts'] if a and a.getHardware()
                ])
                if hardware == lasthardware:
                    hardware = []
            try:
                support = Support.objects.order_by('-id')[0]
            except IndexError:
                support = Support.objects.create()
            if hardware and support.is_enabled():
                self.ticket(support, hardware)

        with open(self.ALERT_FILE, 'wb') as f:
            pickle.dump({
                'last': time.time(),
                'alerts': rvs,
                'results': results,
            }, f)
        return rvs

    def get_alerts(self):
        if not os.path.exists(self.ALERT_FILE):
            return []
        with open(self.ALERT_FILE, 'rb') as f:
            return pickle.load(f)['alerts']


alertPlugins = AlertPlugins()
alertPlugins.rescan()
