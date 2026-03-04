"""
根据当前市场上的买卖价格，判断如果卖出/买入某个资产能否提升组合表现
如果可以，说明组合还没有达到最优组合
如果不行，说明组合已经达到了最优组合

但一旦市场上的买卖价格发生了变化，又要重新判断

判断投资组合是否为最优组合是一种被动的判断
如果这个时候达到最优了（即被动接受市场价格达到最优）
机器人就要考虑主动出价（往有利于组合表现的方向进行交易）

主动出价：
记录并计算当前holdings下每个资产的边际价值
如果资产持仓过多，挂卖单，价格略低于边际价值
如果资产持仓过少，挂买单，价格略高于边际价值
同时监视order book
如果别人的出价比边际价值更有利，立刻吃单

如果在多个市场都有下单，一旦任意一个市场的order成交了
都要立马计算新组合的表现，判断是否需要继续其他市场的交易
如果继续其他市场交易是unprofitable的，那就取消订单
"""

import copy
import logging
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
    _robot_type: bool

    def __init__(self, account: str, email: str, password: str, marketplace_id: int, risk_penalty: float, bot_name: str = "CAPMBot"):
        super().__init__(account, email, password, marketplace_id, name=bot_name)
        self._risk_penalty = risk_penalty
        self._payoffs = {}

        self._holdings = None
        self._orderbook = {}
        self._my_standing_orders = {}
        self._robot_type = None

    def initialised(self):
        # Extract payoff distribution for each asset
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

        # Compute state payoffs across 4 states (in dollars)
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
            f"Potential performance: "
            f"Expected payoff={expected_payoff:.4f}, "
            f"Variance={variance:.4f}, "
            f"Performance={performance:.4f}"
        )

        return performance

    def is_portfolio_optimal(self) -> bool:
        """
        Returns true if the current holdings are optimal (as per the performance formula) based on each of
        the current best standing prices in the market, and false otherwise.
        :return: performance is optimal (bool)
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
                        f"at ask price=[{ask / 100:.2f}] improves performance"
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
                        f"at bid price=[{bid / 100:.2f}] improves performance"
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
        Workflow:
        1. Update and print best bid/ask.
        2. Update and print my standing orders.
        3. Cancel my noprofitable standing orders.
        4. Execute different trading strategy based on current performance.
        """
        self._update_best_standing_orders()
        self._update_my_standing_orders()
        self._cancel_unprofitable_standing_orders()
        self._execute_trading_strategy()

    def _update_best_standing_orders(self):
        """
        Update best bid/ask and print table.
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
        Update my LIMIT orders and print table.
        """
        # Collect
        self._my_standing_orders = {}

        for order in Order.my_current().values():
            if order.order_type is OrderType.LIMIT:
                self._my_standing_orders[order.market.fm_id] = order

        # Print
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
        Cancel any my standing orders that are no longer profitable.

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
        Switch robot type based on porfolio performance.
            - REACTIVE → take profitable market orders immediately.
            - ACTIVE   → post limit orders at marginal indifference prices.
        """
        current_performance = self.get_potential_performance([])

        # ----- a) Deactivate model -----
        if not self.is_portfolio_optimal():
            self._robot_type = BotType.REACTIVE
            self.inform("Robot type switch to [REACTIVE]")

            for market in self.markets.values():
                market_id = market.fm_id

                entry = self._orderbook.get(market_id)
                if not entry:
                    continue

                # Send buy order at best ask
                ask = entry["ask"]
                if ask is not None:

                    if self._holdings.cash_available < ask:
                        self.inform(f"Insufficient cash: need {ask} have {self._holdings.cash_available}")
                    else:
                        buy_order = Order.create_new(market)
                        buy_order.order_type = OrderType.LIMIT
                        buy_order.order_side = OrderSide.BUY
                        buy_order.price = ask
                        buy_order.units = 1
                        buy_order.mine = True

                        if self.get_potential_performance([buy_order]) > current_performance:
                            self.inform(f"REACTIVE BUY on {market.name} at price {ask / 100:.2f}")
                            self.send_order(buy_order)
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
                            self.inform(f"REACTIVE SELL on {market.name} at price {bid / 100:.2f}")
                            self.send_order(sell_order)
                            return
        
        #  ----- b) Activate robot -----
        else:
            self._robot_type = BotType.ACTIVE
            self.inform("Robot type switch to [ACTIVE]")

            for market in self.markets.values():
                market_id = market.fm_id

                if market_id in self._my_standing_orders:
                    continue

                # Compute marginal value for different assets
                margin_value = self._compute_marginal_value(market)

                margin_price = int(round(margin_value / market.price_tick)) * market.price_tick
                margin_price = max(market.min_price, min(market.max_price, margin_price))
                
                # Send buy order just below indifference price
                buy_price = int(max(market.min_price, margin_price - market.price_tick))
                if self._holdings.cash_available < buy_price:
                    self.inform(f"Insufficient cash: need {buy_price} have {self._holdings.cash_available}")
                else:
                    candidate = Order.create_new(market)
                    candidate.order_type = OrderType.LIMIT
                    candidate.order_side = OrderSide.BUY
                    candidate.price = buy_price
                    candidate.units = 1
                    candidate.mine = True

                    if self.get_potential_performance([candidate]) > current_performance:
                        self.inform(f"ACTIVE BUY limit on {market.name} at price {buy_price / 100:.2f}")
                        self.send_order(candidate)
                        continue
                
                # Send sell order just above indifferent price
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
                        self.send_order(candidate)
                        return


    def _compute_marginal_value(self, market: Market) -> float:
        """
        Return the marginal value (indifference price) of asset in cents.

        Definition: the price X (cents) at which buying 1 extra unit of this
        asset leaves portfolio performance exactly unchanged.

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
        for market, asset in self._holdings.assets.items():
            if market.fm_id not in self._payoffs:
                continue
            for s in range(4):
                portfolio_payoffs[s] += asset.units * (self._payoffs[market.fm_id][s] / 100.0)
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
    
    def order_accepted(self, order: Order):
        self._waiting_for_server = False

        self.inform(f"Order accepted in [{order.market.name}]: fm_id={order.fm_id}, side={order.order_side.name}, price={order.price}, traded={order.has_traded}")
        

    def order_rejected(self, info: dict[str, str], order: Order):
        self._waiting_for_server = False

        self.warning(f"Order rejected in [{order.market.name}]: order={order} info={info}")



if __name__ == "__main__":
    capm_bot = CAPMBot(FM_ACCOUNT, FM_EMAIL, FM_PASSWORD, FM_MARKETPLACE_ID, RISK_PENALTY)
    capm_bot.run()
