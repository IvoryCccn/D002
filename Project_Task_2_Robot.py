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
    _my_standing_orders: dict[int, dict[str, Order]]
    _pending_order: bool

    # Check whether value has changed. If changed, log.
    _last_logged_performance: float | None
    _last_logged_holdings: dict | None
    _last_logged_standing_orders: dict | None
    _last_logged_marginal_values: dict | None

    def __init__(self, account: str, email: str, password: str, marketplace_id: int, risk_penalty: float, bot_name: str = "CAPMBot"):
        super().__init__(account, email, password, marketplace_id, name=bot_name)
        self._risk_penalty = risk_penalty
        self._payoffs = {}

        self._holdings = None
        self._orderbook = {}
        self._my_standing_orders = {}
        self._pending_order = False

        self._last_logged_performance = None
        self._last_logged_holdings = None
        self._last_logged_standing_orders = None
        self._last_logged_marginal_values = {}

    def initialised(self):
        """
        Extract payoff distribution for each asset
        """
        for market in self.markets.values():
            asset_id = market.fm_id
            description = market.description
            self._payoffs[asset_id] = [int(payoff) for payoff in description.split(",")]

        self.inform("Bot initialised, I have the payoffs for the states.")



    # ---------- Order helpers ----------

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
        """Send an order if no other order is currently pending. Returns True if sent."""
        if self._pending_order:
            self.inform("Waiting for pending order, skip.")
            return False
        self._pending_order = True
        self.send_order(order)
        # Record this order to prevent information delay and duplicated order
        market_id = order.market.fm_id
        if market_id not in self._my_standing_orders:
            self._my_standing_orders[market_id] = {}
        self._my_standing_orders[market_id][order.order_side.name] = order
        return True

    def _cancel_all_my_orders_in_market(self, market_id: int):
        """Cancel every standing order I have in a given market."""
        sides = self._my_standing_orders.get(market_id, {})
        for order in list(sides.values()):
            self._cancel_order(order)
        if market_id in self._my_standing_orders:
            del self._my_standing_orders[market_id]

    def _sell_blocked_check(self, market: Market):
        asset = self._holdings.assets.get(market)

        if asset is None:
            self.inform(f"[SELL blocked] {market.name}: No asset position found.")
            return True

        if not asset.can_sell:
            self.inform(f"[SELL blocked] {market.name}: Asset cannot be sold.")
            return True

        if asset.units_available < 1:
            self.inform(f"[SELL blocked] {market.name}: No available units (available={asset.units_available}).")
            return True

        return False



    # ---------- Performance and valuation ----------

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
    


    # ---------- Auto functions ----------

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

        current_holdings = {
            "cash": holdings.cash,
            "assets": {mkt.fm_id: asset.units for mkt, asset in holdings.assets.items()}
        }

        # Nothing changed, skip printing
        if current_holdings == self._last_logged_holdings:
            return

        # Holdings changed, continue printing
        self._last_logged_holdings = current_holdings

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

        self._execute_trading_strategy()

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
        self._execute_trading_strategy()

    def order_accepted(self, order: Order):
        self._pending_order = False
        self.inform(f"Order accepted in [{order.market.name}]: fm_id={order.fm_id}, side={order.order_side.name}, price={order.price}, traded={order.has_traded}")
        

    def order_rejected(self, info: dict[str, str], order: Order):
        self._pending_order = False
        self.warning(f"Order rejected in [{order.market.name}]: order={order} info={info}")

        market_id = order.market.fm_id
        side_key = order.order_side.name
        if market_id in self._my_standing_orders:
            tracked = self._my_standing_orders[market_id].get(side_key)
            if tracked is not None and (tracked.fm_id == order.fm_id or tracked.fm_id is None):
                del self._my_standing_orders[market_id][side_key]
                if not self._my_standing_orders[market_id]:
                    del self._my_standing_orders[market_id]
                self.inform(f"Remove rejected order from local tracking for {order.market.name} [{side_key}]")


    # ---------- Orderbook functions ----------

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
        Reconcile _my_standing_orders with live orders from Order.my_current().
        
        Rules:
        - In each market, at most one BUY and one SELL order, never both.
        - If the live orderbook in each market shows more than one order per side, keep 
          only the most recently one and cancel extras.
        - Each order must be 1 unit. If units > 1, cancel.
        """
        live: dict[int, dict[str, list[Order]]] = {}

        # ----- Check -----
        for order in Order.my_current().values():
            if order.order_type is not OrderType.LIMIT: 
                continue
            
            # Cancel if order units > 1
            if order.units != 1:
                self.inform(
                    f"[Update] Cancelling order with units={order.units} "
                    f"in {order.market.name} at {order.price/100:.2f}"
                )
                self._cancel_order(order)
                continue

            mid = order.market.fm_id
            side_key = order.order_side.name
            if mid not in live:
                live[mid] = {}
            live[mid].setdefault(side_key, []).append(order)

        # Cancel duplicated order in same side and same market
        for mid, sides in live.items():
            for side_key, order_list in sides.items():
                if len(order_list) > 1:
                    # Keep the one already tracked if possible, cancel the rest
                    tracked = self._my_standing_orders.get(mid, {}).get(side_key)
                    keep = tracked if tracked and any(o.fm_id == tracked.fm_id for o in order_list) else order_list[0]
                    for o in order_list:
                        if o.fm_id != keep.fm_id:
                            self.inform(f"[Update] Cancelling duplicate {side_key} in {o.market.name} at {o.price/100:.2f}")
                            self._cancel_order(o)
                    sides[side_key] = [keep]
        
        # Cancel if both BUY and SELL order exist in same market
        for mid, sides in live.items():
            if "BUY" in sides and "SELL" in sides:
                # Cancel both; strategy will re-evaluate
                for side_key, order_list in sides.items():
                    o = order_list[0]
                    self.inform(f"[Update] Cancelling conflicting {side_key} in {o.market.name}")
                    self._cancel_order(o)
                live[mid] = {}

        # Remove markets no longer live
        for mid in list(self._my_standing_orders.keys()):
            if mid not in live or not live[mid]:
                self.inform(f"[Update] Order in market {mid} no longer live, removing.")
                del self._my_standing_orders[mid]

        # ----- Collect -----
        # Update/add from live
        for mid, sides in live.items():
            if not sides:
                if mid in self._my_standing_orders:
                    del self._my_standing_orders[mid]
                continue
            self._my_standing_orders[mid] = {sk: ol[0] for sk, ol in sides.items() if ol}
 
        # ----- Print -----
        current_snapshot = {
            mid: {sk: (o.order_side.name, o.price)
                  for sk, o in sides.items()}
            for mid, sides in self._my_standing_orders.items()
        }
        if current_snapshot == self._last_logged_standing_orders:
            return
        self._last_logged_standing_orders = current_snapshot
 
        lines = ["\n[Changed] My standing orders:"]
        lines.append(f"{'market_id':<16} {'name':<16} {'side':<6} {'price':>10}")
        lines.append("-" * 54)
        for mid, sides in sorted(self._my_standing_orders.items()):
            for sk, o in sides.items():
                lines.append(f"{mid:<16} {o.market.name:<16} {sk:<6} {o.price/100:>10.2f}")
        if not self._my_standing_orders:
            lines.append("None.")
        self.inform("\n".join(lines))



    # ---------- Trading Strategy ----------
            
    def _execute_trading_strategy(self):
        """
        Main strategy function. Called on every orderbook change and every holdings change.

        Switch robot type based on portfolio performance.
            1. No market orders at all → DEACTIVE (cancel all my orders, do nothing).
            2. is_portfolio_optimal() == False → REACTIVE (take profitable market orders).
            3. is_portfolio_optimal() == True  → ACTIVE (post limit orders at marginal value).
        """
        if self._holdings is None:
            return
        
        current_performance = self.get_potential_performance([])

        # Log performance change
        if current_performance != self._last_logged_performance:
            self._last_logged_performance = current_performance
            lines = [f"\n--- Performance changed: {current_performance:.4f} ---"]
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
        
        # ----- 1) Reactive model: market has not any standing order -----
        has_market_orders = any(
            entry["bid"] is not None or entry["ask"] is not None
            for entry in self._orderbook.values()
        )
        if not has_market_orders:
            self.inform("No standing orders in markets. Robot is [DEACTIVE].")
            for mid in list(self._my_standing_orders.keys()):
                self._cancel_all_my_orders_in_market(mid)
            return
        
        # ----- 2) Reactive model: market has standing order & portfolio not optimal -----
        if not self.is_portfolio_optimal():
            self.inform("Robot type switch to [REACTIVE]")
            self._run_reactive(current_performance)

        # ----- 3) Activate robot: market has standing order & portfolio optimal -----        
        else:
            self.inform("Robot type switch to [ACTIVE]")
            self._run_active(current_performance) 


    def _run_reactive(self, current_performance: float):
        """
        REACTIVE mode: scan every market for a direct trade that 
        improves performance and execute the first one found.

        Rules:
        For each market:
           - If there is a standing order on the same side as the trade I want
             to make, cancel it first because a better price exist.
           - BUY at best ask if it improves performance and I have the cash.
           - SELL at best bid if it improves performance and I hold the asset.
        """
        for market in self.markets.values():

            entry = self._orderbook.get(market.fm_id)

            if not entry:
                continue

            ask = entry["ask"]
            if ask and self._try_trade(market, OrderSide.BUY, ask, 
                                       current_performance, reactive=True):
                return

            bid = entry["bid"]
            if bid and self._try_trade(market, OrderSide.SELL, bid, 
                                       current_performance, reactive=True):
                return
    
    def _run_active(self, current_performance: float):
        """
        ACTIVE mode: portfolio is locally optimal and post LIMIT 
        orders just inside the marginal value.

        Rules:
        For each market, per side (BUY / SELL):
            1. If a market order has appeared that is directly profitable to take, cancel 
               any standing order on that side and switch to REACTIVE MODE immediately.
            2. Otherwise, if the side has no other-participant order, check whether
               I already have a standing order at the correct marginal value price.
               - If yes and price matches → leave it.
               - If yes but price has drifted (holdings changed) → cancel and repost.
               - If no standing order → post a new one.
        """
        for market in self.markets.values():
            market_id = market.fm_id
            entry = self._orderbook.get(market_id)
 
            # Compute marginal value for this market and log if changed
            margin_value = self._compute_marginal_value(market)
            margin_price = int(round(margin_value / market.price_tick)) * market.price_tick
            margin_price = max(market.min_price, min(market.max_price, margin_price))

            ask = entry["ask"] if entry else None
            bid = entry["bid"] if entry else None

            buy_price = max(market.min_price, margin_price - market.price_tick)
            sell_price = min(market.max_price, margin_price + market.price_tick)
            
            # ----- Special case: Negative marginal value -----
            # Negative marginal value means this asset is over-weighted.
            # Only consider selling so skip the BUY logic entirely.
            if margin_value <= 0:
                self.inform(f"{market.name} has negative marginal value {margin_value/100:.2f}. Only consider selling.")
                sell_price_neg = max(market.min_price, margin_price)
                existing_sell_neg = self._my_standing_orders.get(market_id, {}).get(OrderSide.SELL.name)
                if existing_sell_neg is not None:
                    if existing_sell_neg.price == sell_price_neg:
                        self.inform(
                            f"[ACTIVE] Standing SELL (neg margin) in {market.name} at "
                            f"{existing_sell_neg.price/100:.2f} still valid, skipping."
                        )
                        continue
                    else:
                        self.inform(
                            f"[ACTIVE] Neg-margin SELL price changed for {market.name}: "
                            f"old {existing_sell_neg.price/100:.2f} → new {sell_price_neg/100:.2f}, reposting."
                        )
                        self._cancel_order(existing_sell_neg)
                        del self._my_standing_orders[market_id][OrderSide.SELL.name]
                        if not self._my_standing_orders.get(market_id):
                            self._my_standing_orders.pop(market_id, None)

                if self._try_trade(market, OrderSide.SELL, sell_price_neg, current_performance):
                    return
                continue

            # ----- Special case: Market price already better than marginal value -----
            if ask is not None and ask <= margin_value:
                if self._try_trade( market, OrderSide.BUY, ask, current_performance):
                    return

            if bid is not None and bid >= margin_value:
                if self._try_trade( market, OrderSide.SELL, bid, current_performance):
                    return

            # ----- Normal case -----
            if ask is None:
                existing_buy = self._my_standing_orders.get(market_id, {}).get(OrderSide.BUY.name)
                if existing_buy is not None:
                    if existing_buy.price == buy_price:
                        self.inform(
                            f"[ACTIVE] Standing BUY in {market.name} at price "
                            f"{existing_buy.price/100:.2f} still valid, skipping."
                        )
                        continue

                    self.inform(
                        f"[ACTIVE] BUY price changed for {market.name}: "
                        f"{existing_buy.price/100:.2f} → {buy_price/100:.2f}, reposting."
                    )

                    self._cancel_order(existing_buy)
                    del self._my_standing_orders[market_id][OrderSide.BUY.name]
                    if not self._my_standing_orders.get(market_id):
                        self._my_standing_orders.pop(market_id, None)
                else:
                    self.inform(
                        f"[ACTIVE] Posting new BUY in {market.name} at price {buy_price/100:.2f}"
                    )

                if self._try_trade(market, OrderSide.BUY, buy_price, current_performance):
                    return

            if bid is None:
                existing_sell = self._my_standing_orders.get(market_id, {}).get(OrderSide.SELL.name)
                if existing_sell is not None:
                    if existing_sell.price == sell_price:
                        self.inform(
                            f"[ACTIVE] Standing SELL in {market.name} at "
                            f"{existing_sell.price/100:.2f} still valid, skipping."
                        )
                        continue

                    self.inform(
                        f"[ACTIVE] SELL price changed for {market.name}: "
                        f"{existing_sell.price/100:.2f} → {sell_price/100:.2f}, reposting."
                    )

                    self._cancel_order(existing_sell)
                    del self._my_standing_orders[market_id][OrderSide.SELL.name]
                    if not self._my_standing_orders.get(market_id):
                        self._my_standing_orders.pop(market_id, None)
                else:
                    self.inform(
                        f"[ACTIVE] Posting new SELL in {market.name} at price {sell_price/100:.2f}"
                    )

                if self._try_trade(market, OrderSide.SELL, sell_price, current_performance):
                    return

    def _try_trade(self, market: Market, side: OrderSide, price: int, 
                   current_performance: float, reactive: bool = False) -> bool:
        """
        Unified trading function used by both ACTIVE and REACTIVE modes.

        Handles:
        - performance improvement check
        - cancel conflicting standing order
        - cash / asset validation
        - order submission

        Returns True if an order was submitted.
        """

        market_id = market.fm_id
        order = self._make_order(market, side, price)

        # Performance check
        if self.get_potential_performance([order]) <= current_performance:
            return False

        # SELL asset check
        if side == OrderSide.SELL:
            asset = self._holdings.assets.get(market)
            if asset is None:
                self.inform(f"[SELL blocked] {market.name}: No asset position.")
                return False
            if not asset.can_sell:
                self.inform(f"[SELL blocked] {market.name}: Cannot sell.")
                return False
            if asset.units_available < 1:
                self.inform(f"[SELL blocked] {market.name}: No units available.")
                return False

        # Cancel my standing order if exists
        existing = self._my_standing_orders.get(market_id, {}).get(side.name)
        if existing is not None:
            mode = "REACTIVE" if reactive else "ACTIVE→REACTIVE"
            self.inform(
                f"[{mode}] Cancelling standing {side.name} in {market.name} "
                f"at {existing.price/100:.2f}"
            )
            self._cancel_order(existing)
            del self._my_standing_orders[market_id][side.name]
            if not self._my_standing_orders.get(market_id):
                self._my_standing_orders.pop(market_id, None)

        # BUY cash check
        if side == OrderSide.BUY:
            if self._holdings.cash_available >= price:
                self.inform(f"[{side.name}] {market.name} at {price/100:.2f}")
                return self._submit_order(order)
            self.inform(f"Insufficient cash: need {price/100:.2f} have {self._holdings.cash_available/100:.2f}")
            return self._handle_cash_shortage(market, price, order, current_performance)

        # SELL submit
        self.inform(f"[SELL] {market.name} at {price/100:.2f}")
        return self._submit_order(order)



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
                continue  # Only sell at live market bids — no guessing
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
            self.inform("[Cash Shortage] No combination of sells improves performance. Skipping trade.")
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
