# PHASE6_EXECUTION_TRACES.md — 英文补充：D1-D100 执行级追踪（与 PHASE6_DEEP_DIVE.md 对应）

D01: Traced validate_tx in nova_node.py:96-127. The 0x0000 gate at :96-99 requires allow_system=True, which is only passed by the internal _system_mint path; a network tx claiming sender 0x0000 returns False before signature checks. Amount checks: isfinite, range, canonical. JSON data parse wrapped in try/except at :294-299. Call context: every /api/tx request lands here; no direct store mutation exists.
D02: State table for apply_tx (nova_node.py:851-856): sender=10, receiver=0, amount=4, FIXED_GAS=0.1 -> sender=5.9, receiver=4, validator_pool decreases by reward when funded. Transfer executes debit before credit, so sender goes non-negative by construction. Call reward for contract receivers is separate (:862-868) and capped by ECOSYSTEM_FUND and once-per-day per key.
D03: canonical_amount (core/transaction.py:6-13) converts float to round(amount,8) string for signing_data. Deterministic across nodes for same value; float representation is the known TM-012 root cause (cross-platform risk), no new path found. Boundary: negative zero normalizes to 0.
D04: Block hash binds header and tx list (core/blockchain.py). Reordering txs changes hash; two blocks with identical txs but different order differ. No user-controlled field reaches hash without validation.
D05: Signature binding in core/crypto.py:186-189: expected address = sha3_512(pubkey)[:40]. Attempted pubkey/address mismatch rejected in all 10 trials. Dilithium branch requires oqs which is absent in this environment; Ed25519 fallback used for all runtime verification.
D06: elect_proposer (core/consensus.py:62-73) is deterministic given height, prev_hash, epoch_stakes. Ran 100 heights, no non-staked proposer elected. Invariant: proposer must be staked.
D07: _detect_equivocation (core/consensus.py:183-193) compares block signatures at the same height; second block from same proposer is rejected and slashed. Execution: two blocks same height, both validly signed by proposer -> detection fires.
D08: block_reward distribution (core/economy.py:66-80) is capped by validator pool balance; pool never goes negative. 1000-block simulation confirms.
D09: early_airdrop (core/economy.py:97-114) dedupes via early_airdrop_received and requires ECOSYSTEM_FUND >= 100; with genesis fund 0 the branch is skipped. No double-claim path.
D10: vm.run (core/vm.py:57-104) loops with max_steps=100000; infinite-loop bytecode terminates. _operand pops with default when stack empty; DIV/0 returns 0 (ZeroDivisionError caught). Operands bounded by 100KB bytecode limit.
D11: nexlang_compiler.compile rejects empty source, unknown opcodes, and malformed tokens with exceptions caught by the caller; no compiled artifact bypasses vm.run validation.
D12: StateStore snapshot/restore round-trip deep-equal for the full state dict including storage_claims, oracle_feeds, bridge_assets. Deterministic replay invariant holds.
D13: pin (core/storage_network.py:71-84) deducts size*days*0.001 from ECOSYSTEM_FUND and creates a reward pool; no file existence or creator-provenance check. One pin at (1024,3650) locks 3737.6 NOVA. This is F-02 surface.
D14: proof (core/storage_network.py:107-127) internally performs no hash-chain verification; nova_node.py:341-345 is the only guard (sha3_256(reveal)==seal.tip). Direct calls bypass the guard, so defense-in-depth is absent. F-02.
D15: storage_incentive.verify_proof (core/storage_incentive.py:264-312) binds fragments to claimed files via fragment_commit; length mismatch rejected at nova_node.py:543-544.
D16: compute state machine (core/compute.py:309-315, 416-471): publish->assigned->submitted->completed; double submit and publisher-submit rejected by status and sender checks.
D17: ai fund approve (core/ai_service.py:304-317) plus daily budget gate (nova_node.py:107-110) restricts outflow; approve requires proper funding state.
D18: socialfi fan-token mint/burn conserves supply == sum(balances); buy/sell paths verified with an accounting table.
D19: arbitration escrow (core/arbitration.py:671-737): deposit held in case, complaint/draw transitions guarded by status; double-draw rejected.
D20: chat mailbox cap (core/chat.py:96-101) trims to 1000 per address; ack requires signature (nova_node.py:1440-1462). Read-without-auth is TM-008.
D21: Combined transfer+gas+pool+call-reward accounting in one state table (nova_node.py:851-868): 500 random transfers, every address balance stays >= 0.
D22: signing_data domain (transaction.py:52-53) = sender+receiver+amount+timestamp+parents+data+pk; op lives in data, so signatures do not cross function domains.
D23: Block signature domain = block hash including prev_hash and height; signatures do not transfer between blocks.
D24: Slash flows: consensus, storage, oracle slash paths all credit a fixed accounting target and set stake=0; no stake laundering after slash (status != active).
D25: p2p framing uses readuntil(newline) with 64MB cap; 20MB message round-trips intact; snapshot sync disabled by default (P0-2 fix). TLS client verification off is TM-013.

