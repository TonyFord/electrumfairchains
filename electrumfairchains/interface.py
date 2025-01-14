#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2011 thomasv@gitorious
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
import os
import re
import ssl
import sys
import traceback
import asyncio
from typing import Tuple, Union, List, TYPE_CHECKING, Optional
from collections import defaultdict

import aiorpcx
from aiorpcx import RPCSession, Notification
import certifi

from .util import PrintError, ignore_exceptions, log_exceptions, bfh, SilentTaskGroup
from . import util
from . import x509
from . import pem
from . import version
from . import blockchain
from .blockchain import Blockchain
# from . import constants
from .i18n import _

if TYPE_CHECKING:
    from .network import Network


ca_path = certifi.where()


class NetworkTimeout:
    # seconds
    class Generic:
        NORMAL = 30
        RELAXED = 45
        MOST_RELAXED = 180
    class Urgent(Generic):
        NORMAL = 10
        RELAXED = 20
        MOST_RELAXED = 60

class NotificationSession(RPCSession):

    def __init__(self, *args, **kwargs):
        super(NotificationSession, self).__init__(*args, **kwargs)
        self.subscriptions = defaultdict(list)
        self.cache = {}
        self.in_flight_requests_semaphore = asyncio.Semaphore(100)
        self.default_timeout = NetworkTimeout.Generic.NORMAL
        self._msg_counter = 0
        self.interface = None  # type: Optional[Interface]

    def _get_and_inc_msg_counter(self):
        # runs in event loop thread, no need for lock
        self._msg_counter += 1
        return self._msg_counter

    async def handle_request(self, request):
        self.maybe_log(f"--> {request}")
        # note: if server sends malformed request and we raise, the superclass
        # will catch the exception, count errors, and at some point disconnect
        if isinstance(request, Notification):
            params, result = request.args[:-1], request.args[-1]
            key = self.get_hashable_key_for_rpc_call(request.method, params)
            if key in self.subscriptions:
                self.cache[key] = result
                for queue in self.subscriptions[key]:
                    await queue.put(request.args)
            else:
                raise Exception('unexpected request: {}'.format(repr(request)))

    async def send_request(self, *args, timeout=None, **kwargs):
        # note: the timeout starts after the request touches the wire!
        if timeout is None:
            timeout = self.default_timeout
        # note: the semaphore implementation guarantees no starvation
        async with self.in_flight_requests_semaphore:
            msg_id = self._get_and_inc_msg_counter()
            self.maybe_log(f"<-- {args} {kwargs} (id: {msg_id})")
            try:
                response = await asyncio.wait_for(
                    super().send_request(*args, **kwargs),
                    timeout)
            except asyncio.TimeoutError as e:
                raise RequestTimedOut(f'request timed out: {args} (id: {msg_id})') from e
            else:
                self.maybe_log(f"--> {response} (id: {msg_id})")
                return response

    async def subscribe(self, method: str, params: List, queue: asyncio.Queue):
        # note: until the cache is written for the first time,
        # each 'subscribe' call might make a request on the network.
        key = self.get_hashable_key_for_rpc_call(method, params)
        self.subscriptions[key].append(queue)
        if key in self.cache:
            result = self.cache[key]
        else:
            result = await self.send_request(method, params)
            self.cache[key] = result
        await queue.put(params + [result])

    def unsubscribe(self, queue):
        """Unsubscribe a callback to free object references to enable GC."""
        # note: we can't unsubscribe from the server, so we keep receiving
        # subsequent notifications
        for v in self.subscriptions.values():
            if queue in v:
                v.remove(queue)

    @classmethod
    def get_hashable_key_for_rpc_call(cls, method, params):
        """Hashable index for subscriptions and cache"""
        return str(method) + repr(params)

    def maybe_log(self, msg: str) -> None:
        if not self.interface: return
        if self.interface.debug or self.interface.network.debug:
            self.interface.print_error(msg)


class GracefulDisconnect(Exception): pass


class RequestTimedOut(GracefulDisconnect):
    def __str__(self):
        return _("Network request timed out.")


class ErrorParsingSSLCert(Exception): pass
class ErrorGettingSSLCertFromServer(Exception): pass


def deserialize_server(server_str: str) -> Tuple[str, str, str]:
    # host might be IPv6 address, hence do rsplit:
    host, port, protocol = str(server_str).rsplit(':', 2)
    if not host:
        raise ValueError('host must not be empty')
    if protocol not in ('s', 't'):
        raise ValueError('invalid network protocol: {}'.format(protocol))
    int(port)  # Throw if cannot be converted to int
    if not (0 < int(port) < 2**16):
        raise ValueError('port {} is out of valid range'.format(port))
    return host, port, protocol


