from typing import (
    AsyncIterator,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Set,
    Tuple,
    Type,
    Union,
    cast,
)

from cancel_token import (
    CancelToken,
)
from eth.exceptions import (
    BlockNotFound,
)
from eth_typing import (
    Hash32,
)
from eth_utils import (
    ValidationError,
    encode_hex,
    to_tuple,
)

from lahja import (
    BroadcastConfig,
    EndpointAPI,
)
from eth2.beacon.typing import SigningRoot

import ssz

from p2p.abc import CommandAPI, NodeAPI
from p2p.peer import BasePeer
from p2p.typing import Payload

from eth2.beacon.typing import (
    Slot,
)
from eth2.beacon.attestation_helpers import (
    get_attestation_data_slot,
)
from eth2.beacon.chains.base import (
    BaseBeaconChain,
)
from eth2.beacon.types.attestations import (
    Attestation,
)
from eth2.beacon.types.blocks import (
    BaseBeaconBlock,
    BeaconBlock,
)
from eth2.beacon.state_machines.forks.serenity.block_validation import (
    validate_attestation,
    validate_attestation_slot,
)

from trinity._utils.les import (
    gen_request_id,
)
from trinity._utils.shellart import (
    bold_red,
)
from trinity.exceptions import (
    AttestationNotFound,
)
from trinity.db.beacon.chain import (
    BaseAsyncBeaconChainDB,
)
from trinity.protocol.common.servers import (
    BaseRequestServer,
    BaseIsolatedRequestServer,
)
from trinity.protocol.bcc.commands import (
    Attestations,
    AttestationsMessage,
    BeaconBlocks,
    BeaconBlocksMessage,
    GetBeaconBlocks,
    GetBeaconBlocksMessage,
    NewBeaconBlock,
    NewBeaconBlockMessage,
)
from trinity.protocol.bcc.events import (
    GetBeaconBlocksEvent,
)
from trinity.protocol.bcc.peer import (
    BCCProxyPeer,
    BCCPeer,
    BCCPeerPool,
)


class BCCRequestServer(BaseIsolatedRequestServer):
    subscription_msg_types: FrozenSet[Type[CommandAPI]] = frozenset({
        GetBeaconBlocks,
    })

    def __init__(self,
                 event_bus: EndpointAPI,
                 broadcast_config: BroadcastConfig,
                 db: BaseAsyncBeaconChainDB,
                 token: CancelToken = None) -> None:
        super().__init__(
            event_bus,
            broadcast_config,
            (GetBeaconBlocksEvent,),
            token,
        )
        self.db = db

    async def _handle_msg(self,
                          remote: NodeAPI,
                          cmd: CommandAPI,
                          msg: Payload) -> None:

        self.logger.debug("cmd %s" % cmd)
        if isinstance(cmd, GetBeaconBlocks):
            await self._handle_get_beacon_blocks(remote, cast(GetBeaconBlocksMessage, msg))
        else:
            raise Exception(f"Invariant: Only subscribed to {self.subscription_msg_types}")

    async def _handle_get_beacon_blocks(self, remote: NodeAPI, msg: GetBeaconBlocksMessage) -> None:

        peer = BCCProxyPeer.from_node(remote, self.event_bus, self.broadcast_config)

        request_id = msg["request_id"]
        max_blocks = msg["max_blocks"]
        block_slot_or_root = msg["block_slot_or_root"]

        try:
            if isinstance(block_slot_or_root, int):
                # TODO: pass accurate `block_class: Type[BaseBeaconBlock]` under
                # per BeaconStateMachine fork
                start_block = await self.db.coro_get_canonical_block_by_slot(
                    Slot(block_slot_or_root),
                    BeaconBlock,
                )
            elif isinstance(block_slot_or_root, bytes):
                # TODO: pass accurate `block_class: Type[BaseBeaconBlock]` under
                # per BeaconStateMachine fork
                start_block = await self.db.coro_get_block_by_root(
                    SigningRoot(block_slot_or_root),
                    BeaconBlock,
                )
            else:
                raise TypeError(
                    f"Invariant: unexpected type for 'block_slot_or_root': "
                    f"{type(block_slot_or_root)}"
                )
        except BlockNotFound:
            start_block = None

        if start_block is not None:
            self.logger.debug2(
                "%s requested %d blocks starting with %s",
                peer,
                max_blocks,
                start_block,
            )
            blocks = tuple([b async for b in self._get_blocks(start_block, max_blocks)])

        else:
            self.logger.debug2("%s requested unknown block %s", block_slot_or_root)
            blocks = ()

        self.logger.debug2("Replying to %s with %d blocks", peer, len(blocks))
        peer.sub_proto.send_blocks(blocks, request_id)

    async def _get_blocks(self,
                          start_block: BaseBeaconBlock,
                          max_blocks: int) -> AsyncIterator[BaseBeaconBlock]:
        if max_blocks < 0:
            raise Exception("Invariant: max blocks cannot be negative")

        if max_blocks == 0:
            return

        yield start_block

        try:
            # ensure only a connected chain is returned (breaks might occur if the start block is
            # not part of the canonical chain or if the canonical chain changes during execution)
            start = start_block.slot + 1
            end = start + max_blocks - 1
            parent = start_block
            for slot in range(start, end):
                # TODO: pass accurate `block_class: Type[BaseBeaconBlock]` under
                # per BeaconStateMachine fork
                block = await self.db.coro_get_canonical_block_by_slot(slot, BeaconBlock)
                if block.parent_root == parent.signing_root:
                    yield block
                else:
                    break
                parent = block
        except BlockNotFound:
            return


