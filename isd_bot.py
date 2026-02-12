from enum import Enum
from fmclient import Agent, Market, Holding, Order, OrderSide, OrderType, Session

# Trading account details
FM_ACCOUNT = "fain-premium"
FM_EMAIL = "nc681@d002"
FM_PASSWORD = "nc681"
FM_MARKETPLACE_ID = 1513
PUBLIC_MARKET_ID = 2681
PRIVATE_MARKET_ID = 2682

NATURE_TRADER_ID = "T011"

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

    _private_order: Order | None
    _public_buy_order: Order | None
    _public_sell_order: Order | None
    
    _waiting_for_server: bool
    _my_standing_order: Order | None

    def __init__(self, account: str, email: str, password: str, marketplace_id: int, bot_type: BotType, bot_name: str = "FMBot"):
        super().__init__(account, email, password, marketplace_id, name=bot_name)
        self._public_market = None
        self._private_market = None

        self._role = None # store seller or buyer the robot should act
        self._bot_type = bot_type # store bot type
        self._holdings = None # store holding information for checking

        self._private_order = None # store any order in private market
        self._public_buy_order = None # store standing buy order in public market
        self._public_sell_order = None # store standing sell order in public market

        self._waiting_for_server = False # check to avoid double order sending
        self._my_standing_order = None # track my standing order

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
            f"Markets loaded: PUBLIC = {self._public_market.name} ({self._public_market.fm_id}), "
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
        self.inform(f"Current holdings in my account:\n Cash: {holdings.cash / 100:.2f} ({holdings.cash_available / 100:.2f}), {", ".join([f"{market.name}: {asset.units} ({asset.units_available})" for market, asset in holdings.assets.items()])}")

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
                self._my_standing_order = order
                self.inform(f"I have a standing order: {order} in {order.market}")
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
                    self._public_buy_order = order
            
            # update best standing sell price (the lower the better)
            elif order.order_side is OrderSide.SELL:
                if is_private and (best_private_sell is None or order.price < best_private_sell.price):
                    best_private_sell = order
                elif is_public and (best_public_sell is None or order.price < best_public_sell.price):
                    best_public_sell = order
                    self._public_sell_order = order

        self.inform(
            f"\n The best standing buy order in public market is [{best_public_buy.price if best_public_buy else None}]"
            f"\n The best standing sell order in public market is [{best_public_sell.price if best_public_sell else None}]"
        )

        private_signal: Order | None = best_private_buy if best_private_buy is not None else best_private_sell

        if private_signal is None:
            self._private_order = None
            self.inform(f"No order in private market")
        else:
            self._private_order = private_signal
            self.inform(f"Order signal in private market is {private_signal.order_side} at price {private_signal.price}")
            new_role = Role.BUYER if private_signal.order_side == OrderSide.BUY else Role.SELLER
            if self._role != new_role:
                self._role = new_role
                self.inform(f"Role updated to: {self._role.name}")

        if private_signal is not None:
            if new_role == Role.BUYER and best_public_sell is not None:
                margin = self._calculate_margin(self._role, self._private_order, self._public_buy_order)
            elif new_role == Role.SELLER and best_public_buy is not None:
                margin = self._calculate_margin(self._role, self._private_order, self._public_sell_order)
            else:
                margin = None

        if self._waiting_for_server == False and margin is not None and margin > PROFIT_MARGIN:
            self.inform(f"margin ({margin}) is bigger than target, take buying action to make profits")
            if self._role == Role.BUYER:
                # best ask
                self._placing_order(OrderSide.BUY, self._public_market, self._public_sell_order.price)
                self._placing_order(OrderSide.SELL, self._private_market, self._private_order.price)
            elif self._role == Role.SELLER:
                # best bid
                self._placing_order(OrderSide.SELL, self._public_market, self._public_buy_order.price)
                self._placing_order(OrderSide.BUY, self._private_market, self._private_order.price)
        elif margin is not None and margin <= PROFIT_MARGIN:
            self.inform(f"margin ({margin}) is not bigger than target, no action and wait")
        else:
            self.inform(f"margin is None, no action and wait")

    def order_accepted(self, order: Order) -> None:
        self.inform(f"My order ({order}) was accepted in {order.market.name}. It received fm_id {order.fm_id} from Flex-E-Markets.")

        # if I have a LIMIT order accepted, update my standing order tracking to include it's fm_id
        if order.order_type is OrderType.LIMIT:
            self._my_standing_order = order

        # if I have a CANCEL order accepted, remove my standing order tracking
        elif order.order_type is OrderType.CANCEL:
            self._my_standing_order = None

    def order_rejected(self, info: dict[str, str], order: Order) -> None:
        self.inform(f"My order ({order}) was rejected. Info: {info}")

        # if I have a LIMIT order rejected, well it was never actually standing, so remove my standing order tracking
        if order.order_type == OrderType.LIMIT:
            self._my_standing_order = None

    def _calculate_margin(self, role: Role, private_price: Order, public_price: Order) -> int:
        """
        Calculating the speculation margin in two markets in cents
        If role is BUYER, margin = private price - best_public_sell
        If role is SELLER, margin = best_public_buy - private_price
        """
        margin = None

        if role == Role.BUYER:
            margin =  private_price.price - public_price.price
        elif role == Role.SELLER:
            margin =  public_price.price - private_price.price

        return margin

    def _placing_order(self, side: OrderSide, market: Market, price: int) -> None:
        """
        Send a 1-unit limit order into the at the given price
        """
        if self._public_market is None:
            self.warning("Cannot send order: public market not initialised.")
            return
        
        if self._private_market is None:
            self.warning("Cannot send order: private market not initialised.")
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

        self._my_standing_order = new_order
        self._waiting_for_server = True

        self.send_order(new_order)
        self.inform(f"Sent order in {market.name}: {new_order}")


if __name__ == "__main__":
    ids_bot = IDSBot(FM_ACCOUNT, FM_EMAIL, FM_PASSWORD, FM_MARKETPLACE_ID, bot_type=MARKET_PERFORMANCE_BOT_TYPE)
    ids_bot.run()
