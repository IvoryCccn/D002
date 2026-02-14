# The Imports

import copy
import logging

from fmclient import Agent, Market, Holding, Session, Order, OrderType, OrderSide


# Flex-E-Market credential

FM_ACCOUNT = "fain-premium"
FM_EMAIL = "trader11@d002"
FM_PASSWORD = "LPINE"
ROBOT_NAME = "My Periodic Robot"
FM_MARKETPLACE_ID = 1516
MARKET_ID_ASSET_A = 2686


# The Base Robot Class definition

class FMRobot(Agent):
    """
    A Robot that Uses Periodic Methods

    Attributes:
        _my_standing_order (Order | None): Used to track my standing order in a given market.
        _my_order_count (int): Track the number of orders I have placed to give my robot unique order IDs
    """

    _my_standing_order: Order | None
    _my_order_count: int


    def __init__(self, account: str, email: str, password: str, marketplace_id: int, name: str = 'FMRobot'):
        # Initialise the parent class, Agent
        super().__init__(account, email, password, marketplace_id, name=name)
        
        # Set the logger to DEBUG to record all events or INFO for key events.
        # logging.getLogger('agent').setLevel(logging.DEBUG)

        self.description = f"This is {name} bot for {email} created in Workshop 2!!"

        # create an attribute which will be used to track my standing order
        # in this implementation, it assumes I only have one standing order and only looks at one market
        self._my_standing_order = None
        self._my_order_count = 0

    def initialised(self) -> None:
        pass
    
    def pre_start_tasks(self) -> None:
        # I use periodic methods here

        # Get and print the best standing sell order every second 
        self.execute_periodically(self._get_best_standing_sell_order, sleep_time=1)

        # Every 5 seconds, if I dont have standing order, place one
        # to create a condition, it must be something callable (a function), so we create a lambda function because the check is simple
        self.execute_periodically_conditionally(self._place_standing_order, sleep_time=5, condition=lambda: self._my_standing_order is None)
        
        # Every 6 seconds, if I do have standing order, cancel it
        # I use 6 seconds to stagger placement and cancellation slightly
        self.execute_periodically_conditionally(self._cancel_standing_order, sleep_time=6, condition=lambda: self._my_standing_order is not None)


    def received_session_info(self, session: Session) -> None:
        pass

    def received_holdings(self, holdings: Holding) -> None:
        pass

    def received_orders(self, orders: list[Order]) -> None:
        # I do this to get any standing orders that belong to me on launch
        self._get_best_standing_sell_order()           

    def order_accepted(self, order: Order) -> None:
        self.inform(f"My order ({order}) was accepted. It received fm_id {order.fm_id} from Flex-E-Markets.")
        # if I have a LIMIT order accepted, update my standing order tracking to include it's fm_id
        if order.order_type is OrderType.LIMIT and order.market.fm_id == MARKET_ID_ASSET_A:
            self._my_standing_order = order

        # if I have a CANCEL order accepted, remove my standing order tracking
        elif order.order_type is OrderType.CANCEL and order.market.fm_id == MARKET_ID_ASSET_A:
            self._my_standing_order = None

    def order_rejected(self, info: dict[str, str], order: Order) -> None:
        self.warning(f"My order ({order}) was rejected. Info: {info}")
        # if I have a LIMIT order rejected, well it was never actually standing, so remove my standing order tracking
        if order.order_type == OrderType.LIMIT and order.market.fm_id == MARKET_ID_ASSET_A:
            self._my_standing_order = None

    def _get_best_standing_sell_order(self) -> None:
        # track the best standing sell order which is not mine
        # and track if I have any standing order
        # all in the market for Asset A
        best_standing_sell_order: Order | None = None

        for order in Order.current().values():
            if order.order_type is OrderType.LIMIT:
                if order.market.fm_id == MARKET_ID_ASSET_A:
                    if order.mine:
                        self._my_standing_order = order
                    else:
                        if order.order_side is OrderSide.SELL:
                            if best_standing_sell_order is None or order.price < best_standing_sell_order.price:
                                best_standing_sell_order = order

        self.inform(f"The best standing sell order in market {MARKET_ID_ASSET_A} is {best_standing_sell_order}!")
    
    def _place_standing_order(self) -> None:
        if self._my_standing_order is not None:
            self.inform("There is already a standing order.")
            return
        
        # TODO need to check here that my order is a valid order and that it will be accepted by the exchange
        
        market = Market.get_by_id(MARKET_ID_ASSET_A)
        if market is None:
            self.warning(f"Could not find market id: {MARKET_ID_ASSET_A}. Cannot place order.")
            return
    
        new_order = Order.create_new(market)
        new_order.order_type = OrderType.LIMIT
        new_order.order_side = OrderSide.BUY
        new_order.price = 100
        new_order.units = 1
        new_order.mine = True

        self._my_order_count += 1
        new_order.ref = f"My order number: {self._my_order_count}"

        self._my_standing_order = new_order
        self.send_order(new_order)
        self.inform(f"I have sent off a new {new_order.order_type.name} order: {new_order}")

    def _cancel_standing_order(self) -> None:
        # a new user created method under the class FMRobot
        if self._my_standing_order is None:
            self.warning("There is no standing order to cancel.")
            return
        
        # In the case that I have sent my order off to the exchange,
        # but it has not received a confirmation back yet,
        # meaning that it doesn't have a fm_id,
        # it cannot yet be cancelled
        if not self._my_standing_order.fm_id:
            self.warning("My standing order has no fm_id yet, it cannot be cancelled.")
            return
        
        cancel_order = copy.copy(self._my_standing_order)
        cancel_order.order_type = OrderType.CANCEL

        self.send_order(cancel_order)
        self.inform(f"I have send a cancel for order: {cancel_order.fm_id}")


# The dunder name equals dunder main

if __name__ == "__main__":
    # Spawn your robot
    bot = FMRobot(account=FM_ACCOUNT, email=FM_EMAIL, password=FM_PASSWORD, marketplace_id=FM_MARKETPLACE_ID, name=ROBOT_NAME)    
    bot.run()