# FIXME: `BaseReceiveServer` is the same as `BaseRequestServer`.
# Since it's not settled that a `BaseReceiveServer` is needed and so
# in order not to pollute /trinity/protocol/common/servers.py,
# add the `BaseReceiveServer` here instead.
class BaseReceiveServer(BaseRequestServer):
    pass


class AttestationPool:
    """
    Stores the attestations not yet included on chain.
    """
    # TODO: can probably use lru-cache or even database
    _pool: Set[Attestation]

    def __init__(self) -> None:
        self._pool = set()

    def __contains__(self, attestation_or_root: Union[Attestation, Hash32]) -> bool:
        attestation_root: Hash32
        if isinstance(attestation_or_root, Attestation):
            attestation_root = attestation_or_root.hash_tree_root
        elif isinstance(attestation_or_root, bytes):
            attestation_root = attestation_or_root
        else:
            raise TypeError(
                f"`attestation_or_root` should be `Attestation` or `Hash32`,"
                f" got {type(attestation_or_root)}"
            )
        try:
            self.get(attestation_root)
            return True
        except AttestationNotFound:
            return False

    def get(self, attestation_root: Hash32) -> Attestation:
        for attestation in self._pool:
            if attestation.hash_tree_root == attestation_root:
                return attestation
        raise AttestationNotFound(
            f"No attestation with root {encode_hex(attestation_root)} is found.")

    def get_all(self) -> Tuple[Attestation, ...]:
        return tuple(self._pool)

    def add(self, attestation: Attestation) -> None:
        if attestation not in self._pool:
            self._pool.add(attestation)

    def batch_add(self, attestations: Iterable[Attestation]) -> None:
        self._pool = self._pool.union(set(attestations))

    def remove(self, attestation: Attestation) -> None:
        if attestation in self._pool:
            self._pool.remove(attestation)

    def batch_remove(self, attestations: Iterable[Attestation]) -> None:
        self._pool.difference_update(attestations)


