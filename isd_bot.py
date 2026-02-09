from enum import Enum
from fmclient import Agent, Market, Holding, Order, OrderSide, OrderType, Session

# Trading account details
# ------ These must be set for testing and market performance evaluation -----
# ------ However, must be removed prior to submission -----
FM_ACCOUNT = "fain-premium"
FM_EMAIL = "nc681@d002"
FM_PASSWORD = "nc681"
FM_MARKETPLACE_ID = 1513


# ------ Add a variable called PROFIT_MARGIN -----
# ------ Add a variable called MARKET_PERFORMANCE_BOT_TYPE ----


# Enum for the roles of the bot
class Role(Enum):
    BUYER = 0
    SELLER = 1


# Enum for the trading style of the bot
class BotType(Enum):
    ACTIVE = 0
    REACTIVE = 1


class IDSBot(Agent):
    _public_market: Market | None
    _private_market: Market | None
    _role: Role | None

    # ------ Add an extra parameter bot_type to the constructor -----
    def __init__(self, account: str, email: str, password: str, marketplace_id: int, bot_name: str = "FMBot"):
        super().__init__(account, email, password, marketplace_id, name=bot_name)
        self._public_market = None
        self._private_market = None
        self._role = None
        # ------ Add new class attribute _bot_type to store the type of the bot

    def initialised(self):
        pass

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
        self.inform(f"Current holdings in my account\n: Cash: {holdings.cash / 100:.2f} ({holdings.cash_available / 100:.2f}), {", ".join([f"{market.name}: {asset.units} ({asset.units_available})" for market, asset in holdings.assets.items()])}")

    def received_orders(self, orders: list[Order]):
        pass

    def order_accepted(self, order: Order):
        pass

    def order_rejected(self, info: dict[str, str], order: Order):
        pass


if __name__ == "__main__":
    # ------ Add an extra argument for bot_type to the initalisation -----
    ids_bot = IDSBot(FM_ACCOUNT, FM_EMAIL, FM_PASSWORD, FM_MARKETPLACE_ID)
    ids_bot.run()
