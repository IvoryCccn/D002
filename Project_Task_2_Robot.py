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
    _my_standing_orders: dict[int, Order]
    _current_performance: float | None
    _pending_order: bool

    # Cached snapshots for change-detection logging
    _last_logged_performance: float | None
    _last_logged_holdings_snapshot: dict | None
    _last_logged_standing_orders_snapshot: dict | None
    _last_logged_marginal_values: dict | None

    # Cash shortage plan: queued sells + the buy that triggered the shortage
    _cash_shortage_plan: dict | None

    def __init__(self, account: str, email: str, password: str, marketplace_id: int, risk_penalty: float, bot_name: str = "CAPMBot"):
        super().__init__(account, email, password, marketplace_id, name=bot_name)
        self._risk_penalty = risk_penalty
        self._payoffs = {}

        self._holdings = None
        self._orderbook = {}
        self._my_standing_orders = {}
        self._current_performance = None
        self._pending_order = False

        self._last_logged_performance = None
        self._last_logged_holdings_snapshot = None
        self._last_logged_standing_orders_snapshot = None
        self._last_logged_marginal_values = {}

        self._cash_shortage_plan = None

    def initialised(self):
        """
        Extract payoff distribution for each asset
        """
        for market in self.markets.values():
            asset_id = market.fm_id
            description = market.description
            self._payoffs[asset_id] = [int(payoff) for payoff in description.split(",")]

        self.inform("Bot initialised, I have the payoffs for the states.")

    def _make_order(self, market: Market, order_side: OrderSide, price: int, units: int = 1) -> Order:
        """Create a LIMIT order without sending it."""
        order = Order.create_new(market)
        order.order_type = OrderType.LIMIT
        order.order_side = order_side
        order.price = price
        order.units = units
        order.mine = True
        return order
    
    def _cancel_order(self, order: Order):
        """Cancel an existing standing order by referencing it directly."""
        cancel = Order.create_new(order.market)
        cancel.order_type = OrderType.CANCEL
        cancel.order_side = order.order_side
        cancel.price = order.price
        cancel.units = order.units
        cancel.mine = True
        cancel.fm_id = order.fm_id
        self.send_order(cancel)
    
    def _submit_order(self, order: Order) -> bool:
        """
        Send an order if no other order is currently pending.
        Records the order in _my_standing_orders on success.

        :param order: Order to send.
        :return: True if submitted, False if blocked by a pending order.
        """
        if self._pending_order:
            self.inform("Waiting for pending order, skip.")
            return False
        self._pending_order = True
        self.send_order(order)
        # Record this order to prevent information delay and duplicated order
        self._my_standing_orders[order.market.fm_id] = order
        return True

    def get_potential_performance(self, orders: list[Order]) -> float:
        """
        Returns the portfolio performance if all the given list of orders is executed based on current holdings.
        Performance = E[Payoff] − b × Var[Payoff], where b is the penalty for risk.
        All monetary values are stored in cents; arithmetic is done in dollars.
        
        :param orders: list of orders
        :return: performance (float)
        """
        # Collect my assets' unit in each market  
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

        # Compute state payoffs across 4 states in dollars
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

    def _log_performance_if_changed(self):
        """
        Print performance + current holdings only when performance has changed
        since the last time it was logged. This avoids flooding the log with
        identical lines on every orderbook tick.
        """
        self._current_performance = self.get_potential_performance([])
        if self._current_performance == self._last_logged_performance:
            return

        self._last_logged_performance = self._current_performance

        # Build holdings snapshot string
        lines = [f"\n--- Performance changed: {self._current_performance:.4f} ---"]
        lines.append(f"{'Account':<12} {'Total':>10} {'Available':>12}")
        lines.append("-" * 38)
        lines.append(
            f"{'Cash':<12} "
            f"{self._holdings.cash / 100:>10.2f} "
            f"{self._holdings.cash_available / 100:>12.2f}"
        )
        for market, asset in self._holdings.assets.items():
            lines.append(f"{market.name:<12} {asset.units:>10} {asset.units_available:>12}")

        self.inform("\n".join(lines))

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

            # Check buy at best ask
            ask = entry["ask"]
            if ask is not None:
                buy_order = self._make_order(market, OrderSide.BUY, ask)
                if self.get_potential_performance([buy_order]) > current_performance:
                    self.inform(
                        f"Portfolio now not optimal: BUY 1 unit of {market.name} "
                        f"at ask price=[{ask / 100:.2f}] improves performance."
                    )
                    return False

            # Check sell at best bid
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

        # Build a comparable snapshot to detect real changes
        snapshot = {
            "cash": holdings.cash,
            "assets": {mkt.fm_id: asset.units for mkt, asset in holdings.assets.items()}
        }

        # Nothing changed, skip printing
        if snapshot == self._last_logged_holdings_snapshot:
            return

        # Holdings changed, continue printing
        self._last_logged_holdings_snapshot = snapshot

        lines = [f"\n[Changed] Current Holdings:"]
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

    def received_orders(self, orders: list[Order]):
        """
        Main event handler called whenever the order book changes.
        Workflow:
        1. Update and print best bid/ask.
        2. Update and print my standing orders.
        3. Cancel my noprofitable standing orders.
        4. Execute different trading strategy based on current performance.
        
        :param orders: Full list of current orders in the marketplace.
        """
        self._update_best_standing_orders()
        self._update_my_standing_orders()
        self._cancel_unprofitable_standing_orders()
        self._execute_trading_strategy()

    def _update_best_standing_orders(self):
        """
        Update self._orderbook from scratch using all current non-own LIMIT orders.
        """
        # ----- Collect -----
        new_orderbook: dict[int, OrderBookEntry] = {}

        for order in Order.current().values():
            if order.order_type is not OrderType.LIMIT or order.mine:
                continue

            market_id = order.market.fm_id
            if market_id not in new_orderbook:
                new_orderbook[market_id] = {"name": order.market.name, "bid": None, "ask": None}

            entry = new_orderbook[market_id]
            # Update best bid
            if order.order_side == OrderSide.BUY:
                if entry["bid"] is None or order.price > entry["bid"]:
                    entry["bid"] = order.price
            # Update best ask
            elif order.order_side == OrderSide.SELL:
                if entry["ask"] is None or order.price < entry["ask"]:
                    entry["ask"] = order.price

        # ----- Print -----
        # Nothing change, skip printing
        if new_orderbook == self._orderbook:
            return

        # Standing order changed, continue printing
        self._orderbook = new_orderbook

        lines = ["\n[Changed] Current best standing order:"]
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
        Update self._my_standing_orders with the live orders from Order.my_current().
        Removes entries for orders that are no longer active (filled or cancelled),
        and adds or updates entries for any newly appearing live orders.
        """
        # ----- Collect -----
        # Rebuild from current live orders
        live_orders: dict[int, Order] = {}
        for order in Order.my_current().values():
            if order.order_type is OrderType.LIMIT:
                live_orders[order.market.fm_id] = order

        # Remove markets whose orders are no longer live
        for market_id in list(self._my_standing_orders.keys()):
            if market_id not in live_orders:
                self.inform(f"Standing order for market {market_id} is no longer live — removing from tracking.")
                del self._my_standing_orders[market_id]

        # Add/update any orders that appeared
        for market_id, order in live_orders.items():
            self._my_standing_orders[market_id] = order
        
        # ----- Print -----
        # Build snapshot for change detection
        snapshot = {
            mid: (o.order_side.name, o.price)
            for mid, o in self._my_standing_orders.items()
        }

        # Nothing change, skip printing
        if snapshot == self._last_logged_standing_orders_snapshot:
            return

        # My standing order changed, continue printing
        self._last_logged_standing_orders_snapshot = snapshot

        lines = ["\n[Changed] My standing orders:"]
        lines.append(f"{'market_id':<16} {'market_name':<16} {'side':<4} {'price':>12}")
        lines.append("-" * 54)
        for market_id, order in sorted(self._my_standing_orders.items()):
            lines.append(
                f"{market_id:<16} {order.market.name:<16} "
                f"{order.order_side.name:<4} {order.price / 100:>12.2f}"
            )
        if not self._my_standing_orders:
            lines.append("No standing LIMIT orders.")
        self.inform("\n".join(lines))

    def _cancel_unprofitable_standing_orders(self):
        """
        Reviews all active standing orders and cancel any standing orders 
        that no longer improve performance.
        """
        current_performance = self.get_potential_performance([])

        for market_id, standing in list(self._my_standing_orders.items()):
            if self.get_potential_performance([standing]) <= current_performance:
                self.inform(
                    f"Cancelling unprofitable standing order in "
                    f"{standing.market.name}: "
                    f"{standing.order_side.name} "
                    f"at price {standing.price / 100:.2f}"
                )
                cancel_order = self._cancel_order(standing)
        return
            
    def _execute_trading_strategy(self):
        """
        Switch robot type based on portfolio performance.
            - DEACTIVE → no standing orders in market, do nothing.
            - REACTIVE → standing orders exist, portfolio not optimal, take profitable orders.
            - ACTIVE   → standing orders exist, portfolio is optimal, post limit orders.
        """
        self._log_performance_if_changed()
        current_performance = self._current_performance
        
        # ----- 1) Reactivate model: market has not any standing order -----
        has_market_orders = any(
            entry["bid"] is not None or entry["ask"] is not None
            for entry in self._orderbook.values()
        )
        if not has_market_orders:
            self.inform("No standing orders in market. Robot is [DEACTIVE].")
            for market_id, my_order in list(self._my_standing_orders.items()):
                cancel_order = self._cancel_order(my_order)
            return
        
        # ----- 2) Reactivate model: market has standing order & portfolio not optimal -----
        if not self.is_portfolio_optimal():
            self.inform("Robot type switch to [REACTIVE]")

            for market in self.markets.values():
                market_id = market.fm_id

                if market_id in self._my_standing_orders:
                    self.inform(f"[REACTIVE] Already have standing order in {market.name}, skipping.")
                    continue

                entry = self._orderbook.get(market_id)
                if not entry:
                    continue

                # Send buy order at best ask
                ask = entry["ask"]
                if ask is not None:
                    buy_order = self._make_order(market, OrderSide.BUY, ask)
                    if self.get_potential_performance([buy_order]) > current_performance:
                        if self._holdings.cash_available < ask:
                            self.inform(f"Insufficient cash: need {ask/100:.2f} have {self._holdings.cash_available/100:.2f}")
                            if self._handle_cash_shortage(ask, current_performance):
                                return
                        else:
                            self.inform(f"REACTIVE BUY on {market.name} at {ask / 100:.2f}")
                            if self._submit_order(buy_order):
                                return

                # Send sell order at best bid
                bid = entry["bid"]
                if bid is not None:
                    asset = self._holdings.assets.get(market)
                    if asset is None:
                        self.inform(f"[SELL blocked] {market.name}: No asset position found.")
                    elif not asset.can_sell:
                        self.inform(f"[SELL blocked] {market.name}: Asset cannot be sold.")
                    elif asset.units_available < 1:
                        self.inform(f"[SELL blocked] {market.name}: No available units (available={asset.units_available}).")
                    else:
                        sell_order = self._make_order(market, OrderSide.SELL, bid)
                        if self.get_potential_performance([sell_order]) > current_performance:
                            self.inform(f"REACTIVE SELL on {market.name} at price {bid / 100:.2f}")
                            if self._submit_order(sell_order):
                                return
        
        #  ----- 3) Activate robot: market has standing order & portfolio optimal -----
        else:
            self.inform("Robot type switch to [ACTIVE]")

            for market in self.markets.values():
                market_id = market.fm_id

                if market_id in self._my_standing_orders:
                    continue

                entry = self._orderbook.get(market_id, {"bid": None, "ask": None})
                
                # Compute marginal value for different assets
                margin_value = self._compute_marginal_value(market)
                
                # ----- Special Case -----
                # Only consider SELL order when margin_value is negative 
                # Negative margin_value means too much asset so BUY order is meaningless
                if margin_value <= 0:
                    if entry.get("ask") is None:
                        asset = self._holdings.assets.get(market)
                        if asset and asset.can_sell and asset.units_available >= 1:
                            candidate = self._make_order(market, OrderSide.SELL, market.min_price)
                            if self.get_potential_performance([candidate]) >= current_performance:
                                self.inform(f"ACTIVE SELL (neg margin) on {market.name} at {sell_price/100:.2f}")
                                if self._submit_order(candidate):
                                    return
                    continue

                # ----- Normal Case -----
                margin_price = int(round(margin_value / market.price_tick)) * market.price_tick
                margin_price = max(market.min_price, min(market.max_price, margin_price))
                
                # Send buy order just below indifference price
                if entry.get("bid") is None:
                    buy_price = int(max(market.min_price, margin_price - market.price_tick))
                    buy_order = self._make_order(market, OrderSide.BUY, buy_price)
                    if self.get_potential_performance([buy_order]) > current_performance:
                        if self._holdings.cash_available < buy_price:
                            self.inform(f"Insufficient cash: need {buy_price} have {self._holdings.cash_available}")
                            if self._handle_cash_shortage(buy_price, current_performance):
                                return
                        else:
                            self.inform(f"ACTIVE BUY limit on {market.name} at price {buy_price / 100:.2f}")
                            if self._submit_order(buy_order):
                                return
                
                # Send sell order just above indifferent price
                if entry.get("ask") is None and market_id not in self._my_standing_orders:
                    sell_price = int(min(market.max_price, margin_price + market.price_tick))
                    asset = self._holdings.assets.get(market)
                    if asset is None:
                        self.inform(f"[SELL blocked] {market.name}: No asset position found.")
                    elif not asset.can_sell:
                        self.inform(f"[SELL blocked] {market.name}: Asset cannot be sold.")
                    elif asset.units_available < 1:
                        self.inform(f"[SELL blocked] {market.name}: No available units (available={asset.units_available}).")
                    else:
                        candidate = self._make_order(market, OrderSide.SELL, sell_price)
                        if self.get_potential_performance([candidate]) > current_performance:
                            self.inform(f"ACTIVE SELL limit on {market.name} at price {sell_price / 100:.2f}")
                            if self._submit_order(candidate):
                                return

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
        # Only log when value has changed
        previous = self._last_logged_marginal_values.get(market.fm_id)
        if previous == margin_value:
            return margin_value
        
        self._last_logged_marginal_values[market.fm_id] = margin_value
        
        lines = ["\n[Changed] Marginal values:"]
        lines.append(f"{'market_id':<16} {'market_name':<16} {'marginal_value':>14}")
        lines.append("-" * 48)
        for mkt_id, val in sorted(self._last_logged_marginal_values.items()):
            mkt_name = next((m.name for m in self.markets.values() if m.fm_id == mkt_id), str(mkt_id))
            marker = " *" if mkt_id == market.fm_id else ""
            lines.append(f"{mkt_id:<16} {mkt_name:<16} {val / 100:>14.2f}{marker}")
        self.inform("\n".join(lines))

        return margin_value

    def _handle_cash_shortage(self, needed_cash: int, current_performance: float) -> bool:
        """
        Attempt to raise cash by selling held assets when funds are insufficient.

        :param needed_cash: Amount of cash to raise (in cents).
        :param current_performance: Current poyrtfolio performance, used as the baseline for sell decisions.
        :return: True if a sell order was successfully submitted, False if no viable candidate was found.
        """
        self.inform(f"[Cash Shortage] Trying to raise {needed_cash/100:.2f} cash. Available: {self._holdings.cash_available/100:.2f}")

        best_sell_candidate = None
        best_sell_performance = current_performance

        for mkt in self.markets.values():
            mkt_id = mkt.fm_id

            if mkt_id in self._my_standing_orders:
                continue

            asset = self._holdings.assets.get(mkt)
            if asset is None or not asset.can_sell or asset.units_available < 1:
                continue

            entry = self._orderbook.get(mkt_id)
            if not entry:
                continue

            bid = entry["bid"]
            if bid is not None:
                price = bid
            else:
                # If no bid, fall back to marginal value with a discount
                margin_value = self._compute_marginal_value(mkt)
                if margin_value <= 0:
                    continue
                margin_price = int(round(margin_value / mkt.price_tick)) * mkt.price_tick
                price = int(max(mkt.min_price, margin_price - mkt.price_tick))
            sell_order = self._make_order(mkt, OrderSide.SELL, price)
            if self.get_potential_performance([sell_order]) > best_sell_performance:
                best_sell_performance = self.get_potential_performance([sell_order])
                best_sell_candidate = (mkt, sell_order, price)

        if best_sell_candidate is not None:
            mkt, sell_order, price = best_sell_candidate
            self.inform(f"[Cash Shortage] SELL {mkt.name} at {price/100:.2f} to raise cash.")
            if self._submit_order(sell_order):
                return True

        self.inform("[Cash Shortage] No viable strategy found to raise cash. Skipping trade.")
        return False

    def order_accepted(self, order: Order):
        self._pending_order = False
        self.inform(f"Order accepted in [{order.market.name}]: fm_id={order.fm_id}, side={order.order_side.name}, price={order.price}, traded={order.has_traded}")
        

    def order_rejected(self, info: dict[str, str], order: Order):
        self._pending_order = False
        self.warning(f"Order rejected in [{order.market.name}]: order={order} info={info}")

        market_id = order.market.fm_id
        if market_id in self._my_standing_orders:
            if self._my_standing_orders[market_id].fm_id == order.fm_id or self._my_standing_orders[market_id].fm_id is None:
                del self._my_standing_orders[market_id]
                self.inform(f"Remove rejected order from local tracking for {order.market.name}")



if __name__ == "__main__":
    capm_bot = CAPMBot(FM_ACCOUNT, FM_EMAIL, FM_PASSWORD, FM_MARKETPLACE_ID, RISK_PENALTY)
    capm_bot.run()
