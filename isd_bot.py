from enum import Enum
from fmclient import Agent, Market, Holding, Order, OrderSide, OrderType, Session

# Trading account details
FM_ACCOUNT = "fain-premium"
FM_EMAIL = "trader11@d002"
FM_PASSWORD = "LIPNE"
FM_MARKETPLACE_ID = 1513
PUBLIC_MARKET_ID = 2681
PRIVATE_MARKET_ID = 2682

NATURE_TRADER_ID = "M000"

# Enum for the roles of the bot
class Role(Enum):
    BUYER = 0
    SELLER = 1


# Enum for the trading style of the bot
class BotType(Enum):
    ACTIVE = 0
    REACTIVE = 1


PROFIT_MARGIN = 10
MARKET_PERFORMANCE_BOT_TYPE = BotType.REACTIVE


class IDSBot(Agent):
    _public_market: Market | None
    _private_market: Market | None

    _role: Role | None
    _bot_type: BotType | None

    _holdings: Holding | None

    _my_private_order: Order | None
    _my_public_order: Order | None

    _waiting_for_server: bool

    def __init__(self, account: str, email: str, password: str, marketplace_id: int, bot_type: BotType, bot_name: str = "FMBot"):
        super().__init__(account, email, password, marketplace_id, name=bot_name)
        self._public_market = None
        self._private_market = None

        self._role = None # store seller or buyer the robot should act
        self._bot_type = bot_type # store bot type
        self._holdings = None # store holding information for checking

        self._my_private_order = None # track my private market standing order
        self._my_public_order = None # track my public market standing order

        self._waiting_for_server = False # check to avoid double order sending

    def initialised(self):
        self._public_market = self.markets[PUBLIC_MARKET_ID]
        self._private_market = self.markets[PRIVATE_MARKET_ID]

        if self._public_market is None or self._private_market is None:
            self.error(
                f"Could not find required markets."
                f"public = {PUBLIC_MARKET_ID} found = {self._public_market is not None}, "
                f"private = {PRIVATE_MARKET_ID} found = {self._private_market is not None}"
            )
        
        self.inform(
            f"Markets loaded: "
            f"PUBLIC = {self._public_market.name} ({self._public_market.fm_id}), "
            f"PRIVATE = {self._private_market.name} ({self._private_market.fm_id})"
        )

    def pre_start_tasks(self) -> None:
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

        lines = [f"Current holdings in my account:"]
        lines.append(f"{'Account':<20} {'Total':>12} {'Available':>12}")
        lines.append("-" * 46)
        lines.append(f"{'Cash':<20} {holdings.cash / 100:>12.2f} {holdings.cash_available / 100:>12.2f}")

        for market, asset in holdings.assets.items():
            lines.append(f"{market.name:<20} {asset.units:>12} {asset.units_available:>12}")

        self.inform("\n".join(lines))

    def received_orders(self, orders: list[Order]) -> None:
        best_private_buy: Order | None = None
        best_private_sell: Order | None = None
        best_public_buy: Order | None = None
        best_public_sell: Order | None = None

        for order in Order.current().values():
            # solve limit order only
            if order.order_type is not OrderType.LIMIT:
                continue
            
            # check my order
            if order.mine:
                if order.market.fm_id == PUBLIC_MARKET_ID:
                    self._my_public_order = order
                    self.inform(f"I have a standing order in PUBLIC market: {order}")
                elif order.market.fm_id == PRIVATE_MARKET_ID:
                    self._my_private_order = order
                    self.inform(f"I have a standing order in PRIVATE market: {order}")
                continue
            
            # judge market type
            is_private = order.market.fm_id == PRIVATE_MARKET_ID
            is_public = order.market.fm_id == PUBLIC_MARKET_ID
            
            # update best standing buy price (the higher the better)
            if order.order_side is OrderSide.BUY:
                if is_private and (best_private_buy is None or order.price > best_private_buy.price):
                    best_private_buy = order
                elif is_public and (best_public_buy is None or order.price > best_public_buy.price):
                    best_public_buy = order
            
            # update best standing sell price (the lower the better)
            elif order.order_side is OrderSide.SELL:
                if is_private and (best_private_sell is None or order.price < best_private_sell.price):
                    best_private_sell = order
                elif is_public and (best_public_sell is None or order.price < best_public_sell.price):
                    best_public_sell = order

        self.inform(
            f"The best standing order in PUBLIC market: "
            f"BUY order price is [{best_public_buy.price if best_public_buy else None}], "
            f"SELL order price is [{best_public_sell.price if best_public_sell else None}]"
        )
        
        # identify private market signal
        private_signal: Order | None = best_private_buy if best_private_buy is not None else best_private_sell
        margin = None

        if private_signal is None:
            self._role = None
            margin = None
            self.inform(f"No order in PRIVATE market")
            return
        else:
            self.inform(f"Order signal in PRIVATE market is [{private_signal.order_side}] at price [{private_signal.price}]")

            # update robot role if signal in private has changed
            new_role = Role.BUYER if private_signal.order_side == OrderSide.BUY else Role.SELLER
            if self._role != new_role:
                self._role = new_role
                self.inform(f"Robot role updated to [{self._role.name}]")

            # calculate profit margin
            if new_role == Role.BUYER:
                if best_public_sell is not None:
                    margin = private_signal.price - best_public_sell.price
                else:
                    margin = None
                    self.inform("no standing SELL order in PUBLIC market")
            elif new_role == Role.SELLER:
                if best_public_buy is not None:
                    margin = best_public_buy.price - private_signal.price
                else:
                    margin = None
                    self.inform("no standing BUY order in PUBLIC market")
            else:
                margin = None
                self.inform("cannot calculate margin due to missing best standing order price")

        # placing order if it is profitable
        if margin != None:
            if self._waiting_for_server == False and margin > PROFIT_MARGIN:
                self.inform(f"margin ({margin}) is bigger than target, take buying action to make profits")
                if self._role == Role.BUYER:
                    if self._holdings.cash_available > best_public_sell.price and self._holdings.assets[self._private_market].units_available >= 1:
                        # best ask
                        self._placing_order(OrderSide.BUY, self._public_market, best_public_sell.price)
                        self._placing_order(OrderSide.SELL, self._private_market, private_signal.price)
                    else:
                        self.inform("insufficient available cash or unit")
                elif self._role == Role.SELLER:
                    if self._holdings.cash_available > private_signal.price and self._holdings.assets[self._public_market].units_available >= 1:
                        # best bid
                        self._placing_order(OrderSide.SELL, self._public_market, best_public_buy.price)
                        self._placing_order(OrderSide.BUY, self._private_market, private_signal.price)
                    else:
                        self.inform("insufficient available cash or unit")
            elif margin <= PROFIT_MARGIN:
                self.inform(f"margin ({margin}) is not bigger than target, no action and wait")
        else:
            margin = None
            self.inform(f"margin is None, no action and wait")

    def order_accepted(self, order: Order) -> None:
        self._waiting_for_server = False

        self.inform(f"Order accepted in [{order.market.name}]: fm_id={order.fm_id}, side={order.order_side.name}, price={order.price}, traded={order.has_traded}")

        # if order is not traded and not cancelled, this order is now a standing order
        if order.order_type == OrderType.LIMIT and (not order.has_traded) and (not order.is_cancelled):
            if order.market is not None:
                if order.market.fm_id == PUBLIC_MARKET_ID:
                    self._my_public_order = order
                    
                elif order.market.fm_id == PRIVATE_MARKET_ID:
                    self._my_private_order = order
        else:
            # if order is traded or cancelled, this order is now not a standing order
            if self._my_public_order is not None and order.fm_id == self._my_public_order.fm_id:
                self._my_public_order = None
            if self._my_private_order is not None and order.fm_id == self._my_private_order.fm_id:
                self._my_private_order = None

    def order_rejected(self, info: dict[str, str], order: Order) -> None:
        self._waiting_for_server = False

        self.warning(f"Order rejected in [{order.market.name}]: order={order} info={info}")

        # if I have a LIMIT order rejected, well it was never actually standing, so remove my standing order tracking
        if self._my_public_order is not None and order.ref == self._my_public_order.ref:
            self._my_public_order = None
        if self._my_private_order is not None and order.ref == self._my_private_order.ref:
            self._my_private_order = None

    def _placing_order(self, side: OrderSide, market: Market, price: int) -> None:
        """
        Send a 1-unit limit order into the at the given price
        """
        if market is None:
            self.warning("Cannot send order: PUBLIC or PRIVATE market not initialised.")
            return

        new_order = Order.create_new(market)
        new_order.market = market
        new_order.order_type = OrderType.LIMIT
        new_order.order_side = side
        new_order.price = price
        new_order.units = 1
        new_order.mine = True
        new_order.ref = f"REACTIVE_TAKE_{side.name}_{price}"

        new_order.owner_or_target = NATURE_TRADER_ID if market.fm_id == PRIVATE_MARKET_ID else None

        if market.fm_id == PRIVATE_MARKET_ID:
            self._my_private_order = new_order
        elif market.fm_id == PUBLIC_MARKET_ID:
            self._my_public_order = new_order

        self._waiting_for_server = True

        self.send_order(new_order)
        self.inform(f"Sent order in {market.name}: {new_order}")


if __name__ == "__main__":
    ids_bot = IDSBot(FM_ACCOUNT, FM_EMAIL, FM_PASSWORD, FM_MARKETPLACE_ID, bot_type=MARKET_PERFORMANCE_BOT_TYPE)
    ids_bot.run()
