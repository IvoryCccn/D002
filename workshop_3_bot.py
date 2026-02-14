# The Imports

import copy
import logging

from fmclient import Agent, Market, Holding, Session, Order, OrderType, OrderSide


# Flex-E-Market credential

FM_ACCOUNT = "fain-premium"
FM_EMAIL = "trader11@d002"
FM_PASSWORD = "LIPNE"
ROBOT_NAME = "robot 20260208"
FM_MARKETPLACE_ID = 1516
MARKET_ID_ASSET_A = 2686


# The Base Robot Class definition

class FMRobot(Agent):
    """
    Basic robot from Workshop 3

    Attributes:
        _my_standing_order (Order | None): Used to track my standing order in a given market. 
    """

    _my_standing_order: Order | None


    def __init__(self, account: str, email: str, password: str, marketplace_id: int, name: str = 'FMRobot'):
        # Initialise the parent class, Agent
        super().__init__(account, email, password, marketplace_id, name=name)
        
        # Set the logger to DEBUG to record all events or INFO for key events.
        # logging.getLogger('agent').setLevel(logging.DEBUG)

        self.description = f"This is {name} bot for {email} created in Workshop 3!!"

        # create an attribute which will be used to track my standing order
        # in this implementation, it assumes I only have one standing order and only looks at one market
        self._my_standing_order = None

    def initialised(self) -> None:
        self.inform(f"{self.marketplace.name} ({self.marketplace.fm_id}); {self.marketplace.description}")
        self.inform(f"\tI can trade in {', '.join(f"{market.name} ({market_id}){" (private)" if market.private_market else ""}" for market_id, market in self.markets.items())}")
    
    def pre_start_tasks(self) -> None:
        # Place an order
        # Commented out to prevent happening every time I start the robot

        # market: Market = Market.get_by_id(MARKET_ID_ASSET_A)
        # new_order = Order.create_new(market)
        # new_order.order_type = OrderType.LIMIT
        # new_order.order_side = OrderSide.BUY
        # new_order.price = 100
        # new_order.units = 1
        # new_order.mine = True
        # new_order.ref = f"My order number: {1}"

        # self._my_standing_order = new_order
        # self.send_order(new_order)
        # self.inform(f"I have sent off a new {new_order.order_type.name} order: {new_order}")
        pass

    def received_session_info(self, session: Session) -> None:
        if session.is_open:
            self.inform(f"Marketplace is now open for trading. The new session is {session.fm_id}")
        elif session.is_paused:
            self.inform("Marketplace is now paused. You can not trade.")
        elif session.is_closed:
            self.inform("Marketplace is now closed. You can not trade.")

    def received_holdings(self, holdings: Holding) -> None:
        self.inform(f"Current holdings - Cash: {holdings.cash / 100:.2f} ({holdings.cash_available / 100:.2f}), {", ".join([f"{market.name}: {asset.units} ({asset.units_available})" for market, asset in holdings.assets.items()])}")

    def received_orders(self, orders: list[Order]) -> None:
        # track the best standing sell order which is not mine
        # and track if I have any standing order
        # all in the market for Asset A
        best_standing_sell_order: Order | None = None

        for order in Order.current().values():
            if order.order_type is OrderType.LIMIT:
                if order.market.fm_id == MARKET_ID_ASSET_A:
                    if order.mine:
                        self._my_standing_order = order
                        self.inform(f"I have a standing order: {order}")
                    else:
                        if order.order_side is OrderSide.SELL:
                            if best_standing_sell_order is None or order.price < best_standing_sell_order.price:
                                best_standing_sell_order = order

        self.inform(f"The best standing sell order in market {MARKET_ID_ASSET_A} is {best_standing_sell_order}!")

        # this will cancel any standing order you have
        if self._my_standing_order is not None:
            self._cancel_my_standing_order()
            

    def _cancel_my_standing_order(self) -> None:
        # a new user created method under the class FMRobot
        if self._my_standing_order is None:
            self.inform("There is no standing order to cancel.")
            return
        
        cancel_order = copy.copy(self._my_standing_order)
        cancel_order.order_type = OrderType.CANCEL

        self.send_order(cancel_order)
        self.inform(f"I have send a cancel for order: {cancel_order.fm_id}")


    def order_accepted(self, order: Order) -> None:
        self.inform(f"My order ({order}) was accepted. It received fm_id {order.fm_id} from Flex-E-Markets.")

        # if I have a LIMIT order accepted, update my standing order tracking to include it's fm_id
        if order.order_type is OrderType.LIMIT and order.market.fm_id == MARKET_ID_ASSET_A:
            self._my_standing_order = order

        # if I have a CANCEL order accepted, remove my standing order tracking
        elif order.order_type is OrderType.CANCEL and order.market.fm_id == MARKET_ID_ASSET_A:
            self._my_standing_order = None

    def order_rejected(self, info: dict[str, str], order: Order) -> None:
        self.inform(f"My order ({order}) was rejected. Info: {info}")

        # if I have a LIMIT order rejected, well it was never actually standing, so remove my standing order tracking
        if order.order_type == OrderType.LIMIT and order.market.fm_id == MARKET_ID_ASSET_A:
            self._my_standing_order = None


# The dunder name equals dunder main

if __name__ == "__main__":
    # Spawn your robot
    bot = FMRobot(account=FM_ACCOUNT, email=FM_EMAIL, password=FM_PASSWORD, marketplace_id=FM_MARKETPLACE_ID, name=ROBOT_NAME)    
    bot.run()
