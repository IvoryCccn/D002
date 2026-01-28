# The Imports

import copy
import logging

from fmclient import Agent, Market, Holding, Session, Order, OrderType, OrderSide


# Flex-E-Market credential

FM_ACCOUNT = "fain-premium"
FM_EMAIL = "trader11@d002"
FM_PASSWORD = "LIPNE"
ROBOT_NAME = "My first trading robot"
FM_MARKETPLACE_ID = 1516


# The Base Robot Class definition

class FMRobot(Agent):
    """ A agent template that ...
    """
    def __init__(self, account: str, email: str, password: str, marketplace_id: int, name: str = 'FMRobot'):
        # Initialise the parent class, Agent
        super().__init__(account, email, password, marketplace_id, name=name)
        
        # Set the logger to DEBUG to record all events or INFO for key events.
        # logging.getLogger('agent').setLevel(logging.DEBUG)

        self.description = f"This is {name} bot for {email}!"

    def initialised(self) -> None:
        # marketplace: fm_id, name, description
        print(f"I am in marketplace {self.marketplace.name} ({self.marketplace.fm_id}) with desciption: {self.marketplace.description}")
        
        # market: fm_id, name, description, price_tick
        for market in self.markets.values():
            self.inform(msg=f"\tWith market {market.name} ({market.fm_id}) with description: {market.description} and tick size {market.price_tick}")

    def pre_start_tasks(self) -> None:
        pass

    def received_session_info(self, session: Session) -> None:
        if session.is_open:
            self.inform(msg=f"The session is open with session id {session.fm_id}. You are able to trade.")
        elif session.is_paused:
            self.inform(msg=f"The session is currently paused. You cannot currently trade.")
        elif session.is_closed:
            self.inform(msg=f"The session is currently closed. Wait for a new session before you can trade.")

    def received_holdings(self, holdings: Holding) -> None:
        self.inform(msg=f"My current cash is: ${holdings.cash / 100: .2f} ({holdings.cash_initial})")
        for market, asset in holdings.assets.items():
            self.inform(msg=f"My houlding of {market.name} are {asset.units} ({asset.units_available})")

    def received_orders(self, orders: list[Order]) -> None:
        for order in orders:
            self.inform(msg=f"There is a new {new_order.order_type.name} order to {new_order.order_side.name} {new_order.units} unit(s) of {new_order.market.name} ({new_order.market.fm_id}) for ${new_order.price / 100:.2f} which is {"" if new_order.mine else "NOT "}mine")

    def order_accepted(self, order: Order) -> None:
        pass

    def order_rejected(self, info: dict[str, str], order: Order) -> None:
        pass


# The dunder name equals dunder main

if __name__ == "__main__":
    # Swap your robot
    bot = FMRobot(account=FM_ACCOUNT, email=FM_EMAIL, password=FM_PASSWORD, marketplace_id=FM_MARKETPLACE_ID, name=ROBOT_NAME)    
    bot.run()
