#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2012 thomasv@ecdsa.org
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
import util
import bitcoin
from bitcoin import *

MAX_TARGET = 0x00000000FFFF0000000000000000000000000000000000000000000000000000
HEADER_SIZE = 108
CHUNK_SIZE  = 2016

class Blockchain(util.PrintError):
    '''Manages blockchain headers and their verification'''
    def __init__(self, config, network):
        self.config = config
        self.network = network
        self.local_height = 0
        self.set_local_height()

    def height(self):
        return self.local_height

    def init(self):
        import threading
        if os.path.exists(self.path()):
            self.downloading_headers = False
            return
        self.downloading_headers = True
        t = threading.Thread(target = self.init_headers_file)
        t.daemon = True
        t.start()

    def verify_header(self, header, prev_header):
        prev_hash = self.hash_header(prev_header)
        assert prev_hash == header.get('prev_block_hash'), "prev hash mismatch: %s vs %s" % (prev_hash, header.get('prev_block_hash'))
        # if bitcoin.TESTNET: return
        # assert bits == header.get('bits'), "bits mismatch: %s vs %s" % (bits, header.get('bits'))
        # _hash = self.hash_header(header)
        # assert int('0x' + _hash, 16) <= target, "insufficient proof of work: %s vs target %s" % (int('0x' + _hash, 16), target)

    def verify_chain(self, chain):
        first_header = chain[0]
        prev_header = self.read_header(first_header.get('block_height') - 1)
        for header in chain:
            self.verify_header(header, prev_header)
            prev_header = header

    def verify_chunk(self, index, data):
        num = len(data) / HEADER_SIZE
        prev_header = None
        if index != 0:
            prev_header = self.read_header(index*CHUNK_SIZE - 1)
        for i in range(num):
            raw_header = data[i*HEADER_SIZE:(i+1) * HEADER_SIZE]
            header = self.deserialize_header(raw_header)
            self.verify_header(header, prev_header)
            prev_header = header

    def serialize_header(self, res):
        s = int_to_hex(res.get('version'), 4) \
            + rev_hex(res.get('prev_block_hash')) \
            + rev_hex(res.get('merkle_root')) \
            + rev_hex(res.get('payload_hash')) \
            + int_to_hex(int(res.get('timestamp')), 4) \
            + int_to_hex(int(res.get('creatorId')), 4)
        return s

    def deserialize_header(self, s):
        hex_to_int = lambda s: int('0x' + s[::-1].encode('hex'), 16)
        h = {}
        h['version'] = hex_to_int(s[0:4])
        h['prev_block_hash'] = hash_encode(s[4:36])
        h['merkle_root'] = hash_encode(s[36:68])
        h['payload_hash'] = hash_encode(s[68:100])
        h['timestamp'] = hex_to_int(s[100:104])
        h['creatorId'] = hex_to_int(s[104:108])
        return h

    def hash_header(self, header):
        if header is None:
            return '0' * 64
        return hash_encode(Hash(self.serialize_header(header).decode('hex')))

    def path(self):
        return util.get_headers_path(self.config)

    def init_headers_file(self):
        filename = self.path()
        try:
            import urllib, socket
            socket.setdefaulttimeout(30)
            self.print_error("downloading ", bitcoin.HEADERS_URL)
            urllib.urlretrieve(bitcoin.HEADERS_URL, filename + '.tmp')
            os.rename(filename + '.tmp', filename)
            self.print_error("done.")
        except Exception:
            self.print_error("download failed. creating file", filename)
            open(filename, 'wb+').close()
        self.downloading_headers = False
        self.set_local_height()
        self.print_error("%d blocks" % self.local_height)

    def save_chunk(self, index, chunk):
        filename = self.path()
        f = open(filename, 'rb+')
        f.seek(index * CHUNK_SIZE * HEADER_SIZE)
        h = f.write(chunk)
        f.close()
        self.set_local_height()

    def save_header(self, header):
        data = self.serialize_header(header).decode('hex')
        assert len(data) == HEADER_SIZE
        height = header.get('block_height')
        filename = self.path()
        f = open(filename, 'rb+')
        f.seek(height * HEADER_SIZE)
        h = f.write(data)
        f.close()
        self.set_local_height()

    def set_local_height(self):
        name = self.path()
        if os.path.exists(name):
            h = os.path.getsize(name)/HEADER_SIZE - 1
            if self.local_height != h:
                self.local_height = h

    def read_header(self, block_height):
        name = self.path()
        if os.path.exists(name):
            f = open(name, 'rb')
            f.seek(block_height * HEADER_SIZE)
            h = f.read(HEADER_SIZE)
            f.close()
            if len(h) == HEADER_SIZE:
                h = self.deserialize_header(h)
                return h

    def connect_header(self, chain, header):
        '''Builds a header chain until it connects.  Returns True if it has
        successfully connected, False if verification failed, otherwise the
        height of the next header needed.'''
        chain.append(header)  # Ordered by decreasing height
        previous_height = header['block_height'] - 1
        previous_header = self.read_header(previous_height)

        # Missing header, request it
        if not previous_header:
            return previous_height

        # Does it connect to my chain?
        prev_hash = self.hash_header(previous_header)
        if prev_hash != header.get('prev_block_hash'):
            self.print_error("reorg")
            return previous_height

        # The chain is complete.  Reverse to order by increasing height
        chain.reverse()
        try:
            self.verify_chain(chain)
            self.print_error("new height:", previous_height + len(chain))
            for header in chain:
                self.save_header(header)
            return True
        except BaseException as e:
            self.print_error(str(e))
            return False

    def connect_chunk(self, idx, hexdata):
        try:
            data = hexdata.decode('hex')
            self.verify_chunk(idx, data)
            self.print_error("validated chunk %d" % idx)
            self.save_chunk(idx, data)
            return idx + 1
        except BaseException as e:
            self.print_error('verify_chunk failed', str(e))
            return idx - 1

