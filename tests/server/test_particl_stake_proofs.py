import json

import pytest
from aiorpcx import RPCError

from electrumx.lib import util
from electrumx.lib.coins import Particl
from electrumx.lib.hash import hash_to_hex_str
from electrumx.server.daemon import Daemon, DaemonError
from electrumx.server.session import DAEMON_ERROR, ElectrumX, ParticlElectrumX


def source_hash(n):
    return bytes([0xa0 + n]) * 32


class FakeEnv:
    def __init__(self, max_send):
        self.max_send = max_send


class FakeDaemon:
    def __init__(self, missing_tx=(), tx_hex_size=64, missing_block=()):
        self.missing_tx = {hash_to_hex_str(source_hash(n)) for n in missing_tx}
        self.tx_hex_size = tx_hex_size
        self.missing_block = missing_block
        self.modifier_asked = []

    async def raw_blocks(self, hex_hashes, replace_errs=False):
        blocks = []
        for n in range(len(hex_hashes)):
            if n in self.missing_block:
                if not replace_errs:
                    raise DaemonError({'code': -5, 'message': 'Block not found'})
                blocks.append(None)
            else:
                blocks.append(bytes([n]))
        return blocks

    async def getrawtransactions_verbose(self, hex_hashes, replace_errs=False):
        txs = []
        for hex_hash in hex_hashes:
            if hex_hash in self.missing_tx:
                if not replace_errs:
                    raise DaemonError({'code': -5, 'message': 'No such transaction'})
                txs.append(None)
            else:
                txs.append({'hex': 'ab' * (self.tx_hex_size // 2),
                            'blockhash': 'bb' + hex_hash[2:]})
        return txs

    async def getblockheaders(self, hex_hashes, replace_errs=False):
        return [{'height': 1} for _ in hex_hashes]

    async def stake_modifier(self, hex_hash):
        self.modifier_asked.append(hex_hash)
        return 'cd' * 32


class FakeDB:
    db_height = 10

    async def fs_block_hashes(self, start, count):
        return [bytes([h]) * 32 for h in range(start, start + count)]


class FakeCoin:
    @staticmethod
    def stake_block_parts(raw_block, height):
        n = raw_block[0]
        return bytes([n]), (source_hash(n), 0), b'\x30'


class FakeSessionManager:
    def __init__(self, daemon):
        self.daemon = daemon

    async def daemon_request(self, method, *args):
        try:
            return await getattr(self.daemon, method)(*args)
        except DaemonError as e:
            raise RPCError(DAEMON_ERROR, f'daemon error: {e!r}') from None

    async def merkle_branch_for_tx_pos(self, height, tx_pos):
        return [], None, 1.0

    async def merkle_branch_for_tx_hash(self, height, tx_hash):
        return [], 0, 1.0

    async def raw_header(self, height):
        return b'\x00' * 112


def make_session(missing_tx=(), tx_hex_size=64, max_send=1_000_000, missing_block=()):
    session = ParticlElectrumX.__new__(ParticlElectrumX)
    session.env = FakeEnv(max_send)
    session.db = FakeDB()
    session.coin = FakeCoin()
    session.session_mgr = FakeSessionManager(FakeDaemon(missing_tx, tx_hex_size, missing_block))
    session.daemon_request = session.session_mgr.daemon_request
    session.bump_cost = lambda cost: None
    return session


@pytest.mark.asyncio
async def test_all_kernels_found():
    proofs = await make_session()._stake_proofs(5, 3, 0)
    assert [p['height'] for p in proofs] == [5, 6, 7]
    assert all('kernel' in p for p in proofs)


@pytest.mark.asyncio
async def test_proofs_stop_at_missing_kernel():
    proofs = await make_session(missing_tx=(1,))._stake_proofs(5, 3, 0)
    assert [p['height'] for p in proofs] == [5]
    assert 'kernel' in proofs[0]


@pytest.mark.asyncio
async def test_proofs_fit_the_response_limit():
    proofs = await make_session(tx_hex_size=1000, max_send=6000)._stake_proofs(5, 3, 0)
    assert [p['height'] for p in proofs] == [5, 6]
    assert len(json.dumps(proofs)) <= 6000 // 2


@pytest.mark.asyncio
async def test_one_proof_even_over_the_limit():
    proofs = await make_session(tx_hex_size=4000, max_send=1000)._stake_proofs(5, 3, 0)
    assert [p['height'] for p in proofs] == [5]


def handler_names(session_class, ptuple):
    session = session_class.__new__(session_class)
    session.set_request_handlers(ptuple)
    return {method: handler.__name__ for method, handler in session.request_handlers.items()}


@pytest.mark.parametrize('client_req', [None, '1.4', '1.4.1', '1.4.2', ['1.4', '1.4.2']])
def test_existing_clients_see_the_same_server(client_req):
    ptuple, _ = util.protocol_version(
        client_req, ParticlElectrumX.PROTOCOL_MIN, ParticlElectrumX.PROTOCOL_MAX)
    upstream_ptuple, _ = util.protocol_version(
        client_req, ElectrumX.PROTOCOL_MIN, ElectrumX.PROTOCOL_MAX)
    assert ptuple == upstream_ptuple
    assert handler_names(ParticlElectrumX, ptuple) == handler_names(ElectrumX, ptuple)


def test_stake_methods_only_from_1_4_3():
    added = set(handler_names(ParticlElectrumX, (1, 4, 3))) - set(
        handler_names(ElectrumX, (1, 4, 2)))
    assert added == {'blockchain.block.stake_proof', 'blockchain.block.stake_proofs'}


def test_particl_serves_the_stake_session():
    assert Particl.SESSIONCLS is ParticlElectrumX


@pytest.mark.asyncio
async def test_raw_blocks_still_raises_by_default():
    daemon = Daemon.__new__(Daemon)

    async def send_vector(method, params_iterable, replace_errs=False):
        assert not replace_errs
        raise DaemonError({'code': -5, 'message': 'Block not found'})

    daemon._send_vector = send_vector
    with pytest.raises(DaemonError):
        await daemon.raw_blocks(['00' * 32])


@pytest.mark.asyncio
async def test_proofs_stop_at_missing_block():
    proofs = await make_session(missing_block=(1,))._stake_proofs(5, 3, 0)
    assert [p['height'] for p in proofs] == [5]
    assert 'kernel' in proofs[0]


@pytest.mark.asyncio
async def test_first_proof_carries_the_stake_modifier():
    session = make_session()
    proofs = await session._stake_proofs(5, 3, 0)
    assert proofs[0]['stake_modifier'] == 'cd' * 32
    assert all('stake_modifier' not in p for p in proofs[1:])
    assert session.session_mgr.daemon.modifier_asked == [hash_to_hex_str(bytes([5]) * 32)]


@pytest.mark.asyncio
async def test_no_modifier_asked_without_a_proof():
    session = make_session(missing_block=(0,))
    assert await session._stake_proofs(5, 3, 0) == []
    assert session.session_mgr.daemon.modifier_asked == []


@pytest.mark.asyncio
async def test_stake_modifier_asks_for_coinstake_details():
    daemon = Daemon.__new__(Daemon)

    async def send_single(method, params=None):
        assert (method, params) == ('getblock', ('ab' * 32, 1, True))
        return {'prevstakemodifier': 'cd' * 32}

    daemon._send_single = send_single
    assert await daemon.stake_modifier('ab' * 32) == 'cd' * 32
