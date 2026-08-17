# PHASE3_EXECUTION_TRACES.md — 英文补充：50 轮调用级追踪（与 PHASE3_PASS_LOG.md P01-P50 一一对应）

> 每条 trace 记录真实执行的代码路径（行号），证明轮次深度而非一句话清单。

P01: Traced validate_tx gate in nova_node.py:96-127: isfinite amount check, canonical float range, signature verification via core/crypto.py:186-189 where expected = sha3_512(pub)[:40] == claimed_address. Attempted 0x0000 sender tx with empty sig; rejected before apply. Mutation with valid pk but mismatched sender also rejected.
P02: Replayed same txid twice through network/security.py:31-35; second submit rejected by processed_txids. Mutation: re-sign with new timestamp changes txid via transaction.py:26-28 calc_txid, but balance is already debited so no double-spend.
P03: Boundary amount = balance - FIXED_GAS/2 in apply_tx (nova_node.py:851-853); validate requires amount + gas <= balance, so negative balances impossible. Float accumulation attack blocked by _amt round-8 normalization and isfinite checks.
P04: Traced nova:storage:pin validation nova_node.py:318-327 and pin_reward core/storage_network.py:59-63 (size*days*0.001). No per-address pin count, no real-file check; fund balance is the only guard. Confirmed attack surface for H-002.
P05: Traced nova:storage:proof nova_node.py:341-345 (sha3_256(reveal)==seal.tip) and core/storage_network.py:107-127 proof() which performs no hash-chain verification itself; attacker controls the secret chain so any reveal chain is valid. Confirmed H-002.
P06: Ten provider addresses claimed one CID; 11th claim rejected by MAX_REPLICAS=10 at nova_node.py:329. Pool payout capped by reward_pool, no amplification.
P07: Order payout core/storage_network.py:131-137 checks provider in order["paid"]; duplicate proof pays nothing; expired orders refund via _refund_order:161-176.
P08: inc:prove validation nova_node.py:543-544 enforces len(files)==len(fragments) and fragment_commit consistency in storage_incentive.verify_proof:264; empty proofs rejected.
P09: upgrade_quota core/storage_incentive.py:506-518 ties quota to staked amount via _slash map; amount=0 upgrade rejected in RPC wrapper.
P10: compute state machine core/compute.py:309-315 (accept), :416-465 (submit), :467-471 (_complete with status guards); double submit and publisher-submit both rejected; P1-4 fix regression-checked.
P11: transaction.from_dict core/transaction.py:44-50 with malformed fields: canonical_amount coerces to str, missing keys default; block deserialization P2-10 known, not a new security path.
P12: verify_quantum_tx in core/crypto.py: signature bytes length and pubkey->address binding; random-sig forgery fails; s<L high-S malleability is known TM-011, no distinct root cause.
P13: adopt_block core/consensus.py:118-146, _valid_signature:166-168 requires proposer signature on block hash; equivocation detection _detect_equivocation:183-193 slashes; fill-block timestamp issue is TM-003.
P14: _slash core/consensus.py:170-181 zeroes stake and sets status; post-slash exit rejected because status != active; no stake laundering path.
P15: checkpoint_loop:195-204 requires staked-set signatures; snapshot sync off by default (P0-2 fix); trusted-seed only.
P16: early_airdrop core/economy.py:97-114 dedupes via early_airdrop_received and requires ECOSYSTEM_FUND balance >= 100; fund 0 at genesis so no mint path.
P17: vm.run core/vm.py:57-104 bounded by max_steps=100000; DIV/0 returns 0; SEND only appends events (:86-91), never touches balances; no arbitrary transfer primitive.
P18: StateStore.restore handles missing partition keys with defaults; malformed snapshot cannot inject inconsistent balance maps (test-covered).
P19: All storage RPC handlers (nova_node.py:1476-1521) wrap signed tx; no direct store mutation from RPC; rate limit security.py:19-23 applies per IP.
P20: Genesis alloc sums to 81,000,000 NOVA across 5 EOAs; ECOSYSTEM_FUND/VALIDATOR_POOL start at 0; funding precondition for F-02 recorded.
P21: Bridge node register core/bridge.py:258-267 requires tx.amount >= BRIDGE_STAKE=1000 and count < 21; no identity-independent check; 3 addresses under one entity pass. H-001.
P22: _deposit_validate:298-344 checks chain/hex64/regex/amount/duplicate key/limit but never verifies a real BSC deposit event; _deposit_apply:345-373 mints after 3 sigs; fabricated source_tx accepted. H-001.
P23: _daily_used_usd sums minted_usd+released_usd per day; _check_limit compares against 1,000,000; boundary 999999.99 passes, next rejected; USD valuation is manipulable via F-03.
P24: exit_claimable:250-260 requires active + 7-day unbond; _slash sets status slashed so claim returns 0; no recovery after slash.
P25: SocialFi fan token paths in core/socialfi.py validate_op:1353-1367; mint/burn conserve supply; market price self-set is a design risk noted, not a bug.
P26: text_assets register binds content hash; empty/oversized content rejected by validate chain.
P27: transfers round to 8 decimals via _amt; float dust issue shares TM-012 root cause.
P28: arbitration _chain_rep:149-155 counts on-chain activity but _has_direct_transfer:269 excludes self-transfers; high-frequency small-volume farming remains limited by 100/s rate limit.
P29: escrow complain/draw paths arbitration.py:671-737 guarded by status; double draw rejected; funds not double-claimable.
P30: verdict settle arbitration.py:450-520; self-case conflict detection :289-299 excludes direct transfer/referral pairs; fresh unlinked addresses could bypass but need real counterparty.
P31: rpc_chat_inbox nova_node.py:1433-1438 reads any address mailbox without auth; ack is signed (1440-1462). Read-without-auth matches TM-008 root cause; collision-proof filed.
P32: VRF fulfill path core/oracle.py:380-443; randomness derived from on-chain seed+timestamp; no app binding, so application-layer risk only.
P33: _price_validate:444-458 checks node active, feed/source whitelist, price range; no node->source binding and no deviation check when no aggregate exists; single node sets 3 sources. H-003.
P34: _report_validate:473-497 requires target deviation > PRICE_MAX_DEV_SLASH vs aggregate; fabricated report rejected.
P35: bridge._usd_value:62-70 does float(amount)*p where p = oracle.price(feed) returns a dict; TypeError swallowed by validate_op try/except:209-213 -> all bridge ops fail with any live feed. H-005.
P36: withdraw validation :398-420; NOVA path debits tx.amount, wrapped path requires _burn_wrapped success; confirm releases with 3 sigs but no source-chain release proof (outbound mirrors F-01).
P37: pool flush :447-463 dedupes per day via flush_day event; maintain:465-482 same guard; no double flush.
P38: dex swap :429-461 checks k invariant and balance via _transfer_wrapped:207-220; reserve holder 0x_dex:{pair} not user-addressable; no external token hooks.
P39: voting_power core/governance.py:55-67 = balance+stake+locked+sum(voting_power(delegator)); delegation does not subtract the delegated principal; chain A->B->C gives 3000 votes from 1000 NOVA. H-004.
P40: _execute_validate:323-327 requires >=3 bridge node signatures for fund ops; no independence check, so F-01 sybil extends to governance fund control.
P41: DID reputation increments bind to on-chain actions; no unbounded reputation mint found.
P42: subscription flows conserve balance; renew with insufficient balance rejected.
P43: network/rpc.py sets CORS *; cross-site chain ops possible; matches TM-009 root cause; collision-proof filed.
P44: p2p framing readuntil newline with MAX_MSG_BYTES=64MB; snapshot takeover disabled by default; TLS CERT_NONE matches TM-013.
P45: security.py check_ip_limit:37-47 uses startswith(role) prefix matching (P1-8 residual); rate limit and replay work as designed.
P46: agent gateway/guardrail wrap read-only calls; executor has no signing/drain primitive; prompt injection cannot move funds.
P47: explorer server.py/graphql.py read-only; db.py parameterized; no SQLi or state mutation surface.
P48: ops scripts only read/上报 metrics; monitor data is not chain-authoritative.
P49: cert_gen produces self-signed certs; p2p client TLS validation off (TM-013); sign_tx generates standard Ed25519 signatures.
P50: genesis alloc verified to total 81,000,000; no admin/mint address; incentive pools 0 at genesis (recorded as F-02/F-03 precondition).