def serialize_server(host: str, port: Union[str, int], protocol: str) -> str:
    return str(':'.join([host, str(port), protocol]))


class Interface(PrintError):
    verbosity_filter = 'i'

    def __init__(self, network: 'Network', server: str, proxy: Optional[dict]):
        self.ready = asyncio.Future()
        self.got_disconnected = asyncio.Future()
        self.server = server
        self.host, self.port, self.protocol = deserialize_server(self.server)
        self.port = int(self.port)
        assert network.config.path
        self.cert_path = os.path.join(network.config.path, 'certs', self.host)
        self.blockchain = None
        self._requested_chunks = set()
        self.network = network
        self._set_proxy(proxy)
        self.session = None  # type: NotificationSession

        self.tip_header = None
        self.tip = 0

        # Dump network messages (only for this interface).  Set at runtime from the console.
        self.debug = False

        asyncio.run_coroutine_threadsafe(
            self.network.main_taskgroup.spawn(self.run()), self.network.asyncio_loop)
        self.group = SilentTaskGroup()

    def diagnostic_name(self):
        return self.host

    def _set_proxy(self, proxy: dict):
        if proxy:
            username, pw = proxy.get('user'), proxy.get('password')
            if not username or not pw:
                auth = None
            else:
                auth = aiorpcx.socks.SOCKSUserAuth(username, pw)
            if proxy['mode'] == "socks4":
                self.proxy = aiorpcx.socks.SOCKSProxy((proxy['host'], int(proxy['port'])), aiorpcx.socks.SOCKS4a, auth)
            elif proxy['mode'] == "socks5":
                self.proxy = aiorpcx.socks.SOCKSProxy((proxy['host'], int(proxy['port'])), aiorpcx.socks.SOCKS5, auth)
            else:
                raise NotImplementedError  # http proxy not available with aiorpcx
        else:
            self.proxy = None

    async def is_server_ca_signed(self, ca_ssl_context):
        """Given a CA enforcing SSL context, returns True if the connection
        can be established. Returns False if the server has a self-signed
        certificate but otherwise is okay. Any other failures raise.
        """
        try:
            await self.open_session(ca_ssl_context, exit_early=True)
        except ssl.SSLError as e:
            if e.reason == 'CERTIFICATE_VERIFY_FAILED':
                # failures due to self-signed certs are normal
                return False
            # e.g. too weak crypto
            raise
        return True

    async def _try_saving_ssl_cert_for_first_time(self, ca_ssl_context):
        try:
            ca_signed = await self.is_server_ca_signed(ca_ssl_context)
        except (OSError, aiorpcx.socks.SOCKSError) as e:
            raise ErrorGettingSSLCertFromServer(e) from e
        if ca_signed:
            with open(self.cert_path, 'w') as f:
                # empty file means this is CA signed, not self-signed
                f.write('')
        else:
            await self.save_certificate()

    def _is_saved_ssl_cert_available(self):
        if not os.path.exists(self.cert_path):
            return False
        with open(self.cert_path, 'r') as f:
            contents = f.read()
        if contents == '':  # CA signed
            return True
        # pinned self-signed cert
        try:
            b = pem.dePem(contents, 'CERTIFICATE')
        except SyntaxError as e:
            self.print_error("error parsing already saved cert:", e)
            raise ErrorParsingSSLCert(e) from e
        try:
            x = x509.X509(b)
        except Exception as e:
            self.print_error("error parsing already saved cert:", e)
            raise ErrorParsingSSLCert(e) from e
        try:
            x.check_date()
            return True
        except x509.CertificateError as e:
            self.print_error("certificate has expired:", e)
            os.unlink(self.cert_path)  # delete pinned cert only in this case
            return False

    async def _get_ssl_context(self):
        if self.protocol != 's':
            # using plaintext TCP
            return None

        # see if we already have cert for this server; or get it for the first time
        ca_sslc = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH, cafile=ca_path)
        if not self._is_saved_ssl_cert_available():
            await self._try_saving_ssl_cert_for_first_time(ca_sslc)
        # now we have a file saved in our certificate store
        siz = os.stat(self.cert_path).st_size
        if siz == 0:
            # CA signed cert
            sslc = ca_sslc
        else:
            # pinned self-signed cert
            sslc = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=self.cert_path)
            sslc.check_hostname = 0
        return sslc

    def handle_disconnect(func):
        async def wrapper_func(self: 'Interface', *args, **kwargs):
            try:
                return await func(self, *args, **kwargs)
            except GracefulDisconnect as e:
                self.print_error("disconnecting gracefully. {}".format(repr(e)))
            finally:
                await self.network.connection_down(self)
                self.got_disconnected.set_result(1)
                # if was not 'ready' yet, schedule waiting coroutines:
                self.ready.cancel()
        return wrapper_func

    @ignore_exceptions  # do not kill main_taskgroup
    @log_exceptions
    @handle_disconnect
    async def run(self):
        try:
            ssl_context = await self._get_ssl_context()
        except (ErrorParsingSSLCert, ErrorGettingSSLCertFromServer) as e:
            self.print_error('disconnecting due to: {}'.format(repr(e)))
            return
        try:
            await self.open_session(ssl_context)
        except (asyncio.CancelledError, OSError, aiorpcx.socks.SOCKSError) as e:
            self.print_error('disconnecting due to: {}'.format(repr(e)))
            return

    def mark_ready(self):
        if self.ready.cancelled():
            raise GracefulDisconnect('conn establishment was too slow; *ready* future was cancelled')
        if self.ready.done():
            return

        assert self.tip_header
        chain = blockchain.check_header(self.tip_header)
        if not chain:
            self.blockchain = blockchain.get_best_chain()
        else:
            self.blockchain = chain
        assert self.blockchain is not None

        self.print_error("set blockchain with height", self.blockchain.height())

        self.ready.set_result(1)

    async def save_certificate(self):
        if not os.path.exists(self.cert_path):
            # we may need to retry this a few times, in case the handshake hasn't completed
            for _ in range(10):
                dercert = await self.get_certificate()
                if dercert:
                    self.print_error("succeeded in getting cert")
                    with open(self.cert_path, 'w') as f:
                        cert = ssl.DER_cert_to_PEM_cert(dercert)
                        # workaround android bug
                        cert = re.sub("([^\n])-----END CERTIFICATE-----","\\1\n-----END CERTIFICATE-----",cert)
                        f.write(cert)
                        # even though close flushes we can't fsync when closed.
                        # and we must flush before fsyncing, cause flush flushes to OS buffer
                        # fsync writes to OS buffer to disk
                        f.flush()
                        os.fsync(f.fileno())
                    break
                await asyncio.sleep(1)
            else:
                raise Exception("could not get certificate")

    async def get_certificate(self):
        sslc = ssl.SSLContext()
        try:
            async with aiorpcx.Connector(RPCSession,
                                         host=self.host, port=self.port,
                                         ssl=sslc, proxy=self.proxy) as session:
                return session.transport._ssl_protocol._sslpipe._sslobj.getpeercert(True)
        except ValueError:
            return None

    async def get_block_header(self, height, assert_mode):
        self.print_error('requesting block header {} in mode {}'.format(height, assert_mode))
        # use lower timeout as we usually have network.bhi_lock here
        timeout = self.network.get_network_timeout_seconds(NetworkTimeout.Urgent)
        res = await self.session.send_request('blockchain.block.header', [height], timeout=timeout)
        return blockchain.deserialize_header(bytes.fromhex(res), height)

    async def request_chunk(self, height, tip=None, *, can_return_early=False):
        index = height // 2016
        if can_return_early and index in self._requested_chunks:
            return
        self.print_error("requesting chunk from height {}".format(height))
        size = 2016
        if tip is not None:
            size = min(size, tip - index * 2016 + 1)
            size = max(size, 0)
        try:
            self._requested_chunks.add(index)
            res = await self.session.send_request('blockchain.block.headers', [index * 2016, size])
        finally:
            try: self._requested_chunks.remove(index)
            except KeyError: pass
        conn = self.blockchain.connect_chunk(index, res['hex'])
        if not conn:
            return conn, 0
        return conn, res['count']

    async def open_session(self, sslc, exit_early=False):
        async with aiorpcx.Connector(NotificationSession,
                                     host=self.host, port=self.port,
                                     ssl=sslc, proxy=self.proxy) as session:
            self.session = session  # type: NotificationSession
            self.session.interface = self
            self.session.default_timeout = self.network.get_network_timeout_seconds(NetworkTimeout.Generic)
            try:
                ver = await session.send_request('server.version', [version.EFC_VERSION, version.PROTOCOL_VERSION])
            except aiorpcx.jsonrpc.RPCError as e:
                raise GracefulDisconnect(e)  # probably 'unsupported protocol version'
            if exit_early:
                return
            self.print_error("connection established. version: {}".format(ver))

            async with self.group as group:
                await group.spawn(self.ping)
                await group.spawn(self.run_fetch_blocks)
                await group.spawn(self.monitor_connection)
                # NOTE: group.__aexit__ will be called here; this is needed to notice exceptions in the group!

    async def monitor_connection(self):
        while True:
            await asyncio.sleep(1)
            if not self.session or self.session.is_closing():
                raise GracefulDisconnect('server closed session')

    async def ping(self):
        while True:
            await asyncio.sleep(300)
            await self.session.send_request('server.ping')

    async def close(self):
        if self.session:
            await self.session.close()
        # monitor_connection will cancel tasks

    async def run_fetch_blocks(self):
        header_queue = asyncio.Queue()
        await self.session.subscribe('blockchain.headers.subscribe', [], header_queue)
        while True:
            item = await header_queue.get()
            raw_header = item[0]
            height = raw_header['height']
            header = blockchain.deserialize_header(bfh(raw_header['hex']), height)
            self.tip_header = header
            self.tip = height
            # if self.tip < constants.net.max_checkpoint():
            if self.tip < 0: # fork management skipped in FairChains because of PoC
                raise GracefulDisconnect('server tip below max checkpoint')
            self.mark_ready()
            await self._process_header_at_tip()
            self.network.trigger_callback('network_updated')
            await self.network.switch_unwanted_fork_interface()
            await self.network.switch_lagging_interface()

    async def _process_header_at_tip(self):
        height, header = self.tip, self.tip_header
        async with self.network.bhi_lock:
            if self.blockchain.height() >= height and self.blockchain.check_header(header):
                # another interface amended the blockchain
                self.print_error("skipping header", height)
                return
            _, height = await self.step(height, header)
            # in the simple case, height == self.tip+1
            if height <= self.tip:
                await self.sync_until(height)
        self.network.trigger_callback('blockchain_updated')

    async def sync_until(self, height, next_height=None):
        if next_height is None:
            next_height = self.tip
        last = None
        while last is None or height <= next_height:
            prev_last, prev_height = last, height
            if next_height > height + 10:
                could_connect, num_headers = await self.request_chunk(height, next_height)
                if not could_connect:
                    # if height <= constants.net.max_checkpoint():
                    if height <= 0: # fork management skipped in FairChains because of PoC
                        raise GracefulDisconnect('server chain conflicts with checkpoints or genesis')
                    last, height = await self.step(height)
                    continue
                self.network.trigger_callback('network_updated')
                height = (height // 2016 * 2016) + num_headers
                assert height <= next_height+1, (height, self.tip)
                last = 'catchup'
            else:
                last, height = await self.step(height)
            assert (prev_last, prev_height) != (last, height), 'had to prevent infinite loop in interface.sync_until'
        return last, height

    async def step(self, height, header=None):
        assert 0 <= height <= self.tip, (height, self.tip)
        if header is None:
            header = await self.get_block_header(height, 'catchup')

        chain = blockchain.check_header(header) if 'mock' not in header else header['mock']['check'](header)
        if chain:
            self.blockchain = chain if isinstance(chain, Blockchain) else self.blockchain
            # note: there is an edge case here that is not handled.
            # we might know the blockhash (enough for check_header) but
            # not have the header itself. e.g. regtest chain with only genesis.
            # this situation resolves itself on the next block
            return 'catchup', height+1

        can_connect = blockchain.can_connect(header) if 'mock' not in header else header['mock']['connect'](height)
        if not can_connect:
            self.print_error("can't connect", height)
            height, header, bad, bad_header = await self._search_headers_backwards(height, header)
            chain = blockchain.check_header(header) if 'mock' not in header else header['mock']['check'](header)
            can_connect = blockchain.can_connect(header) if 'mock' not in header else header['mock']['connect'](height)
            assert chain or can_connect
        if can_connect:
            self.print_error("could connect", height)
            height += 1
            if isinstance(can_connect, Blockchain):  # not when mocking
                self.blockchain = can_connect
                self.blockchain.save_header(header)
            return 'catchup', height

        good, bad, bad_header = await self._search_headers_binary(height, bad, bad_header, chain)
        return await self._resolve_potential_chain_fork_given_forkpoint(good, bad, bad_header)

    async def _search_headers_binary(self, height, bad, bad_header, chain):
        assert bad == bad_header['block_height']
        _assert_header_does_not_check_against_any_chain(bad_header)

        self.blockchain = chain if isinstance(chain, Blockchain) else self.blockchain
        good = height
        while True:
            assert good < bad, (good, bad)
            height = (good + bad) // 2
            self.print_error("binary step. good {}, bad {}, height {}".format(good, bad, height))
            header = await self.get_block_header(height, 'binary')
            chain = blockchain.check_header(header) if 'mock' not in header else header['mock']['check'](header)
            if chain:
                self.blockchain = chain if isinstance(chain, Blockchain) else self.blockchain
                good = height
            else:
                bad = height
                bad_header = header
            if good + 1 == bad:
                break

        mock = 'mock' in bad_header and bad_header['mock']['connect'](height)
        real = not mock and self.blockchain.can_connect(bad_header, check_height=False)
        if not real and not mock:
            raise Exception('unexpected bad header during binary: {}'.format(bad_header))
        _assert_header_does_not_check_against_any_chain(bad_header)

        self.print_error("binary search exited. good {}, bad {}".format(good, bad))
        return good, bad, bad_header

    async def _resolve_potential_chain_fork_given_forkpoint(self, good, bad, bad_header):
        assert good + 1 == bad
        assert bad == bad_header['block_height']
        _assert_header_does_not_check_against_any_chain(bad_header)
        # 'good' is the height of a block 'good_header', somewhere in self.blockchain.
        # bad_header connects to good_header; bad_header itself is NOT in self.blockchain.

        bh = self.blockchain.height()
        assert bh >= good, (bh, good)
        if bh == good:
            height = good + 1
            self.print_error("catching up from {}".format(height))
            return 'no_fork', height

        # this is a new fork we don't yet have
        height = bad + 1
        self.print_error(f"new fork at bad height {bad}")
        forkfun = self.blockchain.fork if 'mock' not in bad_header else bad_header['mock']['fork']
        b = forkfun(bad_header)  # type: Blockchain
        self.blockchain = b
        assert b.forkpoint == bad
        return 'fork', height

    async def _search_headers_backwards(self, height, header):
        async def iterate():
            nonlocal height, header
            checkp = False
            # if height <= constants.net.max_checkpoint():
            if height <= 0:
                # height = constants.net.max_checkpoint()
                height = 0
                checkp = True
            header = await self.get_block_header(height, 'backward')
            chain = blockchain.check_header(header) if 'mock' not in header else header['mock']['check'](header)
            can_connect = blockchain.can_connect(header) if 'mock' not in header else header['mock']['connect'](height)
            if chain or can_connect:
                return False
            if checkp:
                raise GracefulDisconnect("server chain conflicts with checkpoints")
            return True

        bad, bad_header = height, header
        _assert_header_does_not_check_against_any_chain(bad_header)
        with blockchain.blockchains_lock: chains = list(blockchain.blockchains.values())
        local_max = max([0] + [x.height() for x in chains]) if 'mock' not in header else float('inf')
        height = min(local_max + 1, height - 1)
        while await iterate():
            bad, bad_header = height, header
            delta = self.tip - height
            height = self.tip - 2 * delta

        _assert_header_does_not_check_against_any_chain(bad_header)
        self.print_error("exiting backward mode at", height)
        return height, header, bad, bad_header


def _assert_header_does_not_check_against_any_chain(header: dict) -> None:
    chain_bad = blockchain.check_header(header) if 'mock' not in header else header['mock']['check'](header)
    if chain_bad:
        raise Exception('bad_header must not check!')


def check_cert(host, cert):
    try:
        b = pem.dePem(cert, 'CERTIFICATE')
        x = x509.X509(b)
    except:
        traceback.print_exc(file=sys.stdout)
        return

    try:
        x.check_date()
        expired = False
    except:
        expired = True

    m = "host: %s\n"%host
    m += "has_expired: %s\n"% expired
    util.print_msg(m)


# Used by tests
def _match_hostname(name, val):
    if val == name:
        return True

    return val.startswith('*.') and name.endswith(val[1:])


def test_certificates():
    from .simple_config import SimpleConfig
    config = SimpleConfig()
    mydir = os.path.join(config.path, "certs")
    certs = os.listdir(mydir)
    for c in certs:
        p = os.path.join(mydir,c)
        with open(p, encoding='utf-8') as f:
            cert = f.read()
        check_cert(c, cert)

if __name__ == "__main__":
    test_certificates()
