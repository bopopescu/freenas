from mako import exceptions
from mako.template import Template
from middlewared.service import Service
from middlewared.client import Client

import hashlib
import imp
import os


class MakoRenderer(object):

    def __init__(self, service):
        self.service = service

    async def render(self, path):
        try:
            tmpl = Template(filename=path)
            # Mako is not asyncio friendly so run it within a thread
            # using the client

            def do():
                with Client() as c:
                    return tmpl.render(client=c, middleware=self.service.middleware)
            return await self.service.middleware.threaded(do)
        except Exception:
            self.service.logger.debug('Failed to render mako template: {0}'.format(
                exceptions.text_error_template().render()
            ))
            raise


class PyRenderer(object):

    def __init__(self, service):
        self.service = service

    async def render(self, path):
        name = os.path.basename(path)
        find = imp.find_module(name, [os.path.dirname(path)])
        mod = imp.load_module(name, *find)
        return await mod.render(self.service, self.service.middleware)


class EtcService(Service):

    GROUPS = {
        #'user': [
        #    {'type': 'mako', 'path': 'main.passwd'},
        #    {'type': 'py', 'path': 'pwd_db'},
        #],

        #
        # Coming soon
        #
        #'kerberos': [
        #    {'type': 'mako', 'path': 'krb5.conf'},
        #    {'type': 'mako', 'path': 'krb5.keytab'},
        #],

        'ldap': [
            {'type': 'mako', 'path': 'local/openldap/ldap.conf'},
        ],
        'network': [
            {'type': 'mako', 'path': 'dhclient.conf'},
        ],
        'nss': [
            {'type': 'mako', 'path': 'nsswitch.conf'},
            {'type': 'mako', 'path': 'local/nss_ldap.conf'},
        ],
        'pam': [
            { 'type': 'mako', 'path': os.path.join('pam.d', f) }
            for f in os.listdir(
                os.path.realpath(
                    os.path.join(
                        os.path.dirname(__file__), '..', 'etc_files', 'pam.d'
                    )
                )
            )
        ],
        's3': [
            {'type': 'py', 'path': 'local/minio/certificates'},
        ]
    }

    class Config:
        private = True

    def __init__(self, *args, **kwargs):
        super(EtcService, self).__init__(*args, **kwargs)
        self.files_dir = os.path.realpath(
            os.path.join(os.path.dirname(__file__), '..', 'etc_files')
        )
        self._renderers = {
            'mako': MakoRenderer(self),
            'py': PyRenderer(self),
        }

    async def generate(self, name):
        group = self.GROUPS.get(name)
        if group is None:
            raise ValueError('{0} group not found'.format(name))

        for entry in group:

            renderer = self._renderers.get(entry['type'])
            if renderer is None:
                raise ValueError(f'Unknown type: {entry["type"]}')

            path = os.path.join(self.files_dir, entry['path'])
            try:
                rendered = await renderer.render(path)
            except Exception:
                self.logger.error(f'Failed to render {entry["type"]}:{entry["path"]}', exc_info=True)
                continue

            if rendered is None:
                continue

            outfile = '/etc/{0}'.format(entry['path'])

            # Check hash of generated and existing file
            # Do not rewrite if they are the same
            if os.path.exists(outfile):
                with open(outfile, 'rb') as f:
                    existing_hash = hashlib.sha256(f.read()).hexdigest()
                new_hash = hashlib.sha256(rendered.encode('utf-8')).hexdigest()
                if existing_hash == new_hash:
                    self.logger.debug(f'No new changes for {outfile}')
                    continue

            with open(outfile, 'w') as f:
                f.write(rendered)

    async def generate_all(self):
        """
        Generate all configuration file groups
        """
        for name in self.GROUPS.keys():
            try:
                await self.generate(name)
            except Exception:
                self.logger.error(f'Failed to generate {name} group', exc_info=True)
