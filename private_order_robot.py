# The Imports

import copy
import logging

from fmclient import Agent, Market, Holding, Session, Order, OrderType, OrderSide


# Flex-E-Market credential

FM_ACCOUNT = "fain-premium"
FM_EMAIL = "trader11@d002"
FM_PASSWORD = "LPINE"
ROBOT_NAME = "robot 20260208"
FM_MARKETPLACE_ID = 1516

PRIVATE_MARKET_ID = 2682
NATURE_TRADER_ID = "M000"  # This is the trader ID of the private signal


# The Base Robot Class definition

class FMRobot(Agent):
    """
    A simple robot to demonstrate placing an order in the private market.
    """

    def __init__(self, account: str, email: str, password: str, marketplace_id: int, name: str = 'FMRobot'):
        super().__init__(account, email, password, marketplace_id, name=name)
    
    def initialised(self) -> None:
        pass

    def pre_start_tasks(self) -> None:
        order: Order = Order.create_new()
        market: Market | None = Market.get_by_id(PRIVATE_MARKET_ID)
        if market:
            order.market = market
            order.order_side = OrderSide.SELL
            order.price = 100
            order.units = 1
            order.order_type = OrderType.LIMIT
            order.mine = True

            # Key part with private market
            order.owner_or_target = NATURE_TRADER_ID

            # This just creates the order, you will also need to validate and submit it

    def received_session_info(self, session: Session) -> None:
        pass

    def received_holdings(self, holdings: Holding) -> None:
        pass

    def received_orders(self, new_orders: list[Order]) -> None:
        pass

    def order_accepted(self, order: Order) -> None:
        pass

    def order_rejected(self, info: dict[str, str], order: Order) -> None:
        pass


# The dunder name equals dunder main

if __name__ == "__main__":
    bot = FMRobot(account=FM_ACCOUNT, email=FM_EMAIL, password=FM_PASSWORD, marketplace_id=FM_MARKETPLACE_ID, name=ROBOT_NAME)
    bot.run()