D26: Fill-block timeout uses proposer-reported timestamps; traced the current path and confirmed it is the same root cause as TM-003 (already documented), no new independent path exists, so it is filed as known_or_duplicate.
D27: Enumerated all ECOSYSTEM_FUND references: income (slashes, governance injection) and outflow (airdrop, early rewards, pin, call rewards, AI). Every outflow has a balance guard except pin, which only checks sufficiency, not validity. F-02 stands.
D28: VM SEND opcode (core/vm.py:86-91) only appends events; 1000 SEND executions leave balances unchanged. No arbitrary transfer primitive exists in the VM.
D29: storage_claims/storage_seals persist through snapshot/restore; proof cannot be replayed after restore because last_proof_day and revealed counters round-trip.
D30: End-to-end fund attack chain: fund=10000 -> pin x2 locks 7475.2 -> attacker extracts 7.5 via fake proofs. State table produced from poc_f02_storage_fund_drain.py output. F-02 confirmed with numbers.
D31: Order payout (storage_network.py:131-137) checks expires_at then paid list; refund (:161-176) sets status=expired first, so payout and refund are mutually exclusive.
D32: register (nova_node.py:500-505) writes both storage_providers and incentive nodes; key sets stay consistent in simulation.
D33: scan_offline (core/storage_incentive.py:321-343) removes daily reward for offline nodes; slash on repeated absence. No offline reward accrual.
D34: upgrade_quota (core/storage_incentive.py:506-518) maps paid amount to quota; amount=0 rejected by RPC validation.
D35: compute bounty escrow: publish deducts, _complete splits among workers, expiry refunds remainder; totals conserved in simulation.
D36: SocialFi content and storage pin intersect only through the creator; an attacker pinning arbitrary CIDs is F-02, not a new path.
D37: arbitration reputation farming requires many transactions; at 100 req/s/IP limit the cost exceeds the arbitration reward, so it is not economical.
D38: oracle->bridge: price() returns dict; bridge _usd_value float*dict raises TypeError (F-05); if fixed, manipulated price 0.0001 would make 50000 nUSDT worth 5 USD (F-04). Both feed into F-03.
D39: All price() call sites enumerated: bridge only. Manipulated aggregate therefore affects only bridge USD metering.
D40: Wrapped nUSDT flows from bridge into DEX pools via _transfer_wrapped (dex.py:207-220); reserve holder is 0x_dex:{pair}, not user-addressable. F-01-minted assets can enter DEX, amplifying impact.
D41: pin_reward maximum = 1024*3650*0.001 = 3737.6 NOVA per pin; attacker cost 0; benefit/cost ratio unbounded. Full parameter space scanned.
D42: Extraction rate = 0.05 NOVA/day/proof; a 3737.6 pool takes 74,752 days to drain; lockup dominates extraction. Severity High, not Critical.
D43: min_fee = 1/USD_price; at manipulated price 0.0001, min fee = 10000 nUSDT per tx, which would confiscate most deposits once F-05 is fixed.
D44: Daily limit 1,000,000 USD; at true price it bounds minting to 1M nUSDT, at manipulated price 0.0001 it allows 10^10 nUSDT. Metering broken via F-03.
D45: quorum = 2.5% of circulating (81,001,000) = 2,025,025 votes; single 1000-NOVA account needs amplification factor 2025 to control quorum alone.
D46: voting_power recursion: 1000 NOVA at A delegated to B then C gives power 1000 each, sum 3000. Amplification factor N+1 for chain length N. F-06.
D47: DEX swap with 10x depth: output follows x*y=k, slippage capped, output never negative; reserve holder isolation prevents direct drain.
D48: LP farm rewards allocated by share; single 100% LP receives full pool; accounting conserved.
D49: AI daily budget (nova_node.py:107-110) gates spend; 30-day simulation stays within budget; budget exhaustion blocks new spends.
D50: compute bounty split among workers: 2-worker minimum, payout + refund conserved.
D51: Arbitration reward pools: pay_arb_reward debits pool, conservation verified over 100 cases.
D52: Subscription fee cashflow: creator credited, subscriber debited, renew with insufficient balance rejected.
D53: SocialFi bond buy/sell price curve is monotonic; zero-supply boundary handled.
D54: Incentive daily reward totals bounded by design budget; all-online case simulated without negative balances.
D55: Validator pool distribution by stake share; pool never negative over 100 validators.
D56: Mailbox spam: 1000 msgs x gas cost is cheap but cap prevents unbounded growth; unauthorized read is TM-008 known.
D57: Median manipulation: 3 attacker sources at 0.0001 + 1 honest at 1.0 -> median 0.50005, deviation filter keeps the 3 clustered values -> aggregate 0.0001. F-03.
D58: Deviation thresholds (10% reject, 5% exclude) are bypassed when a majority of sources collude; threshold arithmetic verified.
D59: Bridge USD metering: usd = amount*price; at 0.0001 a 50000 deposit counts 5 USD -> limit amplification 10,000x. F-04 merged into F-03.
D60: Genesis alloc sums to 81,000,000 NOVA across 5 EOAs; no admin mint account; incentive pools zero at genesis (precondition recorded for F-02).

