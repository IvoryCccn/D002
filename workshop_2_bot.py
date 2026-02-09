# The Imports

import copy
import logging

from fmclient import Agent, Market, Holding, Session, Order, OrderType, OrderSide


# Flex-E-Market credential

FM_ACCOUNT = "fain-premium"
FM_EMAIL = "nc681@d002"
FM_PASSWORD = "nc681"
ROBOT_NAME = "robot 20260208"
FM_MARKETPLACE_ID = 1516


# The Base Robot Class definition

class FMRobot(Agent):
    """
    Basic robot from Workshop 2
    """
    def __init__(self, account: str, email: str, password: str, marketplace_id: int, name: str = 'FMRobot'):
        # Initialise the parent class, Agent
        super().__init__(account, email, password, marketplace_id, name=name)
        
        # Set the logger to DEBUG to record all events or INFO for key events.
        # logging.getLogger('agent').setLevel(logging.DEBUG)

        self.description = f"This is {name} bot for {email} created in Workshop 2!!"

    def initialised(self) -> None:
        self.inform(f"{self.marketplace.name} ({self.marketplace.fm_id}); {self.marketplace.description}")
        self.inform(f"\tI can trade in {', '.join(f"{market.name} ({market_id}){" (private)" if market.private_market else ""}" for market_id, market in self.markets.items())}")
    
    def pre_start_tasks(self) -> None:
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
        for new_order in orders:
            if not (new_order.order_type == OrderType.LIMIT and new_order.is_cancelled):
                self.inform(f"There is a new {new_order.order_type.name} order to {new_order.order_side.name} {new_order.units} unit(s) of {new_order.market.name} ({new_order.market.fm_id}) for ${new_order.price / 100:.2f} which is {"" if new_order.mine else "NOT "}mine")

    def order_accepted(self, order: Order) -> None:
        pass

    def order_rejected(self, info: dict[str, str], order: Order) -> None:
        pass


# The dunder name equals dunder main

if __name__ == "__main__":
    # Spawn your robot
    bot = FMRobot(account=FM_ACCOUNT, email=FM_EMAIL, password=FM_PASSWORD, marketplace_id=FM_MARKETPLACE_ID, name=ROBOT_NAME)    
    bot.run()
