from enum import Enum
from typing import TypedDict
from fmclient import Agent, Market, Holding, Session, Order, OrderType, OrderSide


# Trading account details
FM_ACCOUNT = "fain-premium"
FM_EMAIL = "nc681@d002"
FM_PASSWORD = "nc681"
FM_MARKETPLACE_ID = 1524

RISK_PENALTY = 0.01

class BotType(Enum):
    """Enum representing the current trading mode of the bot."""
    ACTIVE = 0
    REACTIVE = 1

class OrderBookEntry(TypedDict):
    name: str
    bid: int | None
    ask: int | None

class CAPMBot(Agent):
    _risk_penalty: float
    _payoffs: dict[int, list[int]]

    _holdings: Holding | None
    _orderbook: dict[int, OrderBookEntry]

    # My confirmed standing orders: market_id → {side_name → Order}
    _my_standing_orders: dict[int, dict[str, Order]]

    # Orders submitted to the exchange but not yet acknowledged (LIMIT orders only).
    # Key: (market_id, side_name). Prevents duplicate submissions within the same tick.
    _pend_orders: dict[tuple[int, str], Order]

    # fm_ids of orders for which a CANCEL has been sent but not yet confirmed. 
    # Prevents reposting on the same slot.
    _cancelling_order_ids: set[int]

    # True while a REACTIVE eat-order is pending (submitted but not yet accepted).
    # No new strategy action is taken until this clears.
    _reactive_pend: bool

    # Check whether marginal value has changed. Print only when changed.
    _last_logged_marginal_values: dict | None

    def __init__(self, account: str, email: str, password: str, marketplace_id: int, risk_penalty: float, bot_name: str = "CAPMBot"):
        super().__init__(account, email, password, marketplace_id, name=bot_name)
        self._risk_penalty = risk_penalty
        self._payoffs = {}

        self._holdings = None
        self._orderbook = {}
        self._my_standing_orders = {}
        self._pend_orders = {}
        self._cancelling_order_ids = set()
        self._reactive_pend = False

        self._last_logged_marginal_values = {}

    def initialised(self):
        """Extract payoff distribution for each asset."""
        for market in self.markets.values():
            asset_id = market.fm_id
            description = market.description
            self._payoffs[asset_id] = [int(payoff) for payoff in description.split(",")]

        self.inform("Bot initialised, I have the payoffs for the states.")



    # ---------- Order helpers ----------

    def _make_order(self, market: Market, order_side: OrderSide, price: int, units: int = 1) -> Order:
        """Create a LIMIT order object without sending it."""
        order = Order.create_new(market)
        order.order_type = OrderType.LIMIT
        order.order_side = order_side
        order.price = price
        order.units = units
        order.mine = True
        return order

    def _cancel_order(self, order: Order):
        """
        Send a CANCEL for an existing standing order and record its fm_id in
        _cancelling_order_ids so that the same slot is not re-used until the
        exchange confirms the cancellation.
        """
        cancel = Order.create_new(order.market)
        cancel.order_type = OrderType.CANCEL
        cancel.order_side = order.order_side
        cancel.price = order.price
        cancel.units = order.units
        cancel.mine = True
        cancel.fm_id = order.fm_id
        if order.fm_id is not None:
            self._cancelling_order_ids.add(order.fm_id)
        self.send_order(cancel)

    def _submit_order(self, order: Order) -> bool:
        """
        Submit a LIMIT order to the exchange.

        Rules:
        - If another REACTIVE eat-order is already pending, block new submissions.
        - If this exact (market, side) slot already has an unconfirmed pending order,
          skip to prevent duplicates.
        - If the slot has a pending cancel, skip until the cancel is confirmed.

        Records the order in both _pend_orders and _my_standing_orders immediately
        so that subsequent logic within the same tick sees it as placed.

        Returns True if the order was sent.
        """
        # Block all new limit orders while a reactive order is pending.
        if self._reactive_pend:
            self.inform("REACTIVE eat-order pending, skip new submission.")
            return False

        market_id = order.market.fm_id
        side_key = order.order_side.name
        key = (market_id, side_key)

        # Duplicate pending guard.
        if key in self._pend_orders:
            self.inform(f"Pending order already exists for {order.market.name} [{side_key}], skip.")
            return False

        # Cancel order pending guard: do not repost while old cancel is travelling.
        existing = self._my_standing_orders.get(market_id, {}).get(side_key)
        if existing is not None and existing.fm_id in self._cancelling_order_ids:
            self.inform(f"Cancel pending for {order.market.name} [{side_key}], skip re-post.")
            return False

        # Register as pending before sending.
        self._pend_orders[key] = order

        # Record in standing orders so the rest of this tick treats this slot as occupied.
        self._my_standing_orders.setdefault(market_id, {})[side_key] = order

        self.send_order(order)
        return True

    def _cancel_all_my_orders(self):
        """Cancel every confirmed standing order across all markets (used when switching to REACTIVE).

        Orders still in _pend_orders have no valid fm_id yet; sending a CANCEL for them
        produces a server rejection. Those slots are skipped and only the confirmed
        (non-pending) entries are cancelled and removed from _my_standing_orders.
        """
        # [FIX 1] Only delete slots that are not still pending confirmation.
        # Deleting the entire market_id entry would also remove pending slots whose
        # fm_id is not yet known, breaking the protected_keys guard in
        # _update_my_standing_orders on the next tick.
        for market_id in list(self._my_standing_orders.keys()):
            for side_key, order in list(self._my_standing_orders[market_id].items()):
                if (market_id, side_key) in self._pend_orders:
                    # Still awaiting server confirmation; do not cancel and do not remove.
                    continue
                self._cancel_order(order)
                del self._my_standing_orders[market_id][side_key]
            # Remove the market entry only if all slots have been cleared.
            if not self._my_standing_orders.get(market_id):
                self._my_standing_orders.pop(market_id, None)



    # ---------- Performance and valuation ----------

    def get_potential_performance(self, orders: list[Order]) -> float:
        """
        Returns the portfolio performance if all the given list of orders is executed based on current holdings.
        Performance = E[Payoff] − b × Var[Payoff], where b is the penalty for risk.
        All monetary values are stored in cents; arithmetic is done in dollars.
        
        :param orders: list of orders
        :return: performance (float)
        """
        # Collect my assets' unit in each market.
        units_map: dict[int, float] = {
            market.fm_id: asset.units
            for market, asset in self._holdings.assets.items()
        }
        cash = self._holdings.cash / 100

        for order in orders:
            market_id = order.market.fm_id
            order_value = (order.price * order.units) / 100

            if order.order_side == OrderSide.BUY:
                units_map[market_id] = units_map.get(market_id, 0) + order.units
                cash -= order_value
            elif order.order_side == OrderSide.SELL:
                units_map[market_id] = units_map.get(market_id, 0) - order.units
                cash += order_value

        # Compute state payoffs across 4 states in dollars.
        state_payoffs = [cash] * 4
        for market_id, units in units_map.items():
            if market_id not in self._payoffs:
                continue
            for s in range(4):
                state_payoffs[s] += units * (self._payoffs[market_id][s] / 100.0)

        expected_payoff = sum(state_payoffs) / 4
        variance = sum((p - expected_payoff) ** 2 for p in state_payoffs) / 4

        performance = expected_payoff - self._risk_penalty * variance

        return performance

    def is_portfolio_optimal(self) -> bool:
        """
        Checks whether the current portfolio is optimal given the best available market prices.

        For each market, tests whether buying at the best ask or selling at the best bid
        would improve performance. Returns False as soon as any such improvement is found.
        Returns True only if no single trade improves performance.

        :return: True if portfolio is optimal, False otherwise.
        """
        current_performance = self.get_potential_performance([])

        for market in self.markets.values():
            market_id = market.fm_id
            entry = self._orderbook.get(market_id)

            if not entry:
                continue

            # Check buy at best ask.
            ask = entry["ask"]
            if ask is not None:
                buy_order = self._make_order(market, OrderSide.BUY, ask)
                if self.get_potential_performance([buy_order]) > current_performance:
                    self.inform(
                        f"Portfolio now not optimal: BUY 1 unit of {market.name} "
                        f"at ask price=[{ask / 100:.2f}] improves performance."
                    )
                    return False

            # Check sell at best bid.
            bid = entry["bid"]
            if bid is not None:
                sell_order = self._make_order(market, OrderSide.SELL, bid)
                if self.get_potential_performance([sell_order]) > current_performance:
                    self.inform(
                        f"Portfolio now not optimal: SELL 1 unit of {market.name} "
                        f"at bid price=[{bid / 100:.2f}] improves performance."
                    )
                    return False

        self.inform(f"Portfolio now is optimal. Current performance={current_performance:.4f}")
        return True

    def _compute_marginal_value(self, market: Market) -> float:
        """
        Return the indifference price (cents) of buying 1 more unit of this asset.
        Only logs when the value has changed since last computation for this market.

        Definition: marginal valus is the price X (cents) at which buying 
        1 extra unit of this asset leaves portfolio performance exactly unchanged.

        original_performance = E[original_payoff] - b × Var[original]
        new_performance = (E[original_payoff] + E[asset's payoff]/100 - X/100) - b × Var[original + 1 unit of asset]

        original_performance = new_performance -> get X = (ΔE - b × ΔVar) * 100
        where ΔE = E[asset's payoff]/100, ΔVar = Var[original + 1 unit of asset] - Var[original]
        
        :param market: the Market whose asset's marginal value we want.
        :return: marginal value in cents (float).
        """
        # ----- Calculate -----
        # E[asset]
        asset_payoffs = [p / 100.0 for p in self._payoffs[market.fm_id]]
        E_asset = sum(asset_payoffs) / 4

        # E[portfolio]
        portfolio_payoffs = [self._holdings.cash / 100.0] * 4
        for mkt, asset in self._holdings.assets.items():
            if mkt.fm_id not in self._payoffs:
                continue
            for s in range(4):
                portfolio_payoffs[s] += asset.units * (self._payoffs[mkt.fm_id][s] / 100.0)
        E_portfolio = sum(portfolio_payoffs) / 4

        # Var[asset]
        var_asset = sum((p - E_asset) ** 2 for p in asset_payoffs) / 4

        # Cov(asset, portfolio)
        cov = sum(
            (asset_payoffs[s] - E_asset) * (portfolio_payoffs[s] - E_portfolio)
            for s in range(4)
        ) / 4

        # ΔVar
        delta_var = var_asset + 2 * cov

        # margin value（cents）
        margin_value = (E_asset - self._risk_penalty * delta_var) * 100

        # ----- Print -----
        # Only log when value has changed.
        previous = self._last_logged_marginal_values.get(market.fm_id)
        if previous == margin_value:
            return margin_value
        
        self._last_logged_marginal_values[market.fm_id] = margin_value
        
        lines = ["\n----- Marginal values -----"]
        lines.append(f"{'market_id':<16} {'market_name':<16} {'marginal_value':>14}")
        lines.append("-" * 48)
        for mkt_id, val in sorted(self._last_logged_marginal_values.items()):
            mkt_name = next((m.name for m in self.markets.values() if m.fm_id == mkt_id), str(mkt_id))
            marker = " *" if mkt_id == market.fm_id else ""
            lines.append(f"{mkt_id:<16} {mkt_name:<16} {val / 100:>14.2f}{marker}")
        self.inform("\n".join(lines))

        return margin_value
    

    # --------------------------------------------------------
    #                      Auto functions
    # --------------------------------------------------------

    def pre_start_tasks(self):
        pass

    def received_session_info(self, session: Session):
        if session.is_open:
            self.inform(f"Marketplace is now open for trading. The new session is {session.fm_id}")
        elif session.is_paused:
            self.inform("Marketplace is now paused. You can not trade.")
        elif session.is_closed:
            self.inform("Marketplace is now closed. You can not trade.")

    def received_holdings(self, holdings: Holding):
        self._holdings = holdings

        # Print holdings on every update.
        lines = [f"\n----- Current Holdings -----"]
        lines.append(f"{'Account':<12} {'Total':>10} {'Available':>12}")
        lines.append("-" * 38)
        lines.append(
            f"{'Cash':<12} "
            f"{holdings.cash / 100:>10.2f} "
            f"{holdings.cash_available / 100:>12.2f}"
        )
        for market, asset in holdings.assets.items():
            lines.append(f"{market.name:<12} {asset.units:>10} {asset.units_available:>12}")
        self.inform("\n".join(lines))

        # Rerun strategy whenever holdings change.
        self._execute_trading_strategy()

    def received_orders(self, orders: list[Order]):
        """
        Called whenever the order book changes.

        Workflow:
        1. Update and print best bid/ask.
        2. Update and print my standing orders.
        3. Execute different trading strategy based on current performance.
        
        :param orders: Full list of current orders in the marketplace.
        """
        self._update_best_standing_orders()
        self._update_my_standing_orders()
        self._execute_trading_strategy()

    def order_accepted(self, order: Order):
        """
        Called when the exchange confirms an order.

        For CANCEL orders: clear the cancelling guard so the slot can be reused.
        For LIMIT orders:  move from _pend_orders to confirmed _my_standing_orders, and clear the REACTIVE pending flag if applicable.
        """
        if order.order_type == OrderType.CANCEL:
            if order.fm_id is not None:
                self._cancelling_order_ids.discard(order.fm_id)
            self.inform(
                f"Cancel confirmed [{order.market.name}]: "
                f"fm_id={order.fm_id}, side={order.order_side.name}"
            )
        else:
            key = (order.market.fm_id, order.order_side.name)
            self._pend_orders.pop(key, None)

            # Replace the optimistic placeholder with the server-assigned copy.
            self._my_standing_orders.setdefault(order.market.fm_id, {})[order.order_side.name] = order

            # If this was our reactive order, clear the pending flag.
            # The holdings update from the fill will trigger strategy reevaluation.
            if self._reactive_pend:
                self._reactive_pend = False

            self.inform(
                f"Order accepted [{order.market.name}]: "
                f"fm_id={order.fm_id}, side={order.order_side.name}, "
                f"price={order.price/100:.2f}, traded={order.has_traded}"
            )

    def order_rejected(self, info: dict[str, str], order: Order):
        """
        Called when the exchange rejects an order. Clean up all tracking state
        so the strategy can retry cleanly on the next tick.
        """
        self.warning(
            f"Order rejected [{order.market.name}]: "
            f"side={order.order_side.name}, price={order.price/100:.2f}, info={info}"
        )

        market_id = order.market.fm_id
        side_key = order.order_side.name

        if order.order_type == OrderType.CANCEL:
            # If CANCEL was rejected means order may have already filled, clear guard.
            if order.fm_id is not None:
                self._cancelling_order_ids.discard(order.fm_id)
        else:
            # If LIMIT is rejected, remove from pending and standing map.
            key = (market_id, side_key)
            self._pend_orders.pop(key, None)

            if self._reactive_pend:
                self._reactive_pend = False

            tracked = self._my_standing_orders.get(market_id, {}).get(side_key)
            if tracked is not None and (tracked.fm_id == order.fm_id or tracked.fm_id is None):
                del self._my_standing_orders[market_id][side_key]
                if not self._my_standing_orders[market_id]:
                    del self._my_standing_orders[market_id]


    # --------------------------------------------------------
    #                    Orderbook functions
    # --------------------------------------------------------

    def _update_best_standing_orders(self):
        """Update best bid/ask for other participants' orders only."""
        # ----- Collect -----
        new_orderbook: dict[int, OrderBookEntry] = {}
        for order in Order.current().values():
            if order.order_type is not OrderType.LIMIT or order.mine:
                continue

            market_id = order.market.fm_id
            if market_id not in new_orderbook:
                new_orderbook[market_id] = {"name": order.market.name, "bid": None, "ask": None}

            entry = new_orderbook[market_id]
            # Update best bid.
            if order.order_side == OrderSide.BUY:
                if entry["bid"] is None or order.price > entry["bid"]:
                    entry["bid"] = order.price
            # Update best ask.
            elif order.order_side == OrderSide.SELL:
                if entry["ask"] is None or order.price < entry["ask"]:
                    entry["ask"] = order.price

        # ----- Print -----
        self._orderbook = new_orderbook

        lines = ["\n----- Current best standing order -----"]
        lines.append(f"{'market_id':<16} {'market_name':<12} {'bid':>8} {'ask':>12}")
        lines.append("-" * 54)
        for market_id in sorted(self._orderbook.keys()):
            entry = self._orderbook[market_id]
            bid = f"{entry['bid'] / 100:.2f}" if entry["bid"] is not None else "-"
            ask = f"{entry['ask'] / 100:.2f}" if entry["ask"] is not None else "-"
            lines.append(f"{market_id:<16} {entry['name']:<12} {bid:>8} {ask:>12}")
        if not self._orderbook:
            lines.append("No standing market orders.")
        self.inform("\n".join(lines))

    def _update_my_standing_orders(self):
        """
        Update _my_standing_orders with what the exchange currently sees via Order.my_current().

        Rules:
        - Each slot (market × side) holds at most 1 unit.
        - At most one side per market.
        - Slots that are in _pend_orders or _cancelling_order_ids are protected and must not be touched here.
        """
        # Build set of protected slots to ensure never override pending state.
        protected_keys: set[tuple[int, str]] = set(self._pend_orders.keys())
        for mid, sides in self._my_standing_orders.items():
            for side_key, o in sides.items():
                if o.fm_id in self._cancelling_order_ids:
                    protected_keys.add((mid, side_key))

        # Collect live confirmed orders from the exchange.
        live: dict[int, dict[str, list[Order]]] = {}
        for order in Order.my_current().values():
            if order.order_type is not OrderType.LIMIT:
                continue
            if order.fm_id in self._cancelling_order_ids:
                continue  # Already being cancelled, skip.

            # Cancel any stray order with more than 1 unit.
            if order.units != 1:
                self.inform(
                    f"[Invalid] Cancelling multi-unit order ({order.units} units) "
                    f"in {order.market.name} [{order.order_side.name}]"
                )
                self._cancel_order(order)
                continue

            mid = order.market.fm_id
            side_key = order.order_side.name
            live.setdefault(mid, {}).setdefault(side_key, []).append(order)

        # Cancel duplicate orders on the same side of the same market.
        for mid, sides in live.items():
            for side_key, order_list in sides.items():
                if len(order_list) > 1:
                    tracked = self._my_standing_orders.get(mid, {}).get(side_key)
                    keep = (
                        tracked
                        if tracked and any(o.fm_id == tracked.fm_id for o in order_list)
                        else order_list[0]
                    )
                    for o in order_list:
                        if o.fm_id != keep.fm_id:
                            self.inform(
                                f"[Invalid] Cancelling duplicate {side_key} "
                                f"in {o.market.name} at {o.price/100:.2f}"
                            )
                            self._cancel_order(o)
                    sides[side_key] = [keep]

        # Cancel if both BUY and SELL exist in the same market.
        for mid, sides in live.items():
            if "BUY" in sides and "SELL" in sides:
                for side_key, order_list in sides.items():
                    o = order_list[0]
                    self.inform(
                        f"[Invalid] Cancelling conflicting {side_key} in {o.market.name}"
                    )
                    self._cancel_order(o)
                live[mid] = {}

        # Remove orders which is no longer alive and not protected by pending state.
        for mid in list(self._my_standing_orders.keys()):
            for side_key in list(self._my_standing_orders[mid].keys()):
                if (mid, side_key) in protected_keys:
                    continue  # Do not remove pending or cancelling orders.
                if not live.get(mid, {}).get(side_key):
                    self.inform(
                        f"[Update] Removing stale {side_key} slot for market {mid}."
                    )
                    del self._my_standing_orders[mid][side_key]
            if not self._my_standing_orders.get(mid):
                self._my_standing_orders.pop(mid, None)

        # Merge live orders into _my_standing_orders.
        for mid, sides in live.items():
            for side_key, order_list in sides.items():
                if order_list:
                    self._my_standing_orders.setdefault(mid, {})[side_key] = order_list[0]

        # Print current state.
        lines = ["\n----- My standing orders -----"]
        lines.append(f"{'market_id':<16} {'name':<16} {'side':<6} {'price':>10} {'status':<12}")
        lines.append("-" * 66)
        for mid, sides in sorted(self._my_standing_orders.items()):
            for sk, o in sides.items():
                status = "(pending)" if (mid, sk) in self._pend_orders else ""
                lines.append(
                    f"{mid:<16} {o.market.name:<16} {sk:<6} "
                    f"{o.price/100:>10.2f} {status:<12}"
                )
        if not self._my_standing_orders:
            lines.append("None.")
        self.inform("\n".join(lines))



    # ---------- Trading Strategy ----------

    def _execute_trading_strategy(self):
        """
        Central strategy function. 
        Called after every orderbook change and every holdings update.

        Rules:
          1. Holdings not yet received → do nothing.
          2. A REACTIVE eat-order is already pending → wait for it to settle, do not send anything else.
          3. No other participant standing orders in the market → cancel all my orders and wait.
          4. is_portfolio_optimal() is False → switch to REACTIVE mode.
          5. is_portfolio_optimal() is True  → switch to ACTIVE mode.
        """
        if self._holdings is None:
            return

        # While a reactive order is travelling, do nothing.
        # When holdings change once it fills, trigger re-evaluation.
        if self._reactive_pend:
            self.inform("[Strategy] REACTIVE eat-order pending, wait for fill.")
            return

        current_performance = self.get_potential_performance([])
        self.inform(f"\n----- Strategy tick | performance={current_performance:.4f} -----")

        # Check whether there is anything to trade against.
        has_market_orders = any(
            entry["bid"] is not None or entry["ask"] is not None
            for entry in self._orderbook.values()
        )
        if not has_market_orders:
            self.inform("[Strategy] No standing orders in market, cancel my orders and wait.")
            self._cancel_all_my_orders()
            return

        # Route to REACTIVE or ACTIVE
        if not self.is_portfolio_optimal():
            self.inform("[Strategy] Portfolio not optimal → REACTIVE mode.")
            self._run_reactive(current_performance)
        else:
            self.inform("[Strategy] Portfolio optimal → ACTIVE mode.")
            self._run_active(current_performance)


    def _run_reactive(self, current_performance: float):
        """
        REACTIVE mode: find the single best REACTIVE eat-order opportunity and execute it.

        Steps:
          1. Gather every (market, side, price) pair from other participants' standing
             orders where trading would strictly improve performance, and where buying
             one more unit would not push any asset's marginal value negative to avoid over-weighting.
          2. Sort candidates by performance improvement, largest first.
          3. Take the best candidate:
             - Cancel all my own standing orders (fire-and-forget, no need to wait).
             - Handle cash availability for BUY orders.
             - Submit the eat-order and set _reactive_pend = True.
          4. Only one eat-order is submitted per call.
          5. Re-evaluation happens after cancelling all my standing order and eat-order is accepted.
        """
        # Build and rank all profitable eat-order opportunities.
        candidates: list[tuple[float, Market, OrderSide, int]] = []

        for market in self.markets.values():
            entry = self._orderbook.get(market.fm_id)
            if not entry:
                continue

            ask = entry["ask"]
            if ask is not None:
                buy_order = self._make_order(market, OrderSide.BUY, ask)
                new_perf = self.get_potential_performance([buy_order])
                if new_perf > current_performance:
                    # Guard: buying must not result in a negative marginal value for this asset to prevent over-weighting.
                    if not self._would_make_marginal_negative(market, OrderSide.BUY):
                        gain = new_perf - current_performance
                        candidates.append((gain, market, OrderSide.BUY, ask))

            bid = entry["bid"]
            if bid is not None:
                sell_order = self._make_order(market, OrderSide.SELL, bid)
                new_perf = self.get_potential_performance([sell_order])
                if new_perf > current_performance:
                    gain = new_perf - current_performance
                    candidates.append((gain, market, OrderSide.SELL, bid))

        if not candidates:
            # No profitable eat opportunities remain, switch to ACTIVE.
            self.inform("[REACTIVE] No profitable eat opportunities, switch to ACTIVE.")
            self._run_active(current_performance)
            return

        # Sort descending by gain; execute the best one.
        candidates.sort(key=lambda x: x[0], reverse=True)
        gain, market, side, price = candidates[0]

        self.inform(
            f"[REACTIVE] Best opportunity: {side.name} {market.name} "
            f"at {price/100:.2f} | gain={gain:.4f}"
        )

        # Cancel all my standing orders (fire-and-forget, no need to wait).
        self._cancel_all_my_orders()

        # Attempt to submit the eat-order.
        submitted = self._submit_reactive_order(market, side, price, current_performance)
        if submitted:
            self._reactive_pend = True


    def _submit_reactive_order(
        self, market: Market, side: OrderSide, price: int, current_performance: float
    ) -> bool:
        """
        Validate and submit a single REACTIVE eat-order.

        For BUY orders checks cash availability:
          - cash_available sufficient
            → submit immediately.
          - cash_available low but cash total is sufficient (ACTIVE BUY orders have frozen some cash)
            → cancel my ACTIVE orders, so submit eat-order once those cancels confirm.
          - total cash insufficient
            → try _handle_cash_shortage.

        For SELL orders checks asset availability.

        Returns True if an order was submitted.
        """
        order = self._make_order(market, side, price)

        if side == OrderSide.BUY:
            # Use total cash because any frozen cash belongs to ACTIVE orders will be cancelled above.
            if self._holdings.cash >= price:
                self.inform(f"[REACTIVE BUY] {market.name} at {price/100:.2f}")
                return self._submit_order(order)
            # True cash shortage: try to raise funds by selling something else.
            self.inform(
                f"[REACTIVE BUY] Cash insufficient: "
                f"need {price/100:.2f}, have {self._holdings.cash/100:.2f}, trying shortage handler."
            )
            return self._handle_cash_shortage(market, price, order, current_performance)

        # SELL side: check asset availability.
        asset = self._holdings.assets.get(market)
        if asset is None or not asset.can_sell or asset.units_available < 1:
            self.inform(f"[REACTIVE SELL] {market.name}: no units available to sell.")
            return False
        self.inform(f"[REACTIVE SELL] {market.name} at {price/100:.2f}")
        return self._submit_order(order)

    def _would_make_marginal_negative(self, market: Market, side: OrderSide) -> bool:
        """
        Returns True if executing a 1-unit BUY in the given market would cause
        ANY asset's marginal value to become negative after the trade.

        Only applied to BUY orders. Selling reduces a position (moving away from
        over-weighting) and the caller already ensures a SELL improves performance,
        so no guard is needed on the sell side.
        """
        if side != OrderSide.BUY:
            return False

        # Build simulated portfolio payoffs after buying 1 extra unit of market.
        sim_portfolio_payoffs = [self._holdings.cash / 100.0] * 4
        for mkt, asset in self._holdings.assets.items():
            if mkt.fm_id not in self._payoffs:
                continue
            extra = 1 if mkt.fm_id == market.fm_id else 0
            for s in range(4):
                sim_portfolio_payoffs[s] += (asset.units + extra) * (self._payoffs[mkt.fm_id][s] / 100.0)
        E_sim_portfolio = sum(sim_portfolio_payoffs) / 4

        # Check every traded asset's marginal value under the simulated portfolio.
        for mkt in self.markets.values():
            if mkt.fm_id not in self._payoffs:
                continue
            asset_payoffs = [p / 100.0 for p in self._payoffs[mkt.fm_id]]
            E_asset = sum(asset_payoffs) / 4
            var_asset = sum((p - E_asset) ** 2 for p in asset_payoffs) / 4
            cov = sum(
                (asset_payoffs[s] - E_asset) * (sim_portfolio_payoffs[s] - E_sim_portfolio)
                for s in range(4)
            ) / 4
            # delta_Var = Var[asset] + 2 * Cov(asset, portfolio)
            delta_var = var_asset + 2 * cov
            marginal_value_after = (E_asset - self._risk_penalty * delta_var) * 100
            if marginal_value_after < 0:
                self.inform(
                    f"[Guard] BUY {market.name} would make {mkt.name} "
                    f"marginal value negative ({marginal_value_after/100:.2f}). Skipping."
                )
                return True

        return False


    def _run_active(self, current_performance: float):
        """
        ACTIVE mode: portfolio is locally optimal, so post limit orders at prices
        just inside each asset's marginal value.

        For each market:
          - Compute marginal value (MV).
          - Determine which side to post based on current holdings:
            · If holding units of this asset, act as a potential seller so post a SELL at MV + 1 tick.
            · If holding no units, act as a potential buyer so post a BUY at MV - 1 tick.
          - At most one order per side per market (1 unit each).
          - Same-market BUY and SELL cannot coexist, only post the side whose order improves performance.
          - If an existing standing order's price is still correct, leave it.
          - If the price has drifted (e.g., holdings changed), cancel and repost.
          - If a cancel is already pending for a slot, skip it this tick.

        All markets are processed in a single pass (no early return), so it is 
        permitted to hold standing orders in multiple markets simultaneously.
        """
        for market in self.markets.values():
            market_id = market.fm_id
            entry = self._orderbook.get(market_id)

            ask = entry["ask"] if entry else None
            bid = entry["bid"] if entry else None

            # Compute this market's marginal value and snap to nearest price tick.
            margin_value = self._compute_marginal_value(market)
            margin_price = int(round(margin_value / market.price_tick)) * market.price_tick
            margin_price = max(market.min_price, min(market.max_price, margin_price))

            buy_price  = max(market.min_price, margin_price - market.price_tick)
            sell_price = min(market.max_price, margin_price + market.price_tick)

            # Decide which side to post based on current holdings.
            # If holding units of this asset, lean toward selling (reducing risk exposure).
            # If holding none or are short, lean toward buying (gaining diversification).
            asset = self._holdings.assets.get(market)
            units_held = asset.units if asset is not None else 0
            prefer_sell = units_held > 0

            # [FIX 4] If marginal value is negative, the asset is already over-weighted;
            # posting a BUY would make things worse. Skip posting entirely for this market
            # when prefer_sell is False but marginal value is non-positive, rather than
            # letting _manage_active_order waste a performance check to reject it.
            if not prefer_sell and margin_value <= 0:
                self._cancel_active_slot(market_id, OrderSide.BUY, reason="negative marginal value")
                self._cancel_active_slot(market_id, OrderSide.SELL, reason="negative marginal value")
                continue
 
            if prefer_sell:
                # Before posting a SELL, cancel any stale BUY on this market first.
                self._cancel_active_slot(market_id, OrderSide.BUY, reason="switched to SELL side")
                if bid is None:
                    self._manage_active_order(
                        market, OrderSide.SELL, sell_price, current_performance
                    )
                else:
                    # A bid exists but is not profitable; cancel any stale SELL.
                    self._cancel_active_slot(market_id, OrderSide.SELL, reason="bid now present")
            else:
                # Before posting a BUY, cancel any stale SELL on this market first.
                self._cancel_active_slot(market_id, OrderSide.SELL, reason="switched to BUY side")
                if ask is None:
                    self._manage_active_order(
                        market, OrderSide.BUY, buy_price, current_performance
                    )
                else:
                    # An ask exists but is not profitable; cancel any stale BUY.
                    self._cancel_active_slot(market_id, OrderSide.BUY, reason="ask now present")

    def _manage_active_order(self, market: Market, side: OrderSide, 
                             target_price: int, current_performance: float):
        """
        Ensure there is exactly one standing order for (market, side) at target_price.

        - If a correctly-priced order already exists: leave it.
        - If an order exists at the wrong price: cancel it (the slot guard in
          _submit_order will prevent re-posting until the cancel confirms).
        - If no order exists (and no cancel is pending for the slot): post one,
          provided it improves performance.
        """
        market_id = market.fm_id
        side_key = side.name
        existing = self._my_standing_orders.get(market_id, {}).get(side_key)

        if existing is not None:
            if existing.price == target_price:
                # Price is correct, do nothing.
                self.inform(
                    f"[ACTIVE] {side_key} {market.name} at {target_price/100:.2f} still valid."
                )
                return
            # Price has drifted, cancel the stale order.
            self.inform(
                f"[ACTIVE] {side_key} {market.name}: price changed "
                f"{existing.price/100:.2f} → {target_price/100:.2f}, reposting."
            )
            self._cancel_order(existing)
            del self._my_standing_orders[market_id][side_key]
            if not self._my_standing_orders.get(market_id):
                self._my_standing_orders.pop(market_id, None)
            # Do not post this tick; _submit_order's cancel-guard will block it,
            # and the next received_orders tick will repost.
            return

        # No existing order for this slot. The cancel-guard in _submit_order will
        # handle the case where a cancel is still in-flight for this slot.
        # [FIX 2] The redundant old_order cancel guard that appeared here has been
        # removed: at this point existing is always None (we returned above if it was
        # not), so the guard could never trigger. _submit_order already enforces it.

        # Validate and submit.
        order = self._make_order(market, side, target_price)
        if self.get_potential_performance([order]) <= current_performance:
            # This order would not improve performance, do not post.
            return

        if side == OrderSide.BUY:
            if self._holdings.cash_available < target_price:
                self.inform(
                    f"[ACTIVE BUY] {market.name}: insufficient cash "
                    f"(need {target_price/100:.2f}, "
                    f"available {self._holdings.cash_available/100:.2f}). Skipping."
                )
                return
        else:
            asset = self._holdings.assets.get(market)
            if asset is None or not asset.can_sell or asset.units_available < 1:
                self.inform(f"[ACTIVE SELL] {market.name}: no units available to sell.")
                return

        self.inform(f"[ACTIVE] Posting {side_key} {market.name} at {target_price/100:.2f}")
        self._submit_order(order)


    def _cancel_active_slot(self, market_id: int, side: OrderSide, reason: str = ""):
        """
        Cancel an existing ACTIVE standing order for (market_id, side) if one exists.
        Used when a market-side condition makes our standing order stale.
        """
        existing = self._my_standing_orders.get(market_id, {}).get(side.name)
        if existing is None:
            return
        # [FIX 3] Do not send a CANCEL for an order that is still pending confirmation.
        # Such orders have no valid fm_id yet; cancelling them produces a server rejection.
        # _submit_order's pending-guard already prevents reposting on this slot, so
        # we simply leave it alone until it is confirmed.
        if (market_id, side.name) in self._pend_orders:
            return
        mkt_name = existing.market.name
        self.inform(
            f"[ACTIVE] Cancelling stale {side.name} in {mkt_name} "
            f"at {existing.price/100:.2f} ({reason})."
        )
        self._cancel_order(existing)
        del self._my_standing_orders[market_id][side.name]
        if not self._my_standing_orders.get(market_id):
            self._my_standing_orders.pop(market_id, None)



    # ---------- Cash Shortage ----------

    def _handle_cash_shortage(self, buy_market: Market, buy_price: int, buy_order: Order, current_performance: float) -> bool:
        """
        Attempt to raise cash by finding the best combination of assets to sell at their
        current live market bids, so that (sells + intended buy) together improve performance.

        Strategy:
        - Collect all markets (excluding buy_market) where we hold sellable units and there is a live best bid.
        - Enumerate all non-empty subsets of those candidates.
        - For each subset, check if combined cash raised >= shortfall needed to cover buy_price.
        - Evaluate get_potential_performance([sell_1, ..., sell_n, buy_order]) for each feasible subset.
        - Pick the subset with highest performance that is strictly > current_performance.
        - Submit only the FIRST sell order of the winning combo this tick, then submit other sells once the first is accepted.

        :param buy_market: The market we want to buy into.
        :param buy_price: The price of the intended buy order (cents).
        :param buy_order: The Order object for the intended buy.
        :param current_performance: Baseline performance before any trades.
        :return: True if a sell order was submitted, False otherwise.
        """
        from itertools import combinations

        shortfall = buy_price - self._holdings.cash_available
        self.inform(
            f"[Cash Shortage] Want to buy {buy_market.name} at {buy_price/100:.2f}. "
            f"Available: {self._holdings.cash_available/100:.2f}, "
            f"Shortfall: {shortfall/100:.2f}"
        )

        # --- Build list of candidate sell positions: (market, sell_order, bid_price) ---
        candidates: list[tuple[Market, Order, int]] = []
        for mkt in self.markets.values():
            if mkt.fm_id == buy_market.fm_id:
                continue
            if mkt.fm_id in self._my_standing_orders:
                continue
            asset = self._holdings.assets.get(mkt)
            if asset is None or not asset.can_sell or asset.units_available < 1:
                continue
            entry = self._orderbook.get(mkt.fm_id)
            if not entry:
                continue
            bid = entry["bid"]
            if bid is None:
                continue  # Only sell at live market bids.
            sell_order = self._make_order(mkt, OrderSide.SELL, bid)
            candidates.append((mkt, sell_order, bid))

        if not candidates:
            self.inform("[Cash Shortage] No sellable assets with live bids. Skipping trade.")
            return False

        # --- Enumerate all non-empty subsets, find best feasible combination ---
        best_perf = current_performance
        best_combo: list[tuple[Market, Order, int]] | None = None

        for size in range(1, len(candidates) + 1):
            for combo in combinations(candidates, size):
                cash_raised = sum(bid for _, _, bid in combo)
                if cash_raised < shortfall:
                    continue  # This combo doesn't raise enough cash

                sell_orders = [o for _, o, _ in combo]
                perf = self.get_potential_performance(sell_orders + [buy_order])
                if perf > best_perf:
                    best_perf = perf
                    best_combo = list(combo)

        if best_combo is None:
            self.inform("[Cash Shortage] No combination of sells improves performance, skip trade.")
            return False

        # --- Log the winning combo and submit the first sell order ---
        combo_desc = ", ".join(f"{mkt.name}@{bid/100:.2f}" for mkt, _, bid in best_combo)
        self.inform(
            f"[Cash Shortage] Best combo: SELL [{combo_desc}] "
            f"then BUY {buy_market.name} at price {buy_price/100:.2f}. "
            f"Expected performance: {best_perf:.4f}"
        )

        first_mkt, first_sell, first_price = best_combo[0]
        self.inform(f"[Cash Shortage] Submitting first sell: {first_mkt.name} at {first_price/100:.2f}")

        return self._submit_order(first_sell)


if __name__ == "__main__":
    capm_bot = CAPMBot(FM_ACCOUNT, FM_EMAIL, FM_PASSWORD, FM_MARKETPLACE_ID, RISK_PENALTY)
    capm_bot.run()