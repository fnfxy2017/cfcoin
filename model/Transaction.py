# -*- coding: utf-8 -*-
"""
Parse, stream, create, sign and verify Bitcoin transactions as Tx structures.


The MIT License (MIT)

Copyright (c) 2013 by Richard Kiss

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import datetime
import io
import warnings

from pycoin.cmds.tx import LOCKTIME_THRESHOLD
from pycoin.convention import SATOSHI_PER_COIN, satoshi_to_mbtc
from pycoin.encoding import double_sha256, from_bytes_32
from pycoin.intbytes import int_to_bytes, byte_to_int
from pycoin.networks.registry import address_prefix_for_netcode
from pycoin.serialize import b2h, h2b, b2h_rev, h2b_rev, stream_to_bytes
from pycoin.serialize.bitcoin_streamer import parse_struct, parse_bc_int, \
    parse_bc_string, stream_struct, stream_bc_string
from pycoin.tx.Spendable import Spendable
from pycoin.tx.exceptions import ValidationFailureError, SolvingError, \
    BadSpendableError
from pycoin.tx.pay_to import script_obj_from_script
from pycoin.tx.pay_to.ScriptPayToScript import ScriptPayToScript
from pycoin.tx.script import tools, opcodes
from pycoin.tx.script.check_signature import parse_signature_blob
from pycoin.tx.script.der import UnexpectedDER
from pycoin.tx.script.disassemble import disassemble_scripts, \
    sighash_type_to_string
from pycoin.tx.script.tools import opcode_list

from dao import TransactionDao
from model.TransactionIn import TransactionIn
from model.TransactionOut import TransactionOut
from utils import TransactionUtils


MAX_MONEY = 21000000 * SATOSHI_PER_COIN
MAX_BLOCK_SIZE = 1000000

SIGHASH_ALL = 1
SIGHASH_NONE = 2
SIGHASH_SINGLE = 3
SIGHASH_ANYONECANPAY = 0x80

ZERO32 = b'\0' * 32

def dump_tx(tx, netcode, verbose_signature, disassembly_level, do_trace, use_pdb):
    address_prefix = address_prefix_for_netcode(netcode)
    tx_bin = stream_to_bytes(tx.stream)
#     print("Tx_type:%2d" % tx.tx_type) 
    if TransactionUtils.isCFTransation(tx):
        print("original_hash : %s" % tx.cf_header.original_hash)
        print("target_amount : %s" % tx.cf_header.target_amount)
        print("pubkey : %s" % tx.cf_header.pubkey)
        print("end_time : %s" % tx.cf_header.end_time)
        print("pre_hash : %s" % tx.cf_header.pre_hash)
        print("lack_amount : %s" % tx.cf_header.lack_amount)
    print("Version: %2d  tx hash %s  %d bytes   " % (tx.version, tx.id(), len(tx_bin)))
    print("TransactionIn count: %d; TransactionOut count: %d" % (len(tx.txs_in), len(tx.txs_out)))
    if tx.lock_time == 0:
        meaning = "valid anytime"
    elif tx.lock_time < LOCKTIME_THRESHOLD:
        meaning = "valid after block index %d" % tx.lock_time
    else:
        when = datetime.datetime.utcfromtimestamp(tx.lock_time)
        meaning = "valid on or after %s utc" % when.isoformat()
    print("Lock time: %d (%s)" % (tx.lock_time, meaning))
    print("Input%s:" % ('s' if len(tx.txs_in) != 1 else ''))
    missing_unspents = tx.missing_unspents()

    def trace_script(old_pc, opcode, data, stack, altstack, if_condition_stack, is_signature):
        from pycoin.tx.script.tools import disassemble_for_opcode_data
        print("%3d : %02x  %s" % (old_pc, opcode, disassemble_for_opcode_data(opcode, data)))
        if use_pdb:
            import pdb
            from pycoin.serialize import b2h
            print("stack: [%s]" % ', '.join(b2h(s) for s in stack))
            if len(altstack) > 0:
                print("altstack: %s" % altstack)
            if len(if_condition_stack) > 0:
                print("condition stack: %s" % ', '.join(int(s) for s in if_condition_stack))
            pdb.set_trace()

    traceback_f = trace_script if do_trace or use_pdb else None
    for idx, tx_in in enumerate(tx.txs_in):
        if disassembly_level > 0:
            def signature_for_hash_type_f(hash_type, script):
                return tx.signature_hash(script, idx, hash_type)
        if tx.is_coinbase():
            print("%4d: COINBASE  %12.5f mBTC" % (idx, satoshi_to_mbtc(tx.total_in())))
        else:
            suffix = ""
            if tx.missing_unspent(idx):
                tx_out = None
                address = tx_in.bitcoin_address(address_prefix=address_prefix)
            else:
                tx_out = tx.unspents[idx]
                sig_result = " sig ok" if tx.is_signature_ok(idx, traceback_f=traceback_f) else " BAD SIG"
                suffix = " %12.5f mBTC %s" % (satoshi_to_mbtc(tx_out.coin_value), sig_result)
                address = tx_out.bitcoin_address(netcode=netcode)
            t = "%4d: %34s from %s:%-4d%s" % (idx, address, b2h_rev(tx_in.previous_hash),
                                              tx_in.previous_index, suffix)
            print(t.rstrip())
            if disassembly_level > 0:
                out_script = b''
                if tx_out:
                    out_script = tx_out.script
                for (pre_annotations, pc, opcode, instruction, post_annotations) in \
                        disassemble_scripts(
                            tx_in.script, out_script, tx.lock_time, signature_for_hash_type_f):
                    for l in pre_annotations:
                        print("           %s" % l)
                    print(    "    %4x: %02x  %s" % (pc, opcode, instruction))
                    for l in post_annotations:
                        print("           %s" % l)

            if verbose_signature:
                signatures = []
                for opcode in opcode_list(tx_in.script):
                    if not opcode.startswith("OP_"):
                        try:
                            signatures.append(parse_signature_blob(h2b(opcode)))
                        except UnexpectedDER:
                            pass
                if signatures:
                    sig_types_identical = (zip(*signatures)[1]).count(signatures[0][1]) == len(signatures)
                    i = 1 if len(signatures) > 1 else ''
                    for sig_pair, sig_type in signatures:
                        print("      r{0}: {1:#x}\n      s{0}: {2:#x}".format(i, *sig_pair))
                        if not sig_types_identical and tx_out:
                            print("      z{}: {:#x} {}".format(i, tx.signature_hash(tx_out.script, idx, sig_type),
                                                               sighash_type_to_string(sig_type)))
                        if i:
                            i += 1
                    if sig_types_identical and tx_out:
                        print("      z:{} {:#x} {}".format(' ' if i else '', tx.signature_hash(tx_out.script, idx, sig_type),
                                                           sighash_type_to_string(sig_type)))

    print("Output%s:" % ('s' if len(tx.txs_out) != 1 else ''))
    for idx, tx_out in enumerate(tx.txs_out):
        amount_mbtc = satoshi_to_mbtc(tx_out.coin_value)
        address = tx_out.bitcoin_address(netcode=netcode) or "(unknown)"
        print("%4d: %34s receives %12.5f mBTC" % (idx, address, amount_mbtc))
        if disassembly_level > 0:
            for (pre_annotations, pc, opcode, instruction, post_annotations) in \
                    disassemble_scripts(b'', tx_out.script, tx.lock_time, signature_for_hash_type_f):
                for l in pre_annotations:
                    print("           %s" % l)
                print(    "    %4x: %02x  %s" % (pc, opcode, instruction))
                for l in post_annotations:
                    print("           %s" % l)

    if not missing_unspents:
        print("Total input  %12.5f mBTC" % satoshi_to_mbtc(tx.total_in()))
    print(    "Total output %12.5f mBTC" % satoshi_to_mbtc(tx.total_out()))
    if not missing_unspents:
        print("Total fees   %12.5f mBTC" % satoshi_to_mbtc(tx.fee()))


class Transaction(object):
    TransactionIn = TransactionIn
    TransactionOut = TransactionOut
    Spendable = Spendable   

    MAX_MONEY = MAX_MONEY
    MAX_TX_SIZE = MAX_BLOCK_SIZE

    SIGHASH_ALL = SIGHASH_ALL
    SIGHASH_NONE = SIGHASH_NONE
    SIGHASH_SINGLE = SIGHASH_SINGLE
    SIGHASH_ANYONECANPAY = SIGHASH_ANYONECANPAY

    ALLOW_SEGWIT = True

    def __init__(self, version, txs_in, txs_out, lock_time=0, unspents=None, state = 0, uid = 0):
        """
        The part of a Tx that specifies where the Bitcoin comes from.
        """
        self.uid = uid
        self.version = version
        self.txs_in = txs_in
        self.txs_out = txs_out
        self.lock_time = lock_time
        self.state = state
        if None == unspents:
            unspents = TransactionDao.unspents_from_db(txs_in)
        self.unspents = unspents or []
        for tx_in in self.txs_in:
            assert type(tx_in) == self.TransactionIn
        for tx_out in self.txs_out:
            assert type(tx_out) == self.TransactionOut
#         self.tx_type = 0x01
    @classmethod
    def coinbase_tx(cls, public_key_sec, coin_value, coinbase_bytes=b'', version=1, lock_time=0, state=0):
        """
        Create the special "first in block" transaction that includes the mining fees.
        """
        tx_in = cls.TransactionIn.coinbase_tx_in(script=coinbase_bytes)
        COINBASE_SCRIPT_OUT = "%s OP_CHECKSIG"
        script_text = COINBASE_SCRIPT_OUT % b2h(public_key_sec)
        script_bin = tools.compile(script_text)
        tx_out = cls.TransactionOut(coin_value, script_bin)
        return cls(version, [tx_in], [tx_out], lock_time, state)

    @classmethod
    def parse(class_, f, allow_segwit=None):
        """Parse a Bitcoin transaction Tx from the file-like object f."""
        if allow_segwit is None:
            allow_segwit = class_.ALLOW_SEGWIT
        txs_in = []
        txs_out = []
        version, = parse_struct("L", f)
        v1 = ord(f.read(1))
        is_segwit = allow_segwit and (v1 == 0)
        v2 = None
        if is_segwit:
            flag = f.read(1)
            if flag == b'\0':
                raise ValueError("bad flag in segwit")
            if flag == b'\1':
                v1 = None
            else:
                is_segwit = False
                v2 = ord(flag)
        count = parse_bc_int(f, v=v1)
        txs_in = []
        for i in range(count):
            txs_in.append(class_.TransactionIn.parse(f))
        count = parse_bc_int(f, v=v2)
        txs_out = []
        for i in range(count):
            txs_out.append(class_.TransactionOut.parse(f))

        if is_segwit:
            for tx_in in txs_in:
                stack = []
                count = parse_bc_int(f)
                for i in range(count):
                    stack.append(parse_bc_string(f))
                tx_in.witness = stack
        lock_time, = parse_struct("L", f)
        return class_(version, txs_in, txs_out, lock_time)

    def getBlockHash(self):
        if hasattr(self, 'block'):
            return self.block.hash()
        else:
            return ''

    def stream(self, f, blank_solutions=False, include_unspents=False, include_witness_data=True):
        """Stream a Bitcoin transaction Tx to the file-like object f."""
        include_witnesses = include_witness_data and self.has_witness_data()
        ####
        if TransactionUtils.isCFTransation(self):
            stream_struct("L", f, 2)
            stream_struct("#", f, self.cf_header.original_hash)
            stream_struct("Q", f, self.cf_header.target_amount)
            stream_struct("S", f, self.cf_header.pubkey.encode(encoding="utf-8"))
            stream_struct("L", f, self.cf_header.end_time)
            stream_struct("#", f, self.cf_header.pre_hash)
            stream_struct("Q", f, self.cf_header.lack_amount)
        else:
            stream_struct("L", f, 1)
        ####
        stream_struct("L", f, self.version)
        if include_witnesses:
            f.write(b'\0\1')
        stream_struct("I", f, len(self.txs_in))
        for t in self.txs_in:
            t.stream(f, blank_solutions=blank_solutions)
        stream_struct("I", f, len(self.txs_out))
        for t in self.txs_out:
            t.stream(f)
        if include_witnesses:
            for tx_in in self.txs_in:
                witness = tx_in.witness
                stream_struct("I", f, len(witness))
                for w in witness:
                    stream_bc_string(f, w)
        stream_struct("L", f, self.lock_time)
        if include_unspents and not self.missing_unspents():
            self.stream_unspents(f)


    def as_bin(self, include_unspents=False, include_witness_data=True):
        """Return the transaction as binary."""
        f = io.BytesIO()
        self.stream(f, include_unspents=include_unspents, include_witness_data=include_witness_data)
        return f.getvalue()

    def as_hex(self, include_unspents=False, include_witness_data=True):
        """Return the transaction as hex."""
        return b2h(self.as_bin(
            include_unspents=include_unspents, include_witness_data=include_witness_data))

    def set_witness(self, tx_idx_in, witness):
        self.txs_in[tx_idx_in].witness = tuple(witness)

    def has_witness_data(self):
        return any(len(tx_in.witness) > 0 for tx_in in self.txs_in)

    def hash(self, hash_type=None):
        """Return the hash for this Tx object."""
        s = io.BytesIO()
        self.stream(s)
        if hash_type is not None:
            stream_struct("L", s, hash_type)
        return double_sha256(s.getvalue())

    def w_hash(self):
        return double_sha256(self.as_bin())

    def w_id(self):
        return b2h_rev(self.w_hash())

    def blanked_hash(self):
        """
        Return the hash for this Tx object with solution scripts blanked.
        Useful for determining if two Txs might be equivalent modulo
        malleability. (That is, even if tx1 is morphed into tx2 using the malleability
        weakness, they will still have the same blanked hash.)
        """
        s = io.BytesIO()
        self.stream(s, blank_solutions=True)
        return double_sha256(s.getvalue())

    def id(self):
        """Return the human-readable hash for this Tx object."""
        return b2h_rev(self.hash())

    def _tx_in_for_idx(self, idx, tx_in, tx_out_script, unsigned_txs_out_idx):
        if idx == unsigned_txs_out_idx:
            return self.TransactionIn(tx_in.previous_hash, tx_in.previous_index, tx_out_script, tx_in.sequence)
        return self.TransactionIn(tx_in.previous_hash, tx_in.previous_index, b'', tx_in.sequence)

    def signature_hash(self, tx_out_script, unsigned_txs_out_idx, hash_type):
        """
        Return the canonical hash for a transaction. We need to
        remove references to the signature, since it's a signature
        of the hash before the signature is applied.

        tx_out_script: the script the coins for unsigned_txs_out_idx are coming from
        unsigned_txs_out_idx: where to put the tx_out_script
        hash_type: one of SIGHASH_NONE, SIGHASH_SINGLE, SIGHASH_ALL,
        optionally bitwise or'ed with SIGHASH_ANYONECANPAY
        """

        # In case concatenating two scripts ends up with two codeseparators,
        # or an extra one at the end, this prevents all those possible incompatibilities.
        tx_out_script = tools.delete_subscript(tx_out_script, int_to_bytes(opcodes.OP_HASH160))

        # blank out other inputs' signatures
        txs_in = [self._tx_in_for_idx(i, tx_in, tx_out_script, unsigned_txs_out_idx)
                  for i, tx_in in enumerate(self.txs_in)]
        txs_out = self.txs_out

        # Blank out some of the outputs
        if (hash_type & 0x1f) == self.SIGHASH_NONE:
            # Wildcard payee
            txs_out = []

            # Let the others update at will
            for i in range(len(txs_in)):
                if i != unsigned_txs_out_idx:
                    txs_in[i].sequence = 0

        elif (hash_type & 0x1f) == self.SIGHASH_SINGLE:
            # This preserves the ability to validate existing legacy
            # transactions which followed a buggy path in Satoshi's
            # original code; note that higher level functions for signing
            # new transactions (e.g., is_signature_ok and sign_tx_in)
            # check to make sure we never get here (or at least they
            # should)
            if unsigned_txs_out_idx >= len(txs_out):
                # This should probably be moved to a constant, but the
                # likelihood of ever getting here is already really small
                # and getting smaller
                return (1 << 248)

            # Only lock in the TransactionOut payee at same index as TransactionIn; delete
            # any outputs after this one and set all outputs before this
            # one to "null" (where "null" means an empty script and a
            # value of -1)
            txs_out = [self.TransactionOut(0xffffffffffffffff, b'')] * unsigned_txs_out_idx
            txs_out.append(self.txs_out[unsigned_txs_out_idx])

            # Let the others update at will
            for i in range(len(self.txs_in)):
                if i != unsigned_txs_out_idx:
                    txs_in[i].sequence = 0

        # Blank out other inputs completely, not recommended for open transactions
        if hash_type & self.SIGHASH_ANYONECANPAY:
            txs_in = [txs_in[unsigned_txs_out_idx]]

        tmp_tx = self.__class__(self.version, txs_in, txs_out, self.lock_time)
        return from_bytes_32(tmp_tx.hash(hash_type=hash_type))

    def hash_prevouts(self, hash_type):
        if hash_type & SIGHASH_ANYONECANPAY:
            return ZERO32
        f = io.BytesIO()
        for tx_in in self.txs_in:
            f.write(tx_in.previous_hash)
            stream_struct("L", f, tx_in.previous_index)
        return double_sha256(f.getvalue())

    def hash_sequence(self, hash_type):
        if (
                (hash_type & SIGHASH_ANYONECANPAY) or
                ((hash_type & 0x1f) == SIGHASH_SINGLE) or
                ((hash_type & 0x1f) == SIGHASH_NONE)
        ):
            return ZERO32

        f = io.BytesIO()
        for tx_in in self.txs_in:
            stream_struct("L", f, tx_in.sequence)
        return double_sha256(f.getvalue())

    def hash_outputs(self, hash_type, tx_in_idx):
        txs_out = self.txs_out
        if hash_type & 0x1f == SIGHASH_SINGLE:
            if tx_in_idx >= len(txs_out):
                return ZERO32
            txs_out = txs_out[tx_in_idx:tx_in_idx + 1]
        elif hash_type & 0x1f == SIGHASH_NONE:
            return ZERO32
        f = io.BytesIO()
        for tx_out in txs_out:
            stream_struct("Q", f, tx_out.coin_value)
            tools.write_push_data([tx_out.script], f)
        return double_sha256(f.getvalue())

    def segwit_signature_preimage(self, script, tx_in_idx, hash_type):
        f = io.BytesIO()
        stream_struct("L", f, self.version)
        # calculate hash prevouts
        f.write(self.hash_prevouts(hash_type))
        f.write(self.hash_sequence(hash_type))
        tx_in = self.txs_in[tx_in_idx]
        f.write(tx_in.previous_hash)
        stream_struct("L", f, tx_in.previous_index)
        tx_out = self.unspents[tx_in_idx]
        stream_bc_string(f, script)
        stream_struct("Q", f, tx_out.coin_value)
        stream_struct("L", f, tx_in.sequence)
        f.write(self.hash_outputs(hash_type, tx_in_idx))
        stream_struct("L", f, self.lock_time)
        stream_struct("L", f, hash_type)
        return f.getvalue()

    def signature_for_hash_type_segwit(self, script, tx_in_idx, hash_type):
        return from_bytes_32(double_sha256(self.segwit_signature_preimage(script, tx_in_idx, hash_type)))

    def solve(self, hash160_lookup, tx_in_idx, tx_out_script, hash_type=None, **kwargs):
        """
        Sign a standard transaction.
        hash160_lookup:
            An object with a get method that accepts a hash160 and returns the
            corresponding (secret exponent, public_pair, is_compressed) tuple or
            None if it's unknown (in which case the script will obviously not be signed).
            A standard dictionary will do nicely here.
        tx_in_idx:
            the index of the tx_in we are currently signing
        tx_out:
            the tx_out referenced by the given tx_in
        """
        if hash_type is None:
            hash_type = self.SIGHASH_ALL
        tx_in = self.txs_in[tx_in_idx]

        is_p2h = (len(tx_out_script) == 23 and byte_to_int(tx_out_script[0]) == opcodes.OP_HASH160 and
                  byte_to_int(tx_out_script[-1]) == opcodes.OP_EQUAL)
        if is_p2h:
            hash160 = ScriptPayToScript.from_script(tx_out_script).hash160
            p2sh_lookup = kwargs.get("p2sh_lookup")
            if p2sh_lookup is None:
                raise ValueError("p2sh_lookup not set")
            if hash160 not in p2sh_lookup:
                raise ValueError("hash160=%s not found in p2sh_lookup" % 
                                 b2h(hash160))

            script_to_hash = p2sh_lookup[hash160]
        else:
            script_to_hash = tx_out_script

        # Leave out the signature from the hash, since a signature can't sign itself.
        # The checksig op will also drop the signatures from its hash.
        def signature_for_hash_type_f(hash_type, script):
            return self.signature_hash(script, tx_in_idx, hash_type)

        def witness_signature_for_hash_type(hash_type, script):
            return self.signature_for_hash_type_segwit(script, tx_in_idx, hash_type)
        witness_signature_for_hash_type.skip_delete = True

        signature_for_hash_type_f.witness = witness_signature_for_hash_type

        if tx_in.verify(
                tx_out_script, signature_for_hash_type_f, lock_time=self.lock_time,
                tx_version=self.version):
            return

        the_script = script_obj_from_script(tx_out_script)
        solution = the_script.solve(
            hash160_lookup=hash160_lookup, signature_type=hash_type,
            existing_script=self.txs_in[tx_in_idx].script, existing_witness=tx_in.witness,
            script_to_hash=script_to_hash, signature_for_hash_type_f=signature_for_hash_type_f, **kwargs)
        return solution

    def sign_tx_in(self, hash160_lookup, tx_in_idx, tx_out_script, hash_type=None, **kwargs):
        if hash_type is None:
            hash_type = self.SIGHASH_ALL
        r = self.solve(hash160_lookup, tx_in_idx, tx_out_script,
                       hash_type=hash_type, **kwargs)
        if isinstance(r, bytes):
            self.txs_in[tx_in_idx].script = r
        else:
            self.txs_in[tx_in_idx].script = r[0]
            self.set_witness(tx_in_idx, r[1])

    def verify_tx_in(self, tx_in_idx, tx_out_script, expected_hash_type=None):
        tx_in = self.txs_in[tx_in_idx]

        def signature_for_hash_type_f(hash_type, script):
            return self.signature_hash(script, tx_in_idx, hash_type)

        if not tx_in.verify(
                tx_out_script, signature_for_hash_type_f, expected_hash_type, tx_version=self.version):
            raise ValidationFailureError(
                "just signed script Tx %s TransactionIn index %d did not verify" % (
                    b2h_rev(tx_in.previous_hash), tx_in_idx))

    def total_out(self):
        total = sum(tx_out.coin_value for tx_out in self.txs_out)
        if TransactionUtils.isCFTransation(self):
            if self.cf_header.lack_amount <= 0:
                pre_tx = TransactionDao.searchByHash(self.cf_header.pre_hash)
                total = total - self.txs_out[0].coin_value + pre_tx.cf_header.lack_amount - self.cf_header.lack_amount
        return total

    def tx_outs_as_spendable(self, block_index_available=0):
        h = self.hash()
        return [
            self.Spendable.from_tx_out(tx_out, h, tx_out_index, block_index_available)
            for tx_out_index, tx_out in enumerate(self.txs_out)]

    def is_coinbase(self):
        return len(self.txs_in) == 1 and self.txs_in[0].is_coinbase()

    def __str__(self):
        return "Tx [%s]" % self.id()

    def __repr__(self):
        return "Tx [%s] (v:%d) [%s] [%s]" % (
            self.id(), self.version, ", ".join(str(t) for t in self.txs_in),
            ", ".join(str(t) for t in self.txs_out))

    def _check_tx_inout_count(self):
        if not self.txs_out:
            raise ValidationFailureError("txs_out = []")
        if not self.is_coinbase() and not self.txs_in:
            raise ValidationFailureError("txs_in = []")

    def _check_size_limit(self):
        size = len(self.as_bin())
        if size > self.MAX_TX_SIZE:
            raise ValidationFailureError("size > MAX_TX_SIZE")

    def _check_txs_out(self):
        # Check for negative or overflow output values
        nValueOut = 0
        for tx_out in self.txs_out:
            if tx_out.coin_value < 0 or tx_out.coin_value > self.MAX_MONEY:
                raise ValidationFailureError("tx_out value negative or out of range")
            nValueOut += tx_out.coin_value
            if nValueOut > self.MAX_MONEY:
                raise ValidationFailureError("tx_out lack_amount out of range")

    def _check_txs_in(self):
        # Check for duplicate inputs
        if [x for x in self.txs_in if self.txs_in.count(x) > 1]:
            raise ValidationFailureError("duplicate inputs")
        if (self.is_coinbase()):
            if not (2 <= len(self.txs_in[0].script) <= 100):
                raise ValidationFailureError("bad coinbase script size")
        else:
            refs = set()
            for tx_in in self.txs_in:
                if tx_in.previous_hash == ZERO32:
                    raise ValidationFailureError("prevout is null")
                pair = (tx_in.previous_hash, tx_in.previous_index)
                if pair in refs:
                    raise ValidationFailureError("spendable reused")
                refs.add(pair)

    def check(self):
        """
        Basic checks that don't depend on any context.
        Adapted from Bicoin Code: main.cpp
        """
        self._check_tx_inout_count()
        self._check_txs_out()
        self._check_txs_in()
        # Size limits
        self._check_size_limit()

    """
    The functions below here deal with an optional additional parameter: "unspents".
    This parameter is a list of tx_out objects that are referenced by the
    list of self.tx_in objects.
    """

    def unspents_from_db(self, tx_db, ignore_missing=False):
        unspents = []
        for tx_in in self.txs_in:
            if tx_in.is_coinbase():
                unspents.append(None)
                continue
            tx = tx_db.get(tx_in.previous_hash)
            if tx and tx.hash() == tx_in.previous_hash:
                unspents.append(tx.txs_out[tx_in.previous_index])
            elif ignore_missing:
                unspents.append(None)
            else:
                raise KeyError(
                    "can't find tx_out for %s:%d" % (b2h_rev(tx_in.previous_hash), tx_in.previous_index))
        self.unspents = unspents

    def set_unspents(self, unspents):
        if len(unspents) != len(self.txs_in):
            raise ValueError("wrong number of unspents")
        self.unspents = unspents

    def missing_unspent(self, idx):
        if self.is_coinbase():
            return True
        if len(self.unspents) <= idx:
            return True
        return self.unspents[idx] is None

    def missing_unspents(self):
        if self.is_coinbase():
            return False
        return (len(self.unspents) != len(self.txs_in) or
                any(self.missing_unspent(idx) for idx, tx_in in enumerate(self.txs_in)))

    def check_unspents(self):
        if self.missing_unspents():
            raise ValueError("wrong number of unspents. Call unspents_from_db or set_unspents.")

    def stream_unspents(self, f):
        self.check_unspents()
        for tx_out in self.unspents:
            if tx_out is None:
                tx_out = self.TransactionOut(0, b'')
            tx_out.stream(f)

    def parse_unspents(self, f):
        unspents = []
        for i in enumerate(self.txs_in):
            tx_out = self.TransactionOut.parse(f)
            if tx_out.coin_value == 0:
                tx_out = None
            unspents.append(tx_out)
        self.set_unspents(unspents)

    def is_signature_ok(self, tx_in_idx, flags=None, traceback_f=None):
        tx_in = self.txs_in[tx_in_idx]
        if tx_in.is_coinbase():
            return True
        if len(self.unspents) <= tx_in_idx:
            return False
        unspent = self.unspents[tx_in_idx]
        if unspent is None:
            return False
        tx_out_script = self.unspents[tx_in_idx].script

        def signature_for_hash_type_f(hash_type, script):
            return self.signature_hash(script, tx_in_idx, hash_type)

        def witness_signature_for_hash_type(hash_type, script):
            return self.signature_for_hash_type_segwit(script, tx_in_idx, hash_type)
        witness_signature_for_hash_type.skip_delete = True

        signature_for_hash_type_f.witness = witness_signature_for_hash_type

        return tx_in.verify(
            tx_out_script, signature_for_hash_type_f, lock_time=self.lock_time,
            flags=flags, traceback_f=traceback_f, tx_version=self.version)

    def sign(self, hash160_lookup, hash_type=None, **kwargs):
        """
        Sign a standard transaction.
        hash160_lookup:
            A dictionary (or another object with .get) where keys are hash160 and
            values are tuples (secret exponent, public_pair, is_compressed) or None
            (in which case the script will obviously not be signed).
        """
        if hash_type is None:
            hash_type = self.SIGHASH_ALL
        self.check_unspents()
        for idx, tx_in in enumerate(self.txs_in):
            if self.is_signature_ok(idx) or tx_in.is_coinbase():
                continue
            try:
                if self.unspents[idx]:
                    self.sign_tx_in(
                        hash160_lookup, idx, self.unspents[idx].script, hash_type=hash_type, **kwargs)
            except SolvingError:
                pass

        return self

    def bad_signature_count(self, flags=None):
        count = 0
        for idx, tx_in in enumerate(self.txs_in):
            if not self.is_signature_ok(idx, flags=flags):
                count += 1
        return count

    def total_in(self):
        if self.is_coinbase():
            return self.txs_out[0].coin_value
        self.check_unspents()
        return sum(tx_out.coin_value for tx_out in self.unspents)

    def fee(self):
        return self.total_in() - self.total_out()
        
    def validate_unspents(self, tx_db):
        """
        Spendable objects returned from blockchain.info or
        similar services contain coin_value information that must be trusted
        on faith. Mistaken coin_value data can result in coins being wasted
        to fees.

        This function solves this problem by iterating over the incoming
        transactions, fetching them from the tx_db in full, and verifying
        that the coin_values are as expected.

        Returns the fee for this transaction. If any of the spendables set by
        tx.set_unspents do not match the authenticated transactions, a
        ValidationFailureError is raised.
        """
        tx_hashes = set((tx_in.previous_hash for tx_in in self.txs_in))

        # build a local copy of the DB
        tx_lookup = {}
        for h in tx_hashes:
            if h == ZERO32:
                continue
            the_tx = tx_db.get(h)
            if the_tx is None:
                raise KeyError("hash id %s not in tx_db" % b2h_rev(h))
            if the_tx.hash() != h:
                raise KeyError("attempt to load Tx %s yielded a Tx with id %s" % (h2b_rev(h), the_tx.id()))
            tx_lookup[h] = the_tx

        for idx, tx_in in enumerate(self.txs_in):
            if tx_in.previous_hash == ZERO32:
                continue
            txs_out = tx_lookup[tx_in.previous_hash].txs_out
            if tx_in.previous_index > len(txs_out):
                raise BadSpendableError("tx_out index %d is too big for Tx %s" % 
                                        (tx_in.previous_index, b2h_rev(tx_in.previous_hash)))
            tx_out1 = txs_out[tx_in.previous_index]
            tx_out2 = self.unspents[idx]
            if tx_out1.coin_value != tx_out2.coin_value:
                raise BadSpendableError(
                    "unspents[%d] coin value mismatch (%d vs %d)" % (
                        idx, tx_out1.coin_value, tx_out2.coin_value))
            if tx_out1.script != tx_out2.script:
                raise BadSpendableError("unspents[%d] script mismatch!" % idx)

        return self.fee()

