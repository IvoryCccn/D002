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
    _robot_type: BotType | None
    _pending_order: bool

    def __init__(self, account: str, email: str, password: str, marketplace_id: int, risk_penalty: float, bot_name: str = "CAPMBot"):
        super().__init__(account, email, password, marketplace_id, name=bot_name)
        self._risk_penalty = risk_penalty
        self._payoffs = {}

        self._holdings = None
        self._orderbook = {}
        self._my_standing_orders = {}
        self._robot_type = None
        self._pending_order = False

    def initialised(self):
        """
        Extract payoff distribution for each asset
        """
        for market in self.markets.values():
            asset_id = market.fm_id
            description = market.description
            self._payoffs[asset_id] = [int(payoff) for payoff in description.split(",")]

        self.inform("Bot initialised, I have the payoffs for the states.")

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

        self.inform(
            f"\nPotential performance: "
            f"\nExpected payoff = {expected_payoff:.4f}, "
            f"\nVariance = {variance:.4f}, "
            f"\nPerformance = {performance:.4f}"
        )

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
                buy_order = Order.create_new(market)
                buy_order.market = market
                buy_order.order_type = OrderType.LIMIT
                buy_order.order_side = OrderSide.BUY
                buy_order.price = ask
                buy_order.units = 1
                if self.get_potential_performance([buy_order]) > current_performance:
                    self.inform(
                        f"Portfolio now not optimal: BUY 1 unit of {market.name} "
                        f"at ask price=[{ask / 100:.2f}] improves performance."
                    )
                    return False

            # Check sell at best bid
            bid = entry["bid"]
            if bid is not None:
                sell_order = Order.create_new(market)
                sell_order.market = market
                sell_order.order_type = OrderType.LIMIT
                sell_order.order_side = OrderSide.SELL
                sell_order.price = bid
                sell_order.units = 1
                sell_order.mine = True
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

        lines = [f"\nCurrent holdings in my account:\n"]
        lines.append(f"{'Account':<12} {'Total':>12} {'Available':>12}")
        lines.append("-" * 40)
        lines.append(f"{'Cash':<12} {holdings.cash / 100:>12.2f} {holdings.cash_available / 100:>12.2f}")

        for market, asset in holdings.assets.items():
            lines.append(f"{market.name:<12} {asset.units:>12} {asset.units_available:>12}")

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
        self._orderbook = {}

        # Collect
        for order in Order.current().values():
            if order.order_type is not OrderType.LIMIT:
                continue
            if order.mine:
                continue

            market_id = order.market.fm_id

            if market_id not in self._orderbook:
                self._orderbook[market_id] = {
                    "name": order.market.name,
                    "bid": None,
                    "ask": None,
                }

            entry = self._orderbook[market_id]

            # Update best bid
            if order.order_side == OrderSide.BUY:
                if entry["bid"] is None or order.price > entry["bid"]:
                    entry["bid"] = order.price

            # Update best ask
            elif order.order_side == OrderSide.SELL:
                if entry["ask"] is None or order.price < entry["ask"]:
                    entry["ask"] = order.price

        # Print
        lines = ["\nCurrent best standing order:\n"]
        lines.append(f"{'market_id':<16} {'market_name':<12} {'bid':>8} {'ask':>12}")
        lines.append("-" * 54)

        for market_id in sorted(self._orderbook.keys()):
            entry = self._orderbook[market_id]

            bid = f"{entry['bid'] / 100:.2f}" if entry["bid"] is not None else "-"
            ask = f"{entry['ask'] / 100:.2f}" if entry["ask"] is not None else "-"

            lines.append(
                f"{market_id:<16} "
                f"{entry['name']:<12} "
                f"{bid:>8} "
                f"{ask:>12}"
            )

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

        # Add / update any orders that appeared
        for market_id, order in live_orders.items():
            self._my_standing_orders[market_id] = order
        
        # ----- Print -----
        lines = [f"\nMy standing orders:\n"]
        lines.append(f"{'market_id':<16} {'market_name':<16} {'side':<4} {'price':>12}")
        lines.append("-" * 54)

        for market_id, order in sorted(self._my_standing_orders.items()):
            lines.append(
                f"{market_id:<16} "
                f"{order.market.name:<16} "
                f"{order.order_side.name:<4} "
                f"{order.price / 100:>12.2f}"
            )

        if not self._my_standing_orders:
            lines.append("No standing LIMIT orders.")

        self.inform("\n".join(lines))

    def _cancel_unprofitable_standing_orders(self):
        """
        Reviews all active standing orders and cancels any that no longer improve performance,
        handling cases where market conditions have changed since the order was placed.
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
                cancel_order = Order.create_new(standing.market)
                cancel_order.fm_id = standing.fm_id
                cancel_order.order_type = OrderType.CANCEL
                cancel_order.order_side = standing.order_side
                cancel_order.price = standing.price
                cancel_order.units = standing.units
                cancel_order.mine = True

                self.send_order(cancel_order)
    
    def _execute_trading_strategy(self):
        """
        Switch robot type based on portfolio performance.
            - DEACTIVE → no standing orders in market, do nothing.
            - REACTIVE → standing orders exist, portfolio not optimal, take profitable orders.
            - ACTIVE   → standing orders exist, portfolio is optimal, post limit orders.
        """
        current_performance = self.get_potential_performance([])
        
        # ----- 1) Reactivate model: market has not any standing order -----
        has_market_orders = any(
            entry["bid"] is not None or entry["ask"] is not None
            for entry in self._orderbook.values()
        )
        if not has_market_orders:
            self.inform("No standing orders in market. Robot is [DEACTIVE].")
            for market_id, my_order in list(self._my_standing_orders.items()):
                cancel_order = Order.create_new(my_order.market)
                cancel_order.fm_id = my_order.fm_id
                cancel_order.order_type = OrderType.CANCEL
                cancel_order.order_side = my_order.order_side
                cancel_order.price = my_order.price
                cancel_order.units = my_order.units
                cancel_order.mine = True
                self.send_order(cancel_order)
                return
        
        # ----- 2) Reactivate model: market has standing order & portfolio not optimal -----
        if not self.is_portfolio_optimal():
            self._robot_type = BotType.REACTIVE
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

                    if self._holdings.cash_available < ask:
                        self.inform(f"Insufficient cash: need {ask} have {self._holdings.cash_available}")
                        if self._handle_cash_shortage(ask, current_performance):
                            return
                    else:
                        buy_order = Order.create_new(market)
                        buy_order.order_type = OrderType.LIMIT
                        buy_order.order_side = OrderSide.BUY
                        buy_order.price = ask
                        buy_order.units = 1
                        buy_order.mine = True

                        if self.get_potential_performance([buy_order]) > current_performance:
                            if self._pending_order:
                                self.inform("Waiting for pending order, skip")
                            else:
                                self.inform(f"REACTIVE BUY on {market.name} at price {ask / 100:.2f}")
                                self._pending_order = True
                                self.send_order(buy_order)
                                # Record this order to prevent information delay and duplicated order
                                self._my_standing_orders[market_id] = buy_order
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
                        self.inform(
                            f"[SELL blocked] {market.name}: "
                            f"Insufficient available units (available={asset.units_available})."
                        )

                    else:
                        sell_order = Order.create_new(market)
                        sell_order.order_type = OrderType.LIMIT
                        sell_order.order_side = OrderSide.SELL
                        sell_order.price = bid
                        sell_order.units = 1
                        sell_order.mine = True

                        if self.get_potential_performance([sell_order]) > current_performance:
                            if self._pending_order:
                                self.inform("Waiting for pending order, skip")
                            else:
                                self.inform(f"REACTIVE SELL on {market.name} at price {bid / 100:.2f}")
                                self._pending_order = True
                                self.send_order(sell_order)
                                # Record this order to prevent information delay and duplicated order
                                self._my_standing_orders[market_id] = sell_order
                                return
        
        #  ----- 3) Activate robot: market has standing order & portfolio optimal -----
        else:
            self._robot_type = BotType.ACTIVE
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
                    margin_price = market.min_price
                    if entry.get("ask") is None:
                        sell_price = market.min_price
                        asset = self._holdings.assets.get(market)
                        if asset and asset.can_sell and asset.units_available >= 1:
                            candidate = Order.create_new(market)
                            candidate.order_type = OrderType.LIMIT
                            candidate.order_side = OrderSide.SELL
                            candidate.price = sell_price
                            candidate.units = 1
                            candidate.mine = True
                            if self.get_potential_performance([candidate]) >= current_performance:
                                self.inform(f"ACTIVE SELL (neg margin value) on {market.name} at {sell_price/100:.2f}")
                                self._pending_order = True
                                self.send_order(candidate)
                                self._my_standing_orders[market_id] = candidate
                                return
                    continue

                # ----- Normal Case -----
                margin_price = int(round(margin_value / market.price_tick)) * market.price_tick
                margin_price = max(market.min_price, min(market.max_price, margin_price))
                
                # Send buy order just below indifference price
                if entry.get("bid") is None:
                    buy_price = int(max(market.min_price, margin_price - market.price_tick))
                    if self._holdings.cash_available < buy_price:
                        self.inform(f"Insufficient cash: need {buy_price} have {self._holdings.cash_available}")
                        if self._handle_cash_shortage(buy_price, current_performance):
                            return
                    else:
                        candidate = Order.create_new(market)
                        candidate.order_type = OrderType.LIMIT
                        candidate.order_side = OrderSide.BUY
                        candidate.price = buy_price
                        candidate.units = 1
                        candidate.mine = True

                        if self.get_potential_performance([candidate]) > current_performance:
                            self.inform(f"ACTIVE BUY limit on {market.name} at price {buy_price / 100:.2f}")
                            self._pending_order = True
                            self.send_order(candidate)
                            self._my_standing_orders[market_id] = candidate
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
                        self.inform(
                            f"[SELL blocked] {market.name}: "
                            f"Insufficient available units (available={asset.units_available})."
                        )

                    else:
                        candidate = Order.create_new(market)
                        candidate.order_type = OrderType.LIMIT
                        candidate.order_side = OrderSide.SELL
                        candidate.price = sell_price
                        candidate.units = 1
                        candidate.mine = True

                        if self.get_potential_performance([candidate]) > current_performance:
                            self.inform(f"ACTIVE SELL limit on {market.name} at price {sell_price / 100:.2f}")
                            self._pending_order = True
                            self.send_order(candidate)
                            self._my_standing_orders[market_id] = candidate
                            return

    def _compute_marginal_value(self, market: Market) -> float:
        """
        Return the marginal value (indifference price) of asset in cents.

        Definition: marginal valus is the price X (cents) at which buying 
        1 extra unit of this asset leaves portfolio performance exactly unchanged.

        original_performance = E[original_payoff] - b × Var[original]
        new_performance = (E[original_payoff] + E[asset's payoff]/100 - X/100) - b × Var[original + 1 unit of asset]

        original_performance = new_performance -> get X = (ΔE - b × ΔVar) * 100
        where ΔE = E[asset's payoff]/100, ΔVar = Var[original + 1 unit of asset] - Var[original]
        
        :param market: the Market whose asset's marginal value we want.
        :return: marginal value in cents (float).
        """

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

        self.inform(f"[{market.name}]: Marginal value={margin_value / 100:.2f}")

        return margin_value

    def _handle_cash_shortage(self, needed_cash: int, current_performance: float) -> bool:
        """
        Attempt to raise cash by selling held assets when funds are insufficient.

        :param needed_cash: Amount of cash to raise (in cents).
        :param current_performance: Current portfolio performance, used as the baseline for sell decisions.
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

            sell_order = Order.create_new(mkt)
            sell_order.order_type = OrderType.LIMIT
            sell_order.order_side = OrderSide.SELL
            sell_order.price = price
            sell_order.units = 1
            sell_order.mine = True

            perf = self.get_potential_performance([sell_order])
            if perf > best_sell_performance:
                best_sell_performance = perf
                best_sell_candidate = (mkt, sell_order, price)

        if best_sell_candidate is not None:
            mkt, sell_order, price = best_sell_candidate
            self.inform(f"[Cash Shortage] SELL {mkt.name} at {price/100:.2f} to raise cash.")
            if not self._pending_order:
                self._pending_order = True
                self.send_order(sell_order)
                self._my_standing_orders[mkt.fm_id] = sell_order
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