class OrphanBlockPool:
    """
    Stores the orphan blocks(the blocks who arrive before their parents).
    """
    # TODO: can probably use lru-cache or even database
    _pool: Set[BaseBeaconBlock]

    def __init__(self) -> None:
        self._pool = set()

    def __contains__(self, block_or_block_root: Union[BaseBeaconBlock, Hash32]) -> bool:
        block_root: Hash32
        if isinstance(block_or_block_root, BaseBeaconBlock):
            block_root = block_or_block_root.signing_root
        elif isinstance(block_or_block_root, bytes):
            block_root = block_or_block_root
        else:
            raise TypeError("`block_or_block_root` should be `BaseBeaconBlock` or `Hash32`")
        try:
            self.get(block_root)
            return True
        except BlockNotFound:
            return False

    def get(self, block_root: Hash32) -> BaseBeaconBlock:
        for block in self._pool:
            if block.signing_root == block_root:
                return block
        raise BlockNotFound(f"No block with signing_root {block_root} is found")

    def add(self, block: BaseBeaconBlock) -> None:
        if block in self._pool:
            return
        self._pool.add(block)

    def pop_children(self, block_root: Hash32) -> Tuple[BaseBeaconBlock, ...]:
        children = tuple(
            orphan_block
            for orphan_block in self._pool
            if orphan_block.parent_root == block_root
        )
        self._pool.difference_update(children)
        return children


