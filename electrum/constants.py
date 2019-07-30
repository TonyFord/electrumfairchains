# -*- coding: utf-8 -*-
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2018 The Electrum developers
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
import json

# from .util import inv_dict

def read_json(filename, default):
    path = os.path.join(os.path.dirname(__file__)+'/fairchains/', filename)
    try:
        with open(path, 'r') as f:
            r = json.loads(f.read())
    except:
        r = default
    return r

def inv_dict(d):
    return {v: k for k, v in d.items()}

# class AbstractNet:

#    @classmethod
#    def max_checkpoint(cls) -> int:
#        print( max(0, len(cls.CHECKPOINTS) * 2016 - 1) )
#        return max(0, len(cls.CHECKPOINTS) * 2016 - 1)

class FairChains():

    FCs=read_json('fairchains.json',[])
    FC =read_json(FCs[0]+'.json', {})
    FCx=read_json(FCs[0]+'.electrumx.json', {})

    TESTNET = False

    NAME            = FC['data']['currencyName']
    SHORTNAME       = FC['data']['currencySymbol']
    BLOCKEXPLORER   = FCx['BLOCKEXPLORER']
    BLOCKEXPLORER_DEFAULT   = FCx['BLOCKEXPLORER_DEFAULT']
    EXCHANGE_RATES  = FCx['EXCHANGE_RATES']

    WIF_PREFIX      = FC['data']['secretKeyVersion']
    ADDRTYPE_P2PKH  = FC['data']['pubKeyAddrVersion']
    ADDRTYPE_P2SH   = FC['data']['scriptAddrVersion']

    SEGWIT_HRP = "bc"

    GENESIS = FC['data']['blockHash']
    DEFAULT_PORTS = FCx['PEER_DEFAULT_PORTS']
    DEFAULT_SERVERS = FCx['PEERS']
    CHECKPOINTS=[] # not used in FairChains or FairCoin

    COINBASE_MATURITY=FC['data']['dynamicChainParams']['coinbaseMaturity']
    TOTAL_SUPPLY_LIMIT=FC['data']['maxMoney']

    xprv_header = int(FC['data']['extSecretPrefix'],16)
    XPRV_HEADERS = {
        'standard':    xprv_header,  # xprv
        'p2wpkh-p2sh': 0x049d7878,  # yprv
        'p2wsh-p2sh':  0x0295b005,  # Yprv
        'p2wpkh':      0x04b2430c,  # zprv
        'p2wsh':       0x02aa7a99,  # Zprv
    }
    XPRV_HEADERS_INV = inv_dict(XPRV_HEADERS)

    xpup_header = int(FC['data']['extPubKeyPrefix'],16)
    XPUB_HEADERS = {
        'standard':    xpup_header,  # xpub
        'p2wpkh-p2sh': 0x049d7cb2,  # ypub
        'p2wsh-p2sh':  0x0295b43f,  # Ypub
        'p2wpkh':      0x04b24746,  # zpub
        'p2wsh':       0x02aa7ed3,  # Zpub
    }
    XPUB_HEADERS_INV = inv_dict(XPUB_HEADERS)
    BIP44_COIN_TYPE = 0



# don't import net directly, import the module instead (so that net is singleton)
net = FairChains

def set_mainnet():
    global net
    net = FairChains