D61: Deposit amount boundary: 0 and negative rejected (bridge.py:319-324); 1e308 passes format checks but exceeds USD limit; effective cap depends on the manipulable daily limit (F-03/F-04).
D62: Large-delay boundary: usd=100,000.01 -> held with 24h available_at; 100,000.00 -> ready immediately. Threshold behavior verified at both sides.
D63: Deposit replay: same chain:source_tx rejected by key scan (bridge.py:325-331); attacker rotates source_tx to mint repeatedly, so the guard does not stop F-01.
D64: Signature dedup (bridge.py:142-147): same node cannot sign twice; does not prevent 3 distinct sybil addresses.
D65: _usd_value boundary: p is None -> fallback price; p is dict -> TypeError (F-05); derived feed returns float and works. Contract mismatch is the root cause.
D66: DEX zero-amount swap rejected; amount_in <= 0 blocked in _swap_validate.
D67: Full LP removal drains pool to zero reserve but stays non-negative; no dust stuck in reserve holder.
D68: quote on missing pair returns safe value; no exception leak to RPC.
D69: Vote power is computed live (governance.py:270-290); both delegate-then-vote and vote-then-delegate orders include the amplified power; no snapshot mitigates F-06.
D70: Delegation cycle A->B->A: _seen set stops recursion, returns 0 for revisits; no stack overflow, but chain amplification (non-cycle) still works. F-06 stands.
D71: Endorsement dedup: same address cannot endorse twice; counted once.
D72: Governance fund execution with fund=0: balance guard prevents negative; no direct mint.
D73: DID reputation clamped; extremes (0 and max) handled without overflow.
D74: Subscription renew at exact balance: passes and leaves 0; no negative.
D75: CORS * confirmed on network/rpc.py; rate limit 100/s per IP enforced by security.py:19-23; Origin header not restricted. TM-009 known.
D76: p2p message boundary: messages up to 64MB frame correctly with embedded newlines escaped by length framing; TLS verify off is TM-013.
D77: Replay guard processed_txids rejects second submission of same txid; checkin interval guard prevents instant re-checkin. Prefix-match residual is P1-8.
D78: Agent long inputs clipped at configured limits; no memory blowup; executor has no fund-movement primitive.
D79: GraphQL deep nesting bounded by resolver recursion limits in explorer server; DB queries parameterized.
D80: Compute expiry vs submit race: status guard ensures single terminal state; expired tasks cannot be submitted after close.
D81: Balance conservation invariant over 100 random transaction sequences (transfer/stake/unstake/contract call): sum of balances constant including pool and fund accounts. Conservation holds; F-01/F-02 are minting-without-reserve, not conservation breaks.
D82: Bridge supply invariant: supply == sum(balances) holds after poc_f01 (49950 minted, balances 49950), but the 1:1 reserve invariant against BSC is violated because no real deposit exists. The internal invariant does not save F-01.
D83: Fund non-negativity invariant: continuous pin attempts stop when fund < reward; fund stays >= 0. Non-negativity holds while the lockup/drain remains valid; F-02 stands.
D84: Pool payout cap invariant: reward = min(reward_pool, 0.05) guarantees cumulative payout <= pool; the problem is pool injection without real storage (F-02).
D85: Governance power invariant: sum(voting_power) <= circulating is broken: 1000 NOVA yields 3000 votes while circulating base is 1000. F-06 confirmed as invariant violation.
D86: DEX k invariant: 1000 random swaps keep x*y within float tolerance; reserve holder isolation verified.
D87: Validator pool non-negative over 500 blocks; distribution drains pool to zero when insufficient.
D88: Oracle domain invariant (price within [PRICE_MIN, PRICE_MAX]) holds for 0.0001, showing the domain check is not a market-sanity check. F-03 stands.
D89: VM step invariant: steps <= 100000 enforced by loop guard; counter-example bytecode terminates.
D90: Mailbox cap invariant: 1001st message trims oldest; cap holds.
D91: Compute terminal-state invariant: each task reaches exactly one terminal state; double-submit blocked.
D92: Replica invariant: providers per CID <= 10 enforced at claim; 11th claim rejected.
D93: Airdrop once-per-address invariant holds; repeated calls no-op.
D94: Bridge daily usage invariant: recorded usage never exceeds limit in accounting terms, but USD metering is distorted by F-03; merged finding.
D95: Subscription balance conservation over multi-user simulation; no leakage.
D96: Arbitration escrow conservation: single withdrawal per case; double-draw blocked by status.
D97: Fee pool flush once per day via flush_day event scan; concurrent flush attempts dedupe.
D98: Snapshot determinism: restore(snapshot(s)) deep-equals s, including event and feed dicts.
D99: Regression: reran all six PoC scripts on the pinned commit; outputs match 05-pocs records (bridge mint 49950, fund lock 7475.2/drain 7.5, oracle 0.0001, TypeError, 3000 votes).
D100: Final sweep: confirmed findings F-01..F-06 (F-04 merged into F-03); 89 cleared passes with concrete guards; 5 known_or_duplicate with collision proofs; ledger reconciled with Destination=phase8_finding for H-001..H-006. Phase 6 complete.
