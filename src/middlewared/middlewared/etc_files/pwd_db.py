from middlewared.utils import run


async def render(service, middleware):
    await run('/usr/sbin/pwd_mkdb', '/etc/main.passwd', check=False)
