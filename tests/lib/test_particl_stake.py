'''Fixtures are tests/blocks/particl_<net>_<height>.json holding the keys
test_blocks.py uses plus "blocksig", "prevstakemodifier" and "tx", from
`getblock <hash> 2 true` (the trailing true adds the stake fields) merged
with {"block": <getblock <hash> 0>}.'''

import json
import os
from binascii import unhexlify

import pytest

from electrumx.lib.coins import Coin
from electrumx.lib.hash import hash_to_hex_str

BLOCKS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'blocks')

blocks = []
for name in sorted(os.listdir(BLOCKS_DIR)):
    if not name.startswith('particl_'):
        continue
    name_parts = name.split("_")
    coin = Coin.lookup_coin_class(name_parts[0], name_parts[1])
    with open(os.path.join(BLOCKS_DIR, name)) as f:
        blocks.append((coin, json.load(f)))


@pytest.fixture(params=blocks)
def block_details(request):
    return request.param


def test_stake_block_parts(block_details):
    coin, block_info = block_details
    raw_block = unhexlify(block_info['block'])

    coinstake, prevout, blocksig = coin.stake_block_parts(raw_block, block_info['height'])

    _tx, tx_hash = coin.DESERIALIZER(coinstake).read_tx_and_hash()
    assert hash_to_hex_str(tx_hash) == block_info['tx'][0]
    assert blocksig.hex() == block_info['blocksig']
    if block_info['height'] == 0:
        assert prevout is None
    else:
        assert prevout is not None
        assert len(prevout[0]) == 32