class BCCReceiveServer(BaseReceiveServer):
    subscription_msg_types: FrozenSet[Type[CommandAPI]] = frozenset({
        Attestations,
        BeaconBlocks,
        NewBeaconBlock,
    })

    attestation_pool: AttestationPool
    map_request_id_block_root: Dict[int, Hash32]
    orphan_block_pool: OrphanBlockPool

    def __init__(
            self,
            chain: BaseBeaconChain,
            peer_pool: BCCPeerPool,
            token: CancelToken = None) -> None:
        super().__init__(peer_pool, token)
        self.chain = chain
        self.attestation_pool = AttestationPool()
        self.map_request_id_block_root = {}
        self.orphan_block_pool = OrphanBlockPool()

    async def _handle_msg(self, base_peer: BasePeer, cmd: CommandAPI,
                          msg: Payload) -> None:
        peer = cast(BCCPeer, base_peer)
        self.logger.debug("cmd %s" % cmd)
        if isinstance(cmd, Attestations):
            await self._handle_attestations(peer, cast(AttestationsMessage, msg))
        elif isinstance(cmd, NewBeaconBlock):
            await self._handle_new_beacon_block(peer, cast(NewBeaconBlockMessage, msg))
        elif isinstance(cmd, BeaconBlocks):
            await self._handle_beacon_blocks(peer, cast(BeaconBlocksMessage, msg))
        else:
            raise Exception(f"Invariant: Only subscribed to {self.subscription_msg_types}")

    async def _handle_attestations(self, peer: BCCPeer, msg: AttestationsMessage) -> None:
        if not peer.is_operational:
            return
        encoded_attestations = msg["encoded_attestations"]
        attestations = tuple(
            ssz.decode(encoded_attestation, Attestation)
            for encoded_attestation in encoded_attestations
        )
        self.logger.debug("Received attestations=%s", attestations)

        # Validate attestations
        valid_attestations = self._validate_attestations(attestations)
        if len(valid_attestations) == 0:
            return

        # Check if attestations has been seen already.
        # Filter out those seen already.
        valid_new_attestations = tuple(
            filter(
                self._is_attestation_new,
                valid_attestations,
            )
        )
        if len(valid_new_attestations) == 0:
            return
        # Add the valid and new attestations to attestation pool.
        self.attestation_pool.batch_add(valid_new_attestations)
        # Broadcast the valid and new attestations.
        self._broadcast_attestations(valid_new_attestations, peer)

    async def _handle_beacon_blocks(self, peer: BCCPeer, msg: BeaconBlocksMessage) -> None:
        if not peer.is_operational:
            return
        request_id = msg["request_id"]
        if request_id not in self.map_request_id_block_root:
            raise Exception(f"request_id={request_id} is not found")
        encoded_blocks = msg["encoded_blocks"]
        # TODO: remove this condition check in the future, when we start requesting more than one
        #   block at a time.
        if len(encoded_blocks) != 1:
            raise Exception("should only receive 1 block from our requests")
        block = ssz.decode(encoded_blocks[0], BeaconBlock)
        if block.signing_root != self.map_request_id_block_root[request_id]:
            raise Exception(
                f"block signing_root {block.signing_root} does not correpond to"
                "the one we requested"
            )
        self.logger.debug("Received request_id=%s, block=%s", request_id, block)
        self._process_received_block(block)
        del self.map_request_id_block_root[request_id]

    async def _handle_new_beacon_block(self, peer: BCCPeer, msg: NewBeaconBlockMessage) -> None:
        if not peer.is_operational:
            return
        encoded_block = msg["encoded_block"]
        block = ssz.decode(encoded_block, BeaconBlock)
        if self._is_block_seen(block):
            raise Exception(f"block {block} is seen before")
        self.logger.debug("Received new block=%s", block)
        # TODO: check the proposer signature before importing the block
        if self._process_received_block(block):
            self._broadcast_block(block, from_peer=peer)

    def _is_attestation_new(self, attestation: Attestation) -> bool:
        """
        Check if the attestation is already in the database or the attestion pool.
        """
        try:
            if attestation.hash_tree_root in self.attestation_pool:
                return True
            else:
                return not self.chain.attestation_exists(attestation.hash_tree_root)
        except AttestationNotFound:
            return True

    @to_tuple
    def _validate_attestations(self,
                               attestations: Iterable[Attestation]) -> Iterable[Attestation]:
        state_machine = self.chain.get_state_machine()
        config = state_machine.config
        state = self.chain.get_head_state()
        for attestation in attestations:
            # Fast forward to state in future slot in order to pass
            # attestation.data.slot validity check
            future_state = state_machine.state_transition.apply_state_transition(
                state,
                future_slot=attestation.data.slot + config.MIN_ATTESTATION_INCLUSION_DELAY,
            )
            try:
                validate_attestation(
                    future_state,
                    attestation,
                    config,
                )
                yield attestation
            except ValidationError:
                pass

    def _broadcast_attestations(self,
                                attestations: Tuple[Attestation, ...],
                                from_peer: BCCPeer = None) ->None:
        """
        Broadcast the attestations to peers, except for ``from_peer``.
        """
        for peer in self._peer_pool.connected_nodes.values():
            peer = cast(BCCPeer, peer)
            # skip the peer who send the attestations to us
            if from_peer is not None and peer.remote == from_peer.remote:
                continue
            self.logger.debug(bold_red("Send attestations=%s to peer=%s"), attestations, peer)
            peer.sub_proto.send_attestation_records(attestations)

    def _process_received_block(self, block: BaseBeaconBlock) -> bool:
        """
        Process the block received from other peers, and returns whether the block should be
        further broadcast to other peers.
        """
        # If the block is an orphan, put it directly to the pool and request for its parent.
        if not self._is_block_root_in_db(block.parent_root):
            if block not in self.orphan_block_pool:
                self.logger.debug("Found orphan_block=%s", block)
                self.orphan_block_pool.add(block)
                self._request_block_from_peers(block_root=block.parent_root)
            return False
        try:
            self.chain.import_block(block)
        # If the block is invalid, we should drop it.
        except ValidationError:
            # TODO: Possibly drop all of its descendants in `self.orphan_block_pool`?
            return False
        # If the other exceptions occurred, raise it.
        except Exception:
            # Unexpected result
            raise
        else:
            # Successfully imported the block. See if anyone in `self.orphan_block_pool` which
            # depends on it. If there are, try to import them.
            # TODO: should be done asynchronously?
            self._try_import_orphan_blocks(block.signing_root)
            # Remove attestations in block that are also in the attestation pool.
            self.attestation_pool.remove(block.body.attestations)
            return True

    def _try_import_orphan_blocks(self, parent_root: Hash32) -> None:
        """
        Perform ``chain.import`` on the blocks in ``self.orphan_block_pool`` in breadth-first
        order, starting from the children of ``parent_root``.
        """
        imported_roots: List[Hash32] = []

        imported_roots.append(parent_root)
        while len(imported_roots) != 0:
            current_parent_root = SigningRoot(imported_roots.pop())
            # Only process the children if the `current_parent_root` is already in db.
            if not self._is_block_root_in_db(block_root=current_parent_root):
                continue
            # If succeeded, handle the orphan blocks which depend on this block.
            children = self.orphan_block_pool.pop_children(current_parent_root)
            if len(children) > 0:
                self.logger.debug(
                    "Blocks=%s match their parent block, parent_root=%s",
                    children,
                    current_parent_root,
                )
            for block in children:
                try:
                    self.chain.import_block(block)
                    self.logger.debug("Successfully imported block=%s", block)
                    imported_roots.append(block.signing_root)
                except ValidationError as e:
                    # TODO: Possibly drop all of its descendants in `self.orphan_block_pool`?
                    self.logger.debug("Fail to import invalid block=%s  reason=%s", block, e)
                    # Remove attestations in block that are also in the attestation pool.
                    self.attestation_pool.remove(block.body.attestations)

    def _request_block_from_peers(self, block_root: Hash32) -> None:
        for peer in self._peer_pool.connected_nodes.values():
            peer = cast(BCCPeer, peer)
            request_id = gen_request_id()
            self.logger.debug(
                bold_red("Send block request with request_id=%s root=%s to peer=%s"),
                request_id,
                encode_hex(block_root),
                peer,
            )

            self.map_request_id_block_root[request_id] = block_root
            peer.sub_proto.send_get_blocks(
                block_root,
                max_blocks=1,
                request_id=request_id,
            )

    def _broadcast_block(self, block: BaseBeaconBlock, from_peer: BCCPeer = None) -> None:
        """
        Broadcast the block to peers, except for ``from_peer``.
        """
        for peer in self._peer_pool.connected_nodes.values():
            peer = cast(BCCPeer, peer)
            # skip the peer who send the block to us
            if from_peer is not None and peer.remote == from_peer.remote:
                continue
            self.logger.debug(bold_red("Send block=%s to peer=%s"), block, peer)
            peer.sub_proto.send_new_block(block=block)

    def _is_block_root_in_orphan_block_pool(self, block_root: SigningRoot) -> bool:
        return block_root in self.orphan_block_pool

    def _is_block_root_in_db(self, block_root: SigningRoot) -> bool:
        try:
            self.chain.get_block_by_root(block_root=block_root)
            return True
        except BlockNotFound:
            return False

    def _is_block_root_seen(self, block_root: SigningRoot) -> bool:
        if self._is_block_root_in_orphan_block_pool(block_root=block_root):
            return True
        return self._is_block_root_in_db(block_root=block_root)

    def _is_block_seen(self, block: BaseBeaconBlock) -> bool:
        return self._is_block_root_seen(block_root=block.signing_root)

    @to_tuple
    def get_ready_attestations(self) -> Iterable[Attestation]:
        state_machine = self.chain.get_state_machine()
        config = state_machine.config
        state = self.chain.get_head_state()
        for attestation in self.attestation_pool.get_all():
            data = attestation.data
            attestation_slot = get_attestation_data_slot(state, data, config)
            try:
                validate_attestation_slot(
                    attestation_slot,
                    state.slot,
                    config.SLOTS_PER_EPOCH,
                    config.MIN_ATTESTATION_INCLUSION_DELAY,
                )
            except ValidationError:
                # TODO: Should clean up attestations with invalid slot because
                # they are no longer available for inclusion into block.
                continue
            else:
                yield attestation
