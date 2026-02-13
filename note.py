    def _no_trade_reasons(self, private_signal: Order, best_public_buy: Order, best_public_sell: Order, margin: int) -> list[str]:
        """
        Get the reasons of no trading
        """
        reasons: list[str] = []
        
        if self._holdings is None:
            reasons.append("no holding snapshot yet")
        else:
            if self._role == Role.BUYER:
                if best_public_sell is not None and self._holdings.cash_available < best_public_sell.price:
                    reasons.append("insufficient cash")
            if self._role == Role.SELLER:
                pass
        
        if self._my_standing_order is not None and self._my_standing_order.market.fm_id == PUBLIC_MARKET_ID:
            reasons.append("already have a standing order in public market")

        return reasons


        """
        Calculating the speculation margin in two markets in cents
        If role is BUYER, margin = private price - best_public_sell
        If role is SELLER, margin = best_public_buy - private_price
        """